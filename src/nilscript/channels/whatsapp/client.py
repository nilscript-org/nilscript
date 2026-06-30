"""Self-contained Evolution API client for nilscript's WhatsApp channel.

`EvolutionClient` is the single integration surface for one Evolution server:
instance lifecycle (create/connect/state/logout/delete), webhook config, and outbound
send (text/media/audio). It is decoupled from the old product — no Mongo, no Salla, no
store_id, no ops-alert fan-out. Construction takes only an `EvolutionConfig`
(base_url + api_key + timeouts); every method operates on an `instance_name` plus plain
arguments.

Kept from the proven reference:
  * pooled httpx.AsyncClient (keepalive across calls; no per-call TLS handshake),
  * split connect/read timeouts (fast connect, op-length read),
  * an in-process circuit breaker (nilscript's own `sdk.breaker.CircuitBreaker`),
  * idempotency keys on outbound sends (stable per instance × recipient × content),
  * tolerant QR / state / message-id extraction and structured error classification.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import httpx

from nilscript.sdk.breaker import CircuitBreaker

from .config import DEFAULT_CONNECT_TIMEOUT_SECONDS, EvolutionConfig
from .errors import (
    EvolutionError,
    extract_message_id,
    extract_qr,
    extract_state,
    is_instance_name_in_use,
    normalize_number,
    payload_message,
)

logger = logging.getLogger("nilscript.channels.whatsapp")

# Events Evolution forwards to our webhook by default. Inbound handling is a later step.
DEFAULT_EVOLUTION_EVENTS = [
    "MESSAGES_UPSERT",
    "MESSAGES_UPDATE",
    "CONNECTION_UPDATE",
    "QRCODE_UPDATED",
    "SEND_MESSAGE",
    "CONTACTS_UPSERT",
    "CONTACTS_UPDATE",
]

# A shared pooled client per process keeps connections warm across all calls.
_POOLED_CLIENT: httpx.AsyncClient | None = None
_POOLED_LOCK = asyncio.Lock()


async def _get_pooled_client() -> httpx.AsyncClient:
    """Shared singleton httpx client for all Evolution calls (keepalive reuse)."""
    global _POOLED_CLIENT
    if _POOLED_CLIENT is not None and not _POOLED_CLIENT.is_closed:
        return _POOLED_CLIENT
    async with _POOLED_LOCK:
        if _POOLED_CLIENT is not None and not _POOLED_CLIENT.is_closed:
            return _POOLED_CLIENT
        _POOLED_CLIENT = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=20,
                keepalive_expiry=30.0,
            ),
        )
        logger.info("EvolutionClient: pooled httpx client created")
        return _POOLED_CLIENT


def make_send_idempotency_key(
    instance_name: str, to: str, content: str, channel: str = "text"
) -> str:
    """Stable per-(instance, recipient, content) key for outbound sends.

    Used as the ``Idempotency-Key`` header so a retry re-issuing the same send lets
    Evolution deduplicate instead of delivering twice. SHA-256 truncated to 32 hex
    chars keeps the header short while preserving collision resistance for the
    (instance × recipient × content) cardinality we care about.
    """
    payload = f"{instance_name}:{to}:{channel}:{content}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class EvolutionClient:
    """Async HTTP client for a single Evolution API server."""

    def __init__(self, config: EvolutionConfig, *, breaker: CircuitBreaker | None = None) -> None:
        self.config = config
        # One breaker per client instance — synchronous, single-loop (see sdk.breaker).
        self._breaker = breaker or CircuitBreaker()

    @property
    def configured(self) -> bool:
        return self.config.configured

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.config.api_key:
            headers["apikey"] = self.config.api_key
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_404: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        if not self.configured:
            raise EvolutionError(
                "Evolution API is not configured. Set EVOLUTION_API_BASE_URL and EVOLUTION_API_KEY.",
                status_code=503,
            )

        if not self._breaker.allow():
            raise EvolutionError(
                "Evolution API circuit breaker is OPEN. Retry shortly.", status_code=503
            )

        url = f"{self.config.base_url}{path}"
        request_headers = dict(self.headers)
        if extra_headers:
            request_headers.update(extra_headers)

        try:
            client = await _get_pooled_client()
            response = await client.request(
                method=method.upper(),
                url=url,
                headers=request_headers,
                json=json_body,
                params=params,
                # Bound TCP connect separately from the (longer) read timeout: a briefly
                # unreachable Evolution fails fast as a clean ConnectTimeout instead of
                # hanging the whole op-timeout and triggering retry storms.
                timeout=httpx.Timeout(
                    self.config.timeout_seconds, connect=DEFAULT_CONNECT_TIMEOUT_SECONDS
                ),
            )
        except Exception as exc:  # noqa: BLE001 — network failure → breaker + wrapped error
            err_detail = str(exc) or f"{type(exc).__name__} connecting to {url}"
            self._breaker.record_failure()
            raise EvolutionError(f"Evolution API request failed: {err_detail}") from exc

        if allow_404 and response.status_code == 404:
            self._breaker.record_success()
            return {}

        payload: Any = {}
        if response.text:
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001 — non-JSON body → keep the raw text
                payload = response.text

        if response.status_code < 200 or response.status_code >= 300:
            message = payload_message(payload) or str(payload)[:240]
            # Breaker discipline: only 5xx / network errors count as backend failure.
            # 4xx are client errors (bad instance, missing arg, rate-limit-by-tenant);
            # counting them would trip the breaker on valid concurrent traffic (self-DoS).
            if response.status_code >= 500:
                self._breaker.record_failure()
            raise EvolutionError(
                f"Evolution API {method.upper()} {path} failed ({response.status_code}): {message}",
                status_code=response.status_code,
                payload=payload,
            )

        self._breaker.record_success()
        return payload

    # ── Instance discovery ──────────────────────────────────────────────────────

    async def fetch_instances(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/instance/fetchInstances")
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return data
        return []

    async def find_instance(self, instance_name: str) -> dict[str, Any] | None:
        normalized = str(instance_name or "").strip()
        if not normalized:
            return None
        for item in await self.fetch_instances():
            if not isinstance(item, dict):
                continue
            name = str(
                item.get("name") or item.get("instanceName") or item.get("instance") or ""
            ).strip()
            if name == normalized:
                return item
        return None

    # ── Instance lifecycle ──────────────────────────────────────────────────────

    async def create_instance(
        self,
        instance_name: str,
        *,
        integration: str | None = None,
        webhook_url: str = "",
        webhook_enabled: bool = True,
    ) -> dict[str, Any]:
        """Create an Evolution instance; tolerate a concurrent create by resolving the
        existing instance on an 'already in use' 4xx."""
        body: dict[str, Any] = {
            "instanceName": instance_name,
            "integration": integration or self.config.default_qr_integration,
            "qrcode": True,
            # Keep Evolution's external inbox bridge disabled. Empty strings disable it;
            # Evolution rejects null and requires string values.
            "chatwootAccountId": "",
            "chatwootToken": "",
            "chatwootUrl": "",
        }
        if webhook_url:
            webhook_config: dict[str, Any] = {
                "url": webhook_url,
                "byEvents": webhook_enabled,
                "enabled": webhook_enabled,
                "events": DEFAULT_EVOLUTION_EVENTS,
            }
            if self.config.webhook_secret:
                webhook_config["headers"] = {"x-evolution-secret": self.config.webhook_secret}
            body["webhook"] = webhook_config

        try:
            return await self._request("POST", "/instance/create", json_body=body)
        except EvolutionError as exc:
            if is_instance_name_in_use(exc):
                existing = await self.find_instance(instance_name)
                if existing:
                    return {"already_exists": True, "instance": existing}
            raise

    async def connect_instance(self, instance_name: str) -> dict[str, Any]:
        payload = await self._request("GET", f"/instance/connect/{instance_name}", allow_404=True)
        if isinstance(payload, dict):
            payload["qr_code"] = extract_qr(payload)
            payload["connection_state"] = extract_state(payload)
            return payload
        return {}

    async def get_connection_state(self, instance_name: str) -> dict[str, Any]:
        payload = await self._request(
            "GET", f"/instance/connectionState/{instance_name}", allow_404=True
        )
        if isinstance(payload, dict):
            payload["connection_state"] = extract_state(payload)
            return payload
        return {}

    async def logout_instance(self, instance_name: str) -> dict[str, Any]:
        payload = await self._request("DELETE", f"/instance/logout/{instance_name}", allow_404=True)
        return payload if isinstance(payload, dict) else {}

    async def delete_instance(self, instance_name: str) -> dict[str, Any]:
        payload = await self._request("DELETE", f"/instance/delete/{instance_name}", allow_404=True)
        return payload if isinstance(payload, dict) else {}

    # ── Webhook config ──────────────────────────────────────────────────────────

    async def set_webhook(
        self,
        instance_name: str,
        webhook_url: str,
        events: list[str] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        # Evolution v2.2.x expects the webhook payload wrapped under "webhook".
        webhook_config: dict[str, Any] = {
            "enabled": bool(enabled),
            "url": webhook_url,
            "events": events or DEFAULT_EVOLUTION_EVENTS,
            "webhook_by_events": True,
        }
        if self.config.webhook_secret:
            webhook_config["headers"] = {"x-evolution-secret": self.config.webhook_secret}
        return await self._request(
            "POST", f"/webhook/set/{instance_name}", json_body={"webhook": webhook_config}
        )

    async def get_webhook(self, instance_name: str) -> dict[str, Any]:
        payload = await self._request("GET", f"/webhook/find/{instance_name}", allow_404=True)
        return payload if isinstance(payload, dict) else {}

    # ── Outbound send ───────────────────────────────────────────────────────────

    async def send_text(
        self, instance_name: str, to: str, text: str, *, quoted: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"number": normalize_number(to), "text": text}
        if quoted:
            body["options"] = {"quoted": quoted}
        payload = await self._request(
            "POST",
            f"/message/sendText/{instance_name}",
            json_body=body,
            extra_headers=self._idem_headers(instance_name, to, text, "text"),
        )
        return self._with_message_id(payload)

    async def send_media(
        self,
        instance_name: str,
        to: str,
        media_url: str,
        *,
        media_type: str = "image",
        caption: str = "",
        filename: str = "",
        mimetype: str = "",
    ) -> dict[str, Any]:
        """Send media (image/audio/video/document). Evolution v2 expects
        mediatype/media/caption at the TOP level (not nested)."""
        if not mimetype:
            mime_map = {"image": "image/jpeg", "video": "video/mp4", "document": "application/pdf"}
            mimetype = mime_map.get(media_type, "application/octet-stream")

        body: dict[str, Any] = {
            "number": normalize_number(to),
            "mediatype": media_type,
            "mimetype": mimetype,
            "media": media_url,
        }
        if caption:
            body["caption"] = caption
        if filename:
            body["fileName"] = filename
        # Same media with a different caption is a different message for idempotency.
        idem_content = f"{media_url}|{caption}" if caption else media_url
        payload = await self._request(
            "POST",
            f"/message/sendMedia/{instance_name}",
            json_body=body,
            extra_headers=self._idem_headers(
                instance_name, to, idem_content, f"media:{media_type}"
            ),
        )
        return self._with_message_id(payload)

    async def send_audio(self, instance_name: str, to: str, audio: str) -> dict[str, Any]:
        """Send a voice note. `audio` may be base64, a data URI, or a public URL.
        Evolution v2 expects `audio` at the top level."""
        body = {"number": normalize_number(to), "audio": audio}
        payload = await self._request(
            "POST",
            f"/message/sendWhatsAppAudio/{instance_name}",
            json_body=body,
            extra_headers=self._idem_headers(instance_name, to, audio, "audio"),
        )
        return self._with_message_id(payload)

    # ── Contact helpers ─────────────────────────────────────────────────────────

    async def check_whatsapp_numbers(
        self, instance_name: str, numbers: list[str]
    ) -> list[dict[str, Any]]:
        """Check which phone numbers are registered on WhatsApp.

        ``POST /chat/whatsappNumbers/{instance}``. Returns a list of dicts with
        ``exists``/``jid``/``number``/``name``. Best-effort: returns [] on failure.
        """
        normalized = [n for n in (normalize_number(x) for x in numbers if x) if n]
        if not normalized:
            return []
        try:
            payload = await self._request(
                "POST", f"/chat/whatsappNumbers/{instance_name}", json_body={"numbers": normalized}
            )
        except Exception as exc:  # noqa: BLE001 — non-fatal lookup
            logger.debug("check_whatsapp_numbers failed (non-fatal): %s", exc)
            return []
        return payload if isinstance(payload, list) else []

    async def fetch_profile(self, instance_name: str, phone: str) -> dict[str, Any]:
        """Fetch a contact's profile (name/picture/status). Best-effort → {} on failure."""
        number = normalize_number(phone)
        if not number:
            return {}
        try:
            payload = await self._request(
                "POST",
                f"/chat/fetchProfile/{instance_name}",
                json_body={"number": number},
                allow_404=True,
            )
        except Exception as exc:  # noqa: BLE001 — non-fatal lookup
            logger.debug("fetch_profile failed (non-fatal): %s", exc)
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            "name": str(
                payload.get("name") or payload.get("pushName") or payload.get("pushname") or ""
            ).strip(),
            "picture_url": str(
                payload.get("picture")
                or payload.get("profilePictureUrl")
                or payload.get("imgUrl")
                or ""
            ).strip(),
            "status": str(payload.get("status") or "").strip(),
        }

    # ── Internals ───────────────────────────────────────────────────────────────

    @staticmethod
    def _with_message_id(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            payload["provider_message_id"] = extract_message_id(payload)
            return payload
        return {}

    @staticmethod
    def _idem_headers(instance_name: str, to: str, content: str, channel: str) -> dict[str, str]:
        """Return the ``Idempotency-Key`` header for an outbound send.

        Scoped by instance_name (always present here, unlike the old workspace_id which
        could be empty) so one instance's retry can never collide with another's.
        """
        return {
            "Idempotency-Key": make_send_idempotency_key(instance_name, to, content, channel)
        }

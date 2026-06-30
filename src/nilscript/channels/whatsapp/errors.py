"""Evolution error taxonomy + payload extractors — pure, network-free helpers.

Ported from the reference product with the product-specific taxonomy/alerting/Mongo
coupling removed. What's kept is the structured logic worth porting:

  * `EvolutionError` — the raised type, carrying status_code + payload.
  * `classify_error` — map a non-2xx Evolution response to a stable `EvolutionErrorCode`.
  * the QR / state / message-id extractors — Evolution's responses nest these under
    several different keys across versions, so the extraction is intentionally tolerant.
  * `normalize_number` — outbound-number cleanup for Evolution send payloads.

These are deliberately self-contained: no logging side effects, no I/O, no globals.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

__all__ = [
    "EvolutionError",
    "EvolutionErrorCode",
    "classify_error",
    "is_instance_name_in_use",
    "payload_message",
    "normalize_number",
    "extract_message_id",
    "extract_qr",
    "extract_state",
]


class EvolutionErrorCode(StrEnum):
    """Stable taxonomy for Evolution failures (decoupled from any product taxonomy)."""

    ACCOUNT_BANNED = "wa_account_banned"
    INSTANCE_CLOSED = "wa_instance_closed"
    RATE_LIMITED = "wa_rate_limited"
    SERVER_ERROR = "wa_server_error"
    EXECUTION_FAILED = "wa_execution_failed"


class EvolutionError(RuntimeError):
    """Raised on non-success responses from (or failures reaching) the Evolution API."""

    def __init__(self, message: str, status_code: int = 0, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload

    @property
    def code(self) -> EvolutionErrorCode:
        """Classify this error on demand from its status + payload."""
        return classify_error(self.status_code, self.payload)


# Structured markers Evolution returns inside its 4xx/5xx payloads. Exact equality on
# documented field values — no free-text substring heuristics.
_BANNED_ERROR_MARKERS = frozenset(
    {"account_restricted", "account_banned", "account_disabled", "forbidden"}
)
_INSTANCE_CLOSED_MARKERS = frozenset(
    {
        "instance_closed",
        "instance_disconnected",
        "qrcode_timeout",
        "connection_closed",
        "logged_out",
        "device_removed",
    }
)

# Evolution keeps connectionStatus="connecting" even after these terminal disconnects.
_TERMINAL_DISCONNECT_CODES = frozenset({401, 428, 440, 515})


def payload_message(payload: Any) -> str:
    """Extract a readable error message from an Evolution error payload."""
    if not isinstance(payload, dict):
        return str(payload or "").strip()

    # Most Evolution errors nest the message under response.message.
    response = payload.get("response")
    if isinstance(response, dict):
        response_msg = response.get("message")
        if isinstance(response_msg, list):
            parts: list[str] = []
            for item in response_msg:
                if isinstance(item, list):
                    parts.extend(str(x).strip() for x in item if str(x).strip())
                else:
                    value = str(item or "").strip()
                    if value:
                        parts.append(value)
            if parts:
                return "; ".join(parts)
        nested = str(response_msg or "").strip()
        if nested:
            return nested

    direct = str(payload.get("message") or payload.get("error") or "").strip()
    if direct:
        return direct

    raw_response = payload.get("response")
    if raw_response:
        return str(raw_response).strip()
    return ""


def is_instance_name_in_use(exc: EvolutionError) -> bool:
    """Detect duplicate-instance errors across Evolution versions/status codes."""
    if exc.status_code not in {400, 403, 409}:
        return False
    text = " ".join(p for p in (str(exc), payload_message(exc.payload)) if p).lower()
    return ("already in use" in text) or ("already exists" in text)


def classify_error(status_code: int, payload: Any) -> EvolutionErrorCode:
    """Map an Evolution non-2xx response to a taxonomy code (structured, no substrings).

      * 401/403 + banned marker (or unmarked) → ACCOUNT_BANNED
      * 404/410 + instance-closed marker       → INSTANCE_CLOSED
      * 429                                     → RATE_LIMITED
      * 5xx                                     → SERVER_ERROR
      * other 4xx                               → EXECUTION_FAILED
    """
    err_field = ""
    if isinstance(payload, dict):
        err_field = str(payload.get("error") or payload.get("code") or "").strip().lower()

    if status_code in (401, 403):
        # 401/403 from the WhatsApp gateway is almost always an account-level block;
        # treat unmarked ones as banned and let operators downgrade if wrong.
        return EvolutionErrorCode.ACCOUNT_BANNED

    if status_code in (404, 410) and err_field in _INSTANCE_CLOSED_MARKERS:
        return EvolutionErrorCode.INSTANCE_CLOSED

    if status_code == 429:
        return EvolutionErrorCode.RATE_LIMITED

    if 500 <= status_code < 600:
        return EvolutionErrorCode.SERVER_ERROR

    return EvolutionErrorCode.EXECUTION_FAILED


def normalize_number(value: str) -> str:
    """Normalize an outbound number for Evolution send payloads.

    Pass JIDs (anything containing ``@``) through untouched; otherwise strip to digits
    and drop a leading international ``00`` prefix.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        return raw
    digits = re.sub(r"[^\d]", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def extract_message_id(payload: Any) -> str:
    """Best-effort message id from an Evolution send response."""
    if not isinstance(payload, dict):
        return ""

    def _key_id(obj: Any) -> str:
        return (obj.get("key") or {}).get("id", "") if isinstance(obj.get("key"), dict) else ""

    candidates: list[Any] = [
        payload.get("id"),
        payload.get("messageId"),
        payload.get("message_id"),
        _key_id(payload),
    ]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend(
            [data.get("id"), data.get("messageId"), data.get("message_id"), _key_id(data)]
        )
    for item in candidates:
        normalized = str(item or "").strip()
        if normalized:
            return normalized
    return ""


def extract_qr(payload: Any) -> str:
    """Best-effort QR from an Evolution connect/state payload.

    Returns **pure base64** (data-URI prefix stripped) so the consumer renders it with
    its own ``data:image/png;base64,`` src.
    """
    if not isinstance(payload, dict):
        return ""
    candidates: list[Any] = [payload.get("base64"), payload.get("qr")]

    def _add_qrcode(container: Any) -> None:
        qrcode = container.get("qrcode") if isinstance(container, dict) else None
        if isinstance(qrcode, dict):
            candidates.extend([qrcode.get("base64"), qrcode.get("qr")])
        elif qrcode is not None:
            candidates.append(qrcode)

    _add_qrcode(payload)
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend([data.get("base64"), data.get("qr")])
        _add_qrcode(data)

    for item in candidates:
        if isinstance(item, dict):
            continue
        normalized = str(item or "").strip()
        if normalized:
            if normalized.startswith("data:"):
                comma = normalized.find(",")
                if comma != -1:
                    normalized = normalized[comma + 1 :]
            return normalized
    return ""


def extract_state(payload: Any) -> str:
    """Best-effort connection state from an Evolution payload.

    A current open state wins over historical disconnect metadata (Evolution can keep a
    stale disconnectionReasonCode after a successful re-pair). Terminal disconnect codes
    (401/428/440/515) collapse a misleading "connecting" to "close".
    """
    if not isinstance(payload, dict):
        return ""

    instance = payload.get("instance")
    data = payload.get("data")

    # 1) Current open-state wins.
    open_candidates: list[Any] = [
        payload.get("connectionStatus"),
        payload.get("state"),
        payload.get("status"),
    ]
    if isinstance(instance, dict):
        open_candidates.extend(
            [instance.get("connectionStatus"), instance.get("state"), instance.get("status")]
        )
    if isinstance(data, dict):
        d_instance = data.get("instance")
        open_candidates.extend(
            [
                data.get("connectionStatus"),
                data.get("state"),
                data.get("status"),
                d_instance.get("connectionStatus") if isinstance(d_instance, dict) else "",
            ]
        )
    for item in open_candidates:
        if str(item or "").strip().lower() in {"open", "connected"}:
            return "open"

    # 2) Terminal disconnect codes → close, even if status says "connecting".
    for src in (payload, instance, data):
        if isinstance(src, dict):
            code = src.get("disconnectionReasonCode")
            if code is not None:
                try:
                    if int(code) in _TERMINAL_DISCONNECT_CODES:
                        return "close"
                except (ValueError, TypeError):
                    pass

    # 3) Fall back to whatever raw state is present.
    candidates: list[Any] = [
        payload.get("state"),
        payload.get("status"),
        payload.get("connectionStatus"),
    ]
    if isinstance(instance, dict):
        candidates.extend([instance.get("state"), instance.get("status"), instance.get("connectionStatus")])
    if isinstance(data, dict):
        d_instance = data.get("instance")
        candidates.extend(
            [
                data.get("state"),
                data.get("status"),
                data.get("connectionStatus"),
                d_instance.get("state") if isinstance(d_instance, dict) else "",
            ]
        )
    for item in candidates:
        normalized = str(item or "").strip().lower()
        if normalized:
            return normalized
    return ""

"""Evolution API channel config — read from the environment, nilscript-style.

Follows the kernel convention (`os.environ.get`, no global settings object): a small
frozen dataclass built by `evolution_config_from_env()`. The WhatsApp client takes a
config explicitly, so tests inject one directly and never touch the process env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Evolution's default QR integration — Baileys (the multi-device web client) unless overridden.
DEFAULT_QR_INTEGRATION = "WHATSAPP-BAILEYS"
# Connect fails fast, separate from the (longer) read timeout, so a briefly-unreachable
# Evolution surfaces a clean ConnectTimeout instead of hanging the whole op-timeout.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
# Floor the read timeout — sub-5s timeouts cause spurious cancellations on slow QR fetches.
MIN_TIMEOUT_SECONDS = 5.0
_DEFAULT_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class EvolutionConfig:
    """Connection config for one Evolution API server.

    No tenant/store coupling: `base_url` + `api_key` identify the server, instance
    names are passed per call. `webhook_secret` (optional) is stamped on webhook
    registrations; `default_qr_integration` is the integration used at create time.
    """

    base_url: str = ""
    api_key: str = ""
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    webhook_secret: str = ""
    default_qr_integration: str = DEFAULT_QR_INTEGRATION

    def __post_init__(self) -> None:
        # Normalize here so every caller (env, test, direct) gets the same shape.
        object.__setattr__(self, "base_url", str(self.base_url or "").strip().rstrip("/"))
        object.__setattr__(self, "api_key", str(self.api_key or "").strip())
        object.__setattr__(self, "webhook_secret", str(self.webhook_secret or "").strip())
        object.__setattr__(
            self,
            "default_qr_integration",
            str(self.default_qr_integration or DEFAULT_QR_INTEGRATION).strip(),
        )
        object.__setattr__(
            self, "timeout_seconds", max(float(self.timeout_seconds or 0), MIN_TIMEOUT_SECONDS)
        )

    @property
    def configured(self) -> bool:
        """True only when both the server URL and key are present."""
        return bool(self.base_url and self.api_key)


def evolution_config_from_env(env: dict[str, str] | None = None) -> EvolutionConfig:
    """Build an `EvolutionConfig` from the environment.

    Reads ``EVOLUTION_API_BASE_URL``, ``EVOLUTION_API_KEY``,
    ``EVOLUTION_API_TIMEOUT_SECONDS``, ``EVOLUTION_WEBHOOK_SECRET``, and
    ``EVOLUTION_DEFAULT_QR_INTEGRATION`` (default ``WHATSAPP-BAILEYS``). Pass `env`
    to read from an explicit mapping instead of `os.environ` (tests / injection).
    """
    src = env if env is not None else os.environ
    raw_timeout = src.get("EVOLUTION_API_TIMEOUT_SECONDS", "")
    try:
        timeout = float(raw_timeout) if raw_timeout else _DEFAULT_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_SECONDS
    return EvolutionConfig(
        base_url=src.get("EVOLUTION_API_BASE_URL", ""),
        api_key=src.get("EVOLUTION_API_KEY", ""),
        timeout_seconds=timeout,
        webhook_secret=src.get("EVOLUTION_WEBHOOK_SECRET", ""),
        default_qr_integration=src.get("EVOLUTION_DEFAULT_QR_INTEGRATION", "")
        or DEFAULT_QR_INTEGRATION,
    )

"""nilscript WhatsApp channel — a self-contained Evolution API integration.

Public surface:
  * `EvolutionClient` — async client for one Evolution server (lifecycle, webhook, send).
  * `EvolutionConfig` / `evolution_config_from_env` — connection config.
  * `EvolutionError` / `EvolutionErrorCode` / `classify_error` — error taxonomy.
  * naming helpers — deterministic, reversible instance names + canonical webhook URL.
  * pure extractors — `extract_qr` / `extract_state` / `extract_message_id` / `normalize_number`.

No coupling to any product backend (Mongo/Salla/store_id). Inbound webhook routing and
tenant resolution are a later step (see module docs).
"""

from __future__ import annotations

from .client import (
    DEFAULT_EVOLUTION_EVENTS,
    EvolutionClient,
    make_send_idempotency_key,
)
from .config import (
    DEFAULT_QR_INTEGRATION,
    EvolutionConfig,
    evolution_config_from_env,
)
from .errors import (
    EvolutionError,
    EvolutionErrorCode,
    classify_error,
    extract_message_id,
    extract_qr,
    extract_state,
    is_instance_name_in_use,
    normalize_number,
    payload_message,
)
from .naming import (
    CANONICAL_WEBHOOK_ROUTE,
    INSTANCE_PREFIX,
    extract_tenant_prefix,
    get_canonical_instance_name,
    get_canonical_webhook_url,
    instances_share_tenant,
    is_canonical_instance,
    is_managed_instance,
)

__all__ = [
    # client
    "EvolutionClient",
    "make_send_idempotency_key",
    "DEFAULT_EVOLUTION_EVENTS",
    # config
    "EvolutionConfig",
    "evolution_config_from_env",
    "DEFAULT_QR_INTEGRATION",
    # errors
    "EvolutionError",
    "EvolutionErrorCode",
    "classify_error",
    "is_instance_name_in_use",
    "payload_message",
    "normalize_number",
    "extract_qr",
    "extract_state",
    "extract_message_id",
    # naming
    "CANONICAL_WEBHOOK_ROUTE",
    "INSTANCE_PREFIX",
    "get_canonical_instance_name",
    "extract_tenant_prefix",
    "is_canonical_instance",
    "is_managed_instance",
    "get_canonical_webhook_url",
    "instances_share_tenant",
]

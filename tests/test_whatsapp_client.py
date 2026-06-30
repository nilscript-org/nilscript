"""EvolutionClient request-building + behavior tests — fully mocked, no network."""

import httpx
import pytest
import respx

from nilscript.channels.whatsapp import (
    EvolutionClient,
    EvolutionConfig,
    EvolutionError,
    evolution_config_from_env,
    make_send_idempotency_key,
)
from nilscript.sdk.breaker import BreakerState, CircuitBreaker

BASE = "https://evo.example.com"
INSTANCE = "wosool-abcdef01-23456789"


def make_client(**overrides: object) -> EvolutionClient:
    cfg = EvolutionConfig(base_url=BASE, api_key="secret-key", **overrides)  # type: ignore[arg-type]
    return EvolutionClient(cfg)


# ── config ───────────────────────────────────────────────────────────────────────


def test_config_from_env_reads_all_keys() -> None:
    cfg = evolution_config_from_env(
        {
            "EVOLUTION_API_BASE_URL": "https://e.test/",
            "EVOLUTION_API_KEY": " k ",
            "EVOLUTION_API_TIMEOUT_SECONDS": "30",
            "EVOLUTION_WEBHOOK_SECRET": "shh",
            "EVOLUTION_DEFAULT_QR_INTEGRATION": "WHATSAPP-BUSINESS",
        }
    )
    assert cfg.base_url == "https://e.test"  # trailing slash stripped
    assert cfg.api_key == "k"  # trimmed
    assert cfg.timeout_seconds == 30.0
    assert cfg.webhook_secret == "shh"
    assert cfg.default_qr_integration == "WHATSAPP-BUSINESS"
    assert cfg.configured


def test_config_defaults_and_timeout_floor() -> None:
    cfg = evolution_config_from_env({})
    assert cfg.default_qr_integration == "WHATSAPP-BAILEYS"
    assert not cfg.configured
    # sub-floor timeout is clamped up
    assert EvolutionConfig(base_url="x", api_key="y", timeout_seconds=1).timeout_seconds == 5.0


def test_unconfigured_client_raises_503() -> None:
    client = EvolutionClient(EvolutionConfig())

    async def go() -> None:
        with pytest.raises(EvolutionError) as ei:
            await client.fetch_instances()
        assert ei.value.status_code == 503

    import asyncio

    asyncio.run(go())


# ── request building ─────────────────────────────────────────────────────────────


@respx.mock
async def test_send_text_builds_payload_and_auth_and_idem_header() -> None:
    route = respx.post(f"{BASE}/message/sendText/{INSTANCE}").mock(
        return_value=httpx.Response(200, json={"key": {"id": "MID-1"}})
    )
    client = make_client()
    result = await client.send_text(INSTANCE, "+966 50 123 4567", "hello")

    assert route.called
    req = route.calls.last.request
    import json

    body = json.loads(req.content)
    assert body == {"number": "966501234567", "text": "hello"}
    assert req.headers["apikey"] == "secret-key"
    assert req.headers["Authorization"] == "Bearer secret-key"
    # idempotency key is the deterministic per-(instance, to, content) digest
    assert req.headers["Idempotency-Key"] == make_send_idempotency_key(
        INSTANCE, "+966 50 123 4567", "hello", "text"
    )
    assert result["provider_message_id"] == "MID-1"


@respx.mock
async def test_connect_instance_attaches_qr_and_state() -> None:
    respx.get(f"{BASE}/instance/connect/{INSTANCE}").mock(
        return_value=httpx.Response(
            200, json={"base64": "data:image/png;base64,QQ", "connectionStatus": "connecting"}
        )
    )
    out = await make_client().connect_instance(INSTANCE)
    assert out["qr_code"] == "QQ"
    assert out["connection_state"] == "connecting"


@respx.mock
async def test_create_instance_includes_webhook_with_secret() -> None:
    route = respx.post(f"{BASE}/instance/create").mock(
        return_value=httpx.Response(200, json={"instance": {"instanceName": INSTANCE}})
    )
    client = make_client(webhook_secret="topsecret")
    await client.create_instance(INSTANCE, webhook_url="https://hook/x")

    import json

    body = json.loads(route.calls.last.request.content)
    assert body["instanceName"] == INSTANCE
    assert body["integration"] == "WHATSAPP-BAILEYS"
    assert body["webhook"]["url"] == "https://hook/x"
    assert body["webhook"]["headers"]["x-evolution-secret"] == "topsecret"


@respx.mock
async def test_create_instance_resolves_existing_on_duplicate() -> None:
    respx.post(f"{BASE}/instance/create").mock(
        return_value=httpx.Response(409, json={"message": "instance name already in use"})
    )
    respx.get(f"{BASE}/instance/fetchInstances").mock(
        return_value=httpx.Response(200, json=[{"name": INSTANCE, "id": "x"}])
    )
    out = await make_client().create_instance(INSTANCE)
    assert out["already_exists"] is True
    assert out["instance"]["name"] == INSTANCE


@respx.mock
async def test_allow_404_returns_empty_without_raising() -> None:
    respx.get(f"{BASE}/instance/connectionState/{INSTANCE}").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    out = await make_client().get_connection_state(INSTANCE)
    # 404 short-circuits to {} inside _request; the method still stamps an (empty) state.
    assert out == {"connection_state": ""}


@respx.mock
async def test_4xx_raises_but_does_not_trip_breaker() -> None:
    respx.post(f"{BASE}/message/sendText/{INSTANCE}").mock(
        return_value=httpx.Response(400, json={"message": "bad request"})
    )
    breaker = CircuitBreaker(failure_threshold=1)
    client = EvolutionClient(EvolutionConfig(base_url=BASE, api_key="k"), breaker=breaker)
    with pytest.raises(EvolutionError) as ei:
        await client.send_text(INSTANCE, "966500000000", "hi")
    assert ei.value.status_code == 400
    assert breaker.state is BreakerState.CLOSED  # 4xx must NOT count


@respx.mock
async def test_5xx_trips_breaker_after_threshold() -> None:
    respx.post(f"{BASE}/message/sendText/{INSTANCE}").mock(
        return_value=httpx.Response(500, json={"message": "boom"})
    )
    breaker = CircuitBreaker(failure_threshold=1)
    client = EvolutionClient(EvolutionConfig(base_url=BASE, api_key="k"), breaker=breaker)
    with pytest.raises(EvolutionError):
        await client.send_text(INSTANCE, "966500000000", "hi")
    assert breaker.state is BreakerState.OPEN  # 5xx counts


@respx.mock
async def test_open_breaker_short_circuits() -> None:
    breaker = CircuitBreaker(failure_threshold=1)
    breaker.record_failure()  # open it
    assert breaker.state is BreakerState.OPEN
    client = EvolutionClient(EvolutionConfig(base_url=BASE, api_key="k"), breaker=breaker)
    with pytest.raises(EvolutionError) as ei:
        await client.send_text(INSTANCE, "966500000000", "hi")
    assert ei.value.status_code == 503


@respx.mock
async def test_send_media_top_level_fields() -> None:
    route = respx.post(f"{BASE}/message/sendMedia/{INSTANCE}").mock(
        return_value=httpx.Response(200, json={"id": "M2"})
    )
    out = await make_client().send_media(
        INSTANCE, "966500000000", "https://img/x.jpg", caption="hi"
    )
    import json

    body = json.loads(route.calls.last.request.content)
    assert body["mediatype"] == "image"
    assert body["media"] == "https://img/x.jpg"
    assert body["caption"] == "hi"
    assert out["provider_message_id"] == "M2"

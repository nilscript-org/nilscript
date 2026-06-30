"""Error classification + payload extractor tests for the WhatsApp channel."""

from nilscript.channels.whatsapp.errors import (
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


# ── classification ──────────────────────────────────────────────────────────────


def test_classify_401_403_is_account_banned() -> None:
    assert classify_error(401, {}) is EvolutionErrorCode.ACCOUNT_BANNED
    assert classify_error(403, {"error": "account_restricted"}) is EvolutionErrorCode.ACCOUNT_BANNED


def test_classify_instance_closed_requires_marker() -> None:
    assert classify_error(404, {"error": "logged_out"}) is EvolutionErrorCode.INSTANCE_CLOSED
    # 404 without a known marker is just a failed call, not a closed instance.
    assert classify_error(404, {"error": "not_found"}) is EvolutionErrorCode.EXECUTION_FAILED


def test_classify_rate_limit_and_server_and_other() -> None:
    assert classify_error(429, {}) is EvolutionErrorCode.RATE_LIMITED
    assert classify_error(503, {}) is EvolutionErrorCode.SERVER_ERROR
    assert classify_error(400, {}) is EvolutionErrorCode.EXECUTION_FAILED


def test_error_code_property_classifies_from_status() -> None:
    exc = EvolutionError("boom", status_code=429, payload={})
    assert exc.code is EvolutionErrorCode.RATE_LIMITED


# ── duplicate-instance detection ─────────────────────────────────────────────────


def test_is_instance_name_in_use_detects_across_codes() -> None:
    exc = EvolutionError("...", status_code=409, payload={"message": "name already in use"})
    assert is_instance_name_in_use(exc)
    exc2 = EvolutionError("instance already exists", status_code=400, payload={})
    assert is_instance_name_in_use(exc2)
    # Wrong status → not a duplicate signal even if text matches.
    exc3 = EvolutionError("already exists", status_code=500, payload={})
    assert not is_instance_name_in_use(exc3)


# ── payload_message ──────────────────────────────────────────────────────────────


def test_payload_message_flattens_nested_response_list() -> None:
    payload = {"response": {"message": ["bad number", ["nested", "items"]]}}
    assert payload_message(payload) == "bad number; nested; items"


def test_payload_message_direct_and_string() -> None:
    assert payload_message({"message": "boom"}) == "boom"
    assert payload_message("plain text") == "plain text"


# ── normalize_number ─────────────────────────────────────────────────────────────


def test_normalize_number_strips_and_handles_jid_and_double_zero() -> None:
    assert normalize_number("+966 50-123 4567") == "966501234567"
    assert normalize_number("00966501234567") == "966501234567"
    assert normalize_number("966@s.whatsapp.net") == "966@s.whatsapp.net"
    assert normalize_number("") == ""


# ── extract_message_id ───────────────────────────────────────────────────────────


def test_extract_message_id_from_key_and_data() -> None:
    assert extract_message_id({"key": {"id": "ABC"}}) == "ABC"
    assert extract_message_id({"data": {"messageId": "XYZ"}}) == "XYZ"
    assert extract_message_id({"id": "TOP"}) == "TOP"
    assert extract_message_id({}) == ""


# ── extract_qr ───────────────────────────────────────────────────────────────────


def test_extract_qr_strips_data_uri_prefix() -> None:
    assert extract_qr({"base64": "data:image/png;base64,AAAA"}) == "AAAA"


def test_extract_qr_from_nested_qrcode_and_data() -> None:
    assert extract_qr({"qrcode": {"base64": "QR1"}}) == "QR1"
    assert extract_qr({"data": {"qrcode": {"base64": "QR2"}}}) == "QR2"
    assert extract_qr({}) == ""


# ── extract_state ────────────────────────────────────────────────────────────────


def test_extract_state_open_wins_over_stale_disconnect_code() -> None:
    payload = {"connectionStatus": "open", "disconnectionReasonCode": 401}
    assert extract_state(payload) == "open"


def test_extract_state_terminal_code_collapses_connecting_to_close() -> None:
    payload = {"connectionStatus": "connecting", "disconnectionReasonCode": 428}
    assert extract_state(payload) == "close"


def test_extract_state_falls_back_to_raw_state() -> None:
    assert extract_state({"data": {"state": "connecting"}}) == "connecting"
    assert extract_state({}) == ""

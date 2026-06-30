"""Naming round-trip + canonical-shape tests for the WhatsApp channel."""

from nilscript.channels.whatsapp import naming


def test_instance_name_is_deterministic() -> None:
    wid = "abcdef0123456789ffff"
    assert naming.get_canonical_instance_name(wid) == naming.get_canonical_instance_name(wid)
    assert naming.get_canonical_instance_name(wid) == "wosool-abcdef01-23456789"


def test_round_trip_id_to_instance_to_prefix() -> None:
    wid = "abcdef0123456789"
    name = naming.get_canonical_instance_name(wid)
    assert naming.extract_tenant_prefix(name) == wid[:8]


def test_short_id_is_padded_and_still_canonical() -> None:
    name = naming.get_canonical_instance_name("abc")
    # padded to 16 with zeros → wosool-abc00000-00000000
    assert name == "wosool-abc00000-00000000"
    assert naming.is_canonical_instance(name)


def test_extract_prefix_returns_none_for_foreign_name() -> None:
    assert naming.extract_tenant_prefix("other-12345678-90abcdef") is None
    assert naming.extract_tenant_prefix("") is None


def test_is_canonical_rejects_non_hex_and_wrong_shape() -> None:
    assert not naming.is_canonical_instance("wosool-ABCDEF01-23456789")  # uppercase
    assert not naming.is_canonical_instance("wosool-abc-def")  # too short
    assert naming.is_canonical_instance("wosool-abcdef01-234567")  # 6-char tail allowed


def test_is_managed_instance_matches_any_prefixed_name() -> None:
    assert naming.is_managed_instance("wosool-legacy-random-tail")
    assert not naming.is_managed_instance("acme-12345678-90abcdef")


def test_canonical_webhook_url_strips_trailing_slash() -> None:
    url = naming.get_canonical_webhook_url("https://hook.example.com/")
    assert url == "https://hook.example.com" + naming.CANONICAL_WEBHOOK_ROUTE


def test_instances_share_tenant() -> None:
    a = naming.get_canonical_instance_name("abcdef0111111111")
    b = naming.get_canonical_instance_name("abcdef0122222222")
    c = naming.get_canonical_instance_name("99999999")
    assert naming.instances_share_tenant(a, b)  # same first-8
    assert not naming.instances_share_tenant(a, c)
    assert not naming.instances_share_tenant(a, "foreign-name")

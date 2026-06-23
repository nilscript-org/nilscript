"""Reference legibility (docs/reference-legibility.md): resolve-and-echo a constrained
field's human label on write AND read-back, so an opaque foreign key is never illegible
at approval (Fault A) and the receipt is grounded in live data, not agent narration (Fault B).

The helper is pure given an injected `lookup`, so a fake backend exercises every branch."""

from nilscript.sdk.legibility import field_label, legible, echo_preview

# A tiny fake backend: the live country table from the incident.
_COUNTRIES = {192: "Saudi Arabia", 224: "Türkiye", 233: "United States", 235: "Uzbekistan"}


def _lookup(model: str, value):
    """Resolve an id-or-name on `model` to its canonical label, or None (best-effort)."""
    if model != "res.country":
        return None
    if str(value).isdigit():
        return _COUNTRIES.get(int(value))
    for label in _COUNTRIES.values():
        if label.lower() == str(value).strip().lower():
            return label
    return None


def test_relation_value_resolves_to_label_by_id():
    # Read-back path: the landed value is the stored id 224 — it must label as Türkiye,
    # never be narratable as "Uzbekistan".
    meta = {"name": "country_id", "relation": "res.country"}
    assert field_label(meta, 224, _lookup) == "Türkiye"


def test_relation_value_resolves_to_label_by_name():
    # Write path: the agent passed the human name; the echo confirms it back.
    meta = {"name": "country_id", "relation": "res.country"}
    assert field_label(meta, "Saudi Arabia", _lookup) == "Saudi Arabia"


def test_selection_label_comes_from_options_no_lookup():
    meta = {"name": "state", "options": [{"value": "available", "label": "Available"},
                                         {"value": "sold", "label": "Sold"}]}
    assert field_label(meta, "available", _lookup) == "Available"


def test_unconstrained_scalar_has_no_label():
    assert field_label({"name": "phone"}, "053436006", _lookup) is None


def test_empty_value_has_no_label():
    assert field_label({"name": "country_id", "relation": "res.country"}, "", _lookup) is None
    assert field_label({"name": "country_id", "relation": "res.country"}, None, _lookup) is None


def test_unresolvable_is_best_effort_none_not_a_guess():
    # Legibility never auto-picks; an unresolvable value just isn't labeled (the gate refuses it).
    meta = {"name": "country_id", "relation": "res.country"}
    assert field_label(meta, 99999, _lookup) is None


def test_legible_echoes_only_constrained_fields():
    schema = [
        {"name": "name"},
        {"name": "phone"},
        {"name": "country_id", "relation": "res.country"},
        {"name": "state", "options": [{"value": "draft", "label": "Draft"}]},
    ]
    fields = {"name": "AHMED", "phone": "053", "country_id": 192, "state": "draft"}
    out = legible(schema, fields, _lookup)
    assert out == {
        "country_id": {"value": 192, "label": "Saudi Arabia"},
        "state": {"value": "draft", "label": "Draft"},
    }


def test_legible_is_symmetric_same_call_write_and_readback():
    # The write echo (value as name) and read-back echo (value as landed id) both land
    # on the same label — that symmetry is what removes Fault B's raw material.
    schema = [{"name": "country_id", "relation": "res.country"}]
    on_write = legible(schema, {"country_id": "Türkiye"}, _lookup)
    on_readback = legible(schema, {"country_id": 224}, _lookup)
    assert on_write["country_id"]["label"] == on_readback["country_id"]["label"] == "Türkiye"


def test_echo_preview_appends_legible_tail():
    labels = {"country_id": {"value": 224, "label": "Türkiye"}}
    assert echo_preview("Update contact 43", labels) == "Update contact 43 · country_id → Türkiye"


def test_echo_preview_unchanged_when_no_labels():
    assert echo_preview("Update contact 43", {}) == "Update contact 43"

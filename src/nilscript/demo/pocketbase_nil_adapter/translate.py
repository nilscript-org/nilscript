"""The translation core: NIL verb args ⇄ PocketBase collection records.

Pure mapping, no I/O — the only module (besides system.py) that knows backend specifics. Each NIL
entity is modelled as a PocketBase collection; `to_native` shapes a record and the edge calls
`client.create(collection, record)`. The NIL edge never changes when you add a verb.

PARKED verbs (deprecated in the standard) are intentionally ABSENT — the standard says do not
implement them, so the shim cannot (commerce.update_order_status, GAP-001).

Write-path dispatch: the edge routes by the standard's verb lexicon — `delete_*` -> DELETE,
`update_*` -> PATCH, everything else (create/record/send/draft/process) -> CREATE — calling
`client.create / client.update / client.delete` accordingly. So `to_native` returns the create
body, the PATCH body, or (for delete) an empty body, and the record id travels from the verb's
first required arg. Reversal (ROLLBACK) lives in compensation.py.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from pocketbase_nil_adapter.system import SystemClient

Bilingual = dict[str, str]


@dataclass(frozen=True)
class WriteVerb:
    verb: str
    tier: str
    doctype: str  # the PocketBase collection this verb writes to
    required: tuple[str, ...]
    to_native: Callable[[dict[str, Any]], dict[str, Any]]
    preview: Callable[[dict[str, Any]], Bilingual]
    entity_type: str
    # DECLARED prerequisites: each field here points to a record in another target (a foreign key).
    # The edge verifies the referenced record EXISTS before proposing — so "an invoice needs a client"
    # is a fact the adapter states, enforced universally, not a PocketBase-specific afterthought.
    references: dict[str, str] = field(default_factory=dict)

    def missing(self, args: dict[str, Any]) -> list[str]:
        return [field for field in self.required if not args.get(field)]


@dataclass(frozen=True)
class QueryVerb:
    verb: str
    run: Callable[[SystemClient, dict[str, Any]], dict[str, Any]]


def _clean(**fields: Any) -> dict[str, Any]:
    """A PocketBase record from the given fields, dropping the ones that are None."""
    return {key: value for key, value in fields.items() if value is not None}


# --- commerce -------------------------------------------------------------------------------

def _to_native_create_coupon(a: dict[str, Any]) -> dict[str, Any]:
    return _clean(code=a["code"], discount_type=a["discount_type"],
                  discount_value=a["discount_value"], min_amount=a.get("min_amount"),
                  expires_at=a.get("expires_at"))

def _to_native_create_product(a: dict[str, Any]) -> dict[str, Any]:
    return _clean(name=a["name"], description=a.get("description"), price=a.get("price"),
                  sku=a.get("sku"), quantity=a.get("quantity"), category=a.get("category"))

def _to_native_delete_product(a: dict[str, Any]) -> dict[str, Any]:
    return {}  # DELETE dispatch uses the record id (product_id) from args; no body is sent

def _to_native_process_refund(a: dict[str, Any]) -> dict[str, Any]:
    return _clean(refund_target=a["refund_target"], amount=a.get("amount"), reason=a.get("reason"))

def _to_native_record_fulfillment(a: dict[str, Any]) -> dict[str, Any]:
    return _clean(order_id=a["order_id"], event=a["event"], tracking=a.get("tracking"),
                  occurred_at=a.get("occurred_at"))

def _to_native_record_payment(a: dict[str, Any]) -> dict[str, Any]:
    return _clean(order_id=a["order_id"], event=a["event"], amount=a.get("amount"),
                  currency=a.get("currency"), method=a.get("method"), reference=a.get("reference"))

def _to_native_send_message(a: dict[str, Any]) -> dict[str, Any]:
    return _clean(phone=a["phone"], text=a["text"])

def _to_native_update_product(a: dict[str, Any]) -> dict[str, Any]:
    updates = a["updates"]  # PATCH body = the fields to change (the id travels in the URL)
    return dict(updates) if isinstance(updates, dict) else {"updates": updates}

def _to_native_update_product_quantity(a: dict[str, Any]) -> dict[str, Any]:
    return {"quantity": a["quantity"]}

# --- services -------------------------------------------------------------------------------

def _to_native_create_client(a: dict[str, Any]) -> dict[str, Any]:
    return _clean(name=a["name"], phone=a["phone"], email=a.get("email"), notes=a.get("notes"))

def _to_native_create_invoice(a: dict[str, Any]) -> dict[str, Any]:
    return _clean(party_id=a["party_id"], amount=a["amount"], currency=a["currency"],
                  description=a.get("description"))

def _to_native_create_payment_link(a: dict[str, Any]) -> dict[str, Any]:
    return _clean(invoice_id=a["invoice_id"], expires_at=a.get("expires_at"))

def _to_native_draft_proposal(a: dict[str, Any]) -> dict[str, Any]:
    return _clean(party_id=a["party_id"], title=a["title"], amount=a["amount"],
                  currency=a["currency"], body=a.get("body"))

def _to_native_send_followup(a: dict[str, Any]) -> dict[str, Any]:
    return _clean(party_id=a["party_id"], message=a["message"], channel=a.get("channel"))

def _to_native_send_proposal(a: dict[str, Any]) -> dict[str, Any]:
    return _clean(biz_proposal_id=a["biz_proposal_id"], channel=a.get("channel"))

def _run_list_clients(client: SystemClient, args: dict[str, Any]) -> dict[str, Any]:
    filters = {"name": args["q"]} if args.get("q") else None
    rows = client.list("clients", filters)
    limit = int(args.get("limit") or 50)
    return {"clients": rows[:limit]}


def _pv(ar: str, en: str) -> Callable[[dict[str, Any]], Bilingual]:
    """Bilingual preview renderer that tolerates any missing arg (renders it as empty)."""
    def render(a: dict[str, Any]) -> Bilingual:
        safe: dict[str, Any] = defaultdict(str, a)
        return {"ar": ar.format_map(safe), "en": en.format_map(safe)}
    return render


WRITE_VERBS: dict[str, WriteVerb] = {
    "commerce.create_coupon": WriteVerb("commerce.create_coupon", "MEDIUM", "coupons",
        ("code", "discount_type", "discount_value"), _to_native_create_coupon,
        _pv("إنشاء كوبون «{code}»", "Create coupon “{code}”"), "create_coupon"),
    "commerce.create_product": WriteVerb("commerce.create_product", "MEDIUM", "products",
        ("name",), _to_native_create_product,
        _pv("إنشاء منتج «{name}» بسعر {price}", "Create product “{name}” at {price}"), "create_product"),
    "commerce.delete_product": WriteVerb("commerce.delete_product", "HIGH", "products",
        ("product_id",), _to_native_delete_product,
        _pv("حذف المنتج {product_id}", "Delete product {product_id}"), "delete_product"),
    "commerce.process_refund": WriteVerb("commerce.process_refund", "HIGH", "refunds",
        ("refund_target",), _to_native_process_refund,
        _pv("استرداد على {refund_target}", "Refund on {refund_target}"), "process_refund"),
    "commerce.record_fulfillment": WriteVerb("commerce.record_fulfillment", "MEDIUM", "fulfillments",
        ("order_id", "event"), _to_native_record_fulfillment,
        _pv("تسجيل شحن للطلب {order_id} ({event})", "Record fulfillment for {order_id} ({event})"), "record_fulfillment"),
    "commerce.record_payment": WriteVerb("commerce.record_payment", "HIGH", "payments",
        ("order_id", "event"), _to_native_record_payment,
        _pv("تسجيل دفعة للطلب {order_id} ({event})", "Record payment for {order_id} ({event})"), "record_payment"),
    "commerce.send_message": WriteVerb("commerce.send_message", "HIGH", "messages",
        ("phone", "text"), _to_native_send_message,
        _pv("إرسال رسالة إلى {phone}", "Send message to {phone}"), "send_message"),
    "commerce.update_product": WriteVerb("commerce.update_product", "MEDIUM", "products",
        ("product_id", "updates"), _to_native_update_product,
        _pv("تحديث المنتج {product_id}", "Update product {product_id}"), "update_product"),
    "commerce.update_product_quantity": WriteVerb("commerce.update_product_quantity", "MEDIUM", "products",
        ("product_id", "quantity"), _to_native_update_product_quantity,
        _pv("ضبط كمية {product_id} إلى {quantity}", "Set {product_id} quantity to {quantity}"), "update_product_quantity"),
    "services.create_client": WriteVerb("services.create_client", "MEDIUM", "clients",
        ("name", "phone"), _to_native_create_client,
        _pv("إضافة عميل «{name}»", "Add client “{name}”"), "create_client"),
    "services.create_invoice": WriteVerb("services.create_invoice", "HIGH", "invoices",
        ("party_id", "amount", "currency"), _to_native_create_invoice,
        _pv("فاتورة بمبلغ {amount} {currency} للعميل {party_id}", "Invoice {amount} {currency} for {party_id}"), "create_invoice",
        references={"party_id": "clients"}),  # an invoice needs an existing client
    "services.create_payment_link": WriteVerb("services.create_payment_link", "HIGH", "payment_links",
        ("invoice_id",), _to_native_create_payment_link,
        _pv("رابط دفع للفاتورة {invoice_id}", "Payment link for invoice {invoice_id}"), "create_payment_link",
        references={"invoice_id": "invoices"}),  # a payment link needs an existing invoice
    "services.draft_proposal": WriteVerb("services.draft_proposal", "MEDIUM", "proposals",
        ("party_id", "title", "amount", "currency"), _to_native_draft_proposal,
        _pv("مسودة عرض «{title}»", "Draft proposal “{title}”"), "draft_proposal"),
    "services.send_followup": WriteVerb("services.send_followup", "MEDIUM", "followups",
        ("party_id", "message"), _to_native_send_followup,
        _pv("متابعة مع {party_id}", "Follow up with {party_id}"), "send_followup"),
    "services.send_proposal": WriteVerb("services.send_proposal", "MEDIUM", "proposals",
        ("biz_proposal_id",), _to_native_send_proposal,
        _pv("إرسال العرض", "Send proposal"), "send_proposal"),
}


QUERY_VERBS: dict[str, QueryVerb] = {
    "services.list_clients": QueryVerb(verb="services.list_clients", run=_run_list_clients),
}


# PARKED — do not implement (deprecated in the standard):
#   commerce.update_order_status (GAP-001)


def entity_ref(verb: WriteVerb, created: dict[str, Any]) -> dict[str, Any]:
    # The SSOT entity id MUST be PocketBase's real record key (`id`), so a compensating
    # update/delete (ROLLBACK) targets the record itself — never the human `name` attribute,
    # which can collide or change (e.g. a product renamed after creation). Falls back to `name`
    # for backends whose primary key IS `name`. Matches the generic resource.* path's precedence.
    rid = created.get("id") or created.get("name") or ""
    return {"type": verb.entity_type, "id": rid, "url": f"/{verb.doctype}/{rid}"}

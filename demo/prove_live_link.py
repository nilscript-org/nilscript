"""Prove the adapter's PocketBaseClient talks to the LIVE PocketBase demo account.

Round-trips create -> list -> delete against the demo's existing `posts` collection,
authenticating as the superuser from the screenshot (test@example.com / KT45bzNA340Xa7L).
Credentials come from env so nothing is hardcoded; the public-demo defaults are filled in
only because this is PocketBase's open demo (it resets hourly).
"""

from __future__ import annotations

import os

from pocketbase_nil_adapter.system import PocketBaseClient

BASE_URL = os.environ.get("PB_URL", "https://pocketbase.io")
EMAIL = os.environ.get("PB_EMAIL", "test@example.com")
PASSWORD = os.environ.get("PB_PASSWORD", "123456")
COLLECTION = os.environ.get("PB_COLLECTION", "posts")


def main() -> None:
    print(f"connecting to {BASE_URL} as {EMAIL} ...")
    client = PocketBaseClient(BASE_URL, admin_email=EMAIL, admin_password=PASSWORD)
    print("  auth OK (token acquired)\n")

    print(f"CREATE in '{COLLECTION}' ...")
    created = client.create(COLLECTION, {"title": "NIL adapter live-link proof"})
    record_id = created["id"]
    print(f"  created id={record_id} title={created.get('title')!r}\n")

    print(f"LIST '{COLLECTION}' (filter title~'NIL adapter') ...")
    rows = client.list(COLLECTION, {"title": "NIL adapter"})
    print(f"  found {len(rows)} matching record(s); our id present: {any(r['id'] == record_id for r in rows)}\n")

    print(f"DELETE id={record_id} (clean up our test record) ...")
    client.delete(COLLECTION, record_id)
    print("  deleted\n")

    print(f"VERIFY gone ...")
    remaining = client.list(COLLECTION, {"title": "NIL adapter"})
    still_there = any(r["id"] == record_id for r in remaining)
    print(f"  our record still present: {still_there}")
    print("\nLIVE LINK PROVEN" if not still_there else "\nWARNING: record not cleaned up")


if __name__ == "__main__":
    main()

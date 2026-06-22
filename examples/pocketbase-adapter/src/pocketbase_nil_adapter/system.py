"""System-client layer: the ONE module that performs I/O against your backend.

`SystemClient` is the protocol the edge/translation depend on, so the conformance proof can run
against `FakeSystem` with no live instance. `PocketBaseClient` talks to a real PocketBase
(https://pocketbase.io) over its REST API — records live in collections.
"""

from __future__ import annotations

from typing import Any, Protocol


class SystemError(RuntimeError):
    """A write the System rejected — its message is surfaced/logged by the edge."""


class SystemClient(Protocol):
    def create(self, target: str, doc: dict[str, Any]) -> dict[str, Any]: ...

    def list(self, target: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def update(self, target: str, record_id: str, doc: dict[str, Any]) -> dict[str, Any]: ...

    def delete(self, target: str, record_id: str) -> None: ...

    def get(self, target: str, record_id: str) -> dict[str, Any] | None: ...  # read-back for verify


class PocketBaseClient:
    """PocketBase backend: every NIL entity is a collection of records.

    Auth: pass an admin token, or admin_email + admin_password to authenticate on construction.
    PocketBase REST surface used:
      - POST   /api/collections/{collection}/records          (create)
      - GET    /api/collections/{collection}/records           (list, with optional filter)
      - DELETE /api/collections/{collection}/records/{id}      (delete — used by ROLLBACK)
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8090",
        *,
        token: str | None = None,
        admin_email: str | None = None,
        admin_password: str | None = None,
    ) -> None:
        import httpx  # imported lazily so the conformance proof (FakeSystem) needs no httpx

        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=20.0)
        if token:
            self._http.headers["Authorization"] = token
        elif admin_email and admin_password:
            resp = self._http.post(
                "/api/admins/auth-with-password",
                json={"identity": admin_email, "password": admin_password},
            )
            if resp.status_code >= 400:
                raise SystemError(f"PocketBase auth failed: {resp.text}")
            self._http.headers["Authorization"] = resp.json()["token"]

    def create(self, target: str, doc: dict[str, Any]) -> dict[str, Any]:
        resp = self._http.post(f"/api/collections/{target}/records", json=doc)
        if resp.status_code >= 400:
            raise SystemError(f"{target}: {resp.text}")
        record = resp.json()
        # The generic edge/entity_ref keys off `name`; PocketBase's primary key is `id`.
        record.setdefault("name", record.get("id"))
        return record

    def list(self, target: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"perPage": 200}
        if filters:
            clause = " && ".join(f"{field}~'{value}'" for field, value in filters.items())
            params["filter"] = f"({clause})"
        resp = self._http.get(f"/api/collections/{target}/records", params=params)
        if resp.status_code >= 400:
            raise SystemError(f"{target}: {resp.text}")
        return resp.json().get("items", [])

    def update(self, target: str, record_id: str, doc: dict[str, Any]) -> dict[str, Any]:
        resp = self._http.patch(f"/api/collections/{target}/records/{record_id}", json=doc)
        if resp.status_code >= 400:
            raise SystemError(f"{target}/{record_id}: {resp.text}")
        record = resp.json()
        record.setdefault("name", record.get("id"))
        return record

    def delete(self, target: str, record_id: str) -> None:
        resp = self._http.delete(f"/api/collections/{target}/records/{record_id}")
        if resp.status_code >= 400:
            raise SystemError(f"{target}/{record_id}: {resp.text}")

    def get(self, target: str, record_id: str) -> dict[str, Any] | None:
        resp = self._http.get(f"/api/collections/{target}/records/{record_id}")
        if resp.status_code >= 400:
            return None
        record = resp.json()
        record.setdefault("name", record.get("id"))
        return record


# Backwards-friendly alias: the scaffold refers to a generic "RealSystemClient".
RealSystemClient = PocketBaseClient


class FakeSystem:
    """In-memory backend for the conformance proof — no live instance needed."""

    def __init__(self) -> None:
        self.docs: dict[str, list[dict[str, Any]]] = {}
        self._counter = 0

    def create(self, target: str, doc: dict[str, Any]) -> dict[str, Any]:
        self._counter += 1
        name = str(doc.get("name") or f"{target}-{self._counter:05d}")
        record = {**doc, "name": name, "target": target}
        self.docs.setdefault(target, []).append(record)
        return record

    def list(self, target: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        rows = list(self.docs.get(target, []))
        for field, value in (filters or {}).items():
            rows = [r for r in rows if str(value).lower() in str(r.get(field, "")).lower()]
        return rows

    def update(self, target: str, record_id: str, doc: dict[str, Any]) -> dict[str, Any]:
        for record in self.docs.get(target, []):
            if record.get("name") == record_id:
                record.update(doc)
                return record
        record = {**doc, "name": record_id, "target": target}  # upsert keeps the proof deterministic
        self.docs.setdefault(target, []).append(record)
        return record

    def delete(self, target: str, record_id: str) -> None:
        rows = self.docs.get(target, [])
        self.docs[target] = [r for r in rows if r.get("name") != record_id]

    def get(self, target: str, record_id: str) -> dict[str, Any] | None:
        return next((r for r in self.docs.get(target, []) if r.get("name") == record_id), None)

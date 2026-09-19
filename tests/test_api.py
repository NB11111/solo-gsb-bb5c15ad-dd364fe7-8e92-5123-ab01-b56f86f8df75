"""End-to-end API tests against a temporary SQLite database."""

import os
import tempfile

os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # Context-manager form runs the lifespan, which creates the tables.
    with TestClient(app) as c:
        yield c


BASE = {"replicas": 1, "image": "app:v1", "db": {"host": "a", "port": 5432}}
LEFT = {"replicas": 3, "image": "app:v1", "db": {"host": "b", "port": 5432}}
RIGHT = {"replicas": 1, "image": "app:v2", "db": {"host": "c", "port": 5433}}


def create_merge(client, key=None):
    payload = {"base": BASE, "left": LEFT, "right": RIGHT}
    if key:
        payload["idempotency_key"] = key
    resp = client.post("/merges", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_auto_merge_and_conflicts(client):
    merge = create_merge(client)
    assert merge["status"] == "pending"
    assert merge["revision"] == 1
    # Single-side changes adopted, conflict only on /db/host.
    assert merge["document"]["replicas"] == 3
    assert merge["document"]["image"] == "app:v2"
    assert merge["document"]["db"]["port"] == 5433
    pointers = [c["pointer"] for c in merge["conflicts"]]
    assert pointers == ["/db/host"]
    conflict = merge["conflicts"][0]
    assert conflict["left"] == "b" and conflict["right"] == "c"
    assert merge["document"]["db"]["host"] == {"__merge_conflict__": "/db/host"}


def test_batch_resolution_to_immutable_result(client):
    merge = create_merge(client)
    mid = merge["merge_id"]

    # Batch 1: wrong pointer -> 400, nothing applied.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/nope", "value": 1}]},
    )
    assert resp.status_code == 400

    # Batch 2: resolve the conflict with an explicit value.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={
            "revision": 1,
            "resolutions": [{"pointer": "/db/host", "value": "final-host"}],
        },
    )
    assert resp.status_code == 200, resp.text
    done = resp.json()
    assert done["status"] == "resolved"
    assert done["revision"] == 2
    assert done["conflicts"] == []
    assert done["result"]["db"]["host"] == "final-host"
    assert done["result"]["replicas"] == 3

    # Result is immutable: further resolutions are rejected with 409.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 2, "resolutions": [{"pointer": "/db/host", "value": "x"}]},
    )
    assert resp.status_code == 409

    # Status query returns the same immutable result.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["result"] == done["result"]


def test_stale_revision_returns_409(client):
    merge = create_merge(client)
    mid = merge["merge_id"]
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 999, "resolutions": [{"pointer": "/db/host", "choice": "left"}]},
    )
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == 1

    # Choice-based resolution still works with the right revision.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/db/host", "choice": "right"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["db"]["host"] == "c"


def test_partial_batches_bump_revision(client):
    merge = create_merge(client)
    # Add a second conflict so batches can be split.
    resp = client.post(
        "/merges",
        json={
            "base": {"a": 1, "b": 1},
            "left": {"a": 2, "b": 2},
            "right": {"a": 3, "b": 3},
        },
    )
    mid = resp.json()["merge_id"]
    assert resp.json()["conflict_count"] == 2

    # First batch resolves /a only; revision bumps to 2, still pending.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "choice": "left"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["revision"] == 2
    assert [c["pointer"] for c in body["conflicts"]] == ["/b"]

    # Old revision is now stale.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/b", "value": 9}]},
    )
    assert resp.status_code == 409

    # Second batch finishes the merge.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 2, "resolutions": [{"pointer": "/b", "value": 9}]},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    assert resp.json()["result"] == {"a": 2, "b": 9}


def test_idempotency_key_replays_merge(client):
    first = create_merge(client, key="order-123")
    resp = client.post(
        "/merges",
        json={"base": BASE, "left": LEFT, "right": RIGHT, "idempotency_key": "order-123"},
    )
    assert resp.status_code == 200  # replay, not 201
    assert resp.json()["merge_id"] == first["merge_id"]

    # Header form works too.
    resp = client.post(
        "/merges",
        json={"base": BASE, "left": LEFT, "right": RIGHT},
        headers={"Idempotency-Key": "order-123"},
    )
    assert resp.json()["merge_id"] == first["merge_id"]


def test_no_conflict_merge_is_immediately_resolved(client):
    resp = client.post(
        "/merges",
        json={"base": {"a": 1}, "left": {"a": 2}, "right": {"a": 2}},
    )
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["result"] == {"a": 2}


def test_unknown_merge_404(client):
    assert client.get("/merges/does-not-exist").status_code == 404
    assert client.get("/merges/does-not-exist/patch").status_code == 404


def test_patch_pending_returns_409(client):
    merge = create_merge(client)
    assert merge["status"] == "pending"
    resp = client.get(f"/merges/{merge['merge_id']}/patch")
    assert resp.status_code == 409
    assert "detail" in resp.json()


def test_patch_for_resolved_merge(client):
    merge = create_merge(client)
    mid = merge["merge_id"]
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={
            "revision": 1,
            "resolutions": [{"pointer": "/db/host", "value": "final-host"}],
        },
    )
    assert resp.status_code == 200
    result = resp.json()["result"]

    resp = client.get(f"/merges/{mid}/patch")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["merge_id"] == mid
    assert body["status"] == "resolved"
    assert body["patch"] == [
        {"op": "replace", "path": "/db/host", "value": "final-host"},
        {"op": "replace", "path": "/db/port", "value": 5433},
        {"op": "replace", "path": "/image", "value": "app:v2"},
        {"op": "replace", "path": "/replicas", "value": 3},
    ]

    # Digests are stable SHA-256 of canonical base / result.
    from app.patch import sha256_hex

    assert body["base_sha256"] == sha256_hex(BASE)
    assert body["result_sha256"] == sha256_hex(result)
    assert len(body["base_sha256"]) == 64

    # Applying ops in the returned order to the base rebuilds the result.
    from tests.patch_apply import apply_patch

    assert apply_patch(BASE, body["patch"]) == result


def test_patch_for_immediately_resolved_merge(client):
    resp = client.post(
        "/merges",
        json={"base": {"a": 1, "b": 2}, "left": {"a": 9}, "right": {"a": 9}},
    )
    mid = resp.json()["merge_id"]
    body = client.get(f"/merges/{mid}/patch").json()
    assert body["patch"] == [
        {"op": "remove", "path": "/b"},
        {"op": "replace", "path": "/a", "value": 9},
    ]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}

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


def test_resolution_preview_partial_batch_is_side_effect_free(client):
    # Two conflicts so a previewed batch can leave one open.
    resp = client.post(
        "/merges",
        json={
            "base": {"a": 1, "b": 1},
            "left": {"a": 2, "b": 2},
            "right": {"a": 3, "b": 3},
        },
    )
    mid = resp.json()["merge_id"]

    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "choice": "left"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["merge_id"] == mid
    assert body["status"] == "pending"
    assert body["revision"] == 1  # not bumped
    assert body["document"]["a"] == 2
    assert body["document"]["b"] == {"__merge_conflict__": "/b"}
    assert [c["pointer"] for c in body["remaining_conflicts"]] == ["/b"]
    assert body["remaining_conflict_count"] == 1
    assert body["would_resolve"] is False
    # Patch/digest are only promised once everything would resolve.
    assert "patch" not in body
    assert "result_sha256" not in body

    # Nothing was persisted: status, revision and document are untouched.
    state = client.get(f"/merges/{mid}").json()
    assert state["revision"] == 1
    assert state["status"] == "pending"
    assert state["document"]["a"] == {"__merge_conflict__": "/a"}

    # The preview can be replayed with a different decision, still revision 1.
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "choice": "right"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["document"]["a"] == 3
    assert client.get(f"/merges/{mid}").json()["revision"] == 1


def test_resolution_preview_full_batch_carries_patch_and_digest(client):
    merge = create_merge(client)
    mid = merge["merge_id"]

    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={
            "revision": 1,
            "resolutions": [{"pointer": "/db/host", "value": "final-host"}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["would_resolve"] is True
    assert body["remaining_conflicts"] == []
    assert body["remaining_conflict_count"] == 0
    assert body["document"]["db"]["host"] == "final-host"
    assert body["status"] == "pending"  # status never flips on a preview

    expected_result = {
        "replicas": 3,
        "image": "app:v2",
        "db": {"host": "final-host", "port": 5433},
    }
    assert body["document"] == expected_result
    from app.patch import sha256_hex

    assert body["result_sha256"] == sha256_hex(expected_result)
    # Same deterministic patch the merge would expose after a real resolve.
    assert body["patch"] == [
        {"op": "replace", "path": "/db/host", "value": "final-host"},
        {"op": "replace", "path": "/db/port", "value": 5433},
        {"op": "replace", "path": "/image", "value": "app:v2"},
        {"op": "replace", "path": "/replicas", "value": 3},
    ]
    from tests.patch_apply import apply_patch

    assert apply_patch(BASE, body["patch"]) == expected_result

    # Preview committed nothing: the merge is still pending and still rev 1.
    state = client.get(f"/merges/{mid}").json()
    assert state["status"] == "pending"
    assert state["revision"] == 1
    assert client.get(f"/merges/{mid}/patch").status_code == 409

    # A real submission after the preview yields exactly the previewed result.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={
            "revision": 1,
            "resolutions": [{"pointer": "/db/host", "value": "final-host"}],
        },
    )
    assert resp.status_code == 200
    done = resp.json()
    assert done["status"] == "resolved"
    assert done["revision"] == 2
    assert done["result"] == body["document"]
    assert client.get(f"/merges/{mid}/patch").json()["result_sha256"] == body[
        "result_sha256"
    ]


def test_resolution_preview_honours_choice_and_delete(client):
    # left deletes /a, right modifies it: choosing left should remove the key.
    resp = client.post(
        "/merges",
        json={"base": {"a": 1, "b": 2}, "left": {"b": 2}, "right": {"a": 9, "b": 2}},
    )
    mid = resp.json()["merge_id"]
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "choice": "left"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["would_resolve"] is True
    assert body["document"] == {"b": 2}
    assert body["patch"] == [{"op": "remove", "path": "/a"}]

    # Explicit null is a real value, distinguishable from "no value given".
    resp = client.post(
        "/merges",
        json={"base": {"a": 1}, "left": {"a": 2}, "right": {"a": 3}},
    )
    mid = resp.json()["merge_id"]
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "value": None}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["document"] == {"a": None}
    assert body["would_resolve"] is True
    from app.patch import sha256_hex

    assert body["result_sha256"] == sha256_hex({"a": None})


def test_resolution_preview_stale_and_resolved_return_409(client):
    merge = create_merge(client)
    mid = merge["merge_id"]

    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 999, "resolutions": [{"pointer": "/db/host", "choice": "left"}]},
    )
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == 1

    # Really resolve it, then previewing is 409 just like submitting.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/db/host", "choice": "left"}]},
    )
    assert resp.status_code == 200
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 2, "resolutions": [{"pointer": "/db/host", "value": "x"}]},
    )
    assert resp.status_code == 409

    assert client.post(
        "/merges/does-not-exist/resolution-preview",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "value": 1}]},
    ).status_code == 404


def test_resolution_preview_invalid_batches_fail_wholesale_with_400(client):
    resp = client.post(
        "/merges",
        json={
            "base": {"a": 1, "b": 1},
            "left": {"a": 2, "b": 2},
            "right": {"a": 3, "b": 3},
        },
    )
    mid = resp.json()["merge_id"]

    def preview(resolutions, revision=1):
        return client.post(
            f"/merges/{mid}/resolution-preview",
            json={"revision": revision, "resolutions": resolutions},
        )

    # Unknown pointer.
    resp = preview([{"pointer": "/nope", "value": 1}])
    assert resp.status_code == 400
    assert "/nope" in resp.json()["detail"]

    # Repeated pointer inside the same batch -> 400, whole batch rejected.
    resp = preview(
        [
            {"pointer": "/a", "choice": "left"},
            {"pointer": "/a", "choice": "right"},
        ]
    )
    assert resp.status_code == 400
    assert "duplicate" in resp.json()["detail"]

    # Both choice and value on one item -> 400 (even with explicit null).
    resp = preview([{"pointer": "/a", "choice": "left", "value": 5}])
    assert resp.status_code == 400
    assert "exactly one" in resp.json()["detail"]
    resp = preview([{"pointer": "/a", "choice": "left", "value": None}])
    assert resp.status_code == 400

    # Pointer already consumed by a real earlier batch: it is no longer open.
    assert preview([{"pointer": "/a", "choice": "left"}]).status_code == 200
    real = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "choice": "left"}]},
    )
    assert real.status_code == 200
    resp = preview(
        [{"pointer": "/a", "value": 1}, {"pointer": "/b", "value": 2}], revision=2
    )
    assert resp.status_code == 400

    # Every rejected batch left the merge at revision 2, /b still open,
    # and /a holds its committed value (no partial previews persisted).
    state = client.get(f"/merges/{mid}").json()
    assert state["revision"] == 2
    assert [c["pointer"] for c in state["conflicts"]] == ["/b"]
    assert state["document"]["a"] == 2


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}

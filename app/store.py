"""Merge-order persistence and state transitions."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from . import db
from .merge import MISSING, delete_at, merge3, parse_pointer, set_at
from .patch import generate_patch, sha256_hex

STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"


class MergeNotFound(Exception):
    pass


class MergeNotResolved(Exception):
    """Raised when the deterministic patch is requested before resolution."""


class StaleRevision(Exception):
    def __init__(self, current_revision: int):
        self.current_revision = current_revision
        super().__init__(f"stale revision, current is {current_revision}")


class AlreadyResolved(Exception):
    pass


class UnknownConflictPointer(Exception):
    def __init__(self, pointer: str):
        self.pointer = pointer
        super().__init__(f"no open conflict at pointer {pointer!r}")


class InvalidResolution(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    return {
        "merge_id": row["id"],
        "status": row["status"],
        "revision": row["revision"],
        "document": json.loads(row["document"]),
        "conflicts": json.loads(row["conflicts"]),
        "conflict_count": len(json.loads(row["conflicts"])),
        "result": json.loads(row["result"]) if row["result"] is not None else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_merge(merge_id: str) -> dict:
    row = db.query_one("SELECT * FROM merges WHERE id = ?", (merge_id,))
    if row is None:
        raise MergeNotFound(merge_id)
    return _row_to_dict(row)


def get_merge_patch(merge_id: str) -> dict:
    """Return the RFC 6902 patch from base_doc to the immutable result.

    Raises MergeNotFound (404) for an unknown merge and MergeNotResolved
    (409) while the merge is still pending.
    """
    row = db.query_one(
        "SELECT status, base_doc, result FROM merges WHERE id = ?",
        (merge_id,),
    )
    if row is None:
        raise MergeNotFound(merge_id)
    if row["status"] != STATUS_RESOLVED or row["result"] is None:
        raise MergeNotResolved(merge_id)
    base = json.loads(row["base_doc"])
    result = json.loads(row["result"])
    return {
        "merge_id": merge_id,
        "status": STATUS_RESOLVED,
        "patch": generate_patch(base, result),
        "base_sha256": sha256_hex(base),
        "result_sha256": sha256_hex(result),
    }


def create_merge(
    base: Any, left: Any, right: Any, idempotency_key: str | None
) -> tuple[dict, bool]:
    """Create a merge order. Returns (merge_view, created).

    A repeated idempotency key replays the existing merge order instead of
    creating a new one.
    """
    if idempotency_key:
        row = db.query_one(
            "SELECT * FROM merges WHERE idempotency_key = ?", (idempotency_key,)
        )
        if row is not None:
            return _row_to_dict(row), False

    document, conflicts = merge3(base, left, right)
    now = _now()
    status = STATUS_RESOLVED if not conflicts else STATUS_PENDING
    # No conflicts: the result is final and immutable from the start.
    result = document if not conflicts else None

    merge_id = uuid.uuid4().hex
    try:
        db.execute(
            """
            INSERT INTO merges (
                id, idempotency_key, status, revision,
                base_doc, left_doc, right_doc,
                document, conflicts, result, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                merge_id,
                idempotency_key,
                status,
                1,
                json.dumps(base),
                json.dumps(left),
                json.dumps(right),
                json.dumps(document),
                json.dumps(conflicts),
                json.dumps(result) if result is not None else None,
                now,
                now,
            ),
        )
    except Exception:
        # Concurrent insert with the same idempotency key: replay wins.
        if idempotency_key:
            row = db.query_one(
                "SELECT * FROM merges WHERE idempotency_key = ?", (idempotency_key,)
            )
            if row is not None:
                return _row_to_dict(row), False
        raise
    return get_merge(merge_id), True


def apply_resolutions(
    merge_id: str, expected_revision: int, resolutions: list[dict]
) -> dict:
    """Apply a batch of conflict resolutions.

    Raises StaleRevision (409) if expected_revision does not match the
    current revision, and AlreadyResolved (409) once the result is final.
    """
    row = db.query_one("SELECT * FROM merges WHERE id = ?", (merge_id,))
    if row is None:
        raise MergeNotFound(merge_id)
    if row["status"] == STATUS_RESOLVED:
        raise AlreadyResolved(merge_id)
    if row["revision"] != expected_revision:
        raise StaleRevision(row["revision"])

    document = json.loads(row["document"])
    conflicts = json.loads(row["conflicts"])
    open_conflicts = {c["pointer"]: c for c in conflicts}

    for item in resolutions:
        pointer = item["pointer"]
        conflict = open_conflicts.pop(pointer, None)
        if conflict is None:
            raise UnknownConflictPointer(pointer)
        tokens = parse_pointer(pointer)

        if item.get("choice") is not None:
            side = item["choice"]
            if side in conflict["missing"]:
                # Chosen side deleted the key: honour the deletion.
                document = delete_at(document, tokens)
            else:
                document = set_at(document, tokens, conflict[side])
        elif item.get("value_provided"):
            document = set_at(document, tokens, item["value"])
        else:
            raise InvalidResolution(
                f"resolution for {pointer!r} needs 'choice' or 'value'"
            )

    remaining = [c for c in conflicts if c["pointer"] in open_conflicts]
    resolved = not remaining
    db.execute(
        """
        UPDATE merges
        SET status = ?, revision = revision + 1, document = ?,
            conflicts = ?, result = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            STATUS_RESOLVED if resolved else STATUS_PENDING,
            json.dumps(document),
            json.dumps(remaining),
            # Once written the result is never updated again: immutable.
            json.dumps(document) if resolved else None,
            _now(),
            merge_id,
        ),
    )
    return get_merge(merge_id)

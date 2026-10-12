"""Gateway-held record of a crewmate's own changes, so the user can take one back.

A crewmate changing its own name, description or face (``update_self``, the
``kirocrew-mate`` server) writes the record at once; what the user gets is one
row in the crewmate's chat with an **Undo**. The value Undo puts back is held
HERE, in the gateway process, never read from the row: the transcript is
agent-writable, so a ``before`` taken from it could put any value on the record
under the user's click. The row carries the same ``before``/``after`` for
drawing only (:mod:`kiro_crew.dashboard.chat_cards`, surface ``self``).

Bounded and in-process on purpose. An undo is a short-horizon affordance -- the
user saw the change land and does not want it -- so the record lives as long as
this gateway does and the newest :data:`MAX_CHANGES` are kept. Past that, the
crew editor is where the user sets any value they like.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from typing import Any

#: Newest self-changes the gateway remembers for undo.
MAX_CHANGES = 256

STATUS_APPLIED = "applied"
STATUS_UNDONE = "undone"


class SelfChangeUnavailable(LookupError):
    """No undoable record for that id: unknown, another member's, or already undone."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_lock = threading.Lock()
_changes: OrderedDict[str, dict[str, Any]] = OrderedDict()


def reset() -> None:
    """Forget every record (tests)."""
    with _lock:
        _changes.clear()


def record(
    *,
    member: str,
    slug: str,
    slot_key: str,
    memory_store: str,
    changed: list[str],
    before: dict[str, Any],
    after: dict[str, Any],
    name: str = "",
) -> dict[str, Any]:
    """Remember one applied self-change and return its public record.

    *name* is the display name the crewmate goes by once the change is applied
    (its key when it has none): what the row calls it.
    """
    change_id = secrets.token_urlsafe(12)
    rec: dict[str, Any] = {
        "id": change_id,
        "member": member,
        "name": name or member,
        "slug": slug,
        "slot_key": slot_key,
        "memory_store": memory_store,
        "changed": list(changed),
        "before": dict(before),
        "after": dict(after),
        "status": STATUS_APPLIED,
        "ts": time.time(),
        # An undo the crewmate has not been told about yet (``take_unreported_undos``).
        "undo_reported": False,
    }
    with _lock:
        _changes[change_id] = rec
        while len(_changes) > MAX_CHANGES:
            _changes.popitem(last=False)
    return dict(rec)


def get(change_id: str) -> dict[str, Any] | None:
    with _lock:
        rec = _changes.get(change_id)
        return dict(rec) if rec else None


def take_for_undo(change_id: str, *, slug: str) -> dict[str, Any]:
    """The record to undo, or raise :class:`SelfChangeUnavailable`.

    *slug* must be the member the route was addressed to: a change id from one
    crewmate's chat cannot act on another's record.
    """
    with _lock:
        rec = _changes.get(change_id)
        if rec is None:
            raise SelfChangeUnavailable(
                "undo_unavailable", "this change can no longer be undone here"
            )
        if rec["slug"] != slug:
            raise SelfChangeUnavailable(
                "undo_unavailable", "that change belongs to another crewmate"
            )
        if rec["status"] != STATUS_APPLIED:
            raise SelfChangeUnavailable("already_undone", "this change was already undone")
        return dict(rec)


def mark_undone(change_id: str) -> dict[str, Any] | None:
    with _lock:
        rec = _changes.get(change_id)
        if rec is None:
            return None
        rec["status"] = STATUS_UNDONE
        rec["undone_ts"] = time.time()
        return dict(rec)


def take_unreported_undos(slot_key: str) -> list[dict[str, Any]]:
    """The undos in *slot_key*'s thread the crewmate has not heard yet; marks them heard.

    Read at the start of the crewmate's next turn and rendered as reference data
    (:func:`kiro_crew.context_blocks.render_self_change_undos`), the way change
    card outcomes are: an undo is the user's click, it starts no turn, and
    without this the crewmate's next reply would still speak of the face or name
    the user took back.
    """
    with _lock:
        out = []
        for rec in _changes.values():
            if rec["slot_key"] != slot_key or rec["status"] != STATUS_UNDONE:
                continue
            if rec.get("undo_reported"):
                continue
            rec["undo_reported"] = True
            out.append(dict(rec))
        return out

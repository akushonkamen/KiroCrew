"""Which model a crew's next turn runs on: its triage model or its work model.

A crew record carries one optional ``triage_model``. Unset, nothing here touches
the crew's session and every turn runs on the crew's ``model``, exactly as a crew
with no triage model always has.

Set, the choice is made from the LEDGER and from nothing else. A crew none of whose
open work items sits in an acting phase (:data:`ACTING_PHASES`) is only reading its
queue and checking whether anything moved, and that turn runs on the triage model.
Once any open item is in an acting phase the crew is doing the work, and every turn
runs on its work model until no item is acting again. The crew moves an item into
an acting phase itself, through the ledger write, so the cheap turn's one decision
-- does anything here need action? -- is what escalates to the work model.

THE SWITCH LANDS BETWEEN TURNS, never inside one. :func:`apply_turn_model` writes
``slot.model`` and arms the slot's deferred session reset, which the next turn
consumes before it acquires a session. That is the path a queued
``session_set_model`` pick takes, so the conversation resumes on the new model
rather than starting over. A running turn keeps the model it started on.

AN UNUSABLE TRIAGE MODEL FALLS BACK, VISIBLY. The record's id is shape-checked on
write; whether the account can run it is answered by the session that starts on
it, which records the backend's verdict on the slot (``slot.model_withheld``). A
withheld triage model is remembered per crew, every later turn runs on the work
model, and :func:`unavailable_triage_model` names it for the crew page. Changing the
setting clears the verdict, because the verdict is about one id.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from kiro_crew.dashboard.chat_utils import effective_session_key

from . import crew_store

logger = logging.getLogger("kirocrew.app.issue-radar")

#: The phases in which the crew is the one doing the work: an item it has chosen
#: and not yet parked, from ``selected`` through ``addressing-review``. Built from
#: the store's own sets so a phase added to either is acting here too. The waiting
#: phases (``awaiting-ci``, ``awaiting-merge``, ``awaiting-reply``) are not in it:
#: a crew holding only those is checking whether something moved, which is the
#: triage model's job.
ACTING_PHASES: frozenset[str] = frozenset(
    {"selected"} | set(crew_store.TTL_ACTIVE_PHASES) | set(crew_store.EDITING_PHASES)
)

#: crew id -> the triage model id a session withheld for that crew. Process
#: memory: a restart forgets it and the next session that starts on the id
#: records the verdict again.
_withheld: dict[str, str] = {}

#: slot key -> the model this module last pointed the slot at. Lets a crew whose
#: triage model was switched off be put back on its work model without touching a
#: crew this module never managed.
_written: dict[str, str] = {}


def triage_model(crew: Mapping[str, Any]) -> str:
    """The crew's configured triage model id, ``""`` when it has none."""
    value = crew.get("triage_model")
    return value.strip() if isinstance(value, str) else ""


def work_model(crew: Mapping[str, Any]) -> str:
    """The crew's work model; ``""`` is the agent's own default."""
    value = crew.get("model")
    return value if isinstance(value, str) else ""


def has_acting_item(items: Iterable[Mapping[str, Any]]) -> bool:
    """Whether any open work item sits in an acting phase."""
    return any(str(item.get("phase") or "") in ACTING_PHASES for item in items)


def is_managed(slot: Any) -> bool:
    """Whether this module last chose *slot*'s model, so switching off has work to do."""
    return str(getattr(slot, "key", "") or "") in _written


def unavailable_triage_model(crew: Mapping[str, Any]) -> str:
    """The configured triage model when a session withheld it, else ``""``."""
    triage = triage_model(crew)
    if not triage:
        return ""
    return triage if _withheld.get(str(crew.get("id") or "")) == triage else ""


def turn_model(crew: Mapping[str, Any], items: Iterable[Mapping[str, Any]]) -> str:
    """The model the crew's next turn runs on.

    The work model unless a triage model is set, usable, and no open item is in an
    acting phase.
    """
    work = work_model(crew)
    triage = triage_model(crew)
    if not triage or triage == work:
        return work
    if unavailable_triage_model(crew) or has_acting_item(items):
        return work
    return triage


def _note_session_verdict(crew: Mapping[str, Any], slot: Any) -> None:
    """Read the session's verdict on the triage model off the slot, if it has one."""
    triage = triage_model(crew)
    crew_id = str(crew.get("id") or "")
    if not triage or (getattr(slot, "model", "") or "") != triage:
        return
    verdict = getattr(slot, "model_withheld", None)
    if verdict is True and _withheld.get(crew_id) != triage:
        _withheld[crew_id] = triage
        logger.warning(
            "issue-radar crew %s: triage model %s is not available to this account; "
            "running every turn on the work model",
            crew_id,
            triage,
        )
    elif verdict is False:
        _withheld.pop(crew_id, None)


def _arm_reset(state: Any, slot: Any) -> None:
    """Make the next turn start on ``slot.model`` instead of the live session's.

    Armed only when a session can still be on the old model -- one registered, or
    an eager spawn in flight that may register one. With neither, the next turn
    cold-starts on the new model and a reset would only tear that session down.
    """
    sessions = getattr(state, "sessions", None)
    get_provider = getattr(sessions, "get_provider", None)
    if not callable(get_provider):
        return
    key = effective_session_key(slot)
    eager = getattr(slot, "_eager_spawn_task", None)
    if get_provider(key) is not None or (eager is not None and not eager.done()):
        slot._pending_reset_history_key = getattr(slot, "_pending_reset_history_key", None) or key


def apply_turn_model(
    state: Any,
    slot: Any,
    crew: Mapping[str, Any],
    items: Iterable[Mapping[str, Any]],
) -> bool:
    """Point *slot* at the model its next turn should run on. Returns whether it moved.

    SYNCHRONOUS on purpose: the running check and the writes happen with no
    suspension between them, so a turn cannot start in the middle. A running slot
    and a model-picker transaction in flight are both left alone; the next call
    decides again.
    """
    if slot is None or getattr(slot, "running", False):
        return False
    lock = getattr(slot, "_model_pick_lock", None)
    if lock is not None and lock.locked():
        return False
    slot_key = str(getattr(slot, "key", "") or "")
    current = getattr(slot, "model", "") or ""
    if triage_model(crew):
        _note_session_verdict(crew, slot)
        target = turn_model(crew, items)
        _written[slot_key] = target
    elif slot_key in _written:
        # Switched off. Put the slot back on the work model, but only if it still
        # holds the model this module put there: a model someone picked since is
        # theirs, and a crew this module never managed is not in the map at all.
        if current != _written.pop(slot_key):
            return False
        target = work_model(crew)
    else:
        return False
    if current == target:
        return False
    slot.model = target
    if isinstance(getattr(slot, "_model_pick_gen", None), int):
        # Counted as a deliberate pick so the model-fallback restore does not
        # undo it.
        slot._model_pick_gen += 1
    _arm_reset(state, slot)
    slot._dirty = True
    logger.info(
        "issue-radar crew %s: next turn runs on %s",
        crew.get("id"),
        target or "the agent default",
    )
    return True


def note_triage_change(slot: Any, before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    """Record that *slot* may still hold the triage model a crew edit just replaced.

    A gateway restart empties :data:`_written`, so a triage model switched off
    before this process ever applied one would otherwise stay on the slot. Marking
    it here lets the next :func:`apply_turn_model` put the work model back.
    """
    old = triage_model(before)
    if not old or old == triage_model(after) or slot is None:
        return
    if (getattr(slot, "model", "") or "") == old:
        _written[str(getattr(slot, "key", "") or "")] = old

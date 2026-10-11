"""A handoff survives a restart, and an unreadable log falls back to the creator.

The handoff's storage model has two halves and this file is the READ half. The verb
publishes a new manager only once its ``session/manager_set`` entry is on disk, so the
crew log is the authority for who drives a session and the slot's metadata line is a
cache of it. Without the rebuild below, every gateway restart would silently hand each
handed-over session back to whoever created it.

Driven against REAL units on disk -- ``CrewLog.create`` plus real appends -- rather
than a stubbed reader, because what is being tested is whether the bytes a handoff
leaves behind are the bytes a start reads back.

The rebuild RECONCILES rather than fills in, and the difference is the subject of half
the cases here. A cached manager on the metadata line is not evidence and does not win
a disagreement: the log publishes, clears and overrules it. A cache that wins over the
log is how a handoff that was taken away comes back at the next boot, because the line
can be older than the log -- written before the newest handoff, or written and then
superseded by a handoff whose own cache write failed.

The fail-closed direction is the other half: a log that says nothing, a log that states
a revert, and a log that cannot be READ all leave the session with its creator, which
is the reach the gateway had before any handoff.
"""

from __future__ import annotations

import pytest
from chat_test_helpers import _make_state

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, emit, session_tree
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log import store as crew_store
from kiro_crew.crew_log.session_tree import slot_manager
from kiro_crew.dashboard import session_control as sc

GATEWAY = "gateway"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Own data home per test, with no write armed on the real pool.

    The same isolation the adopt suite sets up, for the same reasons: the process-wide
    projection binds to ONE store, and the debounced writer saves outside the lock.

    Patched through ``_floor_monkeypatch`` because this fixture is AUTOUSE. The shared
    ``monkeypatch`` is undone as a whole by any ``monkeypatch.undo()`` a test makes to
    drop one of its own patches, which would lift this ``KIROCREW_HOME`` pin mid-test
    and point the reads below at the operator's real data home. The floor instance is
    undone independently, and a test that pins something itself still wins: its own
    ``monkeypatch`` is set up after this and torn down before it.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    _floor_monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_NoPool", (), {"submit": staticmethod(lambda *a, **k: None)}),
    )
    stp.reset_for_tests()
    yield
    stp.reset_for_tests()
    emit.reset_caches()


def _unit(sid: str, slot: str) -> CrewLog:
    """A real session unit for *slot*, with the opening entry a gateway writes."""
    handle = CrewLog.create(lg.KIND_SESSION, sid, owner="raymond", agent="kirocrew", slot=slot)
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": slot,
            "model": "opus",
            "cwd": "/w",
            "owner": "raymond",
            "resumed": False,
        },
        src=GATEWAY,
    )
    return handle


def _hand_over(handle: CrewLog, slot: str, manager: str, previous: str = "") -> None:
    """What the handoff verb appends at its commit point."""
    data = {"slot": slot, "manager": manager}
    if previous:
        data["previous"] = previous
    handle.append("session/manager_set", data, src=GATEWAY)


def _take_back(handle: CrewLog, slot: str, previous: str = "") -> None:
    """What a refused handoff appends to revert the slot to its creator.

    Spelled out here rather than taken from the emitter, so the bytes the reader folds
    are pinned independently of the code that writes them: a reader that stopped
    recognising this shape would still pass a test that asked the emitter what it emits.
    """
    data: dict[str, object] = {"slot": slot, "manager": "", "to_creator": True}
    if previous:
        data["previous"] = previous
    handle.append("session/manager_set", data, src=GATEWAY)


def _slot(state, key: str, creator: str):
    slot = state.get_or_create_slot(key)
    slot._created_by = creator
    return slot


def _manager(state, key: str, *, store: str = ""):
    """A live, ADMISSIBLE manager session under *key*.

    The rebuild re-validates a recorded grant before it publishes it, so a manager the
    log names has to be a session that could still be handed something: open, not a
    member's own DM surface, bound to the same memory store as the session it manages,
    and reachable at all. A test that skipped this would be asserting against the
    re-validation's refusal rather than against the fold it means to pin.
    """
    slot = state.get_or_create_slot(key)
    slot.memory_store = store
    return slot


async def _rebuild(state) -> int:
    return await sc.rebuild_managers_from_log(state)


def test_a_handoff_survives_a_restart(tmp_path, monkeypatch):
    """The case the rebuild exists for: the entry is on disk, so the manager comes back.

    Without this the fence would read the creator after every restart and refuse the
    conductor the worker was handed to -- the handoff would look like it never happened.
    """
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-conductor", "chat-lead")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-conductor")
    assert sc.manager_of(worker) == "chat-lead", "before the rebuild the creator holds it"

    assert asyncio.run(_rebuild(state)) == 1
    assert worker._manager == "chat-conductor"
    assert sc.manager_of(worker) == "chat-conductor"


def test_an_unreadable_log_falls_back_to_the_creator(tmp_path, monkeypatch):
    """FAIL CLOSED. A read that could not see the bytes must not be treated as "no
    handoff was recorded" in one direction or "somebody else holds it" in the other:
    the session stays with its creator, which is the reach the gateway already had.

    The read is made to fail at the seam a real unreadable store fails at -- the
    complete per-unit scan -- because the portable ways to make a file unreadable
    differ by platform and this suite runs on all of them.
    """
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-conductor")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")

    def _unreadable(directory):
        raise OSError("the unit could not be read")

    # Patched where the reader RESOLVES it: ``session_tree`` imports the name, so the
    # store's own attribute is not the one ``slot_manager`` calls.
    monkeypatch.setattr(session_tree, "find_last_manager_set", _unreadable)
    assert crew_store.find_last_manager_set is not _unreadable, "the store keeps its own"
    manager, decided = slot_manager("chat-worker")
    assert (manager, decided) == ("", False), "an unreadable unit decides nothing"

    assert asyncio.run(_rebuild(state)) == 0
    assert worker._manager == ""
    assert sc.manager_of(worker) == "chat-lead"


def test_a_log_with_no_handoff_leaves_the_creator_in_place(tmp_path, monkeypatch):
    """The ordinary session: an entry-less log is DECIDED, and it says the creator.

    Kept beside the unreadable case so the two empties cannot be collapsed: this one
    is a fact the gateway may act on, and the other is a read that failed.
    """
    import asyncio

    _unit("sid-plain", "chat-plain")
    state = _make_state(tmp_path)
    plain = _slot(state, "chat-plain", "chat-lead")

    assert slot_manager("chat-plain") == ("", True), "no entry is a decided answer"
    assert asyncio.run(_rebuild(state)) == 0
    assert sc.manager_of(plain) == "chat-lead"


def test_the_newest_handoff_wins(tmp_path, monkeypatch):
    """A session handed on twice is held by the manager the LAST entry names."""
    import asyncio

    handle = _unit("sid-worker", "chat-worker")
    _hand_over(handle, "chat-worker", "chat-conductor", "chat-lead")
    _hand_over(handle, "chat-worker", "chat-second", "chat-conductor")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-second")

    assert asyncio.run(_rebuild(state)) == 1
    assert sc.manager_of(worker) == "chat-second"


def test_an_entry_naming_another_slot_decides_nothing(tmp_path, monkeypatch):
    """The entry repeats the slot from the unit header, so the two can be compared --
    and a pair that disagrees is unprovable rather than authoritative."""
    import asyncio

    handle = _unit("sid-worker", "chat-worker")
    handle.append(
        "session/manager_set",
        {"slot": "chat-somebody-else", "manager": "chat-conductor"},
        src=GATEWAY,
    )
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")

    assert slot_manager("chat-worker") == ("", False)
    assert asyncio.run(_rebuild(state)) == 0
    assert sc.manager_of(worker) == "chat-lead"


def test_a_cached_manager_is_overruled_by_the_log(tmp_path, monkeypatch):
    """A cached value is reconciled, not trusted. The log names a different manager, and
    the log is the authority, so the cache is replaced rather than left standing.

    The line can be OLDER than the log: it is written after the entry and the write can
    fail, so a line naming the manager from before the newest handoff is an ordinary
    outcome rather than a corrupt one.
    """
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-second", "chat-first")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-second")
    worker._manager = "chat-first"

    assert asyncio.run(_rebuild(state)) == 1
    assert worker._manager == "chat-second"


def test_a_cached_manager_is_cleared_when_the_log_reverts_it(tmp_path, monkeypatch):
    """The case a statement that can only name a successor cannot express. The handoff
    was taken back, so the log says "no manager", and the rebuild has to CLEAR the
    cached value rather than find nothing to overwrite it with.

    This is the authority working in the direction that matters for authorization: the
    cache names a session the revert took off this one, and leaving it would re-grant at
    boot exactly the reach that revert removed.
    """
    import asyncio

    handle = _unit("sid-worker", "chat-worker")
    _hand_over(handle, "chat-worker", "chat-conductor", "chat-lead")
    _take_back(handle, "chat-worker", "chat-conductor")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    worker._manager = "chat-conductor"

    assert slot_manager("chat-worker") == ("", True), "a revert is a DECIDED empty"
    assert asyncio.run(_rebuild(state)) == 1
    assert worker._manager == "", "the cached manager is gone"
    assert sc.manager_of(worker) == "chat-lead", "and the creator holds it again"


def test_a_cached_manager_is_cleared_when_the_log_holds_no_entry(tmp_path, monkeypatch):
    """A line naming a manager the log cannot show was ever granted does not survive.

    The decided-empty answer of an entry-less log is a fact about the slot, so it is
    acted on: nothing granted this manager durably, and the gateway must not behave as
    though something did.
    """
    import asyncio

    _unit("sid-worker", "chat-worker")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    worker._manager = "chat-never-recorded"

    assert asyncio.run(_rebuild(state)) == 1
    assert worker._manager == ""
    assert sc.manager_of(worker) == "chat-lead"


def test_an_unreadable_log_clears_a_cached_manager(tmp_path, monkeypatch):
    """FAIL CLOSED in the reconciling direction too. An undecided read is not a reason to
    keep a cached manager: the cache cannot be checked, so it is not evidence, and the
    conservative answer is the reach the gateway had before any handoff.

    The cost is a handoff lost until the next start, which is the deliberate trade: the
    other direction's cost is a revoked session driving one it was taken off.
    """
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-conductor")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    worker._manager = "chat-conductor"

    def _unreadable(directory):
        raise OSError("the unit could not be read")

    monkeypatch.setattr(session_tree, "find_last_manager_set", _unreadable)
    assert slot_manager("chat-worker") == ("", False)

    assert asyncio.run(_rebuild(state)) == 1
    assert worker._manager == ""
    assert sc.manager_of(worker) == "chat-lead"


def test_a_cache_that_agrees_with_the_log_is_not_counted_as_moved(tmp_path, monkeypatch):
    """The ordinary restart. The line and the log say the same thing, so nothing moves
    and the count says so -- which is what makes the count usable as a signal that a
    cache and the authority disagreed."""
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-conductor")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-conductor")
    worker._manager = "chat-conductor"

    assert asyncio.run(_rebuild(state)) == 0
    assert worker._manager == "chat-conductor"


def test_the_crew_log_being_off_leaves_no_manager_standing(tmp_path, monkeypatch):
    """With no log there is no authority to read, so the creator holds every session.

    The empty-field case is the easy half. The half that matters is a slot whose
    metadata LINE names a manager: with the log switched off there is nothing that could
    confirm that grant, and a cache with no authority behind it is not a weaker
    authority but none at all. Returning early here is what would let a line written by
    a gateway that HAD a log go on granting reach on a gateway that cannot check it.
    """
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-conductor")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-conductor")
    plain = _slot(state, "chat-plain", "chat-lead")
    worker._manager = "chat-conductor"
    monkeypatch.setattr(emit, "enabled", lambda: False)

    assert asyncio.run(_rebuild(state)) == 1, "the cached grant is the one that moves"
    assert worker._manager == "", "nothing confirms it, so nothing stands"
    assert sc.manager_of(worker) == "chat-lead"
    assert plain._manager == "", "a slot that named nobody needs no change"


# -- a recorded grant is RE-VALIDATED before it is published ------------------


def test_a_recorded_manager_rebound_to_another_store_is_refused(tmp_path, monkeypatch):
    """The log is the authority on WHAT was recorded, never on whether it is still
    ALLOWED. A recipient admitted because it shared the giver's memory store can be
    rebound to another principal's store while the gateway is DOWN, and no append-only
    entry can know that -- so the admission is re-asked at the one point the entry would
    otherwise take effect.

    This is also what closes the gap the verb's corrective append leaves: a correction
    that never landed, or a crash between the entry and its correction, leaves the log
    stating a grant that was refused. Re-validating catches every one of those without
    depending on a second write having succeeded.
    """
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-conductor")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    worker.memory_store = ""
    # Rebound while the gateway was down: the entry on disk still names it.
    _manager(state, "chat-conductor", store="member-raymond")

    assert slot_manager("chat-worker") == ("chat-conductor", True), "the log still says so"
    assert asyncio.run(_rebuild(state)) == 0
    assert worker._manager == "", "but it is not published"
    assert sc.manager_of(worker) == "chat-lead"


def test_a_recorded_manager_that_is_no_longer_open_is_refused(tmp_path, monkeypatch):
    """A key names nothing once its session is gone, and a session that is not there
    cannot drive anything. Fail closed rather than publish a manager the gateway could
    not even look at."""
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-conductor")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    # No slot for the recorded manager: it was closed before this start.

    assert asyncio.run(_rebuild(state)) == 0
    assert worker._manager == ""
    assert sc.manager_of(worker) == "chat-lead"


def test_a_recorded_manager_that_went_channel_linked_is_refused(tmp_path, monkeypatch):
    """The same target-side containment the admission applied. A channel-linked session
    mirrors its conversation to Slack or Telegram, so it is not addressable at all --
    and a grant that predates the link must not survive it."""
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-conductor")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    conductor = _manager(state, "chat-conductor")
    conductor.linked_session_key = "slack:C123"

    assert asyncio.run(_rebuild(state)) == 0
    assert worker._manager == ""
    assert sc.manager_of(worker) == "chat-lead"


def test_a_recorded_manager_on_a_member_dm_surface_is_refused(tmp_path, monkeypatch):
    """Refused by KEY, as it is at admission: a crew member's own DM surface is the
    member's operating surface and not a session work runs in. No entry could make it
    one, so no entry is trusted to."""
    import asyncio

    from kiro_crew.members import DM_SLOT_KEY_PREFIX

    dm = f"{DM_SLOT_KEY_PREFIX}raymond"
    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", dm)
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    # No slot is created for it, deliberately: the prefix refusal is read off the KEY,
    # so it has to answer before anything asks whether a session is open there.

    assert slot_manager("chat-worker") == (dm, True), "the log records it"
    assert asyncio.run(_rebuild(state)) == 0
    assert worker._manager == "", "and it is refused anyway"
    assert sc.manager_of(worker) == "chat-lead"

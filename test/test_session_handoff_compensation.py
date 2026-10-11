"""A handoff whose commit refuses puts the parent back, and the window settles alone.

The two halves of a handoff are the tree edge and the manager, and this file is about
what happens when only the first of them lands. ``_reparent_target`` appends the edge and
waits for it to be on disk BEFORE the commit runs, which is the right order -- a field
written first and an append that then failed would leave the gateway acting on a move no
record carries. The cost of that order is this file's subject: between the two there is a
window where the edge is durable and the manager has not moved.

A refusal inside that window is not a tidy no-op if it is left alone. The target hangs
under the recipient while the giver is still its manager, so the recipient cannot drive
it and the giver's own ``session_release`` answers ``not_parent`` -- the move is neither
done nor undoable by either session, and only an operator editing state could separate
them.

So the refusal is compensated: the previous parent is restored, durably, before the
caller is told. The log is append-only, so "restored" is another edge stated on top and
the fold prefers the newest -- both moves stay readable.

The second half of the file is the cancellation rule. The window runs in its own task
behind a shield, because a request that goes away must not take a half-finished handoff
with it: the entry is already queued and the commit may already have published. Cancelling
the caller cancels the WAIT and not the work.
"""

from __future__ import annotations

import asyncio

import pytest
from chat_test_helpers import _make_state
from test_session_control_tree import (  # noqa: F401 -- imported FIXTURES apply here
    _enabled,
    _hold,
    _key,
    _live,
    _run,
    _slot,
    _tree_on,
)

from kiro_crew.crew_log import emit
from kiro_crew.dashboard import session_control as sc


@pytest.fixture(autouse=True)
def _saves(_floor_monkeypatch):
    """Report every slot save as landed and record which slots were saved.

    The same floor the handoff suite uses, for the same reason: what these tests are
    about is the ORDER of the durable writes, so the cache write is stubbed and observed.
    """
    saved: list[str] = []

    async def _save(state, slot, **kwargs):
        saved.append(slot.key)
        return True

    _floor_monkeypatch.setattr(sc, "save_slot_off_loop", _save)
    return saved


def _team(state):
    """A lead and a conductor, plus a worker the lead created. The dispatch shape."""
    owner = _live(state, _slot(state, "chat-owner-tab"), "sid-owner")
    lead = _live(state, _slot(state, "chat-lead"), "sid-lead")
    conductor = _live(state, _slot(state, "chat-conductor"), "sid-conductor")
    worker = _live(state, _slot(state, "chat-worker"), "sid-worker")
    lead._created_by = owner.key
    conductor._created_by = lead.key
    worker._created_by = lead.key
    return lead, conductor, worker


def _entries(monkeypatch, *, manager_lands: bool = True) -> list[dict]:
    """Record every tree and manager entry the verb emits, IN ORDER.

    All three emitters, which is the point: the compensation is itself an entry, so a
    recorder that watched only the manager write could not tell a restored parent from an
    abandoned one. The order is what the assertions read -- the edge, then the manager,
    then (only on a refusal) the edge that puts the parent back.

    Each stub settles its own append, because the verb waits on it. ``manager_lands``
    stands in for a manager entry the writer gave up on, which is the second way a commit
    refuses after the edge is already durable.
    """
    calls: list[dict] = []

    def _record(op: str, sid: str, kw: dict, landed: bool) -> None:
        settle = kw.pop("on_settled", None)
        calls.append({"op": op, "sid": sid, **kw})
        if settle is not None:
            settle(landed)

    monkeypatch.setattr(
        emit, "on_session_adopted", lambda sid, **kw: _record("adopt", sid, kw, True)
    )
    monkeypatch.setattr(
        emit, "on_session_released", lambda sid, **kw: _record("release", sid, kw, True)
    )
    monkeypatch.setattr(
        emit, "on_manager_set", lambda sid, **kw: _record("manager", sid, kw, manager_lands)
    )
    return calls


def _refuse_recipient_once(state, conductor, calls: list[dict]) -> None:
    """Close the recipient from the EDGE's settle callback.

    That callback is the suspension between the last gate and the commit, which is where
    a recipient really can go: its own routes take its slot lock and not this verb's. It
    is the cheapest honest way to reach the commit's refusal with the edge already down.
    """
    original = emit.on_session_adopted

    def _close_then_settle(sid, **kw):
        if not calls:
            state._slots.pop(conductor.key, None)
        original(sid, **kw)

    emit.on_session_adopted = _close_then_settle


def _refuse_recipient_after_the_manager_append(state, conductor, calls: list[dict]) -> None:
    """Close the recipient from the MANAGER entry's settle callback.

    A later suspension than :func:`_refuse_recipient_once` reaches, and the one the
    check before the append cannot cover: the manager entry is on disk by the time this
    runs, so the only thing standing between a replaced recipient and the reach is a
    check made AFTER the append. The recipient's own routes take its slot lock and not
    this verb's, which is what makes this window real rather than contrived.
    """
    original = emit.on_manager_set

    def _close_then_settle(sid, **kw):
        if not any(call["op"] == "manager" for call in calls):
            state._slots.pop(conductor.key, None)
        original(sid, **kw)

    emit.on_manager_set = _close_then_settle


def _undecided_manager_append(monkeypatch) -> None:
    """Make the handoff's own manager append answer UNDECIDED, once.

    Stubbed at :func:`~kiro_crew.crew_log.emit.awaiting_settlement` rather than by
    withholding a settle for the real bound, because the bound is seconds and what is
    under test here is not how long the wait is. The entry is still QUEUED through the
    recorder, with nothing settling it, which is exactly the state the undecided answer
    describes: an append that may land at any time afterwards.

    Only the first call is answered this way. The correction that follows goes through
    the real wait, so what the assertions read is the correction's own outcome.
    """
    real = emit.awaiting_settlement
    answered: list[str] = []

    async def _settlement(emit_one, *, what, **kwargs):
        if not answered:
            answered.append(what)
            emit_one(lambda _wrote: None)
            return None
        return await real(emit_one, what=what, **kwargs)

    monkeypatch.setattr(emit, "awaiting_settlement", _settlement)


# -- a refused commit puts the parent back ------------------------------------


def test_a_refused_commit_restores_the_previous_parent(tmp_path, monkeypatch):
    """The finding itself. The edge is durable, the commit refuses, and the parent the
    target had is put back by a further entry -- so the tree and the manager agree again
    and the giver's own release still works.

    Without the compensation the last entry on disk would name the conductor as the
    parent while the lead was still the manager, and neither session could undo it.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    _refuse_recipient_once(state, conductor, calls)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_replaced"
    assert [call["op"] for call in calls] == ["adopt", "adopt"], "the edge, then the undo"
    undo = calls[-1]
    assert undo["slot"] == worker.key
    assert undo["parent_slot"] == lead.key, "back under the parent it had"
    assert undo["previous_parent_slot"] == conductor.key, "and it says what it came from"
    assert worker._manager == "", "the manager never moved, so nothing to take back"


def test_a_refused_commit_releases_a_target_that_had_no_parent(tmp_path, monkeypatch):
    """Putting a root target back is a RELEASE, and that is not a detail.

    An adoption naming an empty parent is not an edge at all and the emitter refuses to
    write one, so a compensation that only ever adopted would have left the root case
    silently uncompensated -- the one case where the undo matters most, because there is
    no earlier parent to notice the target is missing.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    # No ``_hold``: the worker is a root, which is what a dispatched session is until
    # something adopts it.
    _refuse_recipient_once(state, conductor, calls)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_replaced"
    assert [call["op"] for call in calls] == ["adopt", "release"], "the edge, then the undo"
    undo = calls[-1]
    assert undo["slot"] == worker.key
    assert undo["previous_parent_slot"] == conductor.key, "released from where it was put"
    assert worker._manager == ""


def test_a_manager_entry_that_never_landed_also_restores_the_parent(tmp_path, monkeypatch):
    """The second way the commit refuses after the edge is down: the manager entry is the
    commit point, so a writer that gave up on it means the handoff did not happen -- and
    the edge that was ordered before it has to come back out."""
    calls = _entries(monkeypatch, manager_lands=False)
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target="chat-worker", to="chat-conductor"
            )
        )

    assert exc.value.code == "handoff_not_recorded"
    assert [call["op"] for call in calls] == ["adopt", "manager", "adopt"]
    assert calls[-1]["parent_slot"] == lead.key
    assert worker._manager == "", "nothing is published on the strength of a dropped append"


def test_a_committed_handoff_leaves_the_parent_where_it_moved(tmp_path, monkeypatch, _saves):
    """Control. With nothing refusing there is NO undo entry -- a compensation that fired
    on the success path would hand every session straight back and the verb would do
    nothing at all."""
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    result = _run(
        sc.handoff_target(state, caller_session_key=_key(lead), target=worker.key, to=conductor.key)
    )

    assert [call["op"] for call in calls] == ["adopt", "manager"], "no undo on success"
    assert result["manager"] == conductor.key
    assert result["previous_manager"] == lead.key
    assert worker._manager == conductor.key
    assert _saves == [], "the two entries are the whole of it; no slot line is written"


def test_a_compensation_that_cannot_be_written_still_reports_the_original_refusal(
    tmp_path, monkeypatch
):
    """The undo is bounded and non-raising. A caller that asked for a handoff must be told
    why the HANDOFF failed, not why the undo did -- the undo's own failure is an operator
    problem and goes to the log, because no retry by this caller fixes it."""
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    _refuse_recipient_once(state, conductor, calls)

    def _undo_explodes(sid, **kw):
        if calls:
            raise OSError("the compensating append could not be queued")
        raise AssertionError("the first edge must still be emitted")

    original = emit.on_session_adopted

    def _dispatch(sid, **kw):
        if calls:
            _undo_explodes(sid, **kw)
        original(sid, **kw)

    monkeypatch.setattr(emit, "on_session_adopted", _dispatch)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_replaced", "the original refusal, not the undo's"
    assert worker._manager == ""


# -- the commit window settles independently of the caller --------------------


def test_the_slot_stays_fenced_across_the_commit(tmp_path, monkeypatch):
    """No other verb may decide about the slot inside the window.

    The append's own fence clears when the append settles, which is exactly when the
    window OPENS -- so without a hold of its own the slot would be unfenced for the one
    stretch where its recorded parent and its manager disagree. Read from inside the
    commit, which is the only place the question can be asked.
    """
    _entries(monkeypatch)
    fenced: list[tuple[str, bool]] = []
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    original = emit.on_manager_set

    def _held(key: str) -> bool:
        pending = sc._tree_pending_slots.get(key)
        return pending is not None and not pending.done()

    def _check_then_emit(sid, **kw):
        # Observed at the commit point, which is the moment that matters: the edge is on
        # disk, the manager has not moved yet, and the two disagree until this lands.
        fenced.append(("target", _held(worker.key)))
        fenced.append(("recipient", _held(conductor.key)))
        original(sid, **kw)

    monkeypatch.setattr(emit, "on_manager_set", _check_then_emit)

    _run(
        sc.handoff_target(state, caller_session_key=_key(lead), target=worker.key, to=conductor.key)
    )

    assert fenced == [("target", True), ("recipient", True)], "BOTH endpoints, at the commit"
    assert worker.key not in sc._tree_pending_slots, "and released when the window closes"
    assert conductor.key not in sc._tree_pending_slots


def test_a_cancelled_caller_does_not_abandon_the_commit(tmp_path, monkeypatch):
    """The cancellation rule. The caller goes away mid-commit -- a disconnect, a tool
    timeout -- and the window finishes anyway: the entry is queued and the manager is
    published, so stopping halfway is what would leave the two halves apart.

    Driven at the manager entry, which is the commit point: the settle is withheld, the
    caller is cancelled while waiting on it, and only then does the entry land.
    """
    pushes: list[int] = []
    settles: list = []
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    monkeypatch.setattr(state, "push_slots_update", lambda: pushes.append(1))

    monkeypatch.setattr(
        emit,
        "on_session_adopted",
        lambda sid, **kw: (kw.get("on_settled") or (lambda _ok: None))(True),
    )

    def _withhold(sid, **kw):
        settles.append(kw.get("on_settled"))

    monkeypatch.setattr(emit, "on_manager_set", _withhold)

    async def _drive() -> None:
        call = asyncio.ensure_future(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )
        # Until the commit has reached the withheld entry, which is where the caller is
        # parked when it is cancelled. Waited against a DEADLINE rather than a count of
        # zero-length sleeps: the verb hops a thread more than once on the way here
        # (the gate's warm, the id resolution), and a thread hop needs wall time, so a
        # fixed number of ``sleep(0)`` passes runs out on a loaded machine and the test
        # fails for the one reason it is not about.
        deadline = asyncio.get_running_loop().time() + 10.0
        while not settles and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        assert settles, "the commit never reached the manager entry"
        window = next(iter(sc._commit_windows))

        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert not window.done(), "the cancellation must not have taken the window with it"

        settles[0](True)
        await asyncio.wait_for(window, timeout=5)

    _run(_drive())

    assert worker._manager == conductor.key, "the window published after the caller left"
    assert pushes == [1], "and the sidebar work it owed still ran"
    assert not sc._commit_windows, "the window is not held after it finishes"
    assert worker.key not in sc._tree_pending_slots, "and the slot is unfenced"
    assert conductor.key not in sc._tree_pending_slots


# -- a handoff refused AFTER its own entry landed is taken back on the log ----


def test_a_recipient_replaced_after_the_append_is_refused(tmp_path, monkeypatch):
    """The check that decides runs AFTER the manager entry, not before it.

    The append suspends, and the recipient can be closed, rebound or reopened inside that
    suspension by its own routes, which take its slot lock and not this verb's. A verdict
    formed before the append and acted on after it would move the reach to whatever sits
    at the key by then.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    _refuse_recipient_after_the_manager_append(state, conductor, calls)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_replaced"
    assert worker._manager == "", "nothing is published to a recipient that changed"


def test_a_handoff_refused_after_its_entry_landed_is_taken_back_on_the_log(tmp_path, monkeypatch):
    """The finding. The entry is the AUTHORITY, so a refusal that only declines to
    publish the field leaves the log still granting the reach -- and the next start
    publishes it, the gateway's refusal having lived in memory alone.

    The correction is another entry on top, because the log is append-only and the fold
    takes the newest statement per slot. This target had no manager before, so the
    correction is the revert: a correction naming the creator would instead record a
    handoff TO the creator, leaving the field set where it was empty.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    _refuse_recipient_after_the_manager_append(state, conductor, calls)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_replaced"
    assert [call["op"] for call in calls] == [
        "adopt",
        "manager",
        "manager",
        "adopt",
    ], "the edge, the manager entry, the manager taken back, then the edge put back"
    taken_back = calls[2]
    assert taken_back["slot"] == worker.key
    assert taken_back["manager"] == "", "no manager is named"
    assert taken_back["to_creator"] is True, "and the revert is what says so"
    assert taken_back["previous"] == conductor.key, "it records the handoff it undoes"


def test_the_correction_names_the_manager_the_handoff_replaced(tmp_path, monkeypatch):
    """A target already handed over once reverts to the manager it HAD, not to its
    creator. The two are different sessions, so a correction that always reverted to the
    creator would quietly take the earlier handoff away as well -- an undo of something
    nobody refused.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    # Already handed over once, so the FIRST manager is the session that may hand it on
    # again: ``handoff`` is one of the verbs a handoff widens, and the creator's own
    # reach went with the first transfer. The recipient is authorized on its own terms
    # too, so the giver has to be able to name it -- hence the conductor is this
    # session's own.
    first = _live(state, _slot(state, "chat-first-manager"), "sid-first-manager")
    first._created_by = lead.key
    conductor._created_by = first.key
    worker._manager = first.key
    _refuse_recipient_after_the_manager_append(state, conductor, calls)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(first), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_replaced"
    taken_back = calls[2]
    assert taken_back["manager"] == first.key, "back to the manager it had"
    assert taken_back["to_creator"] is False, "which is not a revert to the creator"
    assert worker._manager == first.key, "and the field never moved off it"


def test_an_undecided_manager_append_is_corrected_rather_than_abandoned(tmp_path, monkeypatch):
    """An append still QUEUED when the wait gives up is the case a two-valued answer
    cannot handle. It is not a refusal: it may land at any time afterwards, and it would
    then be the newest statement on the log -- so a start would publish a manager this
    verb refused.

    The correction is queued behind it rather than skipped. The writer is FIFO per unit,
    so whichever order the two land in, the newest statement is the correction.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    _undecided_manager_append(monkeypatch)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "handoff_not_recorded"
    assert [call["op"] for call in calls] == ["adopt", "manager", "manager", "adopt"]
    assert calls[2]["to_creator"] is True, "the queued handoff is taken back"
    assert calls[2]["previous"] == conductor.key
    assert worker._manager == "", "and nothing is published on an undecided append"


# -- the settlement answer the correction is decided from ---------------------


def test_a_settled_append_answers_true_and_a_dropped_one_answers_false(tmp_path):
    """The two decided answers, which need no correction between them: one landed and one
    never will."""

    async def _both() -> tuple[bool | None, bool | None]:
        landed = await emit.awaiting_settlement(lambda s: s(True), what="a landed append")
        dropped = await emit.awaiting_settlement(lambda s: s(False), what="a dropped append")
        return landed, dropped

    assert _run(_both()) == (True, False)


def test_an_append_that_never_settles_answers_undecided(tmp_path):
    """The third answer, and the only one that obliges a caller to correct the log.

    Kept apart from False because the two differ in exactly what matters to a caller that
    has to undo: a dropped append left nothing behind, and this one may still land.
    """

    async def _never() -> bool | None:
        return await emit.awaiting_settlement(
            lambda _settle: None, what="an append nothing settles", timeout=0.05
        )

    assert _run(_never()) is None


def test_the_two_valued_wait_reads_undecided_as_not_recorded(tmp_path):
    """``awaiting_commit`` is the same wait for callers with nothing to take back, and it
    folds the two ways of not being on disk together -- which is safe only because those
    callers publish on the append rather than undo on its refusal."""

    async def _folded() -> bool:
        return await emit.awaiting_commit(
            lambda _settle: None, what="an append nothing settles", timeout=0.05
        )

    assert _run(_folded()) is False


# -- BOTH endpoints are fenced while the tree append is in flight -------------


def _withheld_tree_append(monkeypatch) -> "tuple[list[dict], list]":
    """Record the emitted entries and WITHHOLD the FIRST tree append's settle.

    Leaves the first move parked exactly where a slow writer parks it: the edge handed
    over, nothing on disk yet, and the projection still showing the tree as it was. That
    is the state every decision made meanwhile is made against, so it is the state the
    fence and the cycle check have to cover.

    Only the first, so a LATER move can still complete inside that window. A helper that
    withheld every append would make every second move fail for the one reason these
    tests are not about, and a guard that refused everything while anything was pending
    would pass just as well as one that reads the edges.
    """
    calls: list[dict] = []
    settles: list = []

    def _record(op: str, sid: str, kw: dict, landed: bool | None) -> None:
        settle = kw.pop("on_settled", None)
        calls.append({"op": op, "sid": sid, **kw})
        if landed is None:
            settles.append(settle)
        elif settle is not None:
            settle(landed)

    def _adopt(sid, **kw):
        # The first is held; every one after it settles, so the window stays open around
        # moves that are meant to be decided inside it.
        _record("adopt", sid, kw, None if not settles else True)

    monkeypatch.setattr(emit, "on_session_adopted", _adopt)
    monkeypatch.setattr(
        emit, "on_session_released", lambda sid, **kw: _record("release", sid, kw, True)
    )
    monkeypatch.setattr(emit, "on_manager_set", lambda sid, **kw: _record("manager", sid, kw, True))
    return calls, settles


def test_the_reverse_handoff_is_refused_while_the_first_append_is_in_flight(tmp_path, monkeypatch):
    """The cycle a target-only fence admits. A handoff's commit window outlives the
    request, so the edge can still be queued when the next verb is decided -- and the
    REVERSE handoff names the other endpoint as its target, so a fence on this one never
    sees it. ``is_ancestor`` then reads a tree the pending edge has not reached, admits
    the opposite move, and both entries land: a loop in an append-only log that no fold
    can present and no verb can undo.

    Fencing both endpoints refuses it instead, and ``tree_write_pending`` is the honest
    code -- the earlier write may still land, so the caller is told to read the tree and
    try again rather than that anything is wrong.
    """
    calls, settles = _withheld_tree_append(monkeypatch)
    monkeypatch.setattr(sc, "_TREE_APPEND_TIMEOUT", 0.05)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    async def _drive() -> None:
        first = asyncio.ensure_future(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )
        # The caller's own wait runs out while the append is still queued, which is the
        # only way to reach the next verb with the window still running.
        with pytest.raises(sc.SessionControlError) as pending:
            await first
        assert pending.value.code == "tree_write_pending"
        assert settles, "the edge was handed to the writer"

        with pytest.raises(sc.SessionControlError) as reverse:
            await sc.handoff_target(
                state, caller_session_key=_key(lead), target=conductor.key, to=worker.key
            )
        assert (
            reverse.value.code == "tree_write_pending"
        ), "the recipient of the in-flight handoff is fenced too"
        assert [call["op"] for call in calls] == ["adopt"], "the reverse appended nothing"

        settles[0](True)
        for window in list(sc._commit_windows):
            await asyncio.wait_for(asyncio.shield(window), timeout=5)

    _run(_drive())

    assert conductor.key not in sc._tree_pending_slots, "the parent fence is released"
    assert worker.key not in sc._tree_pending_slots


def test_a_handoff_the_caller_stopped_waiting_for_is_still_audited_and_broadcast(
    tmp_path, monkeypatch, _saves
):
    """The reach moves in the window, so the record of it has to be written there too.

    A caller whose wait runs out raises through ``_audit_denials``, which writes a
    ``denied`` line for that timeout. The window then commits and the manager really
    does move -- so an ``allowed`` line written in the caller's frame is never written
    at all, and the SEL is left saying the opposite of what happened. The sidebar is the
    same problem in a different place: a broadcast tied to the caller's frame never
    tells anyone the tree changed.
    """
    calls, settles = _withheld_tree_append(monkeypatch)
    monkeypatch.setattr(sc, "_TREE_APPEND_TIMEOUT", 0.05)
    audits: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    pushes: list[int] = []
    monkeypatch.setattr(state, "push_slots_update", lambda: pushes.append(1))

    async def _drive() -> None:
        with pytest.raises(sc.SessionControlError) as pending:
            await sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        assert pending.value.code == "tree_write_pending"
        assert [entry["outcome"] for entry in audits] == ["denied"], "the timeout is denied"
        assert not pushes, "and nothing is broadcast yet"

        settles[0](True)
        for window in list(sc._commit_windows):
            await asyncio.wait_for(asyncio.shield(window), timeout=5)

    _run(_drive())

    assert worker._manager == conductor.key, "the window published after the caller left"
    outcomes = [entry["outcome"] for entry in audits]
    assert outcomes == ["denied", "allowed"], "and the SEL says the handoff happened"
    allowed = audits[-1]
    assert allowed["slot_key"] == worker.key
    assert allowed["operation"] == "handoff"
    assert pushes == [1], "the sidebar is told exactly once"


# -- the verb's own pre-lock refusals are audited -----------------------------


def test_a_handoff_that_changes_nothing_is_audited(tmp_path, monkeypatch):
    """``already_manager`` is the verb's OWN refusal and it is raised before the lock, so
    nothing on the success path records it. A denied attempt to move control of another
    session is exactly what the SEL is for -- a boundary with no trail is the one most
    worth a trail."""
    audits: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=worker.key
            )
        )

    assert exc.value.code == "already_manager"
    assert audits == [
        {
            "caller_session_key": _key(lead),
            "operation": "handoff",
            "slot_key": worker.key,
            "outcome": "denied",
            "detail": {"code": "already_manager"},
        }
    ]


def test_a_recipient_on_another_memory_store_is_audited(tmp_path, monkeypatch):
    """The recipient-side refusals are resolved before the lock as well, and they are the
    ones worth seeing most: each is an attempt to put a session under a principal that
    may not have it. Audited against the TARGET, which is the session whose control was
    being moved; the recipient is named in the refusal's own message."""
    audits: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    conductor.memory_store = "member-raymond"

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_other_store"
    assert [entry["outcome"] for entry in audits] == ["denied"]
    assert audits[0]["slot_key"] == worker.key, "attributed to the session being moved"
    assert audits[0]["detail"] == {"code": "recipient_other_store"}


# -- the cycle check sees QUEUED edges, not only settled ones -----------------


def _four_node_tree(state):
    """Four sessions and the two settled edges from the finding: ``D -> A``, ``B -> C``.

    All four are opened by the owner tab, so none of them is ownership-fenced and any of
    them may hand any other over -- which is what lets the sequence be driven at all. The
    shape is the point: the loop the fourth move closes runs through every one of them, so
    no two moves share an endpoint and a per-endpoint fence can never see it coming.
    """
    owner = _live(state, _slot(state, "chat-owner-tab"), "sid-owner")
    nodes = {}
    for name in ("a", "b", "c", "d"):
        slot = _live(state, _slot(state, f"chat-{name}"), f"sid-{name}")
        slot._created_by = owner.key
        nodes[name] = slot
    _hold(state, nodes["d"].key, nodes["a"].key, sid="sid-d")
    _hold(state, nodes["b"].key, nodes["c"].key, sid="sid-b")
    return owner, nodes


def test_a_queued_edge_is_part_of_the_cycle_check(tmp_path, monkeypatch):
    """The four-node loop a per-endpoint fence cannot refuse.

    ``D -> A`` and ``B -> C`` are settled. ``A -> B`` is handed to the writer and stays
    queued, so the projection does not carry it. A handoff of ``C`` to ``D`` then names
    neither ``A`` nor ``B``: both endpoint fences pass, and an ancestor check against the
    settled tree alone sees no loop. Let it through and the log holds
    ``A -> B -> C -> D -> A``, four valid append-only entries the fold can only mark.

    The fix is to check against the settled tree PLUS every queued edge, so the move is
    refused ``would_cycle`` on the view that includes the one edge still in flight.
    """
    calls, settles = _withheld_tree_append(monkeypatch)
    monkeypatch.setattr(sc, "_TREE_APPEND_TIMEOUT", 0.05)
    state = _make_state(tmp_path)
    owner, nodes = _four_node_tree(state)

    async def _drive() -> None:
        with pytest.raises(sc.SessionControlError) as pending:
            await sc.handoff_target(
                state,
                caller_session_key=_key(owner),
                target=nodes["a"].key,
                to=nodes["b"].key,
            )
        assert pending.value.code == "tree_write_pending", "A -> B is queued, not settled"
        assert sc._tree_pending_edges == {nodes["a"].key: nodes["b"].key}

        with pytest.raises(sc.SessionControlError) as closing:
            await sc.handoff_target(
                state,
                caller_session_key=_key(owner),
                target=nodes["c"].key,
                to=nodes["d"].key,
            )
        assert closing.value.code == "would_cycle", "the queued edge closes the loop"
        assert [call["op"] for call in calls] == ["adopt"], "the fourth move wrote nothing"

        settles[0](True)
        for window in list(sc._commit_windows):
            await asyncio.wait_for(asyncio.shield(window), timeout=5)

    _run(_drive())

    assert sc._tree_pending_edges == {}, "the queued edge is forgotten once it settles"


def test_the_same_move_is_allowed_once_the_queued_edge_settles_elsewhere(tmp_path, monkeypatch):
    """The guard refuses a LOOP, not a coincidence of timing.

    Same four sessions and the same fourth move, with ``A -> B`` never queued: nothing is
    in flight, the settled tree holds no path from ``C`` up to ``D``, and the handoff goes
    through. Without this the previous test would pass just as well against a guard that
    refused every move while any append was pending.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    owner, nodes = _four_node_tree(state)
    assert sc._tree_pending_edges == {}

    result = _run(
        sc.handoff_target(
            state, caller_session_key=_key(owner), target=nodes["c"].key, to=nodes["d"].key
        )
    )

    assert result["manager"] == nodes["d"].key
    assert [call["op"] for call in calls] == ["adopt", "manager"], "it really moved"
    assert sc._tree_pending_edges == {}


def test_a_queued_edge_that_closes_no_loop_does_not_refuse(tmp_path, monkeypatch):
    """A pending edge is not a veto. ``A -> B`` is queued and a handoff of ``D`` to ``C``
    is decided in that window: the combined view carries the queued edge and still shows
    no path from ``D`` up to ``C``, so the move is allowed.

    This is the half that keeps the guard honest about what it refuses -- an
    overlay that made every concurrent move fail would satisfy the loop case and be
    useless.
    """
    calls, settles = _withheld_tree_append(monkeypatch)
    monkeypatch.setattr(sc, "_TREE_APPEND_TIMEOUT", 0.05)
    state = _make_state(tmp_path)
    owner, nodes = _four_node_tree(state)

    async def _drive() -> None:
        with pytest.raises(sc.SessionControlError):
            await sc.handoff_target(
                state,
                caller_session_key=_key(owner),
                target=nodes["a"].key,
                to=nodes["b"].key,
            )
        assert sc._tree_pending_edges == {nodes["a"].key: nodes["b"].key}

        # D is under A; putting it under C adds D -> C, and C's own chain is C (B -> C is
        # the other direction), so nothing closes.
        moved = await sc.handoff_target(
            state, caller_session_key=_key(owner), target=nodes["d"].key, to=nodes["c"].key
        )
        assert moved["manager"] == nodes["c"].key
        # Asserted HERE, while the first edge is still queued: this is the move that had
        # to be allowed inside the window, and once the first one settles its own commit
        # adds a second manager entry that says nothing about this.
        assert [call["op"] for call in calls].count("manager") == 1, "it committed"
        assert nodes["d"]._manager == nodes["c"].key

        settles[0](True)
        for window in list(sc._commit_windows):
            await asyncio.wait_for(asyncio.shield(window), timeout=5)

    _run(_drive())

    assert sc._tree_pending_edges == {}, "both edges are forgotten once they settle"


def test_a_pending_set_that_cannot_be_read_refuses(tmp_path, monkeypatch):
    """FAIL CLOSED on the view itself. A cycle check is only as good as the tree it ran
    against, so "no loop" concluded from a view that could not be built is a confident
    wrong answer rather than a missing one -- the same rule this whole PR applies to an
    unreadable log.
    """
    _entries(monkeypatch)
    state = _make_state(tmp_path)
    owner, nodes = _four_node_tree(state)

    def _unreadable(_nodes):
        raise RuntimeError("the queued edges could not be folded in")

    monkeypatch.setattr(sc, "_tree_with_pending_edges", _unreadable)

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(owner), target=nodes["c"].key, to=nodes["d"].key
            )
        )

    assert exc.value.code == "tree_not_ready"
    assert exc.value.status == 409, "retryable: the next attempt may read it"


def test_no_reachable_sequence_writes_a_cycle_to_the_log(tmp_path, monkeypatch):
    """The property the three cases above are instances of, asserted over the whole
    four-node shape: every ORDERED pair of moves is attempted, and whatever the verb
    allows, the edges that reach the log never form a loop.

    Driven against the real fold rather than a hand-checked list, because the thing worth
    pinning is not which pairs refuse but that no pair persists a cycle -- an append-only
    log cannot take one back.
    """

    def _loops(edges: dict[str, str]) -> bool:
        """Whether following ``child -> parent`` from any node ever revisits a node.

        Written out rather than taken from ``is_ancestor``, which answers a different
        question and answers it False for ``candidate == of`` by contract -- so asking it
        whether a slot is its own ancestor can never report a loop.
        """
        for start in edges:
            seen: set[str] = set()
            cursor = start
            while cursor in edges:
                if cursor in seen:
                    return True
                seen.add(cursor)
                cursor = edges[cursor]
        return False

    pairs = [("a", "b"), ("c", "d"), ("d", "c"), ("b", "a"), ("a", "c"), ("b", "d")]
    for first, second in [(x, y) for x in pairs for y in pairs if x != y]:
        calls, settles = _withheld_tree_append(monkeypatch)
        monkeypatch.setattr(sc, "_TREE_APPEND_TIMEOUT", 0.05)
        state = _make_state(tmp_path)
        owner, nodes = _four_node_tree(state)
        edges = {nodes["d"].key: nodes["a"].key, nodes["b"].key: nodes["c"].key}

        async def _drive() -> None:
            for child, parent in (first, second):
                try:
                    await sc.handoff_target(
                        state,
                        caller_session_key=_key(owner),
                        target=nodes[child].key,
                        to=nodes[parent].key,
                    )
                except sc.SessionControlError:
                    # A refusal the CALLER saw says nothing about the log: an append
                    # handed over before the refusal still lands. What reached the log is
                    # read off the emissions below, which is the whole shape of this bug.
                    pass
            for settle in settles:
                if settle is not None:
                    settle(True)
            for window in list(sc._commit_windows):
                try:
                    await asyncio.wait_for(asyncio.shield(window), timeout=5)
                except sc.SessionControlError:
                    pass

        _run(_drive())

        # Folded from what was EMITTED, in order, the way the tree folds it: an adoption
        # sets the parent and a release clears it, newest entry winning. This is the tree
        # a cold start would rebuild, including every compensation.
        for call in calls:
            if call["op"] == "adopt":
                edges[call["slot"]] = call["parent_slot"]
            elif call["op"] == "release":
                edges.pop(call["slot"], None)

        assert not _loops(edges), f"{first} then {second} persisted a loop: {edges}"
        sc._tree_pending_edges.clear()
        sc._commit_windows.clear()


# -- a rollback is a forward write and is checked like one --------------------


def _loop_waiting_for_the_rollback(state):
    """The tree a compensation of ``A`` meets after the window ran: ``A -> B``,
    ``R -> A``, ``P -> R``.

    No cycle in it -- each edge was checked and each was fine -- and yet putting ``A``
    back under ``P``, the parent it had when the verb started, closes
    ``A -> P -> R -> A``. That is the shape the rollback has to refuse, and the reason it
    cannot be read off the tree the verb started from: the previous parent moved BELOW
    the target while the window was running.
    """
    owner = _live(state, _slot(state, "chat-owner-tab"), "sid-owner")
    nodes = {}
    for name in ("a", "p", "r", "b"):
        slot = _live(state, _slot(state, f"chat-{name}"), f"sid-{name}")
        slot._created_by = owner.key
        nodes[name] = slot
    _hold(state, nodes["a"].key, nodes["b"].key, at=300, sid="sid-a")
    _hold(state, nodes["r"].key, nodes["a"].key, sid="sid-r")
    _hold(state, nodes["p"].key, nodes["r"].key, at=200, sid="sid-p")
    return owner, nodes


def test_a_rollback_that_would_cycle_is_recognised(tmp_path, monkeypatch):
    """The rollback is checked against the same settled-plus-queued view the forward path
    uses. Asserted on the tree above, where no single edge is a loop and the restoration
    is the one that would complete it."""
    state = _make_state(tmp_path)
    _owner, nodes = _loop_waiting_for_the_rollback(state)

    assert sc._restoring_would_cycle(nodes["a"].key, nodes["p"].key) is True
    # The other direction, so the check is not simply answering True: B is above A, and
    # putting A back under B closes nothing.
    assert sc._restoring_would_cycle(nodes["a"].key, nodes["b"].key) is False


def test_a_rollback_that_cannot_be_checked_is_treated_as_cyclic(tmp_path, monkeypatch):
    """FAIL CLOSED, and for a compensation that means answering True. A view this cannot
    build is a view that cannot clear the edge, and the alternative -- releasing to the
    root -- loses nesting rather than persisting a loop. That is the opposite direction
    from the forward path, which refuses the operation, because a compensation has no
    refusal available: it IS the undo.
    """
    state = _make_state(tmp_path)
    _owner, nodes = _loop_waiting_for_the_rollback(state)

    def _unreadable(_nodes):
        raise RuntimeError("the queued edges could not be folded in")

    monkeypatch.setattr(sc, "_tree_with_pending_edges", _unreadable)
    assert sc._restoring_would_cycle(nodes["a"].key, nodes["b"].key) is True


def test_the_compensation_releases_instead_of_restoring_into_a_loop(tmp_path, monkeypatch):
    """The whole rollback, driven through the helper the window calls: the target is
    RELEASED to the root rather than adopted back into the cycle.

    Without this the compensation is the forward path missing its guard -- it would state
    ``A -> P`` on an append-only log, and four valid entries later the fold has a loop it
    can only mark while no verb undoes it.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    _owner, nodes = _loop_waiting_for_the_rollback(state)

    landed = _run(
        sc._restore_parent(
            target_sid="sid-a",
            slot_key=nodes["a"].key,
            parent_key=nodes["b"].key,
            parent_sid="sid-b",
            previous_parent=nodes["p"].key,
            previous_parent_sid="sid-p",
        )
    )

    assert landed is True
    assert [call["op"] for call in calls] == ["release"], "released, not adopted back"
    assert calls[0]["slot"] == nodes["a"].key
    assert calls[0]["previous_parent_slot"] == nodes["b"].key, "it says where it came from"


def test_a_rollback_with_no_loop_still_restores_the_parent(tmp_path, monkeypatch):
    """The guard refuses a loop, not a restoration. With the previous parent still ABOVE
    the target, the compensation adopts it back exactly as before -- otherwise every
    refused move would quietly flatten its target to the root."""
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    _owner, nodes = _loop_waiting_for_the_rollback(state)

    landed = _run(
        sc._restore_parent(
            target_sid="sid-a",
            slot_key=nodes["a"].key,
            parent_key=nodes["r"].key,
            parent_sid="sid-r",
            previous_parent=nodes["b"].key,
            previous_parent_sid="sid-b",
        )
    )

    assert landed is True
    assert [call["op"] for call in calls] == ["adopt"]
    assert calls[0]["parent_slot"] == nodes["b"].key, "back under the parent it had"


# -- the commit admission is the admission a restart re-applies ---------------


def test_a_recipient_on_another_store_than_the_target_is_refused(tmp_path, monkeypatch):
    """The sharpest of the store rules: the one a RESTART will apply.

    The commit compared GIVER against RECIPIENT, and the rebuild compares RECIPIENT
    against the session it manages. Three sessions make those disagree -- an
    owner-rooted lead on the default store dispatches a member-store worker and a
    default-store conductor -- so a handoff is accepted now and silently revoked at the
    next start, which is worse than refusing it up front. Both comparisons are made at
    the verb, so the answer the caller gets is the answer the next boot will give.
    """
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    lead.memory_store = ""
    conductor.memory_store = ""
    worker.memory_store = "member-raymond"

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_other_store"
    assert "different memory stores" in exc.value.message
    assert calls == [], "no grant is appended for a handoff that would not survive"
    assert worker._manager == ""


def test_the_stores_are_re_read_immediately_before_publication(tmp_path, monkeypatch):
    """The append suspends, so neither reading survives from the gate. A target rebound
    inside that window is refused at the publication check rather than published on a
    verdict formed before the change."""
    calls = _entries(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")

    original = emit.on_manager_set

    def _rebind_then_settle(sid, **kw):
        if not any(call["op"] == "manager" for call in calls):
            worker.memory_store = "member-raymond"
        original(sid, **kw)

    emit.on_manager_set = _rebind_then_settle

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "recipient_replaced"
    assert worker._manager == "", "nothing is published across a store that moved"
    assert [call["op"] for call in calls] == [
        "adopt",
        "manager",
        "manager",
        "adopt",
    ], "the grant is taken back on the log and the parent put back"


# -- the manager reconstruction is off the boot path and gated ----------------


def test_the_widened_verbs_refuse_until_the_manager_restore_settles(tmp_path, monkeypatch):
    """The reconstruction is history-sized, so it runs as its own task and READY does not
    wait on it. Until it settles every manager field is empty, so the fence would read the
    CREATOR for every slot -- which admits a giver whose reach a recorded handoff already
    moved away. That is the direction that must not be guessed, so the verbs whose answer
    depends on the reconstruction refuse while it runs.

    Self-clearing: the pass releases the gate however it ends, so a failure cannot leave
    the gateway refusing for the life of the process.
    """
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    assert not sc.manager_restore_pending(), "no window is open by default"

    sc.arm_manager_restore()
    try:
        assert sc.manager_restore_pending()
        for operation in ("send", "read", "stop", "close", "adopt", "release", "handoff"):
            with pytest.raises(sc.SessionControlError) as exc:
                sc.authorize_target(
                    state,
                    caller_session_key=_key(lead),
                    target=worker.key,
                    operation=operation,
                )
            assert exc.value.code == "managers_not_restored", operation
            assert exc.value.status == 409, "retryable: the pass is still running"
        # A verb OUTSIDE the widened set does not depend on the manager, so it is not
        # held up: the fence reads the creator for it either way.
        assert (
            sc.authorize_target(
                state, caller_session_key=_key(lead), target=worker.key, operation="rename"
            )
            is worker
        )
    finally:
        _run(sc.rebuild_managers_from_log(state))

    assert not sc.manager_restore_pending(), "the pass cleared the gate"
    assert (
        sc.authorize_target(
            state, caller_session_key=_key(lead), target=worker.key, operation="send"
        )
        is worker
    ), "and the verb is served once it has"


def test_the_gate_clears_even_when_the_restore_raises(tmp_path, monkeypatch):
    """A gate left armed by a failure would refuse every widened verb for the life of the
    process, which is worse than the fail-closed per-slot answer the pass already gives.
    Released in a ``finally``, so however the pass ends the gateway is usable."""
    state = _make_state(tmp_path)
    _team(state)

    async def _explode(*_a, **_k):
        raise RuntimeError("the pass could not run")

    monkeypatch.setattr(sc, "_restore_managers", _explode)
    sc.arm_manager_restore()
    assert sc.manager_restore_pending()

    with pytest.raises(RuntimeError):
        _run(sc.rebuild_managers_from_log(state))

    assert not sc.manager_restore_pending(), "the gate is not left armed by a failure"


def test_a_queued_rollback_is_announced_to_the_next_mover(tmp_path, monkeypatch):
    """The mirror image of the rollback's own cycle guard. Checking the restoration
    against the queued edges while not PUBLISHING it among them leaves the loop reachable
    from the other side: the next mover's cycle check reads a tree without the edge this
    compensation is about to write, admits a move that closes against it, and both land.

    So the rollback takes a settlement-backed waiter like any forward append -- it fences
    the slot and publishes ``slot -> previous parent`` -- and both survive a wait that
    runs out, because an append still queued is exactly the one a later decision must see.
    """
    settles: list = []

    def _withhold(sid, **kw):
        settles.append(kw.get("on_settled"))

    monkeypatch.setattr(emit, "on_session_adopted", _withhold)
    monkeypatch.setattr(emit, "COMMIT_WAIT_SECONDS", 0.05)
    state = _make_state(tmp_path)
    _owner, nodes = _loop_waiting_for_the_rollback(state)

    async def _drive() -> None:
        landed = await sc._restore_parent(
            target_sid="sid-a",
            slot_key=nodes["a"].key,
            parent_key=nodes["r"].key,
            parent_sid="sid-r",
            previous_parent=nodes["b"].key,
            previous_parent_sid="sid-b",
        )
        assert landed is False, "nothing is on disk while the append is still queued"
        assert sc._tree_pending_edges == {
            nodes["a"].key: nodes["b"].key
        }, "the rollback edge is published for the next mover's cycle check"
        pending = sc._tree_pending_slots.get(nodes["a"].key)
        assert pending is not None and not pending.done(), "and the slot stays fenced"

        settles[0](True)
        await asyncio.sleep(0)

    _run(_drive())

    assert sc._tree_pending_edges == {}, "both clear when the writer settles, not before"
    assert nodes["a"].key not in sc._tree_pending_slots


def test_a_queued_rollback_blocks_the_move_that_would_close_on_it(tmp_path, monkeypatch):
    """What the announcement buys, end to end: while the restoration of ``A`` under ``B``
    is queued, a handoff that would put ``B`` under ``A`` is refused ``would_cycle``
    rather than decided against a tree missing that edge."""
    settles: list = []

    def _withhold(sid, **kw):
        settles.append(kw.get("on_settled"))

    monkeypatch.setattr(emit, "on_session_adopted", _withhold)
    monkeypatch.setattr(emit, "COMMIT_WAIT_SECONDS", 0.05)
    state = _make_state(tmp_path)
    owner, nodes = _loop_waiting_for_the_rollback(state)

    async def _drive() -> None:
        assert (
            await sc._restore_parent(
                target_sid="sid-a",
                slot_key=nodes["a"].key,
                parent_key=nodes["r"].key,
                parent_sid="sid-r",
                previous_parent=nodes["b"].key,
                previous_parent_sid="sid-b",
            )
            is False
        )
        assert sc._tree_pending_edges == {nodes["a"].key: nodes["b"].key}

        with pytest.raises(sc.SessionControlError) as exc:
            await sc.handoff_target(
                state,
                caller_session_key=_key(owner),
                target=nodes["b"].key,
                to=nodes["a"].key,
            )
        # Either refusal is the guard doing its job: the slot fence catches the move
        # whose endpoint is already in flight, and the cycle check catches it against
        # the announced edge. Both are fail-closed and retryable.
        assert exc.value.code in ("would_cycle", "tree_write_pending"), exc.value.code

        settles[0](True)
        await asyncio.sleep(0)

    _run(_drive())

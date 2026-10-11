"""Skip-level reach along the manager line, and where a closed manager's reports go.

``_manager`` holds a session's DIRECT manager and falls back to ``_created_by`` when
nothing has been handed over. Two things read the line above that first hop:

* the ownership fence in ``authorize_target``, for the operations a handoff widens
  only, so a caller that manages the target's manager (or manages that one's manager)
  reaches the target as well -- the skip level a lead needs to reach the worker its
  conductor runs. ``create``, ``fork`` and the memory delegation gate keep reading
  ``_created_by`` alone and are untouched by the walk;
* ``revert_manager_line``, run on the committed side of a close, which sends a closing
  manager's reports up to the manager above it so a finished conductor does not strand
  the workers it was running.

Every edge of the walk fails CLOSED -- an empty hop, a cycle, a hop whose slot is gone,
a line past the depth bound -- so a line that cannot be stated denies reach.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from chat_test_helpers import _make_state
from test_session_handoff import WIDENED as PR1_WIDENED
from test_session_handoff import _team as pr1_team

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key


@pytest.fixture(autouse=True)
def _enabled(_floor_monkeypatch):
    """Session control AND the manager line on for every test here.

    The line is OFF in shipped config, so the tests that exercise it have to turn it
    on; ``TestTheLineOffIsTheFenceWithoutIt`` turns it back off to pin that default.

    ``_floor_monkeypatch`` rather than the shared ``monkeypatch``: the shared one is
    undone as a whole, so a test calling ``monkeypatch.undo()`` would lift this
    autouse patch with its own and read the real config switch.
    """
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    _floor_monkeypatch.setattr(sc, "manager_chain_enabled", lambda: True)
    # The occupancy and departure records are MODULE-level mutable state. Cleared
    # around every test in this file, because a record one test wrote would otherwise
    # resolve a hop another test deliberately left unresolvable -- which is a passing
    # test proving nothing.
    _forget_everything()

    async def _save(state, slot, **kwargs):
        """Report every report-metadata save as landed.

        The repair's ORDERING is what these tests are about -- staged, saved, published --
        so the disk write is stubbed and the landing is the thing varied. The failure
        half overrides this per test.
        """
        return True

    _floor_monkeypatch.setattr(sc, "save_slot_off_loop", _save)


def _forget_everything():
    """Both halves of the in-memory record, which is what a RESTART leaves behind.

    The occupancy table has to go with the departure table: it is written when a key is
    taken, so clearing only the departures would leave an occupancy no live process
    could have, and the walk would read the close window rather than the gap a fresh
    process sees.
    """
    sc._key_occupancy.clear()
    sc._departed_managers.clear()


def _entry(key):
    """*key*'s CURRENT occupant's whole entry, or None.

    The same two steps the walk takes -- resolve the occupancy, then match the pair
    exactly -- because a lookup by key alone is the thing the pair key exists to make
    impossible.
    """
    return sc._departed_managers.get((key, sc._key_occupancy.get(key)))


def _recorded(key):
    """What *key*'s current occupant reported to, or None when there is no entry.

    ``None`` is "no entry for whoever holds this key now" and ``""`` is "reported to
    nobody". Whether the entry is settled is :func:`_pending`'s question, because the
    two are different facts and a test that means one should not read the other.
    """
    entry = _entry(key)
    return None if entry is None else entry.manager


def _pending(key):
    """Whether *key*'s current occupant is inside its close, with its line unsettled."""
    entry = _entry(key)
    return entry is not None and entry.pending


def _depart(state, key):
    """Take *key* out of the live table the way every departure path does.

    Pops the slot and records what it reported to -- one mapping write, which is the
    whole of what a departure now does to the reporting line. The walk reads THROUGH
    this record; nothing rewrites any session's own manager.
    """
    slot = state.get_slot(key)
    state._slots.pop(key, None)
    sc.note_manager_departed(state, key, slot)
    return slot


def _outside(state, slot, caller_key):
    """``_outside_manager_line``, which the fence reaches only with the line ON."""
    return sc._outside_manager_line(state, slot, caller_key)


def _line(state, *keys: str):
    """Open ``keys`` as a reporting line, each one managed by the one before it.

    Returns the slots in the order given, so ``_line(state, "lead", "mid", "worker")``
    leaves ``worker`` managed by ``mid`` and ``mid`` managed by ``lead``.
    """
    slots = []
    for index, key in enumerate(keys):
        slot = state.get_or_create_slot(key)
        if index:
            slot._created_by = keys[index - 1]
        slots.append(slot)
    return slots


class TestManagerLineWalk:
    """``_read_manager_line``: who is on the line above a session."""

    def test_the_direct_manager_is_on_the_line(self, tmp_path):
        state = _make_state(tmp_path)
        _, worker = _line(state, "chat-mid", "chat-worker")
        assert sc._read_manager_line(state, sc.manager_of(worker), "chat-mid")

    def test_the_manager_two_levels_up_is_on_the_line(self, tmp_path):
        state = _make_state(tmp_path)
        _, _, worker = _line(state, "chat-lead", "chat-mid", "chat-worker")
        assert sc._read_manager_line(state, sc.manager_of(worker), "chat-lead")

    def test_a_handed_over_hop_is_followed_like_a_created_one(self, tmp_path):
        """``manager_of`` reads ``_manager`` first, so a handoff redirects the walk."""
        state = _make_state(tmp_path)
        _, mid, worker = _line(state, "chat-lead", "chat-mid", "chat-worker")
        state.get_or_create_slot("chat-other")
        mid._manager = "chat-other"
        assert sc._read_manager_line(state, sc.manager_of(worker), "chat-other")
        assert not sc._read_manager_line(state, sc.manager_of(worker), "chat-lead")

    def test_a_session_off_the_line_is_not_on_it(self, tmp_path):
        state = _make_state(tmp_path)
        _, _, worker = _line(state, "chat-lead", "chat-mid", "chat-worker")
        state.get_or_create_slot("chat-stranger")
        assert not sc._read_manager_line(state, sc.manager_of(worker), "chat-stranger")

    def test_a_report_is_not_on_its_own_managers_line(self, tmp_path):
        """The line runs one way: a worker never reaches the lead above it."""
        state = _make_state(tmp_path)
        _, mid, _ = _line(state, "chat-lead", "chat-mid", "chat-worker")
        assert not sc._read_manager_line(state, sc.manager_of(mid), "chat-worker")

    def test_an_empty_caller_key_is_on_no_line(self, tmp_path):
        state = _make_state(tmp_path)
        _, worker = _line(state, "chat-mid", "chat-worker")
        assert not sc._read_manager_line(state, sc.manager_of(worker), "")

    def test_a_gap_in_the_line_fails_closed(self, tmp_path):
        """A middle manager whose slot is gone stops the walk short of the lead."""
        state = _make_state(tmp_path)
        _line(state, "chat-lead", "chat-mid", "chat-worker")
        worker = state.get_slot("chat-worker")
        state._slots.pop("chat-mid", None)
        assert not sc._read_manager_line(state, sc.manager_of(worker), "chat-lead")

    def test_a_cycle_fails_closed_and_terminates(self, tmp_path):
        state = _make_state(tmp_path)
        first, second = _line(state, "chat-a", "chat-b")
        first._manager = "chat-b"
        second._manager = "chat-a"
        assert not sc._read_manager_line(state, sc.manager_of(first), "chat-stranger")
        assert not sc._read_manager_line(state, sc.manager_of(second), "chat-stranger")

    def test_a_line_past_the_depth_bound_fails_closed(self, tmp_path):
        state = _make_state(tmp_path)
        keys = [f"chat-{index}" for index in range(sc.LINEAGE_WALK_MAX_DEPTH + 3)]
        _line(state, *keys)
        bottom = state.get_slot(keys[-1])
        # Inside the bound the root is reachable; the root of a longer line is not.
        assert sc._read_manager_line(state, sc.manager_of(bottom), keys[2])
        assert not sc._read_manager_line(state, sc.manager_of(bottom), keys[0])


class TestOutsideManagerLine:
    """``_outside_manager_line``: the fence's own reading of the walk."""

    def test_the_direct_manager_is_inside(self, tmp_path):
        state = _make_state(tmp_path)
        _, worker = _line(state, "chat-mid", "chat-worker")
        assert not _outside(state, worker, "chat-mid")

    def test_the_skip_level_is_inside(self, tmp_path):
        state = _make_state(tmp_path)
        _, _, worker = _line(state, "chat-lead", "chat-mid", "chat-worker")
        assert not _outside(state, worker, "chat-lead")

    def test_a_stranger_is_outside(self, tmp_path):
        state = _make_state(tmp_path)
        _, worker = _line(state, "chat-mid", "chat-worker")
        state.get_or_create_slot("chat-stranger")
        assert _outside(state, worker, "chat-stranger")

    def test_a_slot_managed_by_nobody_is_outside_every_line(self, tmp_path):
        """Fail-closed at the edge ``_created_by_other`` fails closed at."""
        state = _make_state(tmp_path)
        orphan = state.get_or_create_slot("chat-orphan")
        assert _outside(state, orphan, "chat-anyone")

    def test_a_self_managing_slot_is_read_as_a_cycle(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-loop")
        slot._manager = "chat-loop"
        assert _outside(state, slot, "chat-stranger")


class TestTheFenceReadsTheLineForWidenedOperationsOnly:
    """``authorize_target`` wiring: which verbs the walk decides.

    The caller is agent-created, so it IS ownership-fenced -- the population the
    walk exists for. ``_created_by`` carries the line here, because a gateway where
    nothing has been handed over is the one a lead and conductor run on.
    """

    def _line(self, tmp_path):
        state = _make_state(tmp_path)
        lead = state.get_or_create_slot("chat-lead")
        lead._created_by = "chat-owner"  # agent-created, therefore fenced
        mid = state.get_or_create_slot("chat-mid")
        mid._created_by = "chat-lead"
        worker = state.get_or_create_slot("chat-worker")
        worker._created_by = "chat-mid"
        return state, lead, worker

    @pytest.mark.parametrize("operation", sorted(sc.HANDOFF_WIDENED_OPERATIONS))
    def test_a_widened_operation_admits_the_skip_level(self, tmp_path, operation):
        state, lead, worker = self._line(tmp_path)
        assert (
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(lead),
                target=worker.key,
                operation=operation,
            )
            is worker
        )

    @pytest.mark.parametrize("operation", ["create", "fork", "rename", "set_model", "retry"])
    def test_an_operation_the_handoff_does_not_widen_still_reads_the_creator(
        self, tmp_path, operation
    ):
        """Minting and the per-slot settings keep the pre-line answer, at every depth."""
        state, lead, worker = self._line(tmp_path)
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(lead),
                target=worker.key,
                operation=operation,
            )
        assert exc.value.code == "not_creator"

    def test_a_session_off_the_line_is_still_refused_on_a_widened_operation(self, tmp_path):
        state, _, worker = self._line(tmp_path)
        stranger = state.get_or_create_slot("chat-stranger")
        stranger._created_by = "chat-owner"
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(stranger),
                target=worker.key,
                operation="send",
            )
        assert exc.value.code == "not_creator"

    def test_a_report_still_cannot_reach_up_the_line(self, tmp_path):
        """Upward send stays refused: reports do not command their manager."""
        state, lead, worker = self._line(tmp_path)
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(worker),
                target=lead.key,
                operation="send",
            )
        assert exc.value.code == "not_creator"

    def test_an_unfenced_caller_is_unaffected(self, tmp_path):
        """A person's own tab was never fenced, so the walk decides nothing for it."""
        state, _, worker = self._line(tmp_path)
        owner = state.get_or_create_slot("chat-owner")
        assert (
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(owner),
                target=worker.key,
                operation="create",
            )
            is worker
        )

    def test_a_closed_middle_manager_leaves_the_worker_reachable(self, tmp_path):
        """The close-revert and the fence together: a finished conductor strands nobody."""
        state, lead, worker = self._line(tmp_path)
        _depart(state, "chat-mid")
        assert (
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(lead),
                target=worker.key,
                operation="send",
            )
            is worker
        )

    def test_a_closed_middle_manager_without_the_revert_fails_closed(self, tmp_path):
        """The gap the revert repairs, so the test above is measuring the repair."""
        state, lead, worker = self._line(tmp_path)
        state._slots.pop("chat-mid", None)
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(lead),
                target=worker.key,
                operation="send",
            )
        assert exc.value.code == "not_creator"


class TestAHandoffCannotCloseTheLineIntoALoop:
    """The writer-side guard the line needs, because the walk fails closed on a loop.

    A loop in the reporting line is not a half-broken state: every walk over it hits
    the cycle edge, so every session in the loop loses its manager at once. It is the
    handoff that can write one -- handing a session DOWN to one of its own reports --
    so the refusal belongs at the handoff.
    """

    def test_a_found_loop_says_so_rather_than_naming_an_unread_line(self, tmp_path):
        """The other half of the shared code: the message names the receiver's report."""
        state = _make_state(tmp_path)
        lead = state.get_or_create_slot("chat-lead")
        lead._created_by = "chat-owner"
        mid = state.get_or_create_slot("chat-mid")
        mid._created_by = "chat-lead"
        worker = state.get_or_create_slot("chat-worker")
        worker._created_by = "chat-mid"

        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(
                sc.handoff_target(
                    state,
                    caller_session_key=slot_history_key(lead),
                    target=mid.key,
                    to=worker.key,
                )
            )
        assert exc.value.code == "would_cycle"

    def test_handing_a_session_to_its_own_report_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        lead = state.get_or_create_slot("chat-lead")
        lead._created_by = "chat-owner"
        mid = state.get_or_create_slot("chat-mid")
        mid._created_by = "chat-lead"
        worker = state.get_or_create_slot("chat-worker")
        worker._created_by = "chat-mid"

        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(
                sc.handoff_target(
                    state,
                    caller_session_key=slot_history_key(lead),
                    target=mid.key,
                    to=worker.key,
                )
            )
        assert exc.value.code == "would_cycle"

    def test_handing_a_session_sideways_is_not_refused_as_a_loop(self, tmp_path):
        """The guard must not swallow the ordinary case it sits next to."""
        state = _make_state(tmp_path)
        lead = state.get_or_create_slot("chat-lead")
        lead._created_by = "chat-owner"
        mid = state.get_or_create_slot("chat-mid")
        mid._created_by = "chat-lead"
        peer = state.get_or_create_slot("chat-peer")
        peer._created_by = "chat-lead"

        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(
                sc.handoff_target(
                    state,
                    caller_session_key=slot_history_key(lead),
                    target=mid.key,
                    to=peer.key,
                )
            )
        # Past the loop guard, refused further in because this gateway records no
        # session tree for the edge to move in.
        assert exc.value.code != "would_cycle"


class TestWhatAGiverKeepsAfterAHandoff:
    """The other half of ``test_session_handoff``'s "the reach moves" pin.

    A giver that still manages the RECEIVER keeps reaching what it handed over, by the
    line and not by the creation record -- it drives the receiver, so it can have the
    session driven either way. A giver that handed the session off its own line keeps
    nothing, which is the case pinned in that file.
    """

    def _handed(self, tmp_path):
        state = _make_state(tmp_path)
        owner = state.get_or_create_slot("chat-owner")
        lead = state.get_or_create_slot("chat-lead")
        lead._created_by = owner.key
        conductor = state.get_or_create_slot("chat-conductor")
        conductor._created_by = lead.key
        worker = state.get_or_create_slot("chat-worker")
        worker._created_by = lead.key
        worker._manager = conductor.key
        return state, lead, conductor, worker

    def test_a_lead_still_reaches_a_worker_it_handed_to_its_own_conductor(self, tmp_path):
        state, lead, _conductor, worker = self._handed(tmp_path)
        assert (
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(lead),
                target=worker.key,
                operation="send",
            )
            is worker
        )

    def test_the_reach_is_the_line_and_not_the_creation_record(self, tmp_path):
        """Drop the lead off the conductor's line and the same lead is refused."""
        state, lead, conductor, worker = self._handed(tmp_path)
        conductor._manager = "chat-owner"
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(lead),
                target=worker.key,
                operation="send",
            )
        assert exc.value.code == "not_creator"
        assert worker._created_by == lead.key

    def test_the_conductor_loses_the_worker_when_the_lead_hands_it_on(self, tmp_path):
        """A sideways handoff out of the line takes the middle manager's reach with it."""
        state, _lead, conductor, worker = self._handed(tmp_path)
        peer = state.get_or_create_slot("chat-peer")
        peer._created_by = "chat-owner"
        worker._manager = peer.key
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(conductor),
                target=worker.key,
                operation="send",
            )
        assert exc.value.code == "not_creator"


class TestWhatAnUnreadableLineCountsAs:
    """``unread_is_present``: one walk, two polarities, because two callers differ.

    A line the walk cannot finish must answer "no" to both questions asked of it, and
    those are opposite booleans. The fence asks "may this caller act" and needs False.
    The loop guard asks "is it safe to write this edge" and needs True. The flag is
    which way the walk's own ignorance points.
    """

    def test_an_exhausted_line_is_read_to_the_end(self, tmp_path):
        """Reaching an empty hop ends the line, so both polarities agree."""
        state = _make_state(tmp_path)
        _, worker = _line(state, "chat-mid", "chat-worker")
        start = sc.manager_of(worker)
        assert not sc._read_manager_line(state, start, "chat-stranger")
        assert not sc._read_manager_line(state, start, "chat-stranger", unread_is_present=True)

    def test_a_found_key_is_found_under_either_polarity(self, tmp_path):
        state = _make_state(tmp_path)
        _, _, worker = _line(state, "chat-lead", "chat-mid", "chat-worker")
        start = sc.manager_of(worker)
        assert sc._read_manager_line(state, start, "chat-lead")
        assert sc._read_manager_line(state, start, "chat-lead", unread_is_present=True)

    def test_an_empty_wanted_key_is_on_no_line(self, tmp_path):
        state = _make_state(tmp_path)
        _, worker = _line(state, "chat-mid", "chat-worker")
        assert not sc._read_manager_line(state, sc.manager_of(worker), "", unread_is_present=True)

    def test_a_gap_counts_as_read_because_that_line_is_already_broken(self, tmp_path):
        """Deliberate: a person closing their own tab leaves this gap in every tree.

        The gap is a key with NOTHING on record -- no occupancy, so no entry to read
        under one. That is what a key from before a restart looks like, and what a
        handoff below it must not be refused for.
        """
        state = _make_state(tmp_path)
        _line(state, "chat-lead", "chat-mid", "chat-worker")
        worker = state.get_slot("chat-worker")
        state._slots.pop("chat-mid", None)
        _forget_everything()
        start = sc.manager_of(worker)
        assert not sc._read_manager_line(state, start, "chat-lead")
        assert not sc._read_manager_line(state, start, "chat-lead", unread_is_present=True)

    def test_a_pending_vacancy_is_unread_rather_than_a_line_that_ended(self, tmp_path):
        """The gap's near neighbour, and the OPPOSITE answer for a writer.

        A key popped by a close in flight has an occupancy whose manager is not
        published yet. Readers are denied either way; the writer must refuse HERE and
        may proceed over a gap, and that difference is what keeps a handoff from
        closing a cycle under a manager whose own edge is about to land.
        """
        state = _make_state(tmp_path)
        _line(state, "chat-lead", "chat-mid", "chat-worker")
        worker = state.get_slot("chat-worker")
        mid = state.get_slot("chat-mid")
        state._slots.pop("chat-mid", None)
        sc.note_key_vacated("chat-mid", mid)  # the pop's own record

        assert _pending("chat-mid")
        start = sc.manager_of(worker)
        assert not sc._read_manager_line(state, start, "chat-lead")
        assert sc._read_manager_line(state, start, "chat-lead", unread_is_present=True)

    def test_a_closed_owner_tab_does_not_freeze_a_whole_tree(self, tmp_path):
        """The regression the gap decision avoids: handoffs under a closed tab still work."""
        state = _make_state(tmp_path)
        owner = state.get_or_create_slot("chat-owner")
        lead, mid, peer = _line(state, "chat-lead", "chat-mid", "chat-peer")
        lead._created_by = owner.key
        peer._created_by = lead.key
        _depart(state, "chat-owner")
        assert sc.manager_of(lead) == "chat-owner"  # the dangling hop
        assert not sc._read_manager_line(state, peer.key, mid.key, unread_is_present=True)

    def test_a_cycle_splits_the_two_polarities(self, tmp_path):
        state = _make_state(tmp_path)
        first, second = _line(state, "chat-a", "chat-b")
        first._manager = "chat-b"
        second._manager = "chat-a"
        start = sc.manager_of(first)
        assert not sc._read_manager_line(state, start, "chat-stranger")
        assert sc._read_manager_line(state, start, "chat-stranger", unread_is_present=True)

    def test_running_out_of_depth_splits_the_two_polarities(self, tmp_path):
        state = _make_state(tmp_path)
        keys = [f"chat-{index}" for index in range(sc.LINEAGE_WALK_MAX_DEPTH + 3)]
        _line(state, *keys)
        start = sc.manager_of(state.get_slot(keys[-1]))
        assert not sc._read_manager_line(state, start, keys[0])
        assert sc._read_manager_line(state, start, keys[0], unread_is_present=True)

    def test_the_fence_refuses_on_an_unread_line(self, tmp_path):
        """The reader's polarity through the fence: unread is outside, which fails closed."""
        state = _make_state(tmp_path)
        keys = [f"chat-{index}" for index in range(sc.LINEAGE_WALK_MAX_DEPTH + 3)]
        _line(state, *keys)
        assert _outside(state, state.get_slot(keys[-1]), keys[0])


class TestAHandoffRefusesALineItCouldNotRead:
    """The writer's polarity, reached through the verb.

    A line deeper than the bound reads as "the target is not above the receiver" to the
    fence, which is the same answer a genuinely unrelated receiver gives. Acting on that
    writes the very loop the guard exists to refuse, and the loop then fails closed for
    every session in it -- so the writer asks the same walk with the opposite polarity.

    One code, ``would_cycle``, for a found loop and an unread line alike, since what a
    caller does about either is name a different receiver.
    """

    def test_a_line_past_the_depth_bound_refuses_the_handoff(self, tmp_path):
        state = _make_state(tmp_path)
        owner = state.get_or_create_slot("chat-owner")
        keys = [f"chat-{index}" for index in range(sc.LINEAGE_WALK_MAX_DEPTH + 3)]
        _line(state, *keys)
        state.get_slot(keys[0])._created_by = owner.key
        top, bottom = state.get_slot(keys[0]), state.get_slot(keys[-1])

        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(
                sc.handoff_target(
                    state,
                    caller_session_key=slot_history_key(owner),
                    target=top.key,
                    to=bottom.key,
                )
            )
        assert exc.value.code == "would_cycle"

    def test_a_cycle_above_the_receiver_refuses_the_handoff(self, tmp_path):
        state = _make_state(tmp_path)
        owner = state.get_or_create_slot("chat-owner")
        lead, _mid, worker = _line(state, "chat-lead", "chat-mid", "chat-worker")
        lead._created_by = owner.key
        first, second = _line(state, "chat-a", "chat-b")
        first._manager = "chat-b"
        second._manager = "chat-a"

        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(
                sc.handoff_target(
                    state,
                    caller_session_key=slot_history_key(owner),
                    target=worker.key,
                    to=second.key,
                )
            )
        assert exc.value.code == "would_cycle"
        assert worker._manager == ""

    def test_a_gap_above_the_receiver_does_not_refuse_the_handoff(self, tmp_path):
        """The other side of the gap decision, at the writer this time."""
        state = _make_state(tmp_path)
        owner = state.get_or_create_slot("chat-owner")
        lead, _mid, worker = _line(state, "chat-lead", "chat-mid", "chat-worker")
        lead._created_by = owner.key
        orphan = state.get_or_create_slot("chat-orphan")
        orphan._manager = "chat-gone"

        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(
                sc.handoff_target(
                    state,
                    caller_session_key=slot_history_key(owner),
                    target=worker.key,
                    to=orphan.key,
                )
            )
        assert exc.value.code != "would_cycle"


class TestTheLineOffIsTheFenceWithoutIt:
    """With ``agent.session_manager_chain`` off, this feature answers nothing of its own.

    The shipped default, and the reason the default ships no behaviour change: the fence
    admits the DIRECT manager and nobody else, a departure hands nothing up, and a
    handoff is refused only for the reasons it was refused before. So these are the
    equivalence tests -- each one states what the gateway answers WITHOUT the feature,
    and the switch being off has to produce that same answer.
    """

    @pytest.fixture(autouse=True)
    def _line_off(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(sc, "manager_chain_enabled", lambda: False)

    def _team(self, tmp_path):
        state = _make_state(tmp_path)
        lead = state.get_or_create_slot("chat-lead")
        lead._created_by = "chat-owner"
        mid = state.get_or_create_slot("chat-mid")
        mid._created_by = "chat-lead"
        worker = state.get_or_create_slot("chat-worker")
        worker._created_by = "chat-mid"
        return state, lead, mid, worker

    def test_the_direct_manager_is_still_admitted(self, tmp_path):
        """The reach the fence grants on its own has to survive the switch being off."""
        state, _lead, mid, worker = self._team(tmp_path)
        assert (
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(mid),
                target=worker.key,
                operation="send",
            )
            is worker
        )

    @pytest.mark.parametrize("operation", PR1_WIDENED)
    def test_pr1s_own_team_and_verb_list_answer_what_pr1_expects(self, tmp_path, operation):
        """Driven from the BASE PR's own fixture and verb tuple, not a restatement here.

        ``_team`` and ``WIDENED`` are imported from the base PR's test module, so the
        equivalence is measured against that PR's own idea of the tree and of which
        verbs a handoff widens. A verb added to that tuple, or a change to how that
        fixture builds the tree, fails here rather than silently going unchecked.

        The base PR's expectation for this shape is ``not_creator`` on every widened
        verb -- the conductor created none of the workers -- and with the line off it
        has to stay exactly that.
        """
        state = _make_state(tmp_path)
        _lead, conductor, worker = pr1_team(state)
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(conductor),
                target=worker.key,
                operation=operation,
            )
        assert exc.value.code == "not_creator"

    def test_the_verb_list_this_file_gates_matches_the_base_prs(self):
        """A verb widened on one side and not the other would make the pin vacuous."""
        assert set(PR1_WIDENED) == set(sc.HANDOFF_WIDENED_OPERATIONS)

    @pytest.mark.parametrize("operation", sorted(sc.HANDOFF_WIDENED_OPERATIONS))
    def test_the_skip_level_is_refused_on_every_widened_operation(self, tmp_path, operation):
        """What the fence answered before the line existed: creator or holder, one level."""
        state, lead, _mid, worker = self._team(tmp_path)
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(lead),
                target=worker.key,
                operation=operation,
            )
        assert exc.value.code == "not_creator"

    def test_the_fence_matches_the_direct_manager_predicate_exactly(self, tmp_path):
        """Equivalence through the REAL fence, over every caller/target pair in the tree.

        Asserted against ``_managed_by_other`` -- the predicate PR1 ships -- rather than
        against a restatement of what it does, so this cannot agree with a wrong idea of
        the old behaviour. Driven through ``authorize_target`` on a widened operation, so
        what is measured is the path a verb actually takes.
        """
        state, lead, mid, worker = self._team(tmp_path)
        for target in (lead, mid, worker):
            for caller in (lead, mid, worker):
                if caller.key == target.key:
                    continue  # self-target is refused by its own rule, before the fence
                refused = None
                try:
                    sc.authorize_target(
                        state,
                        caller_session_key=slot_history_key(caller),
                        target=target.key,
                        operation="send",
                    )
                except sc.SessionControlError as exc:
                    refused = exc.code
                assert (refused == "not_creator") == sc._managed_by_other(target, caller.key), (
                    target.key,
                    caller.key,
                    refused,
                )

    def test_a_handed_over_session_still_reaches_its_new_manager(self, tmp_path):
        """A handoff keeps working with the line off -- that reach is PR1's, not this PR's."""
        state, _lead, mid, worker = self._team(tmp_path)
        worker._manager = mid.key
        assert (
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(mid),
                target=worker.key,
                operation="send",
            )
            is worker
        )

    def test_a_departure_hands_nothing_up(self, tmp_path):
        state, _lead, mid, worker = self._team(tmp_path)
        state._slots.pop(mid.key, None)

        assert worker._manager == ""
        assert worker._created_by == "chat-mid"
        assert not worker._dirty

    def test_a_close_hands_nothing_up(self, tmp_path, monkeypatch):
        """Through the close path, so the switch is read where the repair is called."""
        state, _lead, mid, worker = self._team(tmp_path)

        async def _archived(state_, slot_, name_, *, pre_pop_check=None):
            state_._slots.pop(name_, None)

        monkeypatch.setattr(chat_handlers, "_close_slot", _archived)
        asyncio.run(chat_handlers.close_slot(state, mid, mid.key))

        assert worker._manager == ""

    def test_handing_a_session_to_its_own_report_is_not_refused_as_a_loop(self, tmp_path):
        """The loop guard is gated with the reach it protects.

        With nothing walking the line a loop breaks nothing, and refusing anyway would
        reject a handoff the gateway accepts today.
        """
        state, lead, mid, worker = self._team(tmp_path)
        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(
                sc.handoff_target(
                    state,
                    caller_session_key=slot_history_key(lead),
                    target=mid.key,
                    to=worker.key,
                )
            )
        assert exc.value.code != "would_cycle"


class TestTheWalkResolvesADepartedHopAtReadTime:
    """A departed manager is read THROUGH, and nothing rewrote a field when it left.

    This is the whole of what a departure does now: it records its own manager, once,
    synchronously, with the slot in hand. No session's own manager is rewritten, nothing
    is saved, and no loop visits other slots -- so there is no second writer to exclude,
    no report whose manager could change between a check and a write, and no promoted
    manager whose identity could go stale across a save.

    Held in memory rather than read back from the departed session's persisted metadata.
    That read is disk I/O and the walk is called from the ownership fence, which runs
    inside the close's no-suspension window; a metadata read there is the blocking-call
    class the fence is built to avoid.
    """

    def test_the_top_manager_still_reaches_the_worker_through_a_closed_middle(self, tmp_path):
        """The case the feature exists for, with no field rewritten anywhere."""
        state = _make_state(tmp_path)
        lead, mid, worker = _line(state, "chat-lead", "chat-mid", "chat-worker")
        lead._created_by = "chat-owner"

        _depart(state, "chat-mid")

        # Nothing was written to the worker: its line is read through the departure.
        assert worker._manager == ""
        assert worker._created_by == "chat-mid"
        assert not _outside(state, worker, lead.key)
        assert (
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(lead),
                target=worker.key,
                operation="send",
            )
            is worker
        )

    def test_two_departed_hops_in_a_row_are_both_read_through(self, tmp_path):
        state = _make_state(tmp_path)
        top, upper, mid, worker = _line(state, "chat-top", "chat-upper", "chat-mid", "chat-worker")
        top._created_by = "chat-owner"

        _depart(state, "chat-mid")
        _depart(state, "chat-upper")

        assert not _outside(state, worker, top.key)

    def test_an_unreadable_departure_refuses_rather_than_skipping(self, tmp_path):
        """No record for the departed hop: the walk STOPS, it does not step over it."""
        state = _make_state(tmp_path)
        lead, _mid, worker = _line(state, "chat-lead", "chat-mid", "chat-worker")
        lead._created_by = "chat-owner"

        _depart(state, "chat-mid")
        _forget_everything()  # the record is gone, as an eviction leaves it

        assert _outside(state, worker, lead.key)
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(lead),
                target=worker.key,
                operation="send",
            )
        assert exc.value.code == "not_creator"

    def test_a_departure_with_nobody_above_records_nobody(self, tmp_path):
        """A person's own unattributed tab has no hop to resolve to.

        An entry is still written, naming nobody, rather than left absent, because an
        ABSENT entry means something else: an occupant still inside its own close.
        """
        state = _make_state(tmp_path)
        owner = state.get_or_create_slot("chat-owner")
        assert sc.note_manager_departed(state, owner.key, owner) == ""
        assert _recorded(owner.key) == ""
        assert not sc._read_manager_line(state, owner.key, "chat-anyone")

    def test_the_record_is_bounded_and_evicts_oldest_first(self, tmp_path):
        state = _make_state(tmp_path)
        for index in range(sc._KEY_OCCUPANCY_MAX + 5):
            slot = state.get_or_create_slot(f"chat-gone-{index}")
            slot._created_by = "chat-lead"
            _depart(state, slot.key)
        assert len(sc._departed_managers) == sc._KEY_OCCUPANCY_MAX
        assert _recorded("chat-gone-0") is None
        assert _recorded(f"chat-gone-{sc._KEY_OCCUPANCY_MAX + 4}") == "chat-lead"

    def test_the_switch_off_reads_through_nothing(self, tmp_path, _floor_monkeypatch):
        """Off, the fence never walks, so a departed hop changes no answer at all."""
        _floor_monkeypatch.setattr(sc, "manager_chain_enabled", lambda: False)
        state = _make_state(tmp_path)
        lead, _mid, worker = _line(state, "chat-lead", "chat-mid", "chat-worker")
        lead._created_by = "chat-owner"

        _depart(state, "chat-mid")

        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(lead),
                target=worker.key,
                operation="send",
            )
        assert exc.value.code == "not_creator"


class TestWhatTheDepartureRecordPromisesAndWhatItDoesNot:
    """The three conditions the in-memory record is accepted under.

    Each is a property of the record itself rather than of one scenario, so each gets
    its own test: it does not survive a restart and says so by refusing, it cannot grow
    without limit, and it can never name a manager the disk does not agree with.
    """

    def test_a_restarted_gateway_refuses_across_a_closed_hop(self, tmp_path):
        """Condition 1. A fresh process has no record, so the hop is unresolvable.

        Refused, not stepped over: nothing above the closed hop is admitted. The reach
        this costs is skip-level reach across a manager that departed BEFORE the
        restart, which is the reach a gateway has without this feature at all -- so the
        loss denies, and grants nobody anything.
        """
        state = _make_state(tmp_path)
        lead, mid, worker = _line(state, "chat-lead", "chat-mid", "chat-worker")
        lead._created_by = "chat-owner"
        _depart(state, "chat-mid")
        assert not _outside(state, worker, lead.key)  # reachable before the restart

        # What a restart leaves: the slots rehydrate, the in-memory record does not.
        _forget_everything()

        assert _outside(state, worker, lead.key)
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(lead),
                target=worker.key,
                operation="send",
            )
        assert exc.value.code == "not_creator"

    def test_a_restart_does_not_widen_anything_either(self, tmp_path):
        """The other half of condition 1: losing the record never ADMITS a caller."""
        state = _make_state(tmp_path)
        lead, mid, worker = _line(state, "chat-lead", "chat-mid", "chat-worker")
        lead._created_by = "chat-owner"
        stranger = state.get_or_create_slot("chat-stranger")
        stranger._created_by = "chat-owner"
        _depart(state, "chat-mid")
        _forget_everything()

        for caller in (lead, stranger):
            assert _outside(state, worker, caller.key), caller.key

    def test_the_record_cannot_grow_without_limit(self, tmp_path):
        """Condition 2, by count: oldest departures are dropped first."""
        state = _make_state(tmp_path)
        for index in range(sc._KEY_OCCUPANCY_MAX + 20):
            slot = state.get_or_create_slot(f"chat-left-{index}")
            slot._created_by = "chat-lead"
            _depart(state, slot.key)

        assert len(sc._departed_managers) == sc._KEY_OCCUPANCY_MAX
        assert _recorded("chat-left-0") is None
        assert _recorded(f"chat-left-{sc._KEY_OCCUPANCY_MAX + 19}") == "chat-lead"

    def test_the_size_bound_is_not_the_walk_depth_bound(self):
        """Two different bounds, two numbers, so a change to one cannot silently cap the
        other: the walk limits how far ONE line is followed, this limits how many lines
        can have a departed hop at once."""
        assert sc._KEY_OCCUPANCY_MAX != sc.LINEAGE_WALK_MAX_DEPTH

    def test_a_dropping_rule_based_on_live_managers_would_break_a_chained_departure(self, tmp_path):
        """Why condition 2 is bounded by COUNT and not by liveness.

        With a line's middle AND upper hops both departed, no LIVE session's manager
        names the upper one -- only the departed middle one did. A rule that dropped
        entries nothing live names would delete exactly the hop the walk still needs,
        and the top manager would lose reach to the bottom session. This states that,
        so the tighter-looking rule is not introduced later as a simplification.
        """
        state = _make_state(tmp_path)
        top, _upper, _mid, worker = _line(
            state, "chat-top", "chat-upper", "chat-mid", "chat-worker"
        )
        top._created_by = "chat-owner"
        _depart(state, "chat-mid")
        _depart(state, "chat-upper")
        assert not _outside(state, worker, top.key)

        named_by_a_live_slot = {sc.manager_of(slot) for slot in state._slots.values()}
        assert "chat-upper" not in named_by_a_live_slot
        del sc._departed_managers[("chat-upper", sc._key_occupancy["chat-upper"])]

        assert _outside(state, worker, top.key)

    def test_the_recorded_value_is_the_one_the_disk_carries(self, tmp_path):
        """Condition 3. The record reads the PERSISTED manager and nothing else.

        ``manager_of`` reads ``_manager`` -- the field the metadata line carries -- and
        falls back to ``_created_by``. There is no second, in-flight manager field on a
        slot for it to pick up, so the record cannot name a manager the disk disagrees
        with. The assertion is on the SLOT's own shape rather than on a scenario,
        because that is what makes the guarantee structural: reintroduce a pending
        manager field and this fails, which forces whoever does it to confirm that the
        record still reads the committed one.
        """
        state = _make_state(tmp_path)
        lead, mid, worker = _line(state, "chat-lead", "chat-mid", "chat-worker")
        lead._created_by = "chat-owner"

        pending_fields = [
            name for name in type(mid).__slots__ if "manager" in name and name not in ("_manager",)
        ]
        assert pending_fields == [], pending_fields

        mid._manager = "chat-lead"  # the committed field, as a handoff leaves it
        _depart(state, "chat-mid")

        assert _recorded("chat-mid") == "chat-lead"
        assert not _outside(state, worker, lead.key)

    def test_the_committed_field_wins_over_the_creator(self, tmp_path):
        """A handed-over session records its MANAGER, not whoever opened it."""
        state = _make_state(tmp_path)
        giver = state.get_or_create_slot("chat-giver")
        holder = state.get_or_create_slot("chat-holder")
        for slot in (giver, holder):
            slot._created_by = "chat-owner"
        mid = state.get_or_create_slot("chat-mid")
        mid._created_by = giver.key
        mid._manager = holder.key  # handed over before it departed
        worker = state.get_or_create_slot("chat-worker")
        worker._created_by = "chat-mid"

        _depart(state, "chat-mid")

        assert _recorded("chat-mid") == "chat-holder"
        assert not _outside(state, worker, holder.key)
        assert _outside(state, worker, giver.key)

    def test_the_record_is_written_only_at_departure(self, tmp_path):
        """Condition 3's other half: nothing else adds to it.

        A handoff, a close that does not remove the slot, and a plain reach check all
        leave it alone -- it is written by the departure paths and by nothing else.
        """
        state = _make_state(tmp_path)
        lead, mid, worker = _line(state, "chat-lead", "chat-mid", "chat-worker")
        lead._created_by = "chat-owner"

        _outside(state, worker, lead.key)
        worker._manager = "chat-lead"
        sc.manager_of(worker)

        assert not sc._departed_managers


class TestAReusedSlotNameDoesNotInheritAnOlderLine:
    """A slot name is reusable, so the record must be about whoever held it LAST.

    Opening a tab under a key a departed session used is ordinary -- the create route
    lets the caller choose the name -- so the record for that key can still name the
    previous occupant's manager. If a later departure under the same key returns without
    clearing it, the walk resolves the NEW departure through the OLD line, and the
    earlier session's manager gains authority over sessions created under the
    replacement that it never had while the replacement was live.
    """

    def test_an_unmanaged_departure_clears_the_previous_occupants_record(self, tmp_path):
        state = _make_state(tmp_path)
        lead = state.get_or_create_slot("chat-lead")
        lead._created_by = "chat-owner"
        first = state.get_or_create_slot("chat-conductor")
        first._created_by = lead.key

        # The lead's conductor departs: the record names the lead.
        _depart(state, "chat-conductor")
        assert _recorded("chat-conductor") == "chat-lead"

        # A person opens a tab under that same key. Unattributed: nobody manages it.
        replacement = state.get_or_create_slot("chat-conductor")
        assert sc.manager_of(replacement) == ""
        worker = state.get_or_create_slot("chat-worker")
        worker._created_by = "chat-conductor"

        # While the replacement is LIVE the lead is already refused: the walk reads the
        # live slot, which nobody manages.
        assert _outside(state, worker, lead.key)

        # And it must STAY refused once the replacement departs.
        _depart(state, "chat-conductor")

        assert _recorded("chat-conductor") == ""
        assert _outside(state, worker, lead.key)
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(lead),
                target=worker.key,
                operation="send",
            )
        assert exc.value.code == "not_creator"

    def test_a_self_managed_departure_also_clears_it(self, tmp_path):
        """The other early return out of the record, same reasoning."""
        state = _make_state(tmp_path)
        lead = state.get_or_create_slot("chat-lead")
        lead._created_by = "chat-owner"
        first = state.get_or_create_slot("chat-conductor")
        first._created_by = lead.key
        _depart(state, "chat-conductor")

        replacement = state.get_or_create_slot("chat-conductor")
        replacement._manager = "chat-conductor"  # names itself, so there is no hop above
        _depart(state, "chat-conductor")

        assert _recorded("chat-conductor") == ""

    def test_a_managed_replacement_records_its_own_manager_not_the_older_one(self, tmp_path):
        """The ordinary case the clearing must not break: the newer line wins."""
        state = _make_state(tmp_path)
        old_lead = state.get_or_create_slot("chat-old-lead")
        new_lead = state.get_or_create_slot("chat-new-lead")
        for slot in (old_lead, new_lead):
            slot._created_by = "chat-owner"
        first = state.get_or_create_slot("chat-conductor")
        first._created_by = old_lead.key
        _depart(state, "chat-conductor")

        replacement = state.get_or_create_slot("chat-conductor")
        replacement._created_by = new_lead.key
        worker = state.get_or_create_slot("chat-worker")
        worker._created_by = "chat-conductor"
        _depart(state, "chat-conductor")

        assert _recorded("chat-conductor") == "chat-new-lead"
        assert not _outside(state, worker, new_lead.key)
        assert _outside(state, worker, old_lead.key)


#: Deadline for every barrier the close-window tests wait on. Each await is bounded, so
#: a close that fails before signalling fails the test that is waiting, naming the
#: barrier, instead of hanging until the runner kills the worker it runs on. Generous
#: because nothing here does real work while it waits: a passing run never reaches it.
_BARRIER_TIMEOUT = 10.0


class TestALateCloseCannotSpeakForAReusedKey:
    """A close is not instantaneous, and the record has to survive that.

    Every departure path takes the slot out of the live table FIRST and then keeps
    awaiting -- a task cancel with its own timeout, then a save -- so the key is free
    for a window before the departure is recorded. A replacement minted in that window
    can open sessions of its own and depart in turn, and the original close then
    arrives LAST. Without a fence it overwrites the replacement's record, and the walk
    resolves the replacement's reports through the ORIGINAL session's manager: a
    manager with reach over sessions it never managed, which nothing takes back before
    the entry is evicted by count.

    The fence is the occupant's GENERATION, carried on the entry, so an older
    occupant's close is refused rather than believed.
    """

    @staticmethod
    def _lead_and_conductor(state):
        """A fenced lead, and the conductor it opened, ready to depart."""
        lead = state.get_or_create_slot("chat-lead")
        lead._created_by = "chat-owner"
        conductor = state.get_or_create_slot("chat-conductor")
        conductor._created_by = lead.key
        return lead, conductor

    @staticmethod
    def _held_at_the_save(monkeypatch):
        """Hold the REAL close open at the archival save that follows its pop.

        Only the awaited save is swapped, so the pop, the vacancy recorded at it and
        the departure recorded on the committed side are all the production ones. A
        stub standing in for the whole close would pop without either, and would then
        pass whatever the fix was.

        Returns the two events: one the close sets on arrival, one that releases it.
        Both waits on them are bounded by the caller.
        """
        reached = asyncio.Event()
        release = asyncio.Event()

        async def _held(_state, _slot, **_kwargs):
            reached.set()
            await asyncio.wait_for(release.wait(), timeout=_BARRIER_TIMEOUT)
            return True

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _held)
        return reached, release

    @staticmethod
    async def _in_the_window(state, slot, reached, release, during):
        """Run the real close, call *during* while it sits in its post-pop window.

        Every await is bounded, and the close task is cancelled and collected in
        cleanup, so a close that raises before arriving fails this test at the barrier
        rather than leaving a task for the next one.
        """
        closing = asyncio.ensure_future(chat_handlers.close_slot(state, slot, slot.key))
        try:
            await asyncio.wait_for(reached.wait(), timeout=_BARRIER_TIMEOUT)
            assert state.get_slot(slot.key) is None, "the close has not popped the key yet"
            during()
            release.set()
            await asyncio.wait_for(closing, timeout=_BARRIER_TIMEOUT)
        finally:
            if not closing.done():
                closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)

    def test_a_close_finishing_after_a_replacement_records_nothing(self, tmp_path, monkeypatch):
        """The finding, through the real close path with the window held open.

        The close pops the slot and then suspends, exactly where the task cancel and
        the save sit. A tab opens under the freed key, opens a worker and closes. Only
        then does the original close return and reach its record.
        """
        state = _make_state(tmp_path)
        lead, original = self._lead_and_conductor(state)
        reached, release = self._held_at_the_save(monkeypatch)
        opened: dict[str, object] = {}

        def _reuse_the_key():
            # A person reopens a tab under the freed key. Unattributed: nobody
            # manages it, and the lead has nothing to do with it.
            state.get_or_create_slot("chat-conductor")
            worker = state.get_or_create_slot("chat-worker")
            worker._created_by = "chat-conductor"
            _depart(state, "chat-conductor")
            opened["worker"] = worker

        asyncio.run(self._in_the_window(state, original, reached, release, _reuse_the_key))
        worker = opened["worker"]

        # The replacement's record stands: nobody above it, so the hop is unresolvable.
        assert _recorded("chat-conductor") == ""
        assert _outside(state, worker, lead.key)
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(lead),
                target=worker.key,
                operation="send",
            )
        assert exc.value.code == "not_creator"

    def test_the_previous_occupants_manager_is_refused_while_the_key_closes(
        self, tmp_path, monkeypatch
    ):
        """The other half of the window: the key is out of the table and the entry on
        it is still the PREVIOUS occupant's, because this one's manager is published
        only once its close commits.

        Reuse the key under a different lead, open a worker, and close it. Across the
        pop-to-commit window the first lead must not reach that worker -- it never
        managed the session that opened it.
        """
        state = _make_state(tmp_path)
        first_lead, first = self._lead_and_conductor(state)
        second_lead = state.get_or_create_slot("chat-second-lead")
        second_lead._created_by = "chat-owner"

        _depart(state, "chat-conductor")
        assert _recorded("chat-conductor") == "chat-lead"

        replacement = state.get_or_create_slot("chat-conductor")
        replacement._created_by = second_lead.key
        worker = state.get_or_create_slot("chat-worker")
        worker._created_by = "chat-conductor"

        reached, release = self._held_at_the_save(monkeypatch)
        reached_in_the_window: dict[str, bool] = {}

        def _ask_who_reaches_the_worker():
            for lead in (first_lead, second_lead):
                reached_in_the_window[lead.key] = not _outside(state, worker, lead.key)

        asyncio.run(
            self._in_the_window(state, replacement, reached, release, _ask_who_reaches_the_worker)
        )

        # In the window the hop resolves to nobody, so BOTH are outside. What that
        # costs is the closing session's own line, for the length of its close.
        assert reached_in_the_window == {"chat-lead": False, "chat-second-lead": False}
        # Once the close commits, the line the key actually had is readable again.
        assert _recorded("chat-conductor") == "chat-second-lead"
        assert not _outside(state, worker, second_lead.key)
        assert _outside(state, worker, first_lead.key)
        assert first._created_by == "chat-lead"  # nothing was written to the first slot

    def test_a_late_close_does_not_overwrite_a_managed_replacements_record(self, tmp_path):
        """The same window, with the replacement handed to someone else.

        The replacement's own manager keeps the key, and the original's manager reaches
        nothing -- the fence refuses the write rather than picking between the two.
        """
        state = _make_state(tmp_path)
        old_lead, original = self._lead_and_conductor(state)
        new_lead = state.get_or_create_slot("chat-new-lead")
        new_lead._created_by = "chat-owner"

        state._slots.pop("chat-conductor", None)  # the pop the close does first

        replacement = state.get_or_create_slot("chat-conductor")
        replacement._created_by = new_lead.key
        worker = state.get_or_create_slot("chat-worker")
        worker._created_by = "chat-conductor"
        _depart(state, "chat-conductor")

        # Only NOW does the original close reach its record.
        assert sc.note_manager_departed(state, "chat-conductor", original) == ""

        assert _recorded("chat-conductor") == "chat-new-lead"
        assert not _outside(state, worker, new_lead.key)
        assert _outside(state, worker, old_lead.key)

    def test_the_generation_decides_and_not_the_arrival_order(self, tmp_path):
        """Reversing the arrival order changes nothing, which is the whole point.

        In order, the replacement's record is the answer because it is written last.
        Out of order it is still the answer, because the answer is whichever entry
        belongs to the occupancy the key HAS -- read off the key, not off the sequence
        the two closes happened to finish in.
        """
        state = _make_state(tmp_path)
        _lead, original = self._lead_and_conductor(state)
        state._slots.pop("chat-conductor", None)
        replacement = state.get_or_create_slot("chat-conductor")
        replacement._created_by = "chat-new-lead"
        state._slots.pop("chat-conductor", None)

        for order in (
            (original, replacement),  # in order: the replacement records last
            (replacement, original),  # out of order: the replacement records first
        ):
            sc._departed_managers.clear()
            for slot in order:
                sc.note_manager_departed(state, "chat-conductor", slot)
            assert _recorded("chat-conductor") == "chat-new-lead"

    def test_the_same_occupant_departing_twice_still_records(self, tmp_path):
        """An equal generation is not an older one: a path that records twice is fine."""
        state = _make_state(tmp_path)
        _lead, conductor = self._lead_and_conductor(state)
        state._slots.pop("chat-conductor", None)

        assert sc.note_manager_departed(state, "chat-conductor", conductor) == "chat-lead"
        assert sc.note_manager_departed(state, "chat-conductor", conductor) == "chat-lead"
        assert _recorded("chat-conductor") == "chat-lead"

    def test_each_slot_object_is_a_later_occupant_than_the_one_before(self, tmp_path):
        """What the fence rests on: construction order, read off the slot.

        Two slot objects share a key only when the first has left the live table or is
        on its way out, because ``get_or_create_slot`` hands back the sitting occupant
        rather than building a second one. So construction order IS occupancy order,
        and a later object always carries the higher number.
        """
        state = _make_state(tmp_path)
        first = state.get_or_create_slot("chat-conductor")
        state._slots.pop("chat-conductor", None)
        second = state.get_or_create_slot("chat-conductor")

        assert sc._occupancy_generation_of(second) > sc._occupancy_generation_of(first)
        # The sitting occupant is handed back, never rebuilt, so its number is stable.
        assert state.get_or_create_slot("chat-conductor") is second

    def test_an_object_with_no_generation_records_as_it_always_did(self, tmp_path):
        """The fence is never PROVEN against an object that cannot say which occupant
        it is, so such a departure records normally rather than being dropped.

        Every real slot carries the field from its constructor, so this is the
        direction for test doubles and any future slot-like object, not for a slot.
        """
        state = _make_state(tmp_path)
        double = SimpleNamespace(key="chat-conductor", _manager="", _created_by="chat-lead")

        assert sc._occupancy_generation_of(double) == 0
        assert sc.note_manager_departed(state, "chat-conductor", double) == "chat-lead"
        assert _recorded("chat-conductor") == "chat-lead"

    def test_the_generation_is_never_persisted(self, tmp_path):
        """It compares two objects alive in ONE process, so the disk must not carry it.

        A restart begins the counter again at 1 while the departure record starts
        empty, so nothing is compared across processes. Persisting the number would put
        an old process's counter beside a new one's and make the fence answer nonsense.
        """
        from kiro_crew.dashboard.slot_persistence.metadata_codec import FIELDS

        persisted = {field.attr for field in FIELDS}
        assert "_occupancy_generation" not in persisted
        # Nor is the manager, as of the base PR: the crew log is the only place a
        # manager lives and the slot line carries none. So BOTH halves of what the
        # record reads are process-local, and the record is written from the published
        # attribute rather than from anything the disk could disagree with.
        assert "_manager" not in persisted


class TestAHandoffCannotWriteACycleUnderAClosingManager:
    """A pending vacancy must not read as the top of a line to the handoff guard.

    The shape: R reports to M, M reports to T. M closes, pops its key and awaits its
    archival. Inside that window the owner hands T to R. The guard walks up from R to
    see whether T sits above it: it reaches M, whose key is popped and whose manager is
    not published yet. Read as a line that ENDED, the walk says T is not above R and
    the handoff is allowed -- and when M's close commits and records T, the line is
    T -> R -> M -> T. Every manager above M is cut off, every walk over the loop fails
    closed, and the cycle is persisted to the manager log.

    The guard is the only check against this, so the answer has to come from the walk:
    a pending vacancy is a line the walk COULD NOT READ, and the writer's polarity
    turns that into a refusal.
    """

    @staticmethod
    def _tree(tmp_path):
        """``owner`` over ``top`` over ``middle`` over ``report``.

        ``owner`` is a person's own tab -- UNATTRIBUTED, so the ownership fence does
        not apply to it at all. That is deliberate and it is the finding's own
        scenario: with the fence out of the way the handoff loop guard is the only
        check standing between this call and a persisted cycle, which is what makes
        the guard's answer the whole of the defence. A fenced caller would be refused
        earlier, by the fence walking the same unreadable hop, and would hide the
        guard behind it.
        """
        state = _make_state(tmp_path)
        owner = state.get_or_create_slot("chat-owner")
        top = state.get_or_create_slot("chat-top")
        top._created_by = owner.key
        middle = state.get_or_create_slot("chat-middle")
        middle._created_by = top.key
        report = state.get_or_create_slot("chat-report")
        report._created_by = middle.key
        return state, owner, top, middle, report

    def test_a_handoff_under_a_closing_manager_is_refused_while_its_line_is_unknown(
        self, tmp_path
    ):
        state, owner, top, middle, report = self._tree(tmp_path)

        # The middle manager's close: popped, its line not yet published.
        state._slots.pop("chat-middle", None)
        sc.note_key_vacated("chat-middle", middle)
        assert _pending("chat-middle")

        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(
                sc.handoff_target(
                    state,
                    caller_session_key=slot_history_key(owner),
                    target=top.key,
                    to=report.key,
                )
            )
        # NOT ``would_cycle``: nothing proved a loop here. The hop above the receiver
        # belongs to a session inside its close, so the honest answer is that it is not
        # known yet, and the caller is told to retry rather than told its handoff is
        # impossible. Both refuse; only this one is retryable.
        assert exc.value.code == "manager_line_pending", exc.value.code
        assert top._manager == ""
        # So the close commits into a line with no loop in it.
        assert sc.note_manager_departed(state, "chat-middle", middle) == "chat-top"
        assert not _pending("chat-middle")
        assert sc._read_manager_line(state, sc.manager_of(report), top.key)

    def test_a_proven_loop_is_still_told_apart_from_one_that_is_merely_unknown(
        self, tmp_path
    ):
        """The other code, so the two refusals cannot quietly become one.

        Nothing is closing here: the receiver really does report up to the target, and
        a retry would answer the same way forever.
        """
        state, owner, top, middle, report = self._tree(tmp_path)

        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(
                sc.handoff_target(
                    state,
                    caller_session_key=slot_history_key(owner),
                    target=top.key,
                    to=report.key,
                )
            )
        assert exc.value.code == "would_cycle", exc.value.code

    def test_a_rolled_back_close_leaves_the_pre_close_chain_governing(self, tmp_path):
        """The refusal is self-clearing in BOTH directions the close can end.

        A close that fails puts the tab back in the live table. The walk then reads the
        manager off the live slot again and never consults the record, so the chain that
        governs is the one that governed before the close started -- and the handoff is
        refused as the proven loop it always was, not as something unknown.
        """
        state, owner, top, middle, report = self._tree(tmp_path)

        state._slots.pop("chat-middle", None)
        sc.note_key_vacated("chat-middle", middle)
        assert _pending("chat-middle")

        # The close fails and the slot goes back.
        state._slots["chat-middle"] = middle
        assert _pending("chat-middle")  # the record is still there, and now unread

        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(
                sc.handoff_target(
                    state,
                    caller_session_key=slot_history_key(owner),
                    target=top.key,
                    to=report.key,
                )
            )
        assert exc.value.code == "would_cycle", exc.value.code

    def test_no_cycle_reaches_the_manager_log_across_the_close_window(self, tmp_path):
        """(d). The refusal happens BEFORE anything is appended, not after.

        A cycle that reached the log would survive a restart, because the rebuild takes
        the newest entry per slot and the metadata line no longer carries a manager at
        all. So the test watches the emitter rather than the field: the handoff must be
        turned away without a single ``session/manager_set`` append.
        """
        from kiro_crew.crew_log import emit

        state, owner, top, middle, report = self._tree(tmp_path)
        state._slots.pop("chat-middle", None)
        sc.note_key_vacated("chat-middle", middle)

        appended = []

        def _watch(sid, **kwargs):
            appended.append((sid, kwargs))

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(emit, "on_manager_set", _watch)
            with pytest.raises(sc.SessionControlError) as exc:
                asyncio.run(
                    sc.handoff_target(
                        state,
                        caller_session_key=slot_history_key(owner),
                        target=top.key,
                        to=report.key,
                    )
                )

        assert exc.value.code == "manager_line_pending", exc.value.code
        assert appended == []

    def test_the_same_handoff_is_allowed_once_the_close_has_committed_to_nobody(
        self, tmp_path
    ):
        """The case the refusal must not swallow: a line that really did end.

        A committed departure reporting to nobody leaves no hop above it, now or
        later, so an edge written below it cannot become a cycle. If this test ever
        fails together with the one above, the two states have been collapsed again.
        """
        state, owner, top, middle, report = self._tree(tmp_path)
        middle._created_by = ""  # reported to nobody, so no hop above it ever
        middle._manager = ""

        _depart(state, "chat-middle")
        assert _recorded("chat-middle") == ""
        assert not _pending("chat-middle")

        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(
                sc.handoff_target(
                    state,
                    caller_session_key=slot_history_key(owner),
                    target=top.key,
                    to=report.key,
                )
            )
        # Past the loop guard, refused further in because this gateway records no
        # session tree for the edge to move in -- the same place the ordinary sideways
        # handoff lands. What matters here is that the guard did not claim a cycle.
        assert exc.value.code != "would_cycle"


class TestAHandoffThatCommitsAfterItsTargetDepartedCorrectsTheRecord:
    """A commit window outlives its request, so the slot can leave the table inside it.

    The owner hands an idle conductor C from P to R. While the manager entry is still
    being written, the idle sweep archives C: that departure records the COMMITTED
    manager, which is still P, because R is not published until the entry lands. The
    commit then publishes R onto a slot that is no longer in the live table.

    Left there, the walk resolves C's hop through the entry -- naming P, the manager the
    handoff was taking it away from -- so P keeps reach over C's workers and R, who was
    given them, is refused. Nothing corrects it until the entry is evicted or the
    gateway restarts. So the commit rewrites the entry for its own occupancy.
    """

    @staticmethod
    def _line(tmp_path):
        """``P`` over conductor ``C`` over worker ``W``, plus an unrelated ``R``."""
        state = _make_state(tmp_path)
        previous = state.get_or_create_slot("chat-previous")
        previous._created_by = "chat-owner"
        receiver = state.get_or_create_slot("chat-receiver")
        receiver._created_by = "chat-owner"
        conductor = state.get_or_create_slot("chat-conductor")
        conductor._created_by = previous.key
        worker = state.get_or_create_slot("chat-worker")
        worker._created_by = conductor.key
        return state, previous, receiver, conductor, worker

    def test_the_new_manager_reaches_the_workers_and_the_old_one_does_not(self, tmp_path):
        state, previous, receiver, conductor, worker = self._line(tmp_path)

        # The archive lands first, recording the manager committed at that moment.
        _depart(state, "chat-conductor")
        assert _recorded("chat-conductor") == "chat-previous"

        # Then the handoff's own commit publishes the new manager on the departed slot.
        conductor._manager = receiver.key
        sc._record_departure(
            "chat-conductor",
            sc._occupancy_generation_of(conductor),
            sc.manager_of(conductor),
            pending=False,
        )

        assert _recorded("chat-conductor") == "chat-receiver"
        assert not _outside(state, worker, receiver.key)
        assert _outside(state, worker, previous.key)
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(previous),
                target=worker.key,
                operation="send",
            )
        assert exc.value.code == "not_creator"

    def test_a_correction_for_an_occupancy_the_key_has_left_is_refused(self, tmp_path):
        """The same guard every other writer goes through: no key is resurrected.

        If the key was taken by a new occupant while the commit was in flight, the
        correction belongs to an occupancy nobody can read, so it must not be written.
        """
        state, _previous, receiver, conductor, _worker = self._line(tmp_path)
        _depart(state, "chat-conductor")
        replacement = state.get_or_create_slot("chat-conductor")
        replacement._created_by = "chat-someone-else"

        conductor._manager = receiver.key
        wrote = sc._record_departure(
            "chat-conductor",
            sc._occupancy_generation_of(conductor),
            sc.manager_of(conductor),
            pending=False,
        )

        assert wrote is False
        assert _recorded("chat-conductor") is None  # the replacement has not departed

    def test_a_live_slot_is_not_the_case_the_correction_is_for(self, tmp_path):
        """The ordinary handoff, where the correction in the commit must not fire.

        Its condition is that the slot has LEFT the table. A slot still sitting in it is
        answered from the table, so an entry written for it would be a record nothing
        reads and one more thing to keep in step. Asserted on the condition itself,
        because that is what the commit branches on.
        """
        state, _previous, receiver, conductor, worker = self._line(tmp_path)
        conductor._manager = receiver.key

        assert state.get_slot(conductor.key) is conductor  # the condition is False
        assert _recorded("chat-conductor") is None
        # And the live slot is what answers, so the new manager already has the reach.
        assert not _outside(state, worker, receiver.key)


class TestAnEntryAnswersForOneOccupantAndNoOther:
    """The three legs that close the eviction shape, each pinned on its own.

    The shape: an old close suspends after popping its key, a same-key replacement
    opens a worker and departs, the bound then evicts the replacement's entry, and the
    old close finally records. Keyed by SLOT KEY that last write is the answer for the
    replacement's worker, and the old manager reaches a session it never managed.

    Three things refuse it, and the first is the one that does not depend on any writer
    having been fenced correctly.
    """

    def test_an_older_generations_entry_never_answers_for_the_current_occupant(
        self, tmp_path
    ):
        """Leg (a). The entry exists, under the same key, and is still not read.

        Planted by hand rather than raced into place, because the property under test
        is the LOOKUP: an entry filed under another generation is not reachable for the
        occupant holding the key, whatever put it there.
        """
        state = _make_state(tmp_path)
        lead = state.get_or_create_slot("chat-lead")
        lead._created_by = "chat-owner"
        first = state.get_or_create_slot("chat-conductor")
        first._created_by = lead.key
        stale_generation = sc._occupancy_generation_of(first)
        state._slots.pop("chat-conductor", None)

        replacement = state.get_or_create_slot("chat-conductor")
        replacement._created_by = "chat-someone-else"
        worker = state.get_or_create_slot("chat-worker")
        worker._created_by = "chat-conductor"
        _depart(state, "chat-conductor")

        # The old occupant's entry, planted under its own generation.
        sc._departed_managers[("chat-conductor", stale_generation)] = lead.key
        assert sc._key_occupancy["chat-conductor"] != stale_generation

        assert _outside(state, worker, lead.key)
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(lead),
                target=worker.key,
                operation="send",
            )
        assert exc.value.code == "not_creator"

    def test_an_absent_exact_entry_refuses_instead_of_falling_back(self, tmp_path):
        """Leg (b). Eviction drops the exact entry; the walk must not reach for another.

        The eviction is performed directly on the table, which is what the bound does
        to it, and the older occupant's entry is left in place as the thing a fallback
        would find.
        """
        state = _make_state(tmp_path)
        lead = state.get_or_create_slot("chat-lead")
        lead._created_by = "chat-owner"
        first = state.get_or_create_slot("chat-conductor")
        first._created_by = lead.key
        stale_generation = sc._occupancy_generation_of(first)
        state._slots.pop("chat-conductor", None)

        replacement = state.get_or_create_slot("chat-conductor")
        replacement._created_by = "chat-someone-else"
        worker = state.get_or_create_slot("chat-worker")
        worker._created_by = "chat-conductor"
        _depart(state, "chat-conductor")

        held = sc._key_occupancy["chat-conductor"]
        del sc._departed_managers[("chat-conductor", held)]  # what eviction leaves
        sc._departed_managers[("chat-conductor", stale_generation)] = lead.key

        assert _recorded("chat-conductor") is None
        assert _outside(state, worker, lead.key)
        # And the occupancy is still what refuses, so no later write can be read for it.
        assert sc._key_occupancy["chat-conductor"] == held

    def test_a_close_whose_generation_lost_the_key_writes_nothing(self, tmp_path):
        """Leg (c). The write-side refusal, so the stale entry is never created.

        The old close arrives last, after the replacement has taken the key, departed
        and had its entry evicted -- the exact ordering the finding needs. It must come
        away having written nothing at all.
        """
        state = _make_state(tmp_path)
        lead = state.get_or_create_slot("chat-lead")
        lead._created_by = "chat-owner"
        original = state.get_or_create_slot("chat-conductor")
        original._created_by = lead.key
        state._slots.pop("chat-conductor", None)  # the pop the close does first

        replacement = state.get_or_create_slot("chat-conductor")
        replacement._created_by = "chat-someone-else"
        worker = state.get_or_create_slot("chat-worker")
        worker._created_by = "chat-conductor"
        _depart(state, "chat-conductor")
        held = sc._key_occupancy["chat-conductor"]
        del sc._departed_managers[("chat-conductor", held)]  # the eviction

        # Only NOW does the original close reach its record.
        assert sc.note_manager_departed(state, "chat-conductor", original) == ""

        assert not [key for key, _ in sc._departed_managers if key == "chat-conductor"]
        assert _outside(state, worker, lead.key)

    def test_the_bound_is_unchanged_because_it_is_not_what_closes_this(self):
        """Raising the bound would narrow the window and leave the shape intact.

        Stated as a test so that a later change which "fixes" this by making the cache
        bigger has to come through here and say so.
        """
        assert sc._KEY_OCCUPANCY_MAX == 512


class TestTheRecordBoundsWhatItRetainsAndNotOnlyHowMany:
    """A COUNT bound does not bound memory if each entry can hold any length.

    The manager a departure records comes from ``manager_of``, which reads
    ``_created_by`` -- restored from history metadata with no length check of its own.
    So a resumed header naming a megabyte-long creator would be retained here, 512
    times over, after the slot itself is gone and whether or not anyone has the chain
    switched on.

    The value is a SLOT KEY, so the slot-key bound is the one that applies to it.
    """

    def test_a_manager_past_the_slot_key_bound_is_not_retained(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-conductor")
        slot._created_by = "chat-" + "x" * sc._SLOT_KEY_MAX_CHARS
        state._slots.pop("chat-conductor", None)

        assert sc.note_manager_departed(state, "chat-conductor", slot) == ""
        assert _recorded("chat-conductor") is None
        # Nothing of the oversized string is held anywhere in the table.
        retained = [m.manager for m in sc._departed_managers.values()]
        assert not [m for m in retained if len(m) > sc._SLOT_KEY_MAX_CHARS]

    def test_the_hop_is_left_unresolved_rather_than_truncated(self, tmp_path):
        """Truncating would invent a different key -- either nobody, or someone else.

        So the hop has no entry and the walk refuses at it, which costs reach across
        one departed hop and grants nothing.
        """
        state = _make_state(tmp_path)
        lead = state.get_or_create_slot("chat-lead")
        lead._created_by = "chat-owner"
        oversized = "chat-" + "x" * sc._SLOT_KEY_MAX_CHARS
        mid = state.get_or_create_slot("chat-mid")
        mid._created_by = oversized
        worker = state.get_or_create_slot("chat-worker")
        worker._created_by = mid.key
        _depart(state, "chat-mid")

        assert _recorded("chat-mid") is None
        assert _outside(state, worker, oversized)
        assert _outside(state, worker, lead.key)

    def test_a_manager_at_the_bound_is_still_recorded(self, tmp_path):
        """The bound is a limit, not a ban: a legal slot key is retained as before."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-conductor")
        slot._created_by = "c" * sc._SLOT_KEY_MAX_CHARS
        state._slots.pop("chat-conductor", None)

        assert sc.note_manager_departed(state, "chat-conductor", slot) == slot._created_by
        assert _recorded("chat-conductor") == slot._created_by

"""A crew's triage model: which model each turn runs on, and how the slot is switched.

The rule under test is the whole contract of the setting:

  * unset, the slot is never touched -- a crew without a triage model behaves
    exactly as one always has;
  * set, a turn with no open item in an acting phase runs on the triage model,
    and any acting item puts the crew back on its work model;
  * the switch lands between turns, through the slot's deferred session reset;
  * a triage model the session withheld falls back to the work model and is
    named for the crew page.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from kiro_crew.apps.builtins.issue_radar.backend import crew_model as cm
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.dashboard.chat_utils import session_key_for

WORK = "claude-opus-4.5"
TRIAGE = "claude-haiku-4.5"


class _Slot:
    """The slot attributes the switch reads and writes, and nothing else."""

    def __init__(self, key: str = "crew-c_1", model: str = WORK) -> None:
        self.key = key
        self.model = model
        self.running = False
        self.model_withheld: bool | None = None
        self._pending_reset_history_key: str | None = None
        self._eager_spawn_task = None
        self._model_pick_gen = 0
        self._model_pick_lock = asyncio.Lock()
        self._dirty = False


class _Sessions:
    def __init__(self, live: set[str] | None = None) -> None:
        self.live = set(live or ())

    def get_provider(self, key: str) -> object | None:
        return object() if key in self.live else None


class _State:
    """``live`` names SLOTS with a registered session; keyed the way the runtime asks."""

    def __init__(self, live: set[str] | None = None) -> None:
        self.sessions = _Sessions({session_key_for(key) for key in (live or ())})


def _crew(**fields: Any) -> dict[str, Any]:
    return {"id": "c_1", "model": WORK, "triage_model": "", **fields}


def _item(phase: str) -> dict[str, Any]:
    return {"number": 7, "phase": phase}


class _Isolated(unittest.TestCase):
    def setUp(self) -> None:
        cm._withheld.clear()
        cm._written.clear()
        self.addCleanup(cm._withheld.clear)
        self.addCleanup(cm._written.clear)


class TestTurnModel(_Isolated):
    def test_unset_runs_every_turn_on_the_work_model(self):
        self.assertEqual(cm.turn_model(_crew(), []), WORK)
        self.assertEqual(cm.turn_model(_crew(), [_item("implementing")]), WORK)

    def test_an_empty_queue_runs_on_the_triage_model(self):
        self.assertEqual(cm.turn_model(_crew(triage_model=TRIAGE), []), TRIAGE)

    def test_items_only_waiting_run_on_the_triage_model(self):
        items = [_item("awaiting-ci"), _item("awaiting-merge"), _item("awaiting-reply")]
        self.assertEqual(cm.turn_model(_crew(triage_model=TRIAGE), items), TRIAGE)

    def test_any_acting_item_runs_on_the_work_model(self):
        for phase in ("selected", "claimed", "investigating", "implementing", "addressing-review"):
            with self.subTest(phase=phase):
                items = [_item("awaiting-ci"), _item(phase)]
                self.assertEqual(cm.turn_model(_crew(triage_model=TRIAGE), items), WORK)

    def test_acting_phases_cover_the_store_sets(self):
        self.assertTrue(set(cs.TTL_ACTIVE_PHASES) <= cm.ACTING_PHASES)
        self.assertTrue(set(cs.EDITING_PHASES) <= cm.ACTING_PHASES)
        self.assertTrue(cm.ACTING_PHASES <= set(cs.PHASES))


class TestApplyTurnModel(_Isolated):
    def test_unset_never_touches_the_slot(self):
        slot = _Slot(model="something-a-person-picked")
        self.assertFalse(cm.apply_turn_model(_State({slot.key}), slot, _crew(), []))
        self.assertEqual(slot.model, "something-a-person-picked")
        self.assertIsNone(slot._pending_reset_history_key)

    def test_a_triage_turn_switches_the_slot_and_arms_the_reset(self):
        slot = _Slot()
        moved = cm.apply_turn_model(_State({slot.key}), slot, _crew(triage_model=TRIAGE), [])
        self.assertTrue(moved)
        self.assertEqual(slot.model, TRIAGE)
        # The live session is still on the work model; the next turn tears it down
        # before acquiring one, so it resumes on the triage model.
        self.assertEqual(slot._pending_reset_history_key, session_key_for(slot.key))
        self.assertEqual(slot._model_pick_gen, 1)
        self.assertTrue(slot._dirty)

    def test_no_live_session_needs_no_reset(self):
        slot = _Slot()
        cm.apply_turn_model(_State(), slot, _crew(triage_model=TRIAGE), [])
        self.assertEqual(slot.model, TRIAGE)
        self.assertIsNone(slot._pending_reset_history_key)

    def test_an_acting_item_puts_the_work_model_back(self):
        slot = _Slot(model=TRIAGE)
        crew = _crew(triage_model=TRIAGE)
        self.assertTrue(cm.apply_turn_model(_State({slot.key}), slot, crew, [_item("claimed")]))
        self.assertEqual(slot.model, WORK)
        self.assertEqual(slot._pending_reset_history_key, session_key_for(slot.key))

    def test_a_running_turn_keeps_its_model(self):
        slot = _Slot()
        slot.running = True
        self.assertFalse(
            cm.apply_turn_model(_State({slot.key}), slot, _crew(triage_model=TRIAGE), [])
        )
        self.assertEqual(slot.model, WORK)
        self.assertIsNone(slot._pending_reset_history_key)

    def test_a_picker_transaction_in_flight_is_left_alone(self):
        async def run() -> None:
            slot = _Slot()
            async with slot._model_pick_lock:
                cm.apply_turn_model(_State({slot.key}), slot, _crew(triage_model=TRIAGE), [])
            self.assertEqual(slot.model, WORK)

        asyncio.run(run())

    def test_already_on_the_target_is_a_no_op(self):
        slot = _Slot(model=TRIAGE)
        self.assertFalse(
            cm.apply_turn_model(_State({slot.key}), slot, _crew(triage_model=TRIAGE), [])
        )
        self.assertIsNone(slot._pending_reset_history_key)
        self.assertEqual(slot._model_pick_gen, 0)


class TestUnavailableTriageModel(_Isolated):
    def test_a_withheld_triage_model_falls_back_and_is_named(self):
        slot = _Slot(model=TRIAGE)
        slot.model_withheld = True  # the session that started on it could not run it
        crew = _crew(triage_model=TRIAGE)
        self.assertTrue(cm.apply_turn_model(_State({slot.key}), slot, crew, []))
        self.assertEqual(slot.model, WORK)
        self.assertEqual(cm.unavailable_triage_model(crew), TRIAGE)
        # And it stays on the work model: the verdict outlives the slot moving off.
        slot.model_withheld = None
        self.assertFalse(cm.apply_turn_model(_State({slot.key}), slot, crew, []))
        self.assertEqual(slot.model, WORK)

    def test_changing_the_setting_clears_the_notice(self):
        slot = _Slot(model=TRIAGE)
        slot.model_withheld = True
        cm.apply_turn_model(_State(), slot, _crew(triage_model=TRIAGE), [])
        other = _crew(triage_model="claude-sonnet-4.5")
        self.assertEqual(cm.unavailable_triage_model(other), "")
        self.assertEqual(cm.turn_model(other, []), "claude-sonnet-4.5")

    def test_a_runnable_verdict_reports_nothing(self):
        slot = _Slot(model=TRIAGE)
        slot.model_withheld = False
        crew = _crew(triage_model=TRIAGE)
        cm.apply_turn_model(_State(), slot, crew, [])
        self.assertEqual(cm.unavailable_triage_model(crew), "")
        self.assertEqual(slot.model, TRIAGE)


class TestSwitchingOff(_Isolated):
    def test_switching_off_puts_the_work_model_back(self):
        slot = _Slot()
        cm.apply_turn_model(_State(), slot, _crew(triage_model=TRIAGE), [])
        self.assertEqual(slot.model, TRIAGE)
        self.assertTrue(cm.apply_turn_model(_State({slot.key}), slot, _crew(), []))
        self.assertEqual(slot.model, WORK)
        self.assertFalse(cm.is_managed(slot))

    def test_a_model_picked_since_is_kept(self):
        slot = _Slot()
        cm.apply_turn_model(_State(), slot, _crew(triage_model=TRIAGE), [])
        slot.model = "picked-by-hand"
        self.assertFalse(cm.apply_turn_model(_State(), slot, _crew(), []))
        self.assertEqual(slot.model, "picked-by-hand")

    def test_an_edit_after_a_restart_still_restores(self):
        # A restart empties the module's memory while the slot keeps the triage
        # model it persisted; the edit that clears the setting is what marks it.
        slot = _Slot(model=TRIAGE)
        cm.note_triage_change(slot, _crew(triage_model=TRIAGE), _crew())
        self.assertTrue(cm.apply_turn_model(_State(), slot, _crew(), []))
        self.assertEqual(slot.model, WORK)


class TestStoreField(unittest.TestCase):
    def test_a_record_without_the_field_reads_as_unset(self):
        self.assertEqual(cs._coerce_crew({"id": "c_1", "name": "Leo"})["triage_model"], "")

    def test_a_valid_id_is_kept_and_trimmed(self):
        self.assertEqual(
            cs._validated_crew_patch({"triage_model": f"  {TRIAGE} "}), {"triage_model": TRIAGE}
        )

    def test_empty_switches_it_off(self):
        self.assertEqual(cs._validated_crew_patch({"triage_model": ""}), {"triage_model": ""})

    def test_an_id_the_picker_would_refuse_is_dropped(self):
        for bad in ("two words", "-leading-dash", 7, None, "x" * 300):
            with self.subTest(bad=bad):
                self.assertEqual(cs._validated_crew_patch({"triage_model": bad}), {})

    def test_a_hand_edited_bad_id_reads_as_unset(self):
        rec = cs._coerce_crew({"id": "c_1", "name": "Leo", "triage_model": "not a model"})
        self.assertEqual(rec["triage_model"], "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

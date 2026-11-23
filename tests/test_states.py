import os
import tempfile
import unittest
from unittest import mock

from swarm.states import (
    GOAL_STATES,
    MAX_FIX_ROUNDS,
    PLAN_STATES,
    RUN_KINDS,
    RUN_STATUSES,
    TERMINAL,
    TRANSITIONS,
    check_plan_move,
)


class StatesTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"SWARM_HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)


class ConstantsTest(StatesTestBase):
    def test_constants(self):
        self.assertEqual(MAX_FIX_ROUNDS, 2)
        self.assertEqual(TERMINAL, {"DONE", "DROPPED"})
        self.assertEqual(GOAL_STATES, ["QUEUED", "ACTIVE", "BLOCKED", "DONE", "DROPPED"])
        self.assertEqual(RUN_KINDS, ["architect", "reviewer", "implementer",
                                     "verifier", "runner", "other"])
        self.assertEqual(RUN_STATUSES, ["RUNNING", "OK", "FAILED", "COST_LIMIT"])
        for state in PLAN_STATES:
            if state not in TERMINAL and state != "BLOCKED":
                self.assertIn(state, TRANSITIONS)


class AllowedEdgesTest(StatesTestBase):
    def test_every_transition_edge_passes(self):
        for current, targets in TRANSITIONS.items():
            for target in targets:
                if current == "VERIFYING" and target == "FIXING":
                    check_plan_move("VERIFYING", "FIXING", 0, None)
                    check_plan_move("VERIFYING", "FIXING", MAX_FIX_ROUNDS - 1, None)
                else:
                    check_plan_move(current, target, 0, None)

    def test_dropped_allowed_from_every_non_terminal_state(self):
        for current in PLAN_STATES:
            if current not in TERMINAL:
                check_plan_move(current, "DROPPED", 0, None)

    def test_blocked_allowed_from_every_non_terminal_non_blocked_state(self):
        for current in PLAN_STATES:
            if current not in TERMINAL and current != "BLOCKED":
                check_plan_move(current, "BLOCKED", 0, None)

    def test_blocked_returns_to_blocked_from(self):
        for previous in PLAN_STATES:
            if previous in TERMINAL or previous == "BLOCKED":
                continue
            check_plan_move("BLOCKED", previous, 5, previous)

    def test_blocked_can_be_dropped(self):
        check_plan_move("BLOCKED", "DROPPED", 0, "EXECUTING")
        check_plan_move("BLOCKED", "DROPPED", 0, None)


class IllegalEdgesTest(StatesTestBase):
    def test_sample_illegal_edges(self):
        for current, target, blocked_from in [
            ("DRAFT", "EXECUTING", None),
            ("DONE", "DRAFT", None),
            ("BLOCKED", "DONE", "EXECUTING"),
        ]:
            with self.assertRaises(ValueError) as ctx:
                check_plan_move(current, target, 0, blocked_from)
            self.assertTrue(str(ctx.exception))

    def test_terminal_states_cannot_move(self):
        for current in TERMINAL:
            for target in PLAN_STATES:
                with self.assertRaises(ValueError):
                    check_plan_move(current, target, 0, None)

    def test_blocked_cannot_move_to_other_states(self):
        for target in PLAN_STATES:
            if target in {"BLOCKED", "DROPPED", "EXECUTING"}:
                continue
            with self.assertRaises(ValueError):
                check_plan_move("BLOCKED", target, 0, "EXECUTING")

    def test_blocked_without_previous_state_refuses_returns(self):
        for target in PLAN_STATES:
            if target == "DROPPED" or target == "BLOCKED":
                continue
            with self.assertRaises(ValueError):
                check_plan_move("BLOCKED", target, 0, None)

    def test_already_blocked_refused(self):
        with self.assertRaises(ValueError) as ctx:
            check_plan_move("BLOCKED", "BLOCKED", 0, "EXECUTING")
        self.assertIn("BLOCKED", str(ctx.exception))

    def test_measuring_cannot_merge(self):
        with self.assertRaises(ValueError):
            check_plan_move("MEASURING", "MERGED", 0, None)


class FixRoundLimitTest(StatesTestBase):
    def test_verifying_to_fixing_under_limit_passes(self):
        check_plan_move("VERIFYING", "FIXING", 0, None)
        check_plan_move("VERIFYING", "FIXING", 1, None)

    def test_third_fix_round_refused_with_exact_message(self):
        with self.assertRaises(ValueError) as ctx:
            check_plan_move("VERIFYING", "FIXING", 2, None)
        self.assertEqual(
            str(ctx.exception),
            "fix round limit (2) reached: move the plan to BLOCKED and ask the operator",
        )

    def test_fix_limit_not_applied_to_other_moves(self):
        check_plan_move("FIXING", "VERIFYING", 5, None)
        check_plan_move("VERIFYING", "MERGED", 5, None)

    def test_blocked_return_to_fixing_ignores_fix_limit(self):
        check_plan_move("BLOCKED", "FIXING", 5, "FIXING")


if __name__ == "__main__":
    unittest.main()

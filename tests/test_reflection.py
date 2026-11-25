import json
import unittest

from refl.reflection import FAILURE, MANUAL, NO_PROGRESS, UNFIXED, StallDetector, choose_roles


def names(roles):
    return [role.name for role in roles]


class RoleTest(unittest.TestCase):
    def test_every_reflector_is_told_to_be_honest_and_to_argue_from_proven_facts(self):
        from refl import challenge, engine
        from refl.reflection import FACTS_RULE, HONESTY_RULE, REFLECTION_PROMPT, ROLES
        for prompt in (REFLECTION_PROMPT, engine._PROPOSAL_PROMPT, challenge.CHALLENGE_PROMPT,
                       challenge.SIMPLIFY_PROMPT, challenge.REBUTTAL_PROMPT, challenge.SIMPLIFY_REBUTTAL_PROMPT):
            self.assertIn(HONESTY_RULE, prompt)
        for prompt in (REFLECTION_PROMPT, engine._PROPOSAL_PROMPT):
            self.assertIn(FACTS_RULE, prompt)
        # Every reviewer can gather facts, and every answer names them for the verifier.
        self.assertTrue(all(role.tools and "facts" in role.fields for role in ROLES))
        self.assertNotIn("without evidence", " ".join(role.focus for role in ROLES))

    def test_first_reflection_is_grounded(self):
        self.assertEqual(names(choose_roles(0, FAILURE, unavailable={"Existing patterns"})),
                         ["Diagnosis", "Observe", "Switch tools"])
        self.assertEqual(names(choose_roles(0, NO_PROGRESS)), ["Existing patterns", "Diagnosis", "Rebuild and swap"])
        self.assertEqual(names(choose_roles(0, UNFIXED, unavailable={"Existing patterns"})),
                         ["Diagnosis", "Observe", "Rebuild and swap"])

    def test_manual_reflection_uses_the_idea_roles(self):
        self.assertEqual(names(choose_roles(2, MANUAL)), ["Known alternative", "Analogy", "Naive shortcut"])

    def test_repeated_stalls_raise_the_temperature_and_rotate_roles(self):
        detector = StallDetector({"delay": 0, "remaining": 0})
        batches = [detector.plan(FAILURE, unavailable={"Existing patterns"}) for _ in range(3)]
        self.assertEqual([level for level, _ in batches], [0, 1, 2])
        self.assertEqual(names(batches[1][1]), ["Prerequisites", "Shrink the problem", "Analogy"])
        self.assertEqual(names(batches[2][1]), ["Rebuild and swap", "Naive shortcut", "Inversion"])

    def test_progress_cools_down(self):
        detector = StallDetector({"delay": 0, "remaining": 0})
        detector.plan(FAILURE)
        detector.plan(FAILURE)
        detector.record({"name": "bash", "arguments": json.dumps({"command": "make"})}, {"exit_code": 1, "output": ""})
        detector.record({"name": "bash", "arguments": json.dumps({"command": "make"})}, {"exit_code": 0, "output": ""})
        self.assertEqual(detector.plan(FAILURE)[0], 0)

    def test_manual_reflection_starts_hot(self):
        self.assertEqual(StallDetector({"delay": 0, "remaining": 0}).plan(MANUAL, manual=True)[0], 2)


class StallDetectorTest(unittest.TestCase):
    def setUp(self):
        self.detector = StallDetector({"delay": 0, "remaining": 0})

    def record(self, name, arguments, result):
        self.detector.record({"name": name, "arguments": json.dumps(arguments)}, result)
        return self.detector.stall()

    def cycle(self, output):
        """Edit a file, then rerun the same failing test."""
        self.assertIsNone(self.record("edit_file", {"path": "app.py"}, {"output": "Edited app.py", "changed": True}))
        return self.record("bash", {"command": "pytest"}, {"exit_code": 1, "output": output})

    def test_edit_and_retest_with_the_same_failure(self):
        self.assertIsNone(self.record("bash", {"command": "pytest"}, {"exit_code": 1, "output": "1 failed in 0.05s"}))
        self.assertIsNone(self.cycle("1 failed in 0.07s"))
        self.assertEqual(self.cycle("1 failed in 0.04s"), "pytest failed the same way three times.")
        self.assertEqual(self.detector.kind, UNFIXED)

    def test_results_that_cycle_back(self):
        check = "python check.py; cat report.txt"
        outputs = ["FAILED\nW", "FAILED\nX", "FAILED\nY", "FAILED\nX"]
        reasons = []
        for output in outputs:
            self.assertIsNone(self.record("edit_file", {"path": "report.py"}, {"output": "Edited", "changed": True}))
            reasons.append(self.record("bash", {"command": check}, {"exit_code": 0, "output": output}))
        self.assertEqual(reasons, [None, None, None, f"{check} returned an earlier result again."])
        self.assertEqual(self.detector.kind, NO_PROGRESS)

    def test_repeating_a_passing_check_is_not_a_cycle(self):
        for output in ["1 failed", "2 failed", "OK", "OK", "OK"]:
            self.record("edit_file", {"path": "app.py"}, {"output": "Edited", "changed": True})
            self.assertIsNone(self.record("bash", {"command": "make check; echo done"}, {"exit_code": 0, "output": output}))

    def test_unchanged_result_is_a_progress_stall(self):
        for _ in range(3):
            reason = self.record("bash", {"command": "make"}, {"exit_code": 0, "output": "Nothing to do."})
        self.assertEqual(reason, "make returned the same result three times.")
        self.assertEqual(self.detector.kind, NO_PROGRESS)

    def test_changing_failures_count_as_progress(self):
        self.record("bash", {"command": "pytest"}, {"exit_code": 1, "output": "3 failed"})
        self.assertIsNone(self.cycle("2 failed"))
        self.assertIsNone(self.cycle("1 failed"))

    def test_needs_three_more_failures_to_trigger_again(self):
        self.record("bash", {"command": "pytest"}, {"exit_code": 1, "output": "1 failed"})
        self.cycle("1 failed")
        self.assertIsNotNone(self.cycle("1 failed"))
        self.assertIsNone(self.cycle("1 failed"))
        self.assertIsNone(self.cycle("1 failed"))
        self.assertIsNotNone(self.cycle("1 failed"))

    def test_passing_run_resets_the_count(self):
        self.record("bash", {"command": "pytest"}, {"exit_code": 1, "output": "1 failed"})
        self.cycle("1 failed")
        self.assertIsNone(self.record("bash", {"command": "pytest"}, {"exit_code": 0, "output": "1 passed"}))
        self.assertIsNone(self.cycle("1 failed"))
        self.assertIsNone(self.cycle("1 failed"))

    def test_final_answer_while_a_rerun_check_fails(self):
        self.record("bash", {"command": "pytest"}, {"exit_code": 1, "output": "2 failed"})
        self.assertIsNone(self.detector.stopped())  # a single failed command may be the answer itself
        self.assertIsNone(self.cycle("1 failed"))
        self.assertEqual(self.detector.stopped(), "The main branch gave a final answer while pytest still fails.")
        self.assertEqual(self.detector.kind, UNFIXED)
        self.assertIsNone(self.detector.stopped())  # reported once

    def explore(self, count, start=0):
        """Different reads and scripts with different results, none of which changes a file."""
        reasons = []
        for index in range(start, start + count):
            if index % 2:
                reasons.append(self.record("read_file", {"path": f"notes{index}.txt"}, {"output": f"line {index}"}))
            else:
                reasons.append(self.record("bash", {"command": f"python try{index}.py"},
                                           {"exit_code": 0, "output": f"no match {index}"}))
        return reasons

    def test_wandering_while_a_check_fails(self):
        self.record("bash", {"command": "python check.py"}, {"exit_code": 1, "output": "FAILED"})
        reasons = self.explore(9)
        self.assertEqual(reasons[:-1], [None] * 8)
        self.assertEqual(reasons[-1], "10 tool calls in a row changed no file while python check.py still fails.")
        self.assertEqual(self.detector.kind, NO_PROGRESS)

    def test_a_piped_test_run_still_counts_as_failing(self):
        # `| tail` exits successfully, so the failure shows only in the test runner's summary.
        command = "python -m unittest pov_test 2>&1 | tail -5"
        self.record("bash", {"command": command}, {"exit_code": 0, "output": "Ran 15 tests\n\nFAILED (failures=10)\n"})
        self.assertEqual(self.explore(9)[-1], f"10 tool calls in a row changed no file while {command} still fails.")

    def test_exploring_without_a_failure_is_not_a_stall(self):
        self.assertEqual(self.explore(20), [None] * 20)

    def test_a_search_without_matches_is_not_a_failing_check(self):
        # grep exits with 1 when nothing matches, which during an investigation is the answer itself.
        self.explore(8)
        self.record("bash", {"command": "ls games | grep -iE 'geebee|bombbee'"}, {"exit_code": 1, "output": ""})
        self.assertEqual(self.explore(12, start=8), [None] * 12)

    def test_a_timeout_counts_as_failing(self):
        self.record("bash", {"command": "mame bombbee"}, {"error": "Bash timed out after 180 seconds", "output": ""})
        self.assertEqual(self.explore(9)[-1], "10 tool calls in a row changed no file while mame bombbee still fails.")

    def test_a_plain_failure_keeps_the_reported_one(self):
        self.record("bash", {"command": "python check.py"}, {"exit_code": 1, "output": "FAILED"})
        self.record("bash", {"command": "test -f out.txt"}, {"exit_code": 1, "output": ""})
        reasons = self.explore(8)
        self.assertEqual(reasons[-1], "10 tool calls in a row changed no file while python check.py still fails.")

    def test_a_file_change_restarts_the_count(self):
        self.record("bash", {"command": "python check.py"}, {"exit_code": 1, "output": "FAILED"})
        self.explore(5)
        self.record("write_file", {"path": "report.py"}, {"output": "Wrote report.py", "changed": True})
        self.assertEqual(self.explore(9, start=5), [None] * 9)
        self.assertIsNotNone(self.explore(1, start=14)[0])

    def test_final_answer_after_the_check_passes(self):
        self.record("bash", {"command": "pytest"}, {"exit_code": 1, "output": "1 failed"})
        self.record("bash", {"command": "pytest"}, {"exit_code": 0, "output": "1 passed"})
        self.record("bash", {"command": "git status"}, {"exit_code": 0, "output": "clean"})
        self.assertIsNone(self.detector.stopped())


if __name__ == "__main__":
    unittest.main()

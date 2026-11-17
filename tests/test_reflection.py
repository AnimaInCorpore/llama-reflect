import json
import unittest

from refl.reflection import FAILURE, MANUAL, NO_PROGRESS, UNFIXED, StallDetector, choose_roles


def names(roles):
    return [role.name for role in roles]


class RoleTest(unittest.TestCase):
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

    def test_final_answer_after_the_check_passes(self):
        self.record("bash", {"command": "pytest"}, {"exit_code": 1, "output": "1 failed"})
        self.record("bash", {"command": "pytest"}, {"exit_code": 0, "output": "1 passed"})
        self.record("bash", {"command": "git status"}, {"exit_code": 0, "output": "clean"})
        self.assertIsNone(self.detector.stopped())


if __name__ == "__main__":
    unittest.main()

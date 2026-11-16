import json
import unittest

from refl.reflection import StallDetector


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


if __name__ == "__main__":
    unittest.main()

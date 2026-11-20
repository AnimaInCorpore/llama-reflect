import contextlib
import io
import tempfile
import unittest

from refl.client import ServerProperties
from refl.main import _check_command, _check_fork_effort, _tolerant_output, main

# Nothing listens on the discard port, so a test that reaches the server fails.
_NO_SERVER = "http://127.0.0.1:9"


class MainTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def run_main(self, prompt):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            try:
                code = main([prompt, "--workspace", self.directory.name, "--url", _NO_SERVER])
            except SystemExit as exit:
                code = exit.code
        return code, errors.getvalue()

    def test_unencodable_output_is_escaped(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp1252")
        _tolerant_output(stream)
        print("Verdict \u2705", file=stream)
        stream.flush()
        self.assertEqual(buffer.getvalue().strip(), b"Verdict \\u2705")

    def test_unknown_command_is_rejected_before_connecting(self):
        code, errors = self.run_main("/tre")
        self.assertEqual(code, 1)
        self.assertIn("Unknown command /tre", errors)

    def test_exit_needs_no_server(self):
        self.assertEqual(self.run_main("/exit")[0], 0)

    def test_msys_mangled_command_is_explained(self):
        code, errors = self.run_main("C:/Program Files/Git/checkout 5")
        self.assertEqual(code, 2)
        self.assertIn("looks like /checkout", errors)

    def test_tasks_that_start_with_a_path_still_run(self):
        _check_command("/tmp", "is full, clean it")
        _check_command("/usr/bin/env", "")


    def test_fork_effort_needs_thinking_in_the_forks(self):
        for argv in (["hi", "--fork-effort", "none"], ["hi", "--fork-effort", "low", "--thinking", "main"]):
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
                main([*argv, "--workspace", self.directory.name, "--url", _NO_SERVER])
            self.assertIn("--thinking", errors.getvalue())

    def test_fork_effort_is_checked_against_the_template(self):
        properties = ServerProperties(effort_levels=["low", "medium", "xhigh"], rejected_efforts=["minimal", "max"])
        with self.assertRaisesRegex(ValueError, "rejects --fork-effort max. Its levels are low, medium, xhigh"):
            _check_fork_effort("max", properties)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            _check_fork_effort("low", properties)
            _check_fork_effort("high", properties)
            _check_fork_effort("low", ServerProperties(effort_levels=[]))
            _check_fork_effort("low", ServerProperties())
        self.assertEqual(errors.getvalue().splitlines(), [
            "--fork-effort high is not one of the chat template's levels (low, medium, xhigh), so the template "
            "treats it like one of them or like its default.",
            "The chat template has no reasoning effort levels, so --fork-effort low has no effect.",
        ])

if __name__ == "__main__":
    unittest.main()

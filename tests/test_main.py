import asyncio
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from refl.client import ServerProperties
from refl.engine import TurnLimitError
from refl.main import _check_command, _check_fork_effort, _submit, _tolerant_output, main

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

    def test_a_new_session_names_the_latest_earlier_one(self):
        from refl.main import _open_tree
        sessions = Path(self.directory.name) / ".refl" / "sessions"
        sessions.mkdir(parents=True)
        for name in ("20261125T100000000000Z-old", "20261125T120000000000Z-newer"):
            (sessions / f"{name}.json").write_text("{}", encoding="utf-8")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            _open_tree(Path(self.directory.name), None)
        self.assertIn("Latest earlier session: 20261125T120000000000Z-newer. Continue it with --resume "
                      "20261125T120000000000Z-newer", errors.getvalue())

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

    def test_project_files_do_not_replace_standard_modules(self):
        # python -m refl starts in the project folder, which could hold a file named like a standard module that
        # Reflect imports. Modules that Python itself needs to run -m, such as types, stay out of reach.
        for name in ("dis", "inspect", "json", "socket"):
            Path(self.directory.name, f"{name}.py").write_text(f"raise SystemExit('{name}.py was imported')",
                                                               encoding="utf-8")
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
        result = subprocess.run([sys.executable, "-m", "refl", "--help"], cwd=self.directory.name, env=environment,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertTrue(result.stdout.startswith("usage: refl"))

    def test_tasks_that_start_with_a_path_still_run(self):
        _check_command("/tmp", "is full, clean it")
        _check_command("/usr/bin/env", "")


    def test_fork_effort_needs_thinking_in_the_forks(self):
        for argv in (["hi", "--fork-effort", "none"], ["hi", "--fork-effort", "low", "--thinking", "main"],
                     ["hi", "--fork-effort", "low", "--thinking", "off"]):
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
                main([*argv, "--workspace", self.directory.name, "--url", _NO_SERVER])
            self.assertIn("--thinking", errors.getvalue())
        self.assertIn("skip thinking with --thinking off", errors.getvalue())

    def test_the_report_is_shown_when_a_turn_runs_out(self):
        class Engine:
            async def respond(self, prompt):
                raise TurnLimitError("Reached the 20-request limit before a final answer.", report="Done: A. Open: B.")

        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(TurnLimitError):
            asyncio.run(_submit(Engine(), "Port it."))
        self.assertEqual(output.getvalue(), "Done: A. Open: B.\n")

    def test_the_plan_challenge_is_opt_in(self):
        seen = []

        async def run(args):
            seen.append(args.challenge)

        with mock.patch("refl.main._run", run):
            self.assertEqual(main(["hi"]), 0)
            self.assertEqual(main(["hi", "--challenge"]), 0)
        self.assertEqual(seen, [False, True])

    def test_thinking_is_on_by_default(self):
        seen = []

        async def run(args):
            seen.append(args.thinking)

        with mock.patch("refl.main._run", run):
            self.assertEqual(main(["hi"]), 0)
            self.assertEqual(main(["hi", "--fork-effort", "low"]), 0)
            self.assertEqual(main(["hi", "--thinking", "off"]), 0)
        self.assertEqual(seen, ["auto", "auto", "off"])

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

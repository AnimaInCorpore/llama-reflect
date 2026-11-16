import contextlib
import io
import tempfile
import unittest

from refl.main import _check_command, _tolerant_output, main

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


if __name__ == "__main__":
    unittest.main()

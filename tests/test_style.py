import contextlib
import io
import tempfile
import unittest

from refl import style
from refl.main import _status, main
from refl.style import Styler, wants_color

ON = Styler(True)


class StyleTest(unittest.TestCase):
    def assertStyled(self, message, expected):
        self.assertEqual(ON.event(message).replace("\x1b", "^"), expected)

    def test_disabled_output_is_unchanged(self):
        for message in ("read_file a.txt", "Challenge\nAnswer\nI will", "[VERIFIED] yes", "refl: failed"):
            self.assertEqual(Styler(False).event(message), message)
        self.assertEqual(Styler(False).answer("Done."), "Done.")

    def test_main_branch_calls_show_the_tool_name_in_bold(self):
        self.assertStyled("bash python check.py", "^[1mbash^[0m python check.py")
        self.assertStyled("write_file text.py", "^[1mwrite_file^[0m text.py")

    def test_each_kind_of_fork_has_its_own_color(self):
        self.assertStyled("verify read_file SPEC.md", "^[36mverify read_file SPEC.md^[0m")
        self.assertStyled("Verifying the claim", "^[36mVerifying the claim^[0m")
        self.assertStyled("challenge grep_search \"slug\" .", "^[35mchallenge grep_search \"slug\" .^[0m")
        self.assertStyled("Challenging as Requirements", "^[35mChallenging as Requirements^[0m")
        self.assertStyled("Challenge Requirements: resolved.", "^[35mChallenge Requirements: resolved.^[0m")
        self.assertStyled("reflect read_file a.py", "^[94mreflect read_file a.py^[0m")
        self.assertStyled("Reviewing as Diagnosis: Name causes", "^[94mReviewing as Diagnosis: Name causes^[0m")

    def test_verdicts_are_green_red_or_yellow(self):
        self.assertStyled("[VERIFIED] line 3", "^[1;32m[VERIFIED]^[0m^[36m line 3^[0m")
        self.assertStyled("[FALSE ASSUMPTION] no", "^[1;31m[FALSE ASSUMPTION]^[0m^[36m no^[0m")
        self.assertStyled("[UNVERIFIED] unclear", "^[1;33m[UNVERIFIED]^[0m^[36m unclear^[0m")

    def test_blocks_have_a_bold_heading_and_style_each_line(self):
        self.assertStyled("Challenge\nProven objections\n\n- Requirements: x",
                          "^[1;35mChallenge^[0m\n^[35mProven objections^[0m\n\n^[35m- Requirements: x^[0m")
        self.assertStyled("Strategy\nReflecting: stall", "^[1;94mStrategy^[0m\n^[94mReflecting: stall^[0m")
        self.assertStyled("Skills\nSaved .refl/skills/a.md", "^[1;2mSkills^[0m\n^[2mSaved .refl/skills/a.md^[0m")

    def test_status_is_dim_and_problems_stand_out(self):
        self.assertStyled("Model: qwen", "^[2mModel: qwen^[0m")
        self.assertStyled("Context: 131,072 tokens per slot", "^[2mContext: 131,072 tokens per slot^[0m")
        self.assertStyled("Context: 98,000 of 131,072 tokens in use (75%).",
                          "^[33mContext: 98,000 of 131,072 tokens in use (75%).^[0m")
        self.assertStyled("Thinking stopped at its budget", "^[33mThinking stopped at its budget^[0m")
        self.assertStyled("refl: Could not reach", "^[31mrefl: Could not reach^[0m")
        self.assertStyled("Something else entirely", "Something else entirely")

    def test_answers_are_bold_line_by_line(self):
        self.assertEqual(ON.answer("Done.\n\nIt works.").replace("\x1b", "^"), "^[1mDone.^[0m\n\n^[1mIt works.^[0m")

    def test_color_follows_the_mode_the_terminal_and_no_color(self):
        self.assertTrue(wants_color(io.StringIO(), "always"))
        self.assertFalse(wants_color(io.StringIO(), "never"))
        self.assertFalse(wants_color(io.StringIO(), "auto", environ={}))

        class Terminal(io.StringIO):
            def isatty(self):
                return True

            def fileno(self):
                raise OSError("no file descriptor")

        self.assertFalse(wants_color(Terminal(), "auto", environ={"NO_COLOR": "1"}))
        self.assertFalse(wants_color(Terminal(), "auto", environ={"TERM": "dumb"}))

    def test_the_mintty_pty_is_told_apart_from_an_ordinary_pipe(self):
        self.assertTrue(style._MSYS_PTY.search(r"\msys-1888ae32e00d56aa-pty0-to-master"))
        self.assertTrue(style._MSYS_PTY.search(r"\cygwin-e022582115c10879-pty3-from-master"))
        self.assertFalse(style._MSYS_PTY.search(r"\Device\NamedPipe\Win32Pipes.00001a2c.00000002"))
        self.assertFalse(style._MSYS_PTY.search(r"\msys-1888ae32e00d56aa-pipe-0x1F"))

    def test_the_agents_words_read_as_chat(self):
        self.assertStyled("» I will read the spec first.", "^[1m»^[0m I will read the spec first.")
        self.assertEqual(Styler(False).event("» Reading."), "» Reading.")

    def test_summary_lines_have_a_bold_label(self):
        self.assertStyled("Plan check: 2 proven, 1 to follow, 1 dismissed (40 s)",
                          "^[1;35mPlan check:^[0m^[35m 2 proven, 1 to follow, 1 dismissed (40 s)^[0m")
        self.assertStyled("Reflection: 1 of 3 ideas proven; nothing to try (9 s)",
                          "^[1;94mReflection:^[0m^[94m 1 of 3 ideas proven; nothing to try (9 s)^[0m")
        self.assertStyled("Check: verified: a.txt holds a", "^[1;36mCheck:^[0m^[36m verified: a.txt holds a^[0m")

    def test_details_hide_without_verbose_and_the_rest_stays(self):
        details = ("Challenge\nLooking for reasons", "Challenging as Security", "challenge read_file a.py",
                   "Moderator\nfollow: Quality", "Verifying a claim", "verify read_file a.py", "[VERIFIED] yes",
                   "Strategy\nReflecting: stall", "Reviewing as Diagnosis: x", "Reflection Observe: dropped",
                   "reflect grep_search")
        shown = ("» I will check it.", "Plan check: no objection held up (5 s)", "Reflection: 0 of 3 ideas proven",
                 "Check: verified: a", "read_file a.py", "Skills\nSaved a.md", "Model: qwen", "refl: failed",
                 "Context: 98,000 of 131,072 tokens in use (75%).")
        self.assertTrue(all(style.is_detail(message) for message in details))
        self.assertFalse(any(style.is_detail(message) for message in shown))
        errors = io.StringIO()
        try:
            style.configure("never", details=False)
            with contextlib.redirect_stderr(errors):
                for message in details + shown:
                    _status(message)
        finally:
            style.configure("never")
        self.assertEqual(errors.getvalue(), "".join(f"{message}\n" for message in shown))

    def test_the_color_option_sets_both_streams(self):
        with tempfile.TemporaryDirectory() as directory:
            try:
                self.assertEqual(main(["/exit", "--color", "always", "--workspace", directory]), 0)
                self.assertTrue(style.err.enabled and style.out.enabled)
                self.assertEqual(main(["/exit", "--color", "never", "--workspace", directory]), 0)
                self.assertFalse(style.err.enabled or style.out.enabled)
            finally:
                style.configure("never")


if __name__ == "__main__":
    unittest.main()

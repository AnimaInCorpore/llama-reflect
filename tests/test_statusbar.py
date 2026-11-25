import asyncio
import io
import os
import unittest
from unittest import mock

from refl import style
from refl.statusbar import StatusBar


class Terminal(io.StringIO):
    encoding = "utf-8"


def panel(**options):
    bar = StatusBar(Terminal(), True, **options)
    bar.start()
    return bar


class StatusBarTest(unittest.TestCase):
    def test_the_header_shows_the_phase_counts_tokens_slots_and_time(self):
        bar = panel()
        bar.progress(phase="Plan check", critics=(4, 6), verifiers=(1, 2))
        bar.progress(stream=("Requirements", "thinking", 120, 40.0, "the spec says"))
        bar.progress(stream=("Security", "writing", 60, 30.0, "{\"objection\""))
        bar.observe([{"is_processing": True}, {"is_processing": True}, {"is_processing": False}])
        header = bar.lines(width=200)[0][0]
        # No forecast: only what is measured, this task's tokens and speed, and the busy slots.
        self.assertRegex(header, r"^◆ Plan check 0:00 · critics 4/6 · verifiers 1/2 · 180 tokens, 70 tok/s · "
                                 r"2/3 slots busy · total 0:00$")

    def test_each_actor_gets_a_line_in_the_style_of_its_tone(self):
        bar = panel()
        bar.progress(phase="Plan check")
        bar.progress(actor=("Requirements", "reading SPEC.md", "work"))
        bar.progress(actor=("Security", "nothing found", "quiet"))
        bar.progress(actor=("Requirements", "objection proven", "good"))
        bar.progress(actor=("Moderator", "waiting for proven findings", "wait"))
        rows = bar.lines(width=200)[1:]
        self.assertEqual(rows, [("  ● Requirements  objection proven", "32"), ("  – Security      nothing found", "2"),
                                ("  ○ Moderator     waiting for proven findings", "2")])
        bar.progress(actor=("Edge cases", "checking read_file a.py", "work"))
        self.assertEqual(bar.lines(width=200)[-1], ("  ◐ Edge cases    checking read_file a.py", "3;36"))
        # A new phase starts with an empty panel.
        bar.progress(phase="Next step")
        self.assertEqual(len(bar.lines(width=200)), 1)

    def test_the_panel_fits_the_terminal(self):
        bar = panel()
        bar.progress(phase="Reflection")
        for number in range(20):
            bar.progress(actor=(f"Reviewer {number}", "looking at the stall", "work"))
        rows = bar.lines(width=30, height=8)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(len(text) < 30 for text, _ in rows))
        self.assertTrue(rows[-1][0].startswith("  ◐ Reviewer 19"))

    def test_a_terminal_without_the_symbols_gets_plain_ones(self):
        class Console(io.StringIO):
            encoding = "cp1252"

        bar = StatusBar(Console(), True)
        bar.start()
        bar.progress(actor=("Quality", "proven", "good"))
        self.assertEqual(bar.lines(width=200)[1][0], "  + Quality  proven")
        self.assertTrue(bar.lines(width=200)[0][0].startswith("» Starting"))

    def test_the_panel_keeps_the_bottom_rows_and_output_scrolls_above_it(self):
        size = os.terminal_size((100, 24))
        with mock.patch("refl.statusbar.shutil.get_terminal_size", return_value=size):
            bar = panel()
            # One header row: the output scrolls up by one row and keeps rows 1 to 23.
            self.assertTrue(bar.stream.getvalue().startswith("\n\x1b[1A\x1b7\x1b[1;23r\x1b8"))
            bar.progress(actor=("Moderator", "ruling", "work"))
            self.assertIn("\n\x1b[1A\x1b7\x1b[1;22r\x1b8", bar.stream.getvalue())
            bar.stream.seek(0)
            bar.stream.truncate()
            bar.write("bash ls")
            output = bar.stream.getvalue()
            # The line goes to the cursor in the scroll region, and the panel is redrawn at its fixed rows.
            self.assertTrue(output.startswith("bash ls\n\x1b7\x1b[23;1H\x1b[2K◆ Starting"))
            self.assertTrue(output.endswith("\x1b[24;1H\x1b[2K  ◐ Moderator  ruling\x1b8"))
            # Fewer rows give the freed one back to the output, empty.
            bar.progress(phase="Next step")
            self.assertIn("\x1b7\x1b[1;23r\x1b8\x1b7\x1b[23;1H\x1b[2K\x1b8", bar.stream.getvalue())
            bar.stop()
            self.assertTrue(bar.stream.getvalue().endswith("\x1b7\x1b[24;1H\x1b[2K\x1b[r\x1b8"))
            self.assertEqual(bar.shown, 0)

    def test_a_disabled_panel_writes_nothing_but_lines(self):
        bar = StatusBar(Terminal(), False)
        bar.start()
        bar.progress(actor=("Security", "reading", "work"))
        bar.write("read_file a.txt")
        self.assertEqual(bar.stream.getvalue(), "read_file a.txt\n")

    def test_each_line_shows_its_reply_streaming_in(self):
        bar = panel()
        bar.progress(phase="Plan check")
        bar.progress(actor=("Edge cases", "looking at the plan", "work"))
        bar.progress(stream=("Edge cases", "waiting", 0, 0.0, ""))
        self.assertEqual(bar.lines(width=200)[1][0],
                         "  ◐ Edge cases  looking at the plan · waiting for the server")
        bar.progress(stream=("Edge cases", "thinking", 412, 44.4, "a caller passes None, so\nthe split fails"))
        self.assertEqual(bar.lines(width=200)[1][0], "  ◐ Edge cases  looking at the plan · thinking 412 tok, "
                                                     "44 tok/s · …a caller passes None, so the split fails")
        bar.progress(stream=("Edge cases", "done", 530, 44.0, ""))
        self.assertEqual(bar.lines(width=200)[1][0], "  ◐ Edge cases  looking at the plan · 530 tokens")
        # A stream of the main branch gets a line of its own.
        bar.progress(stream=("Main branch", "writing", 12, 0.0, "I will read"))
        self.assertEqual(bar.lines(width=200)[2][0], "  ◐ Main branch  writing 12 tok · …I will read")

    def test_the_task_counts_every_request_and_streams_do_not_redraw(self):
        bar = panel()
        drawn = len(bar.stream.getvalue())
        for tokens in (1, 2, 3):
            bar.progress(stream=("Moderator", "thinking", tokens, 10.0, "x"))
        self.assertEqual(len(bar.stream.getvalue()), drawn)
        bar.progress(stream=("Moderator", "done", 3, 10.0, ""))
        # The moderator's next request starts at zero, and the task's total keeps counting.
        bar.progress(stream=("Moderator", "waiting", 0, 0.0, ""))
        bar.progress(stream=("Moderator", "writing", 5, 10.0, "y"))
        self.assertEqual(bar.tokens, 8)
        self.assertEqual(bar.rate(), 10.0)

    def test_polling_survives_a_server_without_slots(self):
        bar = panel()

        async def fetch():
            raise OSError("no /slots")

        async def run():
            task = asyncio.create_task(bar.poll(fetch, interval=0.01))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        self.assertIn("Starting", bar.stream.getvalue())

    def test_styled_panels_use_the_tone_codes(self):
        bar = panel(styled=True)
        bar.progress(actor=("Security", "hole proven", "bad"))
        self.assertIn("\x1b[31m  ✗ Security  hole proven\x1b[0m", bar.stream.getvalue())
        self.assertIn("\x1b[1;7m◆ Starting", bar.stream.getvalue())

    def test_thinking_is_cut_to_its_last_lines(self):
        message = "Thinking\n" + "\n".join(f"step {number}" for number in range(1, 10))
        self.assertEqual(style.brief(message), "Thinking\n... 3 earlier lines, --verbose shows all\n"
                                               + "\n".join(f"step {number}" for number in range(4, 10)))
        self.assertEqual(style.brief("Thinking\nshort"), "Thinking\nshort")
        self.assertEqual(style.brief("read_file a.txt"), "read_file a.txt")
        with mock.patch.object(style, "THINKING_LINES", 2):
            self.assertTrue(style.brief(message, lines=2).endswith("step 8\nstep 9"))


if __name__ == "__main__":
    unittest.main()

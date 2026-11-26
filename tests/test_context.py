import json
import unittest

from refl.context import shorten, shorten_point, without_old_thinking


def tool(content):
    return {"role": "tool", "tool_call_id": "a", "content": json.dumps(content)}


class ContextTest(unittest.TestCase):
    def test_only_the_latest_thinking_is_kept(self):
        messages = [{"role": "system", "content": "s"},
                    {"role": "assistant", "content": "a", "reasoning_content": "one"},
                    {"role": "assistant", "content": "b", "reasoning_content": "two"}]
        self.assertEqual([message.get("reasoning_content") for message in without_old_thinking(messages)],
                         [None, None, "two"])
        self.assertEqual(messages[1]["reasoning_content"], "one")  # the input stays as it is

    def test_shortening_cuts_long_output_and_arguments_and_leaves_out_images(self):
        write = {"role": "assistant", "content": "Writing.", "tool_calls": [{
            "id": "a", "type": "function",
            "function": {"name": "write_file", "arguments": json.dumps({"path": "a.js", "content": "x" * 5000})}}]}
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u" * 5000}, write,
                    tool({"output": "y" * 5000}),
                    tool({"output": "Showing a.png. The image follows.", "image": "data:image/png;base64,AA"}),
                    tool({"output": "z" * 5000})]
        shortened = shorten(messages, 5)
        self.assertEqual(shortened[:2], messages[:2])
        arguments = json.loads(shortened[2]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(arguments["path"], "a.js")
        self.assertIn("4,600 more characters left out to save context. Read the file", arguments["content"])
        self.assertIn("4,300 characters left out to save context", json.loads(shortened[3]["content"])["output"])
        image = json.loads(shortened[4]["content"])
        self.assertNotIn("image", image)
        self.assertIn("The image is left out to save context.", image["output"])
        self.assertEqual(shortened[5], messages[5])

    def test_the_latest_messages_stay_whole(self):
        self.assertEqual(shorten_point(40), 28)
        self.assertEqual(shorten_point(5), 1)


if __name__ == "__main__":
    unittest.main()

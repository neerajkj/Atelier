import json
import os
import unittest
from atelier.main import execute_tool, MockClient

class DummyToolCall:
    def __init__(self, name, arguments):
        self.function = type("Func", (), {"name": name, "arguments": json.dumps(arguments)})()

class TestAtelierTools(unittest.TestCase):
    def test_tool_write_and_read(self):
        test_file = "test_scratch.tmp"
        try:
            write_call = DummyToolCall("Write", {"file_path": test_file, "content": "Testing Atelier Mock"})
            write_res = execute_tool(write_call)
            self.assertEqual(write_res, "")

            read_call = DummyToolCall("Read", {"file_path": test_file})
            read_res = execute_tool(read_call)
            self.assertEqual(read_res, "Testing Atelier Mock")
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)

    def test_tool_read_nonexistent(self):
        call = DummyToolCall("Read", {"file_path": "non_existent_file_123.tmp"})
        res = execute_tool(call)
        self.assertIn("Error reading file", res)

    def test_tool_bash(self):
        call = DummyToolCall("Bash", {"command": "echo 'Atelier Test'"})
        res = execute_tool(call)
        self.assertIn("Atelier Test", res)

    def test_mock_client_completion(self):
        client = MockClient()
        resp = client.chat.completions.create(messages=[{"role": "user", "content": "read pyproject"}])
        self.assertEqual(len(resp.choices), 1)
        self.assertTrue(resp.choices[0].message.tool_calls is not None)
        self.assertEqual(resp.choices[0].message.tool_calls[0].function.name, "Read")

if __name__ == "__main__":
    unittest.main()

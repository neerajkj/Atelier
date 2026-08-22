import json
import os
import shutil
import tempfile
import unittest
from atelier.main import (
    execute_tool,
    resolve_safe_path,
    MockClient,
    SessionState,
    dispatch_slash_command,
    COMMAND_REGISTRY,
    register_command,
)


class DummyToolCall:
    def __init__(self, name, arguments):
        self.function = type("Func", (), {"name": name, "arguments": json.dumps(arguments)})()


class TestAtelierSandbox(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="atelier_test_")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_resolve_safe_path_valid(self):
        resolved = resolve_safe_path("sub/file.py", self.test_dir)
        expected = os.path.abspath(os.path.join(self.test_dir, "sub/file.py"))
        self.assertEqual(resolved, expected)

    def test_resolve_safe_path_escape_rejected(self):
        with self.assertRaises(PermissionError):
            resolve_safe_path("../../etc/passwd", self.test_dir)

        with self.assertRaises(PermissionError):
            resolve_safe_path("/etc/passwd", self.test_dir)

    def test_tool_write_and_read_sandboxed(self):
        write_call = DummyToolCall("Write", {"file_path": "test_script.py", "content": "print('hello from sandbox')"})
        write_res = execute_tool(write_call, workdir=self.test_dir)
        self.assertEqual(write_res, "")

        # Verify file exists inside sandbox
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, "test_script.py")))

        # Read file through sandbox
        read_call = DummyToolCall("Read", {"file_path": "test_script.py"})
        read_res = execute_tool(read_call, workdir=self.test_dir)
        self.assertEqual(read_res, "print('hello from sandbox')")

    def test_tool_write_escape_blocked(self):
        write_call = DummyToolCall("Write", {"file_path": "../outside.txt", "content": "escape attempt"})
        write_res = execute_tool(write_call, workdir=self.test_dir)
        self.assertIn("Security Error", write_res)
        self.assertFalse(os.path.exists(os.path.join(self.test_dir, "../outside.txt")))

    def test_tool_read_escape_blocked(self):
        read_call = DummyToolCall("Read", {"file_path": "../../etc/passwd"})
        read_res = execute_tool(read_call, workdir=self.test_dir)
        self.assertIn("Security Error", read_res)

    def test_tool_bash_cwd_confinement(self):
        call = DummyToolCall("Bash", {"command": "pwd"})
        res = execute_tool(call, workdir=self.test_dir, auto_approve=True)
        # Verify pwd output matches real test_dir
        self.assertEqual(os.path.realpath(res.strip()), os.path.realpath(self.test_dir))

    def test_tool_write_overwrite_permission_denied(self):
        # Create initial file
        target = os.path.join(self.test_dir, "existing.txt")
        with open(target, "w") as f:
            f.write("original content")

        write_call = DummyToolCall("Write", {"file_path": "existing.txt", "content": "overwritten content"})
        
        # Simulate user typing 'n' to deny permission
        from unittest.mock import patch
        with patch("builtins.input", return_value="n"):
            res = execute_tool(write_call, workdir=self.test_dir, auto_approve=False)

        self.assertIn("Permission Denied", res)
        # Verify content was NOT overwritten
        with open(target, "r") as f:
            self.assertEqual(f.read(), "original content")

    def test_tool_write_overwrite_approved(self):
        target = os.path.join(self.test_dir, "existing.txt")
        with open(target, "w") as f:
            f.write("original content")

        write_call = DummyToolCall("Write", {"file_path": "existing.txt", "content": "overwritten content"})
        res = execute_tool(write_call, workdir=self.test_dir, auto_approve=True)
        self.assertEqual(write_res := res, "")
        with open(target, "r") as f:
            self.assertEqual(f.read(), "overwritten content")

    def test_tool_bash_permission_denied(self):
        call = DummyToolCall("Bash", {"command": "echo 'should not run'"})
        from unittest.mock import patch
        with patch("builtins.input", return_value="n"):
            res = execute_tool(call, workdir=self.test_dir, auto_approve=False)

        self.assertIn("Permission Denied", res)

    def test_tool_bash_permission_approved(self):
        call = DummyToolCall("Bash", {"command": "echo 'allowed run'"})
        res = execute_tool(call, workdir=self.test_dir, auto_approve=True)
        self.assertIn("allowed run", res)

    def test_mock_client_completion(self):
        client = MockClient()
        resp = client.chat.completions.create(messages=[{"role": "user", "content": "read pyproject"}])
        self.assertEqual(len(resp.choices), 1)
        self.assertTrue(resp.choices[0].message.tool_calls is not None)
        self.assertEqual(resp.choices[0].message.tool_calls[0].function.name, "Read")

    def test_slash_command_exit(self):
        state = SessionState(model="test-m", provider="mock", workdir=self.test_dir, context_window=32768)
        handled = dispatch_slash_command("/exit", state)
        self.assertTrue(handled)
        self.assertTrue(state.should_exit)

    def test_slash_command_model_switch(self):
        state = SessionState(model="qwen2.5-coder:7b", provider="local", workdir=self.test_dir, context_window=32768)
        handled = dispatch_slash_command("/model claude-3-5-sonnet", state)
        self.assertTrue(handled)
        self.assertEqual(state.model, "claude-3-5-sonnet")
        self.assertEqual(state.context_window, 200000)

    def test_slash_command_cd(self):
        sub_dir = os.path.join(self.test_dir, "sub_folder")
        os.makedirs(sub_dir, exist_ok=True)
        state = SessionState(model="test-m", provider="mock", workdir=self.test_dir, context_window=32768)
        handled = dispatch_slash_command(f"/cd {sub_dir}", state)
        self.assertTrue(handled)
        self.assertEqual(state.workdir, sub_dir)

    def test_slash_command_clear(self):
        state = SessionState(
            model="test-m",
            provider="mock",
            workdir=self.test_dir,
            context_window=32768,
            messages=[
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
            last_prompt_tokens=500,
        )
        handled = dispatch_slash_command("/clear", state)
        self.assertTrue(handled)
        self.assertEqual(len(state.messages), 1)
        self.assertEqual(state.messages[0]["role"], "system")
        self.assertEqual(state.last_prompt_tokens, 0)

    def test_slash_command_approve_toggle(self):
        state = SessionState(model="test-m", provider="mock", workdir=self.test_dir, context_window=32768, auto_approve=False)
        handled = dispatch_slash_command("/approve", state)
        self.assertTrue(handled)
        self.assertTrue(state.auto_approve)

    def test_slash_command_custom_extensibility(self):
        @register_command("/custom_ping", "Custom ping test command")
        def cmd_ping(state: SessionState, args: str):
            state.model = "pinged"

        state = SessionState(model="original", provider="mock", workdir=self.test_dir, context_window=32768)
        handled = dispatch_slash_command("/custom_ping", state)
        self.assertTrue(handled)
        self.assertEqual(state.model, "pinged")

    def test_slash_command_non_slash_input(self):
        state = SessionState(model="test-m", provider="mock", workdir=self.test_dir, context_window=32768)
        handled = dispatch_slash_command("Read pyproject.toml", state)
        self.assertFalse(handled)

    def test_mock_client_streaming_text(self):
        client = MockClient()
        chunks = list(client.chat.completions.create(messages=[{"role": "user", "content": "hello"}], stream=True))
        self.assertGreater(len(chunks), 1)
        text_streamed = "".join([c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content])
        self.assertIn("Echo from Zero-Model Mock", text_streamed)
        # Verify usage in final chunk
        final_chunk = chunks[-1]
        self.assertIsNotNone(final_chunk.usage)
        self.assertEqual(final_chunk.usage.prompt_tokens, 110)

    def test_mock_client_streaming_tool_call(self):
        client = MockClient()
        chunks = list(client.chat.completions.create(messages=[{"role": "user", "content": "read pyproject"}], stream=True))
        tc_chunks = [c for c in chunks if c.choices and c.choices[0].delta.tool_calls]
        self.assertTrue(len(tc_chunks) > 0)
        self.assertEqual(tc_chunks[0].choices[0].delta.tool_calls[0].function.name, "Read")


if __name__ == "__main__":
    unittest.main()

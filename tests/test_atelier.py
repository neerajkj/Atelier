import io
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from atelier.main import (
    execute_tool,
    resolve_safe_path,
    SessionState,
    dispatch_slash_command,
    COMMAND_REGISTRY,
    register_command,
    fetch_local_models,
    switch_model_and_provider,
    create_client,
    prune_tool_outputs,
    compact_context,
    perform_web_search,
)


class DummyToolCall:
    def __init__(self, name, arguments):
        self.function = type("Func", (), {"name": name, "arguments": json.dumps(arguments)})()


class TestAtelierSandbox(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="atelier_test_")
        self.dummy_client = MagicMock()

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
        target = os.path.join(self.test_dir, "existing.txt")
        with open(target, "w") as f:
            f.write("original content")

        write_call = DummyToolCall("Write", {"file_path": "existing.txt", "content": "overwritten content"})
        
        with patch("builtins.input", return_value="n"):
            res = execute_tool(write_call, workdir=self.test_dir, auto_approve=False)

        self.assertIn("Permission Denied", res)
        with open(target, "r") as f:
            self.assertEqual(f.read(), "original content")

    def test_tool_write_overwrite_approved(self):
        target = os.path.join(self.test_dir, "existing.txt")
        with open(target, "w") as f:
            f.write("original content")

        write_call = DummyToolCall("Write", {"file_path": "existing.txt", "content": "overwritten content"})
        res = execute_tool(write_call, workdir=self.test_dir, auto_approve=True)
        self.assertEqual(res, "")
        with open(target, "r") as f:
            self.assertEqual(f.read(), "overwritten content")

    def test_tool_bash_permission_denied(self):
        call = DummyToolCall("Bash", {"command": "echo 'should not run'"})
        with patch("builtins.input", return_value="n"):
            res = execute_tool(call, workdir=self.test_dir, auto_approve=False)

        self.assertIn("Permission Denied", res)

    def test_tool_bash_permission_approved(self):
        call = DummyToolCall("Bash", {"command": "echo 'allowed run'"})
        res = execute_tool(call, workdir=self.test_dir, auto_approve=True)
        self.assertIn("allowed run", res)

    def test_slash_command_exit(self):
        state = SessionState(client=self.dummy_client, model="qwen2.5-coder:7b", provider="local", workdir=self.test_dir, context_window=32768)
        handled = dispatch_slash_command("/exit", state)
        self.assertTrue(handled)
        self.assertTrue(state.should_exit)

    def test_slash_command_model_switch_direct(self):
        state = SessionState(client=self.dummy_client, model="qwen2.5-coder:7b", provider="local", workdir=self.test_dir, context_window=32768)
        handled = dispatch_slash_command("/model claude-3-5-sonnet", state)
        self.assertTrue(handled)
        self.assertEqual(state.model, "claude-3-5-sonnet")
        self.assertEqual(state.context_window, 200000)

    def test_slash_command_model_local_switch(self):
        state = SessionState(client=self.dummy_client, model="claude-3-5-sonnet", provider="cloud", workdir=self.test_dir, context_window=200000)
        handled = dispatch_slash_command("/model local qwen2.5-coder:14b", state)
        self.assertTrue(handled)
        self.assertEqual(state.provider, "local")
        self.assertEqual(state.model, "qwen2.5-coder:14b")
        self.assertEqual(state.context_window, 32768)

    def test_slash_command_model_cloud_switch(self):
        state = SessionState(
            client=self.dummy_client,
            model="qwen2.5-coder:7b",
            provider="local",
            workdir=self.test_dir,
            context_window=32768,
            api_key="test-api-key"
        )
        handled = dispatch_slash_command("/model cloud anthropic/claude-3.5-sonnet", state)
        self.assertTrue(handled)
        self.assertEqual(state.provider, "cloud")
        self.assertEqual(state.model, "anthropic/claude-3.5-sonnet")
        self.assertEqual(state.context_window, 200000)

    def test_fetch_local_models_mocked(self):
        sample_response = json.dumps({
            "models": [
                {"name": "qwen2.5-coder:7b", "size": 4700000000, "modified_at": "2026-08-20T10:00:00Z"},
                {"name": "deepseek-r1:8b", "size": 5200000000, "modified_at": "2026-08-21T10:00:00Z"},
            ]
        }).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = sample_response
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            models = fetch_local_models()
            self.assertEqual(len(models), 2)
            self.assertEqual(models[0]["name"], "qwen2.5-coder:7b")
            self.assertEqual(models[1]["name"], "deepseek-r1:8b")

    def test_slash_command_models_discovery(self):
        state = SessionState(client=self.dummy_client, model="qwen2.5-coder:7b", provider="local", workdir=self.test_dir, context_window=32768)
        handled = dispatch_slash_command("/models", state)
        self.assertTrue(handled)

    def test_slash_command_cd(self):
        sub_dir = os.path.join(self.test_dir, "sub_folder")
        os.makedirs(sub_dir, exist_ok=True)
        state = SessionState(client=self.dummy_client, model="qwen2.5-coder:7b", provider="local", workdir=self.test_dir, context_window=32768)
        handled = dispatch_slash_command(f"/cd {sub_dir}", state)
        self.assertTrue(handled)
        self.assertEqual(state.workdir, sub_dir)

    def test_slash_command_clear(self):
        state = SessionState(
            client=self.dummy_client,
            model="qwen2.5-coder:7b",
            provider="local",
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
        state = SessionState(client=self.dummy_client, model="qwen2.5-coder:7b", provider="local", workdir=self.test_dir, context_window=32768, auto_approve=False)
        handled = dispatch_slash_command("/approve", state)
        self.assertTrue(handled)
        self.assertTrue(state.auto_approve)

    def test_slash_command_custom_extensibility(self):
        @register_command("/custom_ping", "Custom ping test command")
        def cmd_ping(state: SessionState, args: str):
            state.model = "pinged"

        state = SessionState(client=self.dummy_client, model="original", provider="local", workdir=self.test_dir, context_window=32768)
        handled = dispatch_slash_command("/custom_ping", state)
        self.assertTrue(handled)
        self.assertEqual(state.model, "pinged")

    def test_slash_command_non_slash_input(self):
        state = SessionState(client=self.dummy_client, model="qwen2.5-coder:7b", provider="local", workdir=self.test_dir, context_window=32768)
        handled = dispatch_slash_command("Read pyproject.toml", state)
        self.assertFalse(handled)

    def test_create_client_local(self):
        client = create_client("local")
        self.assertIsNotNone(client)

    def test_create_client_cloud_without_key_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                create_client("cloud")

    def test_prune_tool_outputs_micro_pruning(self):
        large_tool_output = "\n".join([f"Line {i}: some verbose log output" for i in range(100)])
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "run tests"},
            {"role": "tool", "tool_call_id": "call_1", "content": large_tool_output},
            {"role": "user", "content": "recent prompt 1"},
            {"role": "assistant", "content": "recent answer 1"},
            {"role": "user", "content": "recent prompt 2"},
            {"role": "assistant", "content": "recent answer 2"},
        ]
        pruned = prune_tool_outputs(messages, preserve_last_n=4, max_lines=35)
        self.assertEqual(pruned, 1)
        tool_content = messages[2]["content"]
        self.assertIn("Output truncated", tool_content)
        self.assertIn("Line 0:", tool_content)
        self.assertIn("Line 99:", tool_content)

    def test_compact_context_successful(self):
        # Create mock completion response for compaction
        mock_choice = MagicMock()
        mock_choice.message.content = "• Goal: Refactor app\n• Files modified: src/main.py\n• Tests passing."
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]

        client = MagicMock()
        client.chat.completions.create.return_value = mock_resp

        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "old task 1"},
            {"role": "assistant", "content": "old answer 1"},
            {"role": "user", "content": "old task 2"},
            {"role": "assistant", "content": "old answer 2"},
            {"role": "user", "content": "recent task 1"},
            {"role": "assistant", "content": "recent answer 1"},
            {"role": "user", "content": "recent task 2"},
            {"role": "assistant", "content": "recent answer 2"},
        ]
        state = SessionState(
            client=client,
            model="qwen2.5-coder:7b",
            provider="local",
            workdir=self.test_dir,
            context_window=32768,
            messages=messages,
            last_prompt_tokens=26000,
        )

        success = compact_context(state, hot_zone_turns=2)
        self.assertTrue(success)
        # System + Summary User + Assistant Ack + 4 Hot Zone messages = 7 messages
        self.assertEqual(len(state.messages), 7)
        self.assertEqual(state.messages[0]["role"], "system")
        self.assertIn("Summary of previous session context", state.messages[1]["content"])
        self.assertEqual(state.messages[2]["role"], "assistant")
        self.assertEqual(state.messages[-1]["content"], "recent answer 2")
        self.assertLess(state.last_prompt_tokens, 26000)

    def test_slash_command_compact(self):
        mock_choice = MagicMock()
        mock_choice.message.content = "Briefing summary."
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]

        client = MagicMock()
        client.chat.completions.create.return_value = mock_resp

        state = SessionState(
            client=client,
            model="qwen2.5-coder:7b",
            provider="local",
            workdir=self.test_dir,
            context_window=32768,
            messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "step 1"},
                {"role": "assistant", "content": "done 1"},
                {"role": "user", "content": "step 2"},
                {"role": "assistant", "content": "done 2"},
            ],
            last_prompt_tokens=1000,
        )
        handled = dispatch_slash_command("/compact", state)
        self.assertTrue(handled)
        self.assertTrue(any("Summary of previous session context" in str(m.get("content", "")) for m in state.messages if isinstance(m, dict)))

    def test_tool_web_search_empty_query(self):
        res = perform_web_search("   ")
        self.assertIn("Error", res)

    def test_tool_web_search_duckduckgo_mocked(self):
        sample_html = """
        <html>
          <body>
            <div class="result">
              <a class="result__title" href="#">FastAPI Lifespan Events</a>
              <a class="result__snippet" href="#">Learn how to use lifespan events with asynccontextmanager in FastAPI.</a>
              <a class="result__url" href="https://duckduckgo.com/l/?kh=-1&uddg=https%3A%2F%2Ffastapi.tiangolo.com%2Flifespan">fastapi.tiangolo.com</a>
            </div>
          </body>
        </html>
        """
        mock_resp = MagicMock()
        mock_resp.read.return_value = sample_html.encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch.dict(os.environ, {}, clear=True):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                res = perform_web_search("FastAPI lifespan")
                self.assertIn("FastAPI Lifespan Events", res)
                self.assertIn("https://fastapi.tiangolo.com/lifespan", res)
                self.assertIn("asynccontextmanager", res)

    def test_tool_web_search_tavily_mocked(self):
        sample_tavily_json = json.dumps({
            "results": [
                {
                    "title": "Pydantic V2 Migration Guide",
                    "url": "https://docs.pydantic.dev/2.0/migration/",
                    "content": "Use model_validate instead of parse_obj in Pydantic V2.",
                }
            ]
        }).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.read.return_value = sample_tavily_json
        mock_resp.__enter__.return_value = mock_resp

        with patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test-123"}, clear=True):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                res = perform_web_search("Pydantic v2 migration")
                self.assertIn("Pydantic V2 Migration Guide", res)
                self.assertIn("model_validate", res)

    def test_execute_tool_web_search(self):
        call = DummyToolCall("WebSearch", {"query": "python dataclass field", "max_results": 2})
        with patch("atelier.main.perform_web_search", return_value="[1] Python Dataclass Reference"):
            res = execute_tool(call, workdir=self.test_dir)
            self.assertIn("Python Dataclass Reference", res)


if __name__ == "__main__":
    unittest.main()

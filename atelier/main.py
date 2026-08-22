import argparse
import json
import os
import re
import subprocess
import sys

from openai import OpenAI

if os.path.exists(".env"):
    with open(".env", "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v

API_KEY = os.getenv("OPENROUTER_API_KEY")
BASE_URL = os.getenv("OPENROUTER_BASE_URL", default="https://openrouter.ai/api/v1")


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[90m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    BOLD_CYAN = "\033[1;36m"
    BOLD_GREEN = "\033[1;32m"
    BOLD_YELLOW = "\033[1;33m"
    BOLD_MAGENTA = "\033[1;35m"
    BOLD_BLUE = "\033[1;34m"


def render_statusbar(model, provider, prompt_tokens, context_window, session_tokens):
    import shutil
    try:
        cols = shutil.get_terminal_size((80, 20)).columns
    except Exception:
        cols = 80

    if context_window > 0 and prompt_tokens > 0:
        pct = (prompt_tokens / context_window) * 100
        filled = int(round(pct / 10))
        filled = min(max(filled, 0), 10)
        bar = "█" * filled + "░" * (10 - filled)
        if pct < 50:
            meter_color = C.BOLD_GREEN
        elif pct < 80:
            meter_color = C.BOLD_YELLOW
        else:
            meter_color = C.RED
        context_str = f"Context: {prompt_tokens:,}/{context_window:,} {meter_color}[{bar}] {pct:.1f}%{C.RESET}"
    else:
        context_str = f"Context: 0/{context_window:,} {C.DIM}[░░░░░░░░░░] 0.0%{C.RESET}"

    if provider == "mock":
        prov_str = f"{C.BOLD_MAGENTA}● Mock{C.RESET}"
    elif provider == "local":
        prov_str = f"{C.BOLD_GREEN}● Local{C.RESET}"
    else:
        prov_str = f"{C.BOLD_BLUE}● Cloud{C.RESET}"

    sep = f"{C.DIM}│{C.RESET}"
    bar_content = f" {C.BOLD_CYAN}🎨 Atelier{C.RESET} {sep} {prov_str} {C.BOLD}{model}{C.RESET} {sep} {context_str} {sep} {C.DIM}Σ Session:{C.RESET} {C.BOLD}{session_tokens:,}{C.RESET}"
    div_width = min(max(cols - 2, 40), 96)
    divider = f"{C.DIM}{'─' * div_width}{C.RESET}"
    print(f"\n{divider}\n{bar_content}\n{divider}", flush=True)


def execute_tool(tool_call):
    function_name = tool_call.function.name
    try:
        arguments = json.loads(tool_call.function.arguments)
    except Exception as e:
        return f"Error parsing arguments: {e}"

    if function_name == "Read":
        file_path = arguments.get("file_path", "")
        print(f"{C.BLUE}[Tool: Read]{C.RESET} Reading file: {C.BOLD}'{file_path}'{C.RESET}")
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            print(f"{C.BLUE}[Tool: Read]{C.RESET} Successfully read {len(content):,} characters from '{file_path}'")
            return content
        except Exception as e:
            print(f"{C.RED}[Tool: Read]{C.RESET} Error reading '{file_path}': {e}")
            return f"Error reading file '{file_path}': {e}"
    elif function_name == "Write":
        file_path = arguments.get("file_path", "")
        content = arguments.get("content", "")
        print(f"{C.GREEN}[Tool: Write]{C.RESET} Writing file: {C.BOLD}'{file_path}'{C.RESET} ({len(content):,} characters)")
        try:
            dir_name = os.path.dirname(file_path)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"{C.GREEN}[Tool: Write]{C.RESET} Successfully wrote '{file_path}'")
            return ""
        except Exception as e:
            print(f"{C.RED}[Tool: Write]{C.RESET} Error writing '{file_path}': {e}")
            return f"Error writing file '{file_path}': {e}"
    elif function_name == "Bash":
        command = arguments.get("command", "")
        print(f"{C.YELLOW}[Tool: Bash]{C.RESET} Running command: {C.BOLD}'{command}'{C.RESET}")
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            print(f"{C.YELLOW}[Tool: Bash]{C.RESET} Command completed with exit code {result.returncode}")
            return result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            print(f"{C.RED}[Tool: Bash]{C.RESET} Command timed out after 60 seconds")
            return "Error: Command timed out after 60 seconds."
        except Exception as e:
            print(f"{C.RED}[Tool: Bash]{C.RESET} Command failed: {e}")
            return str(e)
    return ""


class MockChatChoice:
    def __init__(self, message):
        self.message = message


class MockUsage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens


class MockChatCompletion:
    def __init__(self, message, prompt_tokens=150, completion_tokens=45):
        self.choices = [MockChatChoice(message)]
        self.usage = MockUsage(prompt_tokens, completion_tokens)


class MockClient:
    class Chat:
        class Completions:
            def create(self, **params):
                messages = params.get("messages", [])
                last_msg = messages[-1] if messages else {}
                from openai.types.chat.chat_completion_message import ChatCompletionMessage
                from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall, Function

                # If last message was a tool result, return final explanation
                if isinstance(last_msg, dict) and last_msg.get("role") == "tool":
                    tool_content = str(last_msg.get("content", ""))
                    snippet = (tool_content[:80] + "...") if len(tool_content) > 80 else tool_content
                    msg = ChatCompletionMessage(
                        role="assistant",
                        content=f"Successfully executed tool in mock mode. Output summary: '{snippet}'",
                    )
                    return MockChatCompletion(msg, prompt_tokens=210, completion_tokens=30)

                # Otherwise inspect user message
                user_content = ""
                for m in reversed(messages):
                    if isinstance(m, dict) and m.get("role") == "user":
                        user_content = m.get("content", "")
                        break

                if "write" in user_content.lower():
                    tool_calls = [
                        ChatCompletionMessageToolCall(
                            id="mock_call_write_1",
                            type="function",
                            function=Function(name="Write", arguments=json.dumps({"file_path": "mock_sample.txt", "content": "Hello from Atelier Mock Mode!"}))
                        )
                    ]
                    msg = ChatCompletionMessage(role="assistant", content=None, tool_calls=tool_calls)
                    return MockChatCompletion(msg, prompt_tokens=140, completion_tokens=40)
                elif "bash" in user_content.lower() or "command" in user_content.lower():
                    tool_calls = [
                        ChatCompletionMessageToolCall(
                            id="mock_call_bash_1",
                            type="function",
                            function=Function(name="Bash", arguments=json.dumps({"command": "echo 'Mock command executed successfully'"}))
                        )
                    ]
                    msg = ChatCompletionMessage(role="assistant", content=None, tool_calls=tool_calls)
                    return MockChatCompletion(msg, prompt_tokens=140, completion_tokens=40)
                elif "read" in user_content.lower() or "pyproject" in user_content.lower():
                    tool_calls = [
                        ChatCompletionMessageToolCall(
                            id="mock_call_read_1",
                            type="function",
                            function=Function(name="Read", arguments=json.dumps({"file_path": "pyproject.toml"}))
                        )
                    ]
                    msg = ChatCompletionMessage(role="assistant", content=None, tool_calls=tool_calls)
                    return MockChatCompletion(msg, prompt_tokens=140, completion_tokens=40)
                else:
                    msg = ChatCompletionMessage(
                        role="assistant",
                        content=f"Echo from Zero-Model Mock: Received '{user_content}'. All agent systems are operational!",
                    )
                    return MockChatCompletion(msg, prompt_tokens=110, completion_tokens=25)

        def __init__(self):
            self.completions = self.Completions()

    def __init__(self):
        self.chat = self.Chat()


def main():
    p = argparse.ArgumentParser(description="Atelier — Minimalist AI Coding Harness")
    p.add_argument("-p", required=False, help="Initial prompt")
    p.add_argument("--mock", "--dry-run", dest="mock", action="store_true", help="Run in zero-model mock mode for instant testing without API or GPU")
    p.add_argument("--local", "--ollama", dest="local", action="store_true", help="Use local Ollama instead of OpenRouter")
    p.add_argument(
        "-m",
        "--model",
        required=False,
        help="Model name (default: qwen2.5-coder:7b for local, liquid/lfm-2.5-2.6b:free for OpenRouter)",
    )
    p.add_argument("--base-url", required=False, help="Custom API Base URL")
    p.add_argument(
        "--max-tokens",
        type=int,
        required=False,
        default=int(os.getenv("MAX_TOKENS", "1024")),
        help="Maximum number of tokens to generate per response (default: 1024)",
    )
    p.add_argument(
        "--context-window",
        type=int,
        required=False,
        help="Maximum context window of the model in tokens (default: auto-detected or 32768 for local / 128000 for cloud)",
    )
    args = p.parse_args()

    if args.mock:
        client = MockClient()
        model = args.model or "mock-model"
        context_window = args.context_window or 32768
    elif args.local:
        base_url = args.base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        api_key = os.getenv("OLLAMA_API_KEY", "ollama")
        model = args.model or "qwen2.5-coder:7b"
        client = OpenAI(api_key=api_key, base_url=base_url)
    else:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set. Use --local or --ollama to use local Ollama models without an API key."
            )
        base_url = args.base_url or os.getenv("OPENROUTER_BASE_URL", default="https://openrouter.ai/api/v1")
        model = args.model or "liquid/lfm-2.5-2.6b:free"
        client = OpenAI(api_key=api_key, base_url=base_url)

    if not args.mock:
        context_window = args.context_window
        if not context_window:
            if "32k" in model or "qwen2.5-coder:7b" in model:
                context_window = 32768
            elif "qwen3:8b" in model:
                context_window = 40960
            elif "claude" in model:
                context_window = 200000
            elif "gemini" in model:
                context_window = 1000000
            elif args.local:
                context_window = 32768
            else:
                context_window = 128000

    tools = [{
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read and return the contents of a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The path to the file to read"
                    }
                },
                "required": ["file_path"]
            }
        }
    },
    {
    "type": "function",
    "function": {
        "name": "Write",
        "description": "Write content to a file",
        "parameters": {
            "type": "object",
            "required": ["file_path", "content"],
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The path of the file to write to"
                    },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file"
                    }
                }
            }
        }
    },
    {
  "type": "function",
  "function": {
    "name": "Bash",
    "description": "Execute a shell command",
    "parameters": {
      "type": "object",
      "required": ["command"],
      "properties": {
        "command": {
          "type": "string",
          "description": "The command to execute"
        }
      }
    }
  }
}
    ]

    messages = []
    initial_prompt = args.p
    session_prompt_tokens = 0
    session_completion_tokens = 0
    last_prompt_tokens = 0

    if args.mock:
        provider_badge = f"{C.BOLD_MAGENTA}Mock (Zero-Model){C.RESET}"
    elif args.local:
        provider_badge = f"{C.BOLD_GREEN}Local (Ollama){C.RESET}"
    else:
        provider_badge = f"{C.BOLD_BLUE}Cloud (OpenRouter){C.RESET}"

    print(f"\n🎨 {C.BOLD_CYAN}Atelier{C.RESET} — AI Coding Harness [{provider_badge} | {C.BOLD}{model}{C.RESET}]", flush=True)
    print(f"{C.DIM}Type your prompt or 'exit' to quit.{C.RESET}", flush=True)

    while True:
        if initial_prompt:
            user_prompt = initial_prompt
            initial_prompt = None
        else:
            provider_type = "mock" if args.mock else ("local" if args.local else "cloud")
            render_statusbar(
                model=model,
                provider=provider_type,
                prompt_tokens=last_prompt_tokens,
                context_window=context_window,
                session_tokens=session_prompt_tokens + session_completion_tokens,
            )
            try:
                user_prompt = input(f"{C.BOLD_CYAN}atelier ❯{C.RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{C.DIM}Goodbye!{C.RESET}")
                break

            if not user_prompt or user_prompt.lower() in ("exit", "quit", "q"):
                print(f"{C.DIM}Goodbye!{C.RESET}")
                break

        messages.append({"role": "user", "content": user_prompt})

        # Agent Loop: call model and execute tools until final text answer is produced
        try:
            while True:
                params = {
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                }
                if args.max_tokens is not None:
                    params["max_tokens"] = args.max_tokens

                chat = client.chat.completions.create(**params)

                if not chat.choices or len(chat.choices) == 0:
                    raise RuntimeError("no choices in response")

                if chat.usage:
                    last_prompt_tokens = chat.usage.prompt_tokens
                    session_prompt_tokens += chat.usage.prompt_tokens
                    session_completion_tokens += chat.usage.completion_tokens
                    context_pct = (chat.usage.prompt_tokens / context_window) * 100
                    pct_color = C.GREEN if context_pct < 50 else (C.YELLOW if context_pct < 80 else C.RED)
                    print(
                        f"\n{C.DIM}┌─ [Context & Tokens]{C.RESET} "
                        f"Context: {C.BOLD}{chat.usage.prompt_tokens:,}{C.RESET}/{context_window:,} ({pct_color}{context_pct:.2f}%{C.RESET}) | "
                        f"Gen: {C.BOLD}{chat.usage.completion_tokens:,}{C.RESET} | "
                        f"Turn: {chat.usage.total_tokens:,} | "
                        f"Session: {session_prompt_tokens + session_completion_tokens:,}",
                        flush=True
                    )

                response_message = chat.choices[0].message
                tool_calls = response_message.tool_calls
                if not tool_calls and response_message.content:
                    text = response_message.content.strip()
                    start = text.find("{")
                    end = text.rfind("}")
                    if start != -1 and end != -1 and end > start:
                        json_candidate = text[start:end+1]
                        try:
                            parsed = json.loads(json_candidate)
                            if isinstance(parsed, dict) and "name" in parsed and "arguments" in parsed:
                                from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall, Function
                                args_str = json.dumps(parsed["arguments"]) if isinstance(parsed["arguments"], dict) else str(parsed["arguments"])
                                tool_calls = [
                                    ChatCompletionMessageToolCall(
                                        id="call_local_0",
                                        type="function",
                                        function=Function(name=parsed["name"], arguments=args_str)
                                    )
                                ]
                                response_message.tool_calls = tool_calls
                                response_message.content = None
                        except Exception:
                            pass

                messages.append(response_message)

                if not tool_calls:
                    if response_message.content:
                        print(f"\n{C.BOLD_GREEN}🤖 {model}:{C.RESET}\n{response_message.content}", flush=True)
                    break

                print(f"\n{C.BOLD_MAGENTA}[Model Response]{C.RESET} {C.MAGENTA}Requested {len(tool_calls)} tool call(s):{C.RESET}")
                for tool_call in tool_calls:
                    print(f"  • {C.BOLD}Tool:{C.RESET} {C.BOLD_CYAN}{tool_call.function.name}{C.RESET} | {C.DIM}ID: {tool_call.id}{C.RESET} | {C.DIM}Args: {tool_call.function.arguments}{C.RESET}")
                    result = execute_tool(tool_call)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    })
        except KeyboardInterrupt:
            print(f"\n{C.YELLOW}[Interrupted]{C.RESET} Operation cancelled by user.", flush=True)
            # Remove incomplete user prompt from history
            if messages and hasattr(messages[-1], "get") and messages[-1].get("role") == "user":
                messages.pop()
            continue
        except Exception as e:
            print(f"\n{C.RED}[Error]{C.RESET} {e}", flush=True)
            if messages and hasattr(messages[-1], "get") and messages[-1].get("role") == "user":
                messages.pop()
            continue


if __name__ == "__main__":
    main()

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

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


def format_display_path(path: str) -> str:
    home = os.path.expanduser("~")
    abs_path = os.path.abspath(path)
    if abs_path == home:
        return "~"
    elif abs_path.startswith(home + os.sep):
        return "~/" + os.path.relpath(abs_path, home)
    return abs_path


def resolve_safe_path(requested_path: str, workdir: str) -> str:
    if not requested_path:
        raise ValueError("File path cannot be empty.")

    base_dir = os.path.abspath(os.path.expanduser(workdir))
    expanded_path = os.path.expanduser(requested_path)

    if not os.path.isabs(expanded_path):
        target_path = os.path.abspath(os.path.join(base_dir, expanded_path))
    else:
        target_path = os.path.abspath(expanded_path)

    try:
        common = os.path.commonpath([base_dir, target_path])
        if common != base_dir:
            raise PermissionError(
                f"Access denied: '{requested_path}' escapes the working directory '{base_dir}'."
            )
    except ValueError:
        raise PermissionError(
            f"Access denied: '{requested_path}' is on a different drive or invalid path relative to '{base_dir}'."
        )

    return target_path


def render_statusbar(model, provider, prompt_tokens, context_window, session_tokens, workdir=None):
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
    dir_str = f" {sep} {C.DIM}📁{C.RESET} {C.BOLD}{format_display_path(workdir)}{C.RESET}" if workdir else ""
    bar_content = f" {C.BOLD_CYAN}🎨 Atelier{C.RESET} {sep} {prov_str} {C.BOLD}{model}{C.RESET}{dir_str} {sep} {context_str} {sep} {C.DIM}Σ Session:{C.RESET} {C.BOLD}{session_tokens:,}{C.RESET}"
    div_width = min(max(cols - 2, 40), 104)
    divider = f"{C.DIM}{'─' * div_width}{C.RESET}"
    print(f"\n{divider}\n{bar_content}\n{divider}", flush=True)


def ask_permission(action_type: str, details: list[tuple[str, str]], auto_approve: bool = False) -> bool:
    if auto_approve:
        return True

    print(f"\n{C.BOLD_YELLOW}⚠️  [Permission Required] {action_type}{C.RESET}")
    for label, val in details:
        print(f"   {C.DIM}{label}:{C.RESET} {C.BOLD}{val}{C.RESET}")

    try:
        choice = input(f"{C.BOLD_YELLOW}Allow action? [y/N]:{C.RESET} ").strip().lower()
        allowed = choice in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        allowed = False

    if not allowed:
        print(f"{C.RED}✗ Action rejected by user.{C.RESET}\n", flush=True)
    else:
        print(f"{C.GREEN}✓ Approved.{C.RESET}\n", flush=True)
    return allowed


def execute_tool(tool_call, workdir=None, auto_approve=False):
    if workdir is None:
        workdir = os.getcwd()
    workdir = os.path.abspath(os.path.expanduser(workdir))

    function_name = tool_call.function.name
    try:
        arguments = json.loads(tool_call.function.arguments)
    except Exception as e:
        return f"Error parsing arguments: {e}"

    if function_name == "Read":
        raw_path = arguments.get("file_path", "")
        try:
            file_path = resolve_safe_path(raw_path, workdir)
        except (PermissionError, ValueError) as e:
            print(f"{C.RED}[Tool: Read]{C.RESET} Security error: {e}")
            return f"Security Error: {e}"

        display_path = os.path.relpath(file_path, workdir) if file_path.startswith(workdir) else file_path
        print(f"{C.BLUE}[Tool: Read]{C.RESET} Reading file: {C.BOLD}'{display_path}'{C.RESET}")
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            print(f"{C.BLUE}[Tool: Read]{C.RESET} Successfully read {len(content):,} characters from '{display_path}'")
            return content
        except Exception as e:
            print(f"{C.RED}[Tool: Read]{C.RESET} Error reading '{display_path}': {e}")
            return f"Error reading file '{raw_path}': {e}"
    elif function_name == "Write":
        raw_path = arguments.get("file_path", "")
        try:
            file_path = resolve_safe_path(raw_path, workdir)
        except (PermissionError, ValueError) as e:
            print(f"{C.RED}[Tool: Write]{C.RESET} Security error: {e}")
            return f"Security Error: {e}"

        display_path = os.path.relpath(file_path, workdir) if file_path.startswith(workdir) else file_path
        content = arguments.get("content", "")

        # Check if file exists and ask for permission before overwriting
        if os.path.exists(file_path):
            existing_size = os.path.getsize(file_path)
            new_size = len(content.encode("utf-8"))
            allowed = ask_permission(
                "Overwrite Existing File?",
                [
                    ("Target File", file_path),
                    ("Relative Path", display_path),
                    ("Existing Size", f"{existing_size:,} bytes"),
                    ("New Size", f"{new_size:,} bytes"),
                ],
                auto_approve=auto_approve,
            )
            if not allowed:
                return f"Permission Denied: User declined to overwrite file '{raw_path}'."

        print(f"{C.GREEN}[Tool: Write]{C.RESET} Writing file: {C.BOLD}'{display_path}'{C.RESET} ({len(content):,} characters)")
        try:
            dir_name = os.path.dirname(file_path)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"{C.GREEN}[Tool: Write]{C.RESET} Successfully wrote '{display_path}'")
            return ""
        except Exception as e:
            print(f"{C.RED}[Tool: Write]{C.RESET} Error writing '{display_path}': {e}")
            return f"Error writing file '{raw_path}': {e}"
    elif function_name == "Bash":
        command = arguments.get("command", "")
        allowed = ask_permission(
            "Execute Shell Command?",
            [
                ("Command", command),
                ("Working Directory", file_path_dir := format_display_path(workdir)),
            ],
            auto_approve=auto_approve,
        )
        if not allowed:
            return f"Permission Denied: User declined to execute command: '{command}'."

        print(f"{C.YELLOW}[Tool: Bash]{C.RESET} Running in {C.BOLD}{file_path_dir}{C.RESET}: {C.BOLD}'{command}'{C.RESET}")
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=workdir,
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


@dataclass
class SessionState:
    model: str
    provider: str
    workdir: str
    context_window: int
    session_prompt_tokens: int = 0
    session_completion_tokens: int = 0
    last_prompt_tokens: int = 0
    messages: list = field(default_factory=list)
    auto_approve: bool = False
    should_exit: bool = False


COMMAND_REGISTRY = {}


def register_command(names, description: str, usage: str = ""):
    def decorator(func):
        name_list = [names] if isinstance(names, str) else names
        primary_name = name_list[0].lower()
        if not primary_name.startswith("/"):
            primary_name = "/" + primary_name

        info = {
            "func": func,
            "description": description,
            "usage": usage or primary_name,
            "aliases": name_list,
        }
        for n in name_list:
            cmd = n.lower()
            if not cmd.startswith("/"):
                cmd = "/" + cmd
            COMMAND_REGISTRY[cmd] = info
        return func
    return decorator


@register_command(["/help", "/h", "/?"], "List all available slash commands", usage="/help")
def cmd_help(state: SessionState, args: str):
    print(f"\n{C.BOLD_CYAN}Available Slash Commands:{C.RESET}")
    seen = set()
    for cmd, info in sorted(COMMAND_REGISTRY.items()):
        if info["usage"] not in seen:
            seen.add(info["usage"])
            print(f"  {C.BOLD}{info['usage']:<22}{C.RESET} {C.DIM}— {info['description']}{C.RESET}")
    print()


@register_command(["/exit", "/quit", "/q"], "Exit Atelier session", usage="/exit")
def cmd_exit(state: SessionState, args: str):
    state.should_exit = True
    print(f"{C.DIM}Goodbye!{C.RESET}")


@register_command(["/clear", "/reset"], "Clear conversation history and reset context window", usage="/clear")
def cmd_clear(state: SessionState, args: str):
    state.messages = [m for m in state.messages if isinstance(m, dict) and m.get("role") == "system"]
    state.last_prompt_tokens = 0
    print(f"{C.GREEN}✓ Conversation history cleared. Context reset to 0 tokens.{C.RESET}\n")


@register_command(["/model", "/m"], "View or switch active LLM model", usage="/model [name]")
def cmd_model(state: SessionState, args: str):
    new_model = args.strip()
    if not new_model:
        print(f"\nActive Model: {C.BOLD}{state.model}{C.RESET} ({state.provider}) | Context Limit: {state.context_window:,} tokens\n")
        return
    state.model = new_model
    if "32k" in new_model or "qwen2.5-coder:7b" in new_model:
        state.context_window = 32768
    elif "qwen3:8b" in new_model:
        state.context_window = 40960
    elif "claude" in new_model:
        state.context_window = 200000
    elif "gemini" in new_model:
        state.context_window = 1000000
    print(f"{C.GREEN}✓ Switched active model to:{C.RESET} {C.BOLD}{state.model}{C.RESET} (Context Limit: {state.context_window:,} tokens)\n")


@register_command(["/cd", "/dir"], "View or change the working directory", usage="/cd [path]")
def cmd_cd(state: SessionState, args: str):
    target = args.strip()
    if not target:
        print(f"\nWorking Directory: {C.BOLD}{state.workdir}{C.RESET}\n")
        return
    resolved = os.path.abspath(os.path.expanduser(target))
    if not os.path.isdir(resolved):
        print(f"{C.RED}Error: Directory does not exist: '{resolved}'{C.RESET}\n")
        return
    state.workdir = resolved
    # Update system message
    for m in state.messages:
        if isinstance(m, dict) and m.get("role") == "system":
            m["content"] = (
                f"You are Atelier, a helpful, minimalist, and precise AI coding assistant.\n"
                f"Working Directory: {state.workdir}\n"
                f"All file reads, writes, and shell commands are strictly confined to this working directory. "
                f"Always use relative file paths from this directory."
            )
    print(f"{C.GREEN}✓ Working directory changed to:{C.RESET} {C.BOLD}{format_display_path(resolved)}{C.RESET}\n")


@register_command(["/stats", "/context"], "Show session token stats and context utilization", usage="/stats")
def cmd_stats(state: SessionState, args: str):
    total_session = state.session_prompt_tokens + state.session_completion_tokens
    pct = (state.last_prompt_tokens / state.context_window * 100) if state.context_window > 0 else 0
    print(f"\n{C.BOLD_CYAN}Session Statistics & Metrics:{C.RESET}")
    print(f"  • {C.BOLD}Model:{C.RESET}               {state.model} ({state.provider})")
    print(f"  • {C.BOLD}Working Directory:{C.RESET}   {format_display_path(state.workdir)}")
    print(f"  • {C.BOLD}Context Used:{C.RESET}        {state.last_prompt_tokens:,} / {state.context_window:,} tokens ({pct:.2f}%)")
    print(f"  • {C.BOLD}Session Total:{C.RESET}       {total_session:,} tokens (Prompt: {state.session_prompt_tokens:,} | Completion: {state.session_completion_tokens:,})")
    print(f"  • {C.BOLD}Auto-Approve:{C.RESET}        {'Enabled (bypasses confirmation)' if state.auto_approve else 'Disabled (prompts before write/bash)'}\n")


@register_command(["/approve", "/auto"], "Toggle auto-approve mode for commands & overwrites", usage="/approve")
def cmd_approve(state: SessionState, args: str):
    state.auto_approve = not state.auto_approve
    status_str = f"{C.BOLD_GREEN}ENABLED{C.RESET}" if state.auto_approve else f"{C.BOLD_YELLOW}DISABLED{C.RESET}"
    print(f"\nAuto-Approve mode is now {status_str}.\n")


@register_command(["/tools"], "List available agent tools and descriptions", usage="/tools")
def cmd_tools(state: SessionState, args: str):
    print(f"\n{C.BOLD_CYAN}Registered Agent Tools:{C.RESET}")
    print(f"  • {C.BOLD}Read:{C.RESET}  Reads file contents safely within working directory.")
    print(f"  • {C.BOLD}Write:{C.RESET} Writes or updates files (prompts before overwriting).")
    print(f"  • {C.BOLD}Bash:{C.RESET}  Executes shell commands with cwd confined to working directory.\n")


def dispatch_slash_command(user_input: str, state: SessionState) -> bool:
    if not user_input.startswith("/"):
        return False
    parts = user_input.split(" ", 1)
    cmd = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""

    if cmd in COMMAND_REGISTRY:
        COMMAND_REGISTRY[cmd]["func"](state, args)
        return True
    else:
        print(f"{C.RED}Unknown command: '{cmd}'. Type {C.BOLD}/help{C.RESET}{C.RED} to see available commands.{C.RESET}\n")
        return True


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
    p.add_argument("-d", "--dir", "--workdir", default=os.getcwd(), help="Target working directory for file operations and commands (default: current directory)")
    p.add_argument("-y", "--yes", "--auto-approve", dest="auto_approve", action="store_true", help="Automatically approve shell commands and file overwrites without prompting")
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

    workdir = os.path.abspath(os.path.expanduser(args.dir))
    if not os.path.exists(workdir):
        os.makedirs(workdir, exist_ok=True)

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

    system_prompt = (
        f"You are Atelier, a helpful, minimalist, and precise AI coding assistant.\n"
        f"Working Directory: {workdir}\n"
        f"All file reads, writes, and shell commands are strictly confined to this working directory. "
        f"Always use relative file paths from this directory."
    )
    messages = [{"role": "system", "content": system_prompt}]
    initial_prompt = args.p

    provider_type = "mock" if args.mock else ("local" if args.local else "cloud")
    state = SessionState(
        model=model,
        provider=provider_type,
        workdir=workdir,
        context_window=context_window,
        messages=messages,
        auto_approve=args.auto_approve,
    )

    if args.mock:
        provider_badge = f"{C.BOLD_MAGENTA}Mock (Zero-Model){C.RESET}"
    elif args.local:
        provider_badge = f"{C.BOLD_GREEN}Local (Ollama){C.RESET}"
    else:
        provider_badge = f"{C.BOLD_BLUE}Cloud (OpenRouter){C.RESET}"

    print(f"\n🎨 {C.BOLD_CYAN}Atelier{C.RESET} — AI Coding Harness [{provider_badge} | {C.BOLD}{state.model}{C.RESET} | {C.DIM}📁 {format_display_path(state.workdir)}{C.RESET}]", flush=True)
    print(f"{C.DIM}Type your prompt, '/help' for commands, or 'exit' to quit.{C.RESET}", flush=True)

    while True:
        if initial_prompt:
            user_prompt = initial_prompt
            initial_prompt = None
        else:
            render_statusbar(
                model=state.model,
                provider=state.provider,
                prompt_tokens=state.last_prompt_tokens,
                context_window=state.context_window,
                session_tokens=state.session_prompt_tokens + state.session_completion_tokens,
                workdir=state.workdir,
            )
            try:
                user_prompt = input(f"{C.BOLD_CYAN}atelier ❯{C.RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{C.DIM}Goodbye!{C.RESET}")
                break

            if not user_prompt:
                continue

            if dispatch_slash_command(user_prompt, state):
                if state.should_exit:
                    break
                continue

        state.messages.append({"role": "user", "content": user_prompt})

        # Agent Loop: call model and execute tools until final text answer is produced
        try:
            while True:
                params = {
                    "model": state.model,
                    "messages": state.messages,
                    "tools": tools,
                }
                if args.max_tokens is not None:
                    params["max_tokens"] = args.max_tokens

                chat = client.chat.completions.create(**params)

                if not chat.choices or len(chat.choices) == 0:
                    raise RuntimeError("no choices in response")

                if chat.usage:
                    state.last_prompt_tokens = chat.usage.prompt_tokens
                    state.session_prompt_tokens += chat.usage.prompt_tokens
                    state.session_completion_tokens += chat.usage.completion_tokens
                    context_pct = (chat.usage.prompt_tokens / state.context_window) * 100
                    pct_color = C.GREEN if context_pct < 50 else (C.YELLOW if context_pct < 80 else C.RED)
                    print(
                        f"\n{C.DIM}┌─ [Context & Tokens]{C.RESET} "
                        f"Context: {C.BOLD}{chat.usage.prompt_tokens:,}{C.RESET}/{state.context_window:,} ({pct_color}{context_pct:.2f}%{C.RESET}) | "
                        f"Gen: {C.BOLD}{chat.usage.completion_tokens:,}{C.RESET} | "
                        f"Turn: {chat.usage.total_tokens:,} | "
                        f"Session: {state.session_prompt_tokens + state.session_completion_tokens:,}",
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

                state.messages.append(response_message)

                if not tool_calls:
                    if response_message.content:
                        print(f"\n{C.BOLD_GREEN}🤖 {state.model}:{C.RESET}\n{response_message.content}", flush=True)
                    break

                print(f"\n{C.BOLD_MAGENTA}[Model Response]{C.RESET} {C.MAGENTA}Requested {len(tool_calls)} tool call(s):{C.RESET}")
                for tool_call in tool_calls:
                    print(f"  • {C.BOLD}Tool:{C.RESET} {C.BOLD_CYAN}{tool_call.function.name}{C.RESET} | {C.DIM}ID: {tool_call.id}{C.RESET} | {C.DIM}Args: {tool_call.function.arguments}{C.RESET}")
                    result = execute_tool(tool_call, workdir=state.workdir, auto_approve=state.auto_approve)
                    state.messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    })
        except KeyboardInterrupt:
            print(f"\n{C.YELLOW}[Interrupted]{C.RESET} Operation cancelled by user.", flush=True)
            # Remove incomplete user prompt from history
            if state.messages and hasattr(state.messages[-1], "get") and state.messages[-1].get("role") == "user":
                state.messages.pop()
            continue
        except Exception as e:
            print(f"\n{C.RED}[Error]{C.RESET} {e}", flush=True)
            if state.messages and hasattr(state.messages[-1], "get") and state.messages[-1].get("role") == "user":
                state.messages.pop()
            continue


if __name__ == "__main__":
    main()

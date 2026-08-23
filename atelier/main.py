import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall, Function

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

    if provider == "local":
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


def perform_web_search(query: str, max_results: int = 5) -> str:
    """Performs web search using Tavily API if configured, otherwise DuckDuckGo Lite."""
    query = query.strip()
    if not query:
        return "Error: Empty search query."

    tavily_key = os.getenv("TAVILY_API_KEY")
    if tavily_key:
        try:
            req_data = json.dumps({
                "api_key": tavily_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.tavily.com/search",
                data=req_data,
                headers={"Content-Type": "application/json", "User-Agent": "Atelier/1.0"},
            )
            with urllib.request.urlopen(req, timeout=6.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                results = data.get("results", [])
                if not results:
                    return f"No web search results found for: '{query}'."
                formatted = [f"Web Search Results for '{query}':"]
                for i, r in enumerate(results[:max_results], start=1):
                    title = r.get("title", "No Title")
                    url = r.get("url", "")
                    content = r.get("content", "").strip()
                    formatted.append(f"[{i}] {title}\n    URL: {url}\n    Snippet: {content}")
                return "\n\n".join(formatted)
        except Exception:
            pass

    # DuckDuckGo HTML Lite search (Zero-key fallback)
    try:
        encoded_query = urllib.parse.urlencode({"q": query})
        url = f"https://html.duckduckgo.com/html/?{encoded_query}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
        )
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        link_pattern = re.compile(r'<a class="result__url"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
        snippet_pattern = re.compile(r'<a class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
        title_pattern = re.compile(r'<a class="result__title"[^>]*>(.*?)</a>', re.DOTALL)

        raw_titles = [re.sub(r"<[^>]+>", "", t).strip() for t in title_pattern.findall(html)]
        raw_snippets = [re.sub(r"<[^>]+>", "", s).strip() for s in snippet_pattern.findall(html)]
        raw_links = link_pattern.findall(html)

        results = []
        count = min(len(raw_snippets), max_results)
        for i in range(count):
            title = raw_titles[i] if i < len(raw_titles) else "Search Result"
            snippet = raw_snippets[i]
            link = raw_links[i][0] if i < len(raw_links) else ""
            if "uddg=" in link:
                parsed_params = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
                link = parsed_params.get("uddg", [link])[0]

            results.append(f"[{i + 1}] {title}\n    URL: {link}\n    Snippet: {snippet}")

        if not results:
            return f"No search results found for query: '{query}'."

        return f"Web Search Results for '{query}':\n\n" + "\n\n".join(results)
    except Exception as e:
        return f"Web search request failed: {e}"


def execute_tool(tool_call, workdir=None, auto_approve=False):
    if workdir is None:
        workdir = os.getcwd()
    workdir = os.path.abspath(os.path.expanduser(workdir))

    function_name = tool_call.function.name
    try:
        arguments = json.loads(tool_call.function.arguments)
    except Exception as e:
        return f"Error parsing arguments: {e}"

    if function_name == "WebSearch":
        query = arguments.get("query", "")
        max_results = int(arguments.get("max_results", 5)) if arguments.get("max_results") else 5
        print(f"{C.BOLD_CYAN}[Tool: WebSearch]{C.RESET} Searching web for: {C.BOLD}'{query}'{C.RESET}")
        return perform_web_search(query, max_results=max_results)
    elif function_name == "Read":
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
    client: Any
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
    base_url: str = None
    api_key: str = None


def detect_context_window(model: str, provider: str = "cloud") -> int:
    m = model.lower()
    if "32k" in m or "qwen2.5" in m or "qwen2" in m:
        return 32768
    elif "qwen3:8b" in m:
        return 40960
    elif "claude" in m or "sonnet" in m or "haiku" in m:
        return 200000
    elif "gemini" in m:
        return 1000000
    elif "deepseek" in m:
        return 65536
    elif "llama-3.3" in m or "llama-3.1" in m:
        return 128000
    elif provider == "local":
        return 32768
    else:
        return 128000


def create_client(provider: str, base_url: str = None, api_key: str = None):
    if provider == "local":
        url = base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        key = api_key or os.getenv("OLLAMA_API_KEY", "ollama")
        return OpenAI(api_key=key, base_url=url)
    elif provider == "cloud":
        key = api_key or os.getenv("OPENROUTER_API_KEY")
        if not key:
            raise ValueError(
                "OPENROUTER_API_KEY is not set. Add it to .env or use '/model local <model>' for Ollama."
            )
        url = base_url or os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        return OpenAI(api_key=key, base_url=url)
    else:
        raise ValueError(f"Unknown provider: '{provider}'. Options: local, cloud.")


def fetch_local_models(base_url: str = None) -> list[dict]:
    """Queries Ollama's /api/tags endpoint to discover downloaded local models."""
    url = base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    root_url = url.replace("/v1", "").rstrip("/")
    tags_url = f"{root_url}/api/tags"
    try:
        req = urllib.request.Request(tags_url, headers={"User-Agent": "Atelier/1.0"})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("models", [])
    except Exception:
        return []


def switch_model_and_provider(state: SessionState, target_provider: str, target_model: str = None):
    new_provider = target_provider.lower()
    if new_provider not in ("local", "cloud"):
        print(f"{C.RED}Unknown provider '{new_provider}'. Options: local, cloud.{C.RESET}\n")
        return

    # Default model names if not provided
    if not target_model:
        if new_provider == "local":
            target_model = "qwen2.5-coder:7b"
        elif new_provider == "cloud":
            target_model = "liquid/lfm-2.5-2.6b:free"

    try:
        new_client = create_client(
            new_provider,
            base_url=state.base_url,
            api_key=state.api_key if new_provider == "cloud" else None
        )
    except Exception as e:
        print(f"\n{C.RED}Error switching to {new_provider}:{C.RESET} {e}\n")
        return

    state.client = new_client
    state.provider = new_provider
    state.model = target_model
    state.context_window = detect_context_window(target_model, new_provider)

    badge = f"{C.BOLD_GREEN}Local (Ollama){C.RESET}" if new_provider == "local" else f"{C.BOLD_BLUE}Cloud (OpenRouter){C.RESET}"
    print(f"\n✓ Switched to {badge}: {C.BOLD}{state.model}{C.RESET} (Context Limit: {state.context_window:,} tokens)\n")


def prune_tool_outputs(messages: list, preserve_last_n: int = 4, max_lines: int = 35) -> int:
    """Strategy 1 (Micro-Pruning): Truncates giant older tool outputs to head/tail snippets."""
    pruned_count = 0
    cutoff = max(0, len(messages) - preserve_last_n)
    for i in range(cutoff):
        msg = messages[i]
        if isinstance(msg, dict) and msg.get("role") == "tool":
            content = str(msg.get("content", ""))
            lines = content.splitlines()
            if len(lines) > max_lines:
                head = lines[:15]
                tail = lines[-10:]
                omitted = len(lines) - 25
                marker = f"... [Output truncated: {omitted:,} lines omitted to conserve context] ..."
                new_content = "\n".join(head + [marker] + tail)
                msg["content"] = new_content
                pruned_count += 1
    return pruned_count


def compact_context(state: SessionState, hot_zone_turns: int = 2, force: bool = False) -> bool:
    """Strategy 2 & 3: 3-Zone Partitioning and Active LLM Summarization."""
    # 1. Run micro-pruning on older tool responses first
    prune_tool_outputs(state.messages, preserve_last_n=hot_zone_turns * 2)

    system_msgs = [m for m in state.messages if isinstance(m, dict) and m.get("role") == "system"]
    work_msgs = [m for m in state.messages if not (isinstance(m, dict) and m.get("role") == "system")]

    min_messages = (hot_zone_turns * 2) + 2
    if len(work_msgs) < (2 if force else min_messages):
        return False

    if force and len(work_msgs) < min_messages:
        split_idx = max(1, len(work_msgs) - 2)
    else:
        split_idx = max(0, len(work_msgs) - (hot_zone_turns * 2))

    # Safeguard: ensure split_idx does not sever an assistant tool_calls from its tool responses
    while split_idx > 0 and split_idx < len(work_msgs):
        curr = work_msgs[split_idx]
        if isinstance(curr, dict) and curr.get("role") == "tool":
            split_idx -= 1
        else:
            break

    to_compact = work_msgs[:split_idx]
    hot_zone = work_msgs[split_idx:]

    if not to_compact:
        return False

    print(f"\n{C.BOLD_YELLOW}🧠 [Context Optimization] Compacting {len(to_compact)} older messages into structured brief...{C.RESET}", flush=True)

    transcript_lines = []
    for m in to_compact:
        if isinstance(m, dict):
            role = m.get("role", "unknown").upper()
            content = str(m.get("content", ""))
            if role == "TOOL":
                tool_id = m.get("tool_call_id", "")
                snippet = (content[:250] + "...") if len(content) > 250 else content
                transcript_lines.append(f"[TOOL RESPONSE ({tool_id})]: {snippet}")
            else:
                snippet = (content[:400] + "...") if len(content) > 400 else content
                transcript_lines.append(f"[{role}]: {snippet}")
        elif hasattr(m, "role"):
            role = getattr(m, "role", "assistant").upper()
            content = getattr(m, "content", "") or ""
            tool_calls = getattr(m, "tool_calls", None)
            if tool_calls:
                tc_names = ", ".join([tc.function.name for tc in tool_calls if hasattr(tc, "function") and hasattr(tc.function, "name")])
                transcript_lines.append(f"[ASSISTANT TOOL REQUEST]: Called tools: {tc_names}")
            if content:
                transcript_lines.append(f"[{role}]: {content[:400]}")

    compaction_prompt = (
        "You are Atelier's context compaction engine. Summarize the preceding conversation into a concise, high-density briefing.\n"
        "Preserve:\n"
        "1. Primary user task/goal\n"
        "2. Files inspected, created, or modified (with paths)\n"
        "3. Key technical decisions and solutions implemented\n"
        "4. Unresolved errors or pending next steps\n"
        "Be concise (under 200 words). Do not include conversational filler.\n\n"
        "CONVERSATION TRANSCRIPT:\n" + "\n".join(transcript_lines)
    )

    try:
        resp = state.client.chat.completions.create(
            model=state.model,
            messages=[{"role": "user", "content": compaction_prompt}],
            max_tokens=450,
            stream=False,
        )
        if not resp.choices or not resp.choices[0].message.content:
            return False
        summary_text = resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"{C.RED}[Context Compaction Failed]{C.RESET} {e}\n")
        return False

    summary_user_msg = {
        "role": "user",
        "content": f"📋 [Summary of previous session context]:\n{summary_text}"
    }
    summary_assistant_ack = {
        "role": "assistant",
        "content": "Understood. I have absorbed the prior session context, file changes, and active tasks. Ready to continue."
    }

    old_msg_count = len(state.messages)
    state.messages = system_msgs + [summary_user_msg, summary_assistant_ack] + hot_zone
    new_msg_count = len(state.messages)

    if state.last_prompt_tokens > 0:
        state.last_prompt_tokens = max(int(state.last_prompt_tokens * 0.35), 250)

    print(f"{C.BOLD_GREEN}✓ Context Compacted:{C.RESET} Reduced history from {C.BOLD}{old_msg_count}{C.RESET} ➔ {C.BOLD}{new_msg_count}{C.RESET} messages. Working memory refreshed.\n", flush=True)
    return True


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
            print(f"  {C.BOLD}{info['usage']:<26}{C.RESET} {C.DIM}— {info['description']}{C.RESET}")
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


@register_command(["/compact", "/compress"], "Compress older conversation history into a brief to reclaim context", usage="/compact")
def cmd_compact(state: SessionState, args: str):
    success = compact_context(state, force=True)
    if not success:
        print(f"{C.DIM}Conversation history is too short to compact or no older messages were found.{C.RESET}\n")


@register_command(["/models", "/ls"], "Discover downloaded Ollama models and cloud recommendations", usage="/models")
def cmd_models(state: SessionState, args: str):
    print(f"\n{C.BOLD_CYAN}🔍 Discovering Local Models (Ollama)...{C.RESET}")
    local_models = fetch_local_models(state.base_url)
    if local_models:
        print(f"\n{C.BOLD_GREEN}Installed Local Models ({len(local_models)} found):{C.RESET}")
        for m in local_models:
            name = m.get("name", "unknown")
            size_gb = m.get("size", 0) / (1024 ** 3)
            modified = m.get("modified_at", "")[:10]
            is_active = f" {C.BOLD_GREEN}● active{C.RESET}" if name == state.model and state.provider == "local" else ""
            print(f"  • {C.BOLD}{name:<28}{C.RESET} {C.DIM}{size_gb:>5.1f} GB  │  modified {modified}{C.RESET}{is_active}")
        print(f"\n{C.DIM}To switch: {C.BOLD}/model local <name>{C.RESET}\n")
    else:
        print(f"  {C.YELLOW}No local models found or Ollama is not running at http://localhost:11434{C.RESET}")
        print(f"  {C.DIM}Start Ollama with 'ollama serve' or pull a model with 'ollama run qwen2.5-coder:7b'{C.RESET}\n")

    print(f"{C.BOLD_BLUE}Popular Cloud Models (OpenRouter):{C.RESET}")
    cloud_recs = [
        ("anthropic/claude-3.5-sonnet", "Top coding reasoning & large refactoring (200k context)"),
        ("deepseek/deepseek-chat", "Fast, capable & cost-effective coding (64k context)"),
        ("meta-llama/llama-3.3-70b-instruct", "Open-weight powerhouse (128k context)"),
        ("liquid/lfm-2.5-2.6b:free", "Free tier lightweight test model (32k context)"),
    ]
    for m_id, desc in cloud_recs:
        is_active = f" {C.BOLD_BLUE}● active{C.RESET}" if m_id == state.model and state.provider == "cloud" else ""
        print(f"  • {C.BOLD}{m_id:<36}{C.RESET} {C.DIM}— {desc}{C.RESET}{is_active}")
    print(f"\n{C.DIM}To switch: {C.BOLD}/model cloud <id>{C.RESET}\n")


@register_command(["/model", "/m"], "View or switch active model/provider", usage="/model [local|cloud] [name]")
def cmd_model(state: SessionState, args: str):
    raw = args.strip()
    if not raw:
        badge = f"{C.BOLD_GREEN}Local (Ollama){C.RESET}" if state.provider == "local" else f"{C.BOLD_BLUE}Cloud (OpenRouter){C.RESET}"
        print(f"\nActive Model: {C.BOLD}{state.model}{C.RESET} [{badge}] | Context Limit: {state.context_window:,} tokens\n")
        return

    parts = raw.split(None, 1)
    first = parts[0].lower()

    if first in ("local", "ollama"):
        target_model = parts[1] if len(parts) > 1 else None
        switch_model_and_provider(state, "local", target_model)
    elif first in ("cloud", "openrouter"):
        target_model = parts[1] if len(parts) > 1 else None
        switch_model_and_provider(state, "cloud", target_model)
    else:
        target_model = raw
        target_provider = state.provider
        if "/" in target_model and target_provider == "local":
            target_provider = "cloud"
        switch_model_and_provider(state, target_provider, target_model)


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
    print(f"  • {C.BOLD}Read:{C.RESET}      Reads file contents safely within working directory.")
    print(f"  • {C.BOLD}Write:{C.RESET}     Writes or updates files (prompts before overwriting).")
    print(f"  • {C.BOLD}Bash:{C.RESET}      Executes shell commands with cwd confined to working directory.")
    print(f"  • {C.BOLD}WebSearch:{C.RESET} Searches the live web for documentation, syntax, and solutions.\n")


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


def main():
    p = argparse.ArgumentParser(description="Atelier — Minimalist AI Coding Harness")
    p.add_argument("-p", required=False, help="Initial prompt")
    p.add_argument("-d", "--dir", "--workdir", default=os.getcwd(), help="Target working directory for file operations and commands (default: current directory)")
    p.add_argument("-y", "--yes", "--auto-approve", dest="auto_approve", action="store_true", help="Automatically approve shell commands and file overwrites without prompting")
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

    provider_type = "local" if args.local else "cloud"
    if args.local:
        model = args.model or "qwen2.5-coder:7b"
    else:
        model = args.model or "liquid/lfm-2.5-2.6b:free"

    context_window = args.context_window or detect_context_window(model, provider_type)

    try:
        client = create_client(provider_type, base_url=args.base_url)
    except Exception as e:
        if provider_type == "cloud":
            raise RuntimeError(f"{e} Use --local to run with local Ollama models.")
        raise

    tools = [
        {
            "type": "function",
            "function": {
                "name": "WebSearch",
                "description": "Search the live web for current documentation, library API references, error troubleshooting, and examples.",
                "parameters": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query (e.g. 'FastAPI lifespan event handler example' or 'Pydantic v2 model_validate')"
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of search results to return (default: 5)"
                        }
                    }
                }
            }
        },
        {
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

    state = SessionState(
        client=client,
        model=model,
        provider=provider_type,
        workdir=workdir,
        context_window=context_window,
        messages=messages,
        auto_approve=args.auto_approve,
        base_url=args.base_url,
    )

    if args.local:
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

        # Agent Loop: call model with real-time streaming and execute tools
        try:
            while True:
                # Active Context Compaction (Auto-trigger when context utilization >= 80%)
                if state.context_window > 0 and state.last_prompt_tokens > 0:
                    usage_ratio = state.last_prompt_tokens / state.context_window
                    if usage_ratio >= 0.80:
                        compact_context(state)

                params = {
                    "model": state.model,
                    "messages": state.messages,
                    "tools": tools,
                    "stream": True,
                }
                if args.max_tokens is not None:
                    params["max_tokens"] = args.max_tokens

                try:
                    stream = state.client.chat.completions.create(**params, stream_options={"include_usage": True})
                except (TypeError, Exception):
                    stream = state.client.chat.completions.create(**params)

                full_content = ""
                tool_calls_dict = {}
                usage_obj = None
                printed_header = False

                for chunk in stream:
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage_obj = chunk.usage

                    if not chunk.choices or len(chunk.choices) == 0:
                        continue

                    delta = chunk.choices[0].delta
                    if not delta:
                        continue

                    if getattr(delta, "content", None):
                        if not printed_header:
                            print(f"\n{C.BOLD_GREEN}🤖 {state.model}:{C.RESET}\n", end="", flush=True)
                            printed_header = True
                        print(delta.content, end="", flush=True)
                        full_content += delta.content

                    if getattr(delta, "tool_calls", None):
                        for tc_chunk in delta.tool_calls:
                            idx = getattr(tc_chunk, "index", 0)
                            if idx not in tool_calls_dict:
                                tool_calls_dict[idx] = {
                                    "id": getattr(tc_chunk, "id", "") or "",
                                    "name": getattr(tc_chunk.function, "name", "") if getattr(tc_chunk, "function", None) and getattr(tc_chunk.function, "name", None) else "",
                                    "arguments": getattr(tc_chunk.function, "arguments", "") if getattr(tc_chunk, "function", None) and getattr(tc_chunk.function, "arguments", None) else "",
                                }
                            else:
                                if getattr(tc_chunk, "id", None):
                                    tool_calls_dict[idx]["id"] += tc_chunk.id
                                if getattr(tc_chunk, "function", None):
                                    if getattr(tc_chunk.function, "name", None):
                                        tool_calls_dict[idx]["name"] += tc_chunk.function.name
                                    if getattr(tc_chunk.function, "arguments", None):
                                        tool_calls_dict[idx]["arguments"] += tc_chunk.function.arguments

                if printed_header and full_content:
                    print(flush=True)

                final_tool_calls = []
                for idx in sorted(tool_calls_dict.keys()):
                    tc_data = tool_calls_dict[idx]
                    final_tool_calls.append(
                        ChatCompletionMessageToolCall(
                            id=tc_data["id"] or f"call_stream_{idx}",
                            type="function",
                            function=Function(name=tc_data["name"], arguments=tc_data["arguments"])
                        )
                    )

                # Fallback for local models outputting JSON tool calls in raw content text
                if not final_tool_calls and full_content:
                    text = full_content.strip()
                    start = text.find("{")
                    end = text.rfind("}")
                    if start != -1 and end != -1 and end > start:
                        json_candidate = text[start:end+1]
                        try:
                            parsed = json.loads(json_candidate)
                            if isinstance(parsed, dict) and "name" in parsed and "arguments" in parsed:
                                args_str = json.dumps(parsed["arguments"]) if isinstance(parsed["arguments"], dict) else str(parsed["arguments"])
                                final_tool_calls = [
                                    ChatCompletionMessageToolCall(
                                        id="call_local_0",
                                        type="function",
                                        function=Function(name=parsed["name"], arguments=args_str)
                                    )
                                ]
                                full_content = None
                        except Exception:
                            pass

                if usage_obj:
                    p_tok = getattr(usage_obj, "prompt_tokens", 0)
                    c_tok = getattr(usage_obj, "completion_tokens", 0)
                    t_tok = getattr(usage_obj, "total_tokens", p_tok + c_tok)
                    state.last_prompt_tokens = p_tok
                    state.session_prompt_tokens += p_tok
                    state.session_completion_tokens += c_tok
                    context_pct = (p_tok / state.context_window) * 100 if state.context_window > 0 else 0
                    pct_color = C.GREEN if context_pct < 50 else (C.YELLOW if context_pct < 80 else C.RED)
                    print(
                        f"\n{C.DIM}┌─ [Context & Tokens]{C.RESET} "
                        f"Context: {C.BOLD}{p_tok:,}{C.RESET}/{state.context_window:,} ({pct_color}{context_pct:.2f}%{C.RESET}) | "
                        f"Gen: {C.BOLD}{c_tok:,}{C.RESET} | "
                        f"Turn: {t_tok:,} | "
                        f"Session: {state.session_prompt_tokens + state.session_completion_tokens:,}",
                        flush=True
                    )

                response_message = ChatCompletionMessage(
                    role="assistant",
                    content=full_content if not final_tool_calls else None,
                    tool_calls=final_tool_calls if final_tool_calls else None,
                )
                state.messages.append(response_message)

                if not final_tool_calls:
                    break

                print(f"\n{C.BOLD_MAGENTA}[Model Response]{C.RESET} {C.MAGENTA}Requested {len(final_tool_calls)} tool call(s):{C.RESET}")
                for tool_call in final_tool_calls:
                    print(f"  • {C.BOLD}Tool:{C.RESET} {C.BOLD_CYAN}{tool_call.function.name}{C.RESET} | {C.DIM}ID: {tool_call.id}{C.RESET} | {C.DIM}Args: {tool_call.function.arguments}{C.RESET}")
                    result = execute_tool(tool_call, workdir=state.workdir, auto_approve=state.auto_approve)
                    state.messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    })

                # Passive Micro-Pruning: truncate large older tool responses
                prune_tool_outputs(state.messages, preserve_last_n=4)
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

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


def execute_tool(tool_call):
    function_name = tool_call.function.name
    arguments = json.loads(tool_call.function.arguments)
    if function_name == "Read":
        file_path = arguments.get("file_path")
        print(f"[Diagnostic] Reading file: '{file_path}'")
        with open(file_path, "r") as f:
            content = f.read()
        print(f"[Diagnostic] Successfully read {len(content)} characters from '{file_path}'")
        return content
    elif function_name == "Write":
        file_path = arguments.get("file_path")
        content = arguments.get("content", "")
        print(f"[Diagnostic] Writing file: '{file_path}' ({len(content)} characters)")
        dir_name = os.path.dirname(file_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(file_path, "w") as f:
            f.write(content)
        print(f"[Diagnostic] Successfully wrote '{file_path}'")
        return ""
    elif function_name == "Bash":
        command = arguments.get("command", "")
        print(f"[Diagnostic] Running command: '{command}'")
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
            )
            print(f"[Diagnostic] Command completed with exit code {result.returncode}")
            return result.stdout + result.stderr
        except Exception as e:
            print(f"[Diagnostic] Command failed: {e}")
            return str(e)
    return ""


def main():
    p = argparse.ArgumentParser(description="Atelier — Minimalist AI Coding Harness")
    p.add_argument("-p", required=False, help="Initial prompt")
    p.add_argument("--local", "--ollama", dest="local", action="store_true", help="Use local Ollama instead of OpenRouter")
    p.add_argument(
        "-m",
        "--model",
        required=False,
        help="Model name (default: alibilge/Huihui-GLM-4.6V-Flash-abliterated:q4_k_m for local, liquid/lfm-2.5-2.6b:free for OpenRouter)",
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

    if args.local:
        base_url = args.base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        api_key = os.getenv("OLLAMA_API_KEY", "ollama")
        model = args.model or "alibilge/Huihui-GLM-4.6V-Flash-abliterated:q4_k_m"
    else:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set. Use --local or --ollama to use local Ollama models without an API key."
            )
        base_url = args.base_url or os.getenv("OPENROUTER_BASE_URL", default="https://openrouter.ai/api/v1")
        model = args.model or "liquid/lfm-2.5-2.6b:free"

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

    client = OpenAI(api_key=api_key, base_url=base_url)

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

    # You can use print statements as follows for debugging, they'll be visible when running tests.
    print("Logs from your program will appear here!", file=sys.stderr)

    while True:
        if initial_prompt:
            user_prompt = initial_prompt
            initial_prompt = None
        else:
            try:
                user_prompt = input("\nEnter your prompt (or 'exit' to quit): ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break

            if not user_prompt or user_prompt.lower() in ("exit", "quit", "q"):
                print("Goodbye!")
                break

        messages.append({"role": "user", "content": user_prompt})

        # Agent Loop: call model and execute tools until final text answer is produced
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
                session_prompt_tokens += chat.usage.prompt_tokens
                session_completion_tokens += chat.usage.completion_tokens
                context_pct = (chat.usage.prompt_tokens / context_window) * 100
                print(
                    f"\n[Context & Token Usage] Context: {chat.usage.prompt_tokens:,}/{context_window:,} tokens ({context_pct:.2f}%) | "
                    f"Generated: {chat.usage.completion_tokens:,} tokens | "
                    f"Turn Total: {chat.usage.total_tokens:,} tokens (Session: {session_prompt_tokens + session_completion_tokens:,})",
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
                    print(response_message.content, flush=True)
                break

            print(f"\n[Model Response] Requested {len(tool_calls)} tool call(s):")
            for tool_call in tool_calls:
                print(f"  • Tool: {tool_call.function.name} | ID: {tool_call.id} | Arguments: {tool_call.function.arguments}")
                result = execute_tool(tool_call)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })


if __name__ == "__main__":
    main()

# Atelier 🎨🛠️

**Atelier** is a minimalist, memory- and compute-efficient AI coding assistant and agent harness. It connects local LLMs (via Ollama) and cloud models (via OpenRouter/OpenAI) to terminal-native tools (`Read`, `Write`, and `Bash`) in an autonomous execution loop.

---

## Features

- **Multi-turn Agent Loop**: Automatically executes tool calls, feeds results back to the model, and iterates until the task is complete.
- **Local & Cloud LLM Support**:
  - Native support for local **Ollama** models (`qwen2.5-coder`, `qwen3`, etc.).
  - Support for **OpenRouter** / OpenAI-compatible cloud models.
- **Robust Tool Calling**:
  - `Read`: Safely reads file contents.
  - `Write`: Writes or updates files on disk.
  - `Bash`: Executes shell commands.
  - Built-in fallback JSON parser for local models that output markdown-fenced or raw JSON tool calls.
- **Live Context & Token Tracking**:
  - Reports exact context size and percentage used against the model's max window.
  - Reports generation tokens and cumulative session consumption.
- **Diagnostic Logging**:
  - Live console feedback for every file read, file written, and command executed.

---

## Getting Started

### Prerequisites
- Python 3.14+ (or Python 3.10+)
- [`uv`](https://github.com/astral-sh/uv)
- (Optional) [Ollama](https://ollama.com) for running local models.

### Installation
```bash
# Clone the repository
git clone <repo-url>
cd atelier

# Install dependencies (managed automatically by uv)
uv sync
```

### Configuration
If using cloud models via OpenRouter, create a `.env` file:
```bash
echo "OPENROUTER_API_KEY=your_key_here" > .env
```

---

## Usage

### 1. Interactive Mode
```bash
# Run with local Ollama (qwen2.5-coder:7b)
./atelier.sh --local -m qwen2.5-coder:7b

# Run with OpenRouter
./atelier.sh -m liquid/lfm-2.5-2.6b:free
```

### 2. Single Prompt Execution
```bash
# Inspect a file
./atelier.sh --local -m qwen2.5-coder:7b -p "Read pyproject.toml and explain its dependencies"

# Create or modify code
./atelier.sh --local -m qwen2.5-coder:7b -p "Write a python script called math_utils.py with add and multiply functions"
```

### 3. Specifying a Target Working Directory
Confine all file reads, writes, and shell execution to a specific project directory:
```bash
./atelier.sh -d /path/to/my-project --local -m qwen2.5-coder:7b
```

### 4. Non-Interactive / Auto-Approve Mode
By default, Atelier prompts for confirmation before running any shell command or overwriting an existing file. Use `-y` to auto-approve:
```bash
./atelier.sh -y -d /path/to/my-project --local -m qwen2.5-coder:7b
```

### 5. Running Automated Unit Tests
```bash
uv run -m unittest discover tests
```

---

## Interactive Slash Commands

Inside the interactive REPL, you can control Atelier dynamically:

| Command | Description | Example |
| :--- | :--- | :--- |
| **`/models`** *(or `/ls`)* | Discover downloaded local Ollama models & cloud recommendations | `/models` |
| **`/model [provider] [name]`** | View or switch active model/provider live | `/model local qwen2.5-coder:7b` |
| **`/compact`** *(or `/compress`)* | Compact older conversation history to reclaim context window | `/compact` |
| **`/cd [path]`** | Change active working directory without restarting | `/cd ~/projects/my-api` |
| **`/clear`** *(or `/reset`)* | Clear conversation history and reset context tokens to 0 | `/clear` |
| **`/stats`** *(or `/context`)* | View active context token usage, model info, and session odometer | `/stats` |
| **`/approve`** *(or `/auto`)* | Toggle auto-approve mode for bash commands and file overwrites | `/approve` |
| **`/tools`** | List registered tools and parameter descriptions | `/tools` |
| **`/help`** | List all available slash commands and usage | `/help` |
| **`/exit`** *(or `/q`)* | Exit the session cleanly | `/exit` |

---

## Options & Flags

```text
./atelier.sh --help

options:
  -h, --help            Show help message and exit
  -p P                  Initial prompt
  -d, --dir, --workdir  Target working directory (default: current directory)
  -y, --yes             Auto-approve shell commands and file overwrites without prompting
  --local, --ollama     Use local Ollama instead of OpenRouter
  -m, --model MODEL     Model name (default: qwen2.5-coder:7b for local, liquid/lfm-2.5-2.6b:free for OpenRouter)
  --base-url BASE_URL   Custom API Base URL
  --max-tokens N        Max tokens to generate per response (default: 1024)
  --context-window N    Model max context window limit in tokens
```


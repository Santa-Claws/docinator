# docinator

Generate exhaustive, developer-grade documentation for any GitHub repository using an LLM. Point it at a repo URL and it reads every source file, sends each one to the model, and writes detailed Markdown docs — either one file per source file (mirroring the repo structure) or a single concatenated file.

## Features

- **Three LLM providers**: OpenAI, OpenRouter, or local Ollama — all via the same OpenAI-compatible API
- **Free model fallback chain**: when using OpenRouter without a `--model` flag, automatically tries `qwen3-coder → llama-3.3-70b → gemma-3-27b → nemotron-120b` and rotates on overload (503)
- **Self-throttle**: spaces requests 9s apart to stay under free-tier rate limits (~8 rpm) without hitting 429s
- **Two output modes**: per-file folder with an `index.md`, or a single concatenated `.md` file
- **Smart file filtering**: skips binaries, lock files, build artifacts, images, and files over 100KB
- **Async + concurrent**: up to N simultaneous LLM calls (configurable), with per-request model fallback

## Installation

```bash
git clone https://github.com/Santa-Claws/docinator
cd docinator
pip install -r requirements.txt
```

Requires Python 3.10+ and `git` on PATH.

## Usage

```bash
python docinator.py <github_url> [options]
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--provider` | `openai` | `openai`, `openrouter`, or `ollama` |
| `--model` | see below | Model name. OpenRouter defaults to free chain |
| `--api-key` | env var | Overrides `OPENAI_API_KEY` / `OPENROUTER_API_KEY` |
| `--base-url` | auto | Override provider base URL (LM Studio, vLLM, etc.) |
| `--output-mode` | `per-file` | `per-file` (folder) or `single` (one `.md`) |
| `--output` | `./docs/<repo>` | Output path |
| `--max-concurrent` | `3` | Max simultaneous LLM requests |

### Examples

```bash
# OpenRouter — free model chain, per-file output
python docinator.py https://github.com/user/repo --provider openrouter --api-key sk-or-...

# Single output file
python docinator.py https://github.com/user/repo --provider openrouter --api-key sk-or-... --output-mode single

# Specific model
python docinator.py https://github.com/user/repo --provider openrouter \
  --model qwen/qwen3-coder:free --api-key sk-or-...

# OpenAI
python docinator.py https://github.com/user/repo --provider openai \
  --model gpt-4o-mini --api-key sk-proj-...

# Local Ollama (no API key needed)
python docinator.py https://github.com/user/repo --provider ollama --model llama3

# Custom OpenAI-compatible endpoint (LM Studio, vLLM, Together AI, etc.)
python docinator.py https://github.com/user/repo \
  --base-url http://localhost:1234/v1 --model my-model --api-key any
```

### Environment variables

```bash
export OPENAI_API_KEY=sk-proj-...
export OPENROUTER_API_KEY=sk-or-...
```

## Output structure

### Per-file mode (default)

```
docs/
└── repo-name/
    ├── index.md            # table of contents with relative links
    ├── README.md.md
    └── src/
        ├── main.py.md
        └── utils/
            └── helpers.py.md
```

### Single-file mode

```
docs/repo-name.md
```

Each file section is headed by `# path/to/file` and separated by `---`.

## What each doc contains

For every source file the LLM produces:

1. **Overview** — 2–5 sentence summary of the file's role and design decisions
2. **Line-by-line annotation** — every import, constant, class, function, loop, and expression explained: what it does, why it exists, inputs/outputs, edge cases
3. **Data flow** — how data enters, transforms, and exits the file
4. **Dependencies and coupling** — role of each import, implicit coupling to other modules
5. **Potential issues** — fragile patterns, magic numbers, non-obvious gotchas

## Free tier notes

OpenRouter free models are limited to ~8 requests/minute and ~50 requests/day per account. docinator self-throttles at 9s between requests and rotates through 4 free models on overload. For larger repos, add credits to your OpenRouter account (`--model` any paid model).

## Skipped files

docinator ignores: `.git`, `node_modules`, `__pycache__`, `dist`, `build`, virtual envs, binary files, images, fonts, lock files (`package-lock.json`, `poetry.lock`, etc.), compiled artifacts, and files over 100KB.

# docinator

Generate detailed LLM documentation for any GitHub repository.

## Installation

```bash
git clone https://github.com/Santa-Claws/docinator
cd docinator
pip install -r requirements.txt
```

Requires Python 3.10+ and `git` on PATH.

## Quick start

```bash
# OpenRouter (recommended — free model chain built in)
python docinator.py https://github.com/user/repo \
  --provider openrouter \
  --api-key sk-or-...

# OpenAI
python docinator.py https://github.com/user/repo \
  --provider openai \
  --api-key sk-proj-...

# Local Ollama (no key needed)
python docinator.py https://github.com/user/repo \
  --provider ollama \
  --model llama3
```

Output goes to `./docs/<repo-name>/` by default — one `.md` per source file plus an `index.md`.

## Output modes

```bash
# Per-file folder (default)
python docinator.py <url> --output-mode per-file

# Single concatenated file
python docinator.py <url> --output-mode single --output ./my-docs.md
```

## All options

| Flag | Default | Description |
|------|---------|-------------|
| `--provider` | `openai` | `openai`, `openrouter`, or `ollama` |
| `--model` | auto | Model name. OpenRouter defaults to free chain |
| `--api-key` | env var | Overrides `OPENAI_API_KEY` / `OPENROUTER_API_KEY` |
| `--base-url` | auto | Override base URL (LM Studio, vLLM, Together AI, etc.) |
| `--output-mode` | `per-file` | `per-file` or `single` |
| `--output` | `./docs/<repo>` | Output path |
| `--max-concurrent` | `3` | Concurrent LLM requests |

## Provider setup

**OpenAI** — set `OPENAI_API_KEY` or pass `--api-key`.

**OpenRouter** — set `OPENROUTER_API_KEY` or pass `--api-key`. Without `--model`, docinator cycles through a free model chain (`qwen3-coder → llama-3.3-70b → gemma-3-27b → nemotron-120b`) and rotates automatically on overload.

**Ollama** — just have Ollama running locally. No key required.

**Custom endpoint** — use `--base-url` to point at any OpenAI-compatible server.

## Free tier tips

OpenRouter free models cap at ~8 requests/minute and ~50/day. docinator self-throttles to stay under the rpm limit. For larger repos, add credits to your OpenRouter account or use a paid model.

# docinator.py

## Overview

`docinator.py` is a single-file Python CLI tool that clones a GitHub repository and generates exhaustive, developer-grade Markdown documentation for every source file using an LLM. It supports three providers (OpenAI, OpenRouter, Ollama) through a unified OpenAI-compatible client, applies smart file filtering to skip binaries and build artifacts, and drives concurrent LLM calls with a self-throttle mechanism to stay within free-tier rate limits. Output is either a folder of per-file Markdown documents mirroring the repo's directory structure, or a single concatenated file.

---

## Detailed Documentation

### Shebang and module docstring

```python
#!/usr/bin/env python3
"""docinator — Generate detailed LLM documentation for any GitHub repository."""
```

The shebang makes the script directly executable on Unix (`chmod +x docinator.py && ./docinator.py`). The module docstring is minimal — it names the tool and states its purpose in one line.

---

### Imports

```python
import argparse     # CLI argument parsing
import time         # time.monotonic() for the request throttle
import asyncio      # async/await event loop, Semaphore, gather
import os           # os.walk for directory traversal, os.environ for API keys
import shutil       # shutil.rmtree (temp cleanup), shutil.which (git check)
import subprocess   # subprocess.run to invoke git clone
import tempfile     # tempfile.mkdtemp for isolated clone directory
from dataclasses import dataclass, field  # Config struct (field imported but unused)
from datetime import date                 # date.today() in output headers
from pathlib import Path                  # type-safe file path operations

import openai       # OpenAI Python SDK — used for all three providers
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
```

**External dependencies** (`openai`, `rich`) are the only two packages in `requirements.txt`. Everything else is stdlib.

`field` is imported from `dataclasses` but not currently used — a minor leftover from an earlier version of the `Config` dataclass.

---

### File filtering constants

#### `SKIP_DIRS`

```python
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", "dist", "build", ".eggs", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".hypothesis", "coverage", ".coverage",
}
```

A set of directory names that are pruned from `os.walk` traversal in-place. Pruning in-place (mutating `dirnames[:]`) prevents descent into these trees entirely, which is much faster than filtering after the fact. Covers Python tooling artifacts, JS dependencies, and common build output directories.

#### `SKIP_EXTENSIONS`

```python
SKIP_EXTENSIONS = {
    ".lock", ".sum", ".min.js", ".min.css", ".map",
    ".png", ".jpg", ...  # images, fonts, archives, compiled objects
}
```

File extensions that are never worth documenting — lock files, minified assets, binary formats, compiled code. Checked against `path.suffix.lower()` so case is normalised.

#### `SKIP_FILENAMES`

```python
SKIP_FILENAMES = {
    "package-lock.json", "yarn.lock", "poetry.lock",
    "Pipfile.lock", "composer.lock", "Gemfile.lock",
    ".DS_Store", "Thumbs.db",
}
```

Exact filename matches that should be skipped regardless of extension. Necessary because `package-lock.json` has a `.json` extension that isn't in `SKIP_EXTENSIONS`, but its contents (thousands of lines of dependency hashes) are useless to document.

#### `MAX_FILE_BYTES`

```python
MAX_FILE_BYTES = 100_000  # 100 KB
```

Files larger than 100 KB are skipped to avoid exceeding model context windows and incurring large token costs. Checked via `path.stat().st_size` before reading. The value is a pragmatic heuristic — most source files that matter are under this threshold; files over it tend to be generated or data files.

#### `EXTENSION_LANGUAGE`

```python
EXTENSION_LANGUAGE = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ...
}
```

Maps file extensions to Markdown code fence language identifiers. Used to produce properly syntax-highlighted fenced code blocks in the output. Falls back to an empty string for unknown extensions (producing an unlabelled fence, which is still valid Markdown).

---

### `FREE_MODEL_CHAIN`

```python
FREE_MODEL_CHAIN = [
    "qwen/qwen3-coder:free",                    # 262K ctx, code-specialized
    "meta-llama/llama-3.3-70b-instruct:free",   # 65K ctx, reliable
    "google/gemma-3-27b-it:free",               # 131K ctx
    "nvidia/nemotron-3-super-120b-a12b:free",   # 262K ctx
]
```

An ordered list of OpenRouter free models used as a fallback chain when no `--model` is specified. `qwen3-coder` is first because it is purpose-built for code tasks and has the largest context window. The chain is tried in order: if a model returns HTTP 503 (overloaded), the next model is tried immediately. If it returns HTTP 429 (rate limited), it retries twice with a 20-second wait before moving to the next model.

---

### `DOC_PROMPT_TEMPLATE`

A module-level multi-line string that is the core of the tool's value. It instructs the LLM to produce:

1. A 2–5 sentence **overview** of the file's role and design decisions
2. **Line-by-line annotation** for every logical block — what it does, why it exists, inputs/outputs, side effects, edge cases
3. **Data flow** — how data enters, transforms, and exits
4. **Dependencies and coupling** — role of each import, implicit coupling
5. **Potential issues** — fragile patterns, magic numbers, non-obvious behaviour

The format section specifies exact Markdown structure with named headers, which makes the output consistent enough to be parsed or compared across files.

The template has four `{...}` substitution slots filled at call time in `document_file`: `{file_path}`, `{repo_url}`, `{language}`, `{file_contents}`.

`temperature=0.2` is passed to all completions calls (not in the template itself). This is deliberately low — documentation should be precise and deterministic, not creative.

---

### `Config` dataclass

```python
@dataclass
class Config:
    provider: str         # "openai" | "openrouter" | "ollama"
    models: list[str]     # primary first; fallbacks follow
    api_key: str
    base_url: str | None  # None = use provider default
    max_concurrent: int   # semaphore limit for document_file coroutines
    output_mode: str      # "per-file" | "single"
    output: str | None    # None = use default path
```

A plain dataclass used as a typed bag of configuration. There is no validation logic here — all validation happens in `main()` before construction. `models` is a list rather than a single string to support the fallback chain; callers always receive at least one model.

---

### `build_client(cfg)`

```python
def build_client(cfg: Config) -> openai.AsyncOpenAI:
```

**Input**: a `Config` instance.  
**Output**: an `openai.AsyncOpenAI` client configured for the chosen provider.

All three providers are OpenAI-API-compatible, so the same client class works for all of them. The only differences are `base_url` and `api_key`:

| Provider | `base_url` | `api_key` |
|---|---|---|
| `openai` | SDK default (`None`) | user-supplied |
| `openrouter` | `https://openrouter.ai/api/v1` | user-supplied |
| `ollama` | `http://localhost:11434/v1` | hardcoded `"ollama"` (server ignores it) |

If `cfg.base_url` is non-`None` (i.e. `--base-url` was passed), it takes precedence over the provider default. This is the escape hatch for LM Studio, vLLM, Together AI, and other OpenAI-compatible servers.

The client is constructed once in `run_async` and shared across all concurrent `document_file` calls. `AsyncOpenAI` is thread-safe and connection-pool-aware, so this is correct.

---

### `is_binary(path)`

```python
def is_binary(path: Path) -> bool:
```

**Input**: a `Path` to a file.  
**Output**: `True` if the file appears to be binary, `False` otherwise.

Reads the first 8,192 bytes and checks for a null byte (`\x00`). This is the same heuristic git itself uses. It is fast (only one small read regardless of file size) and accurate for the vast majority of real-world files. Returns `True` on `OSError` (unreadable file) to err on the side of skipping.

**Edge case**: some binary formats (e.g. certain UTF-16 encoded text files) may contain null bytes. These will be incorrectly classified as binary and skipped. This is an acceptable false positive for a documentation tool.

---

### `collect_files(repo_root)`

```python
def collect_files(repo_root: Path) -> list[Path]:
```

**Input**: the root of a cloned repository.  
**Output**: a sorted list of `Path` objects for all documentable source files.

Uses `os.walk`. At each directory level, `SKIP_DIRS` entries are pruned from `dirnames` in-place:

```python
dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
```

This slice-assignment mutation is a key `os.walk` pattern — it prevents the walk from descending into those directories at all, avoiding potentially millions of files in `node_modules` etc.

For each file, five filters are applied in order (cheapest first):
1. Exact filename match against `SKIP_FILENAMES`
2. Extension match against `SKIP_EXTENSIONS`
3. File size > `MAX_FILE_BYTES` (stat call, cheap)
4. Binary content check via `is_binary()` (one partial read)

All four must pass for a file to be included. The result is sorted for deterministic output ordering (alphabetical by full path).

---

### `clone_repo(url, target_dir)`

```python
def clone_repo(url: str, target_dir: Path) -> None:
```

**Input**: a GitHub URL and a target directory path (must not exist yet).  
**Output**: none. Raises `RuntimeError` on failure.

Runs `git clone --depth 1 <url> <target_dir>`. The `--depth 1` flag performs a shallow clone — only the latest commit is fetched, not the full history. This is significantly faster for large repos and is appropriate here since we only need current file contents.

`shutil.which("git")` is checked first to provide a clear error message if git is not installed, rather than a confusing `FileNotFoundError` from subprocess.

`capture_output=True` suppresses git's progress output to stderr during normal operation. If the clone fails, `result.stderr` is included in the `RuntimeError` message for debugging.

The target directory is always a fresh `tempfile.mkdtemp()` path — git requires the target to not exist, and `mkdtemp` guarantees a unique non-existent path.

---

### `document_file(client, models, repo_url, repo_root, file_path, semaphore)`

```python
async def document_file(...) -> tuple[Path, str]:
```

The core async worker. Called once per source file, concurrently across up to `max_concurrent` files.

**Inputs**:
- `client`: the shared `AsyncOpenAI` instance
- `models`: the ordered list of models to try
- `repo_url`: the original GitHub URL (included in the prompt for context)
- `repo_root`: the clone root (used to compute relative paths)
- `file_path`: absolute path to the file being documented
- `semaphore`: controls how many of these can run simultaneously

**Output**: `(file_path, markdown_string)`. On error, the markdown string is an italicised error message (e.g. `*API quota exceeded: ...*`) so failures are visible in the output without crashing the run.

**Execution flow**:

1. Acquires `semaphore` — blocks here if `max_concurrent` workers are already active.
2. Reads file contents as UTF-8 with `errors="replace"` (replacement character for undecodable bytes).
3. Formats `DOC_PROMPT_TEMPLATE` with the file's relative path, repo URL, language hint, and contents.
4. Iterates through `models`. For each model, attempts the API call up to 3 times:
   - **Success**: returns immediately with the LLM's output.
   - **`RateLimitError` with quota message**: returns an error string — no retries, the account has no credits.
   - **`RateLimitError` (temporary rate limit)**: sleeps 20 seconds and retries the same model. On the 3rd failure, moves to the next model.
   - **`APIStatusError` 503**: immediately `break`s the inner loop (no sleep) and moves to the next model. 503 means the model server is overloaded — waiting doesn't help, switching models does.
   - **Other `APIStatusError`**: sleeps 5 seconds and retries. On 3rd failure, moves to the next model.
   - **Generic `APIError`**: sleeps 5 seconds and retries. On 3rd failure, returns an error string.
5. If all models are exhausted, returns an error string listing all models tried.

**Data flow**: `file_path` (disk) → `contents` (string) → `prompt` (string) → LLM API (network) → `response.choices[0].message.content` (string) → returned as `(file_path, markdown)`.

---

### `run_async(files, repo_root, repo_url, cfg)`

```python
async def run_async(...) -> list[tuple[Path, str]]:
```

Orchestrates all `document_file` calls concurrently. Called once per run via `asyncio.run()`.

**Key mechanics**:

**`semaphore`** — limits the number of files being actively processed simultaneously to `cfg.max_concurrent`. This controls memory pressure and API concurrency.

**Self-throttle** — a second coordination mechanism on top of the semaphore:

```python
request_gate = asyncio.Semaphore(1)
last_request_time: list[float] = [0.0]
```

`request_gate` is a binary semaphore (mutex). Each worker acquires it, checks how long ago the last request was launched, sleeps up to 9 seconds if needed, updates `last_request_time`, then releases it. This ensures requests are launched no faster than one every 9 seconds (~6.5 rpm), safely under the free-tier 8 rpm cap.

`last_request_time` is a `list[float]` rather than a bare `float` because Python closures cannot rebind names in enclosing scopes — only mutate objects. A single-element list is the idiomatic workaround.

**`asyncio.gather`** — fans out all worker coroutines simultaneously and collects results in the original file order (gather preserves order regardless of completion order).

**Rich progress bar** — the `Progress` context manager renders a live terminal progress bar. Each worker calls `progress.advance(task)` after completing, so the bar updates as files finish.

---

### `write_per_file_output(results, repo_root, output_dir, repo_url)`

```python
def write_per_file_output(...) -> None:
```

Writes one `.md` file per source file, mirroring the repo's directory structure under `output_dir`.

For example, `src/utils/helpers.py` in the repo becomes `output_dir/src/utils/helpers.py.md`. The `.md` extension is appended (not substituted) so that `main.py` and `main.py.md` cannot collide.

`out_path.parent.mkdir(parents=True, exist_ok=True)` creates intermediate directories as needed. `exist_ok=True` makes it idempotent if the directory already exists.

Also writes `output_dir/index.md` — a table of contents with a relative Markdown link to every generated file. Windows path separators are normalised to `/` for cross-platform compatibility.

---

### `write_single_output(results, repo_root, output_file, repo_url)`

```python
def write_single_output(...) -> None:
```

Concatenates all documentation into a single file. Structure:

```
# Documentation

**Repository**: <url>
**Generated**: <date>

---

# path/to/file1.py

<LLM output>

---

# path/to/file2.py

<LLM output>

---
```

Files appear in the order `asyncio.gather` returned them, which matches the sorted order from `collect_files` (alphabetical by path).

---

### `resolve_api_key(args)`

```python
def resolve_api_key(args: argparse.Namespace) -> str:
```

Priority order for the API key:
1. `--api-key` CLI argument
2. `OPENROUTER_API_KEY` env var (when `--provider openrouter`)
3. `OPENAI_API_KEY` env var (checked for all providers except Ollama)
4. Hardcoded `"ollama"` string (when `--provider ollama` — the server ignores it)

Returns an empty string if no key is found, which `main()` checks and rejects with a user-friendly error.

---

### `repo_name_from_url(url)`

```python
def repo_name_from_url(url: str) -> str:
```

Extracts the repository name from a URL for use in default output paths. Strips trailing slashes, takes the last path component, and removes a `.git` suffix if present. Falls back to `"repo"` if the result is empty.

Examples:
- `https://github.com/user/my-project` → `my-project`
- `https://github.com/user/my-project.git` → `my-project`
- `https://github.com/user/` → `repo`

---

### `main()`

The CLI entrypoint. Executed when the script is run directly.

**Argument parsing** — `argparse.ArgumentParser` with `RawDescriptionHelpFormatter` (preserves newlines in help text). All flags have sensible defaults; only `url` is positional and required.

**Model list construction** — after parsing, a `models` list is built:
- If `--model` is given and provider is OpenRouter: user model goes first, then the rest of `FREE_MODEL_CHAIN` as fallbacks.
- If `--model` is given for other providers: only that model, no fallbacks.
- If no `--model` and provider is OpenRouter: full `FREE_MODEL_CHAIN`.
- Otherwise: `["gpt-4o-mini"]` (OpenAI default).

**Temp directory lifecycle**:

```python
tmpdir = Path(tempfile.mkdtemp())
try:
    clone_repo(args.url, tmpdir)
    ...
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)
```

`try/finally` guarantees the clone is deleted even if an exception occurs mid-run. `ignore_errors=True` prevents a secondary exception during cleanup from masking the original one.

**Error summary** — after `run_async` completes, counts results whose markdown starts and ends with `*` (the error string format) and prints a warning if any failed.

---

## Data flow (end to end)

```
CLI args
  └─▶ main()
        ├─▶ clone_repo()         git clone → tmpdir/
        ├─▶ collect_files()      walk tmpdir → [Path, ...]
        └─▶ asyncio.run(run_async())
              ├─▶ build_client()         → AsyncOpenAI
              └─▶ asyncio.gather(worker × N)
                    └─▶ document_file()
                          ├─▶ read file   → str
                          ├─▶ format prompt
                          └─▶ API call    → markdown str
                    └─▶ [(Path, markdown), ...]
        ├─▶ write_per_file_output()  OR
        └─▶ write_single_output()
              └─▶ .md files on disk
  finally: shutil.rmtree(tmpdir)
```

---

## Potential issues

**`field` imported but unused** — `from dataclasses import dataclass, field` — `field` is not used in `Config`. Harmless but slightly misleading.

**`last_request_time` list trick** — the single-element list closure workaround is correct but non-obvious. A future refactor to a class or `nonlocal` would be cleaner.

**No timeout on LLM calls** — if a model hangs (rare but possible with self-hosted endpoints or unusual network conditions), `document_file` will block indefinitely on that file. Adding `timeout=120` to `client.chat.completions.create()` would bound this.

**503 detection via `APIStatusError`** — `openai.RateLimitError` is a subclass of `openai.APIStatusError`, so the `except openai.RateLimitError` clause must come before `except openai.APIStatusError` in the handler chain. The current code has this correct, but it's a subtle ordering dependency.

**`errors="replace"` in file reading** — undecodable bytes are replaced with `\ufffd`. The LLM will see replacement characters in the prompt, which may confuse it for files with significant non-UTF-8 content. An alternative would be to try multiple encodings (latin-1, cp1252) before falling back.

**No deduplication of model chain** — if the user passes `--model qwen/qwen3-coder:free` with `--provider openrouter`, the model list becomes `["qwen/qwen3-coder:free", "meta-llama/...", "google/...", "nvidia/..."]` — correct, no duplicate. But if they pass a model that isn't in `FREE_MODEL_CHAIN`, the chain is just prepended, which is the intended behaviour.

---

## Summary

`docinator.py` is a self-contained async Python CLI that turns a GitHub URL into structured LLM-generated documentation. Its three key design choices are: (1) using the OpenAI SDK for all providers to avoid per-provider abstraction overhead, (2) a model fallback chain that rotates on overload rather than waiting, and (3) a self-throttle that proactively spaces requests to avoid hitting rate limits in the first place rather than reacting to them.

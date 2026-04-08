#!/usr/bin/env python3
"""docinator — Generate detailed LLM documentation for any GitHub repository."""

import argparse
import asyncio
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import openai
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

console = Console()

# ---------------------------------------------------------------------------
# File filtering
# ---------------------------------------------------------------------------

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", "dist", "build", ".eggs", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".hypothesis", "coverage", ".coverage",
}

SKIP_EXTENSIONS = {
    ".lock", ".sum", ".min.js", ".min.css", ".map",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".pyc", ".pyo", ".pyd", ".class", ".o", ".so",
    ".dylib", ".dll", ".exe", ".bin", ".dat", ".db", ".sqlite",
}

SKIP_FILENAMES = {
    "package-lock.json", "yarn.lock", "poetry.lock",
    "Pipfile.lock", "composer.lock", "Gemfile.lock",
    ".DS_Store", "Thumbs.db",
}

MAX_FILE_BYTES = 100_000  # 100 KB

EXTENSION_LANGUAGE = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "jsx", ".go": "go", ".rs": "rust",
    ".rb": "ruby", ".java": "java", ".kt": "kotlin", ".swift": "swift",
    ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
    ".cs": "csharp", ".php": "php", ".sh": "bash", ".zsh": "bash",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".json": "json",
    ".md": "markdown", ".html": "html", ".css": "css", ".scss": "scss",
    ".sql": "sql", ".tf": "hcl", ".lua": "lua", ".r": "r",
    ".jl": "julia", ".ex": "elixir", ".exs": "elixir",
    ".clj": "clojure", ".hs": "haskell", ".ml": "ocaml",
    ".dockerfile": "dockerfile",
}

# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

DOC_PROMPT_TEMPLATE = """\
You are an expert software documentation engineer. Your task is to produce \
exhaustive, developer-grade documentation for the source file provided below.

## Instructions

1. **Overview section**: Begin with a concise summary (2-5 sentences) of what \
this file does, its role in the codebase, and any important design decisions \
or patterns it embodies.

2. **Line-by-line annotation**: Go through the entire file systematically. \
For every logical block — imports, constants, class definitions, method \
definitions, standalone functions, decorators, conditionals, loops, and \
non-obvious expressions — provide a clear explanation of:
   - What it does
   - Why it exists (its purpose in context)
   - Any side effects, assumptions, or edge cases
   - What inputs it expects and outputs it produces (for functions/methods)

3. **Data flow**: Where applicable, describe how data enters the file, \
transforms through functions or methods, and exits (return values, side \
effects, written files, network calls, etc.).

4. **Dependencies and coupling**: Note any external imports and explain what \
role each plays. Flag any implicit coupling to other modules or global state.

5. **Potential issues**: Call out anything that looks fragile, undocumented, \
or that a new developer might misunderstand — e.g. mutable defaults, \
non-obvious exception handling, magic numbers, etc.

## Output Format

Produce a single Markdown document structured as follows:

# <filename>

## Overview
<overview text>

## Detailed Documentation

### <Section name, e.g. "Imports", "Constants", "Class Foo", "Function bar">
<explanation>

```<language>
<relevant code snippet(s) with inline comments added>
```

<further prose explanation if needed>

... (repeat for every logical section) ...

## Summary
<1-3 sentence summary of the file's purpose and key takeaways>

Do not truncate. Document every line or logical group.

## File to Document

**Path**: `{file_path}`
**Repository**: `{repo_url}`

```{language}
{file_contents}
```
"""

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    provider: str
    model: str
    api_key: str
    base_url: str | None
    max_concurrent: int
    output_mode: str
    output: str | None


def build_client(cfg: Config) -> openai.AsyncOpenAI:
    provider = cfg.provider
    api_key = cfg.api_key
    base_url = cfg.base_url

    if base_url is None:
        if provider == "openrouter":
            base_url = "https://openrouter.ai/api/v1"
        elif provider == "ollama":
            base_url = "http://localhost:11434/v1"
            if not api_key:
                api_key = "ollama"

    return openai.AsyncOpenAI(api_key=api_key, base_url=base_url)


# ---------------------------------------------------------------------------
# File utilities
# ---------------------------------------------------------------------------

def is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except OSError:
        return True


def collect_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name in SKIP_FILENAMES:
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() in SKIP_EXTENSIONS:
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            if is_binary(p):
                continue
            files.append(p)
    return sorted(files)


# ---------------------------------------------------------------------------
# Git clone
# ---------------------------------------------------------------------------

def clone_repo(url: str, target_dir: Path) -> None:
    if not shutil.which("git"):
        raise RuntimeError("git is not installed or not on PATH")
    result = subprocess.run(
        ["git", "clone", "--depth", "1", url, str(target_dir)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed:\n{result.stderr.strip()}")


# ---------------------------------------------------------------------------
# LLM documentation
# ---------------------------------------------------------------------------

async def document_file(
    client: openai.AsyncOpenAI,
    model: str,
    repo_url: str,
    repo_root: Path,
    file_path: Path,
    semaphore: asyncio.Semaphore,
) -> tuple[Path, str]:
    async with semaphore:
        try:
            contents = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return file_path, f"*Error reading file: {e}*"

        rel = file_path.relative_to(repo_root)
        lang = EXTENSION_LANGUAGE.get(file_path.suffix.lower(), "")
        prompt = DOC_PROMPT_TEMPLATE.format(
            file_path=rel,
            repo_url=repo_url,
            language=lang,
            file_contents=contents,
        )

        for attempt in range(2):
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                )
                return file_path, response.choices[0].message.content or ""
            except openai.RateLimitError as e:
                # insufficient_quota = no credits, retrying won't help
                if "insufficient_quota" in str(e) or "quota" in str(e).lower():
                    return file_path, f"*API quota exceeded (add credits to your account): {e}*"
                if attempt == 0:
                    await asyncio.sleep(5)
                else:
                    return file_path, f"*Rate limit error after retry: {e}*"
            except openai.APIError as e:
                if attempt == 0:
                    await asyncio.sleep(2)
                else:
                    return file_path, f"*LLM error after retry: {e}*"

    return file_path, "*Unknown error*"


async def run_async(
    files: list[Path],
    repo_root: Path,
    repo_url: str,
    cfg: Config,
) -> list[tuple[Path, str]]:
    client = build_client(cfg)
    semaphore = asyncio.Semaphore(cfg.max_concurrent)

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Documenting files...", total=len(files))

        async def worker(fp: Path) -> tuple[Path, str]:
            result = await document_file(client, cfg.model, repo_url, repo_root, fp, semaphore)
            progress.advance(task)
            return result

        results = await asyncio.gather(*[worker(f) for f in files])

    return list(results)


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_per_file_output(
    results: list[tuple[Path, str]],
    repo_root: Path,
    output_dir: Path,
    repo_url: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    index_lines = [
        "# Documentation Index\n",
        f"**Repository**: {repo_url}  \n",
        f"**Generated**: {date.today()}  \n",
        "\n## Files\n",
    ]

    for file_path, markdown in results:
        rel = file_path.relative_to(repo_root)
        out_path = output_dir / (str(rel) + ".md")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")
        link = str(rel).replace("\\", "/")
        index_lines.append(f"- [{link}]({link}.md)\n")

    (output_dir / "index.md").write_text("".join(index_lines), encoding="utf-8")
    console.print(f"[green]Wrote {len(results)} docs to[/green] {output_dir}/")


def write_single_output(
    results: list[tuple[Path, str]],
    repo_root: Path,
    output_file: Path,
    repo_url: str,
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        "# Documentation\n\n",
        f"**Repository**: {repo_url}  \n",
        f"**Generated**: {date.today()}  \n\n",
        "---\n\n",
    ]
    for file_path, markdown in results:
        rel = file_path.relative_to(repo_root)
        parts.append(f"# {rel}\n\n")
        parts.append(markdown)
        parts.append("\n\n---\n\n")
    output_file.write_text("".join(parts), encoding="utf-8")
    console.print(f"[green]Wrote documentation to[/green] {output_file}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def resolve_api_key(args: argparse.Namespace) -> str:
    if args.api_key:
        return args.api_key
    if args.provider == "openrouter":
        return os.environ.get("OPENROUTER_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
    if args.provider == "ollama":
        return "ollama"
    return os.environ.get("OPENAI_API_KEY", "")


def repo_name_from_url(url: str) -> str:
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or "repo"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate detailed LLM documentation for a GitHub repository.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("url", help="GitHub repository URL")
    parser.add_argument(
        "--provider",
        choices=["openai", "openrouter", "ollama"],
        default="openai",
        help="LLM provider (default: openai)",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="Model name (default: gpt-4o-mini)",
    )
    parser.add_argument("--api-key", help="API key (overrides env vars)")
    parser.add_argument("--base-url", help="Override provider base URL")
    parser.add_argument(
        "--output-mode",
        choices=["per-file", "single"],
        default="per-file",
        help="Output mode: per-file folder or single markdown file (default: per-file)",
    )
    parser.add_argument(
        "--output",
        help="Output path (directory for per-file, file for single). Default: ./docs/<repo-name>[.md]",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=5,
        help="Max concurrent LLM requests (default: 5)",
    )
    args = parser.parse_args()

    api_key = resolve_api_key(args)
    if not api_key and args.provider != "ollama":
        console.print("[red]Error:[/red] No API key provided. Use --api-key or set OPENAI_API_KEY.")
        raise SystemExit(1)

    cfg = Config(
        provider=args.provider,
        model=args.model,
        api_key=api_key,
        base_url=args.base_url,
        max_concurrent=args.max_concurrent,
        output_mode=args.output_mode,
        output=args.output,
    )

    repo_name = repo_name_from_url(args.url)
    if args.output:
        output_path = Path(args.output)
    elif args.output_mode == "per-file":
        output_path = Path("docs") / repo_name
    else:
        output_path = Path("docs") / f"{repo_name}.md"

    tmpdir = Path(tempfile.mkdtemp())
    try:
        console.print(f"[blue]Cloning[/blue] {args.url} ...")
        clone_repo(args.url, tmpdir)

        files = collect_files(tmpdir)
        console.print(f"[blue]Found[/blue] {len(files)} files to document")

        if not files:
            console.print("[yellow]No documentable files found.[/yellow]")
            return

        results = asyncio.run(run_async(files, tmpdir, args.url, cfg))

        if args.output_mode == "per-file":
            write_per_file_output(results, tmpdir, output_path, args.url)
        else:
            write_single_output(results, tmpdir, output_path, args.url)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()

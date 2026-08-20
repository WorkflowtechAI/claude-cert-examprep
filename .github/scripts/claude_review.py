"""Generic Claude code-review for a pull request or full-codebase snapshot.

Dropped into a repo by operator-tools/Bootstrap-Repo.ps1 as
.github/scripts/claude_review.py and driven by .github/workflows/claude-review.yml.
Unlike the kit's own tuned reviewer, this one is project-agnostic: it names no
specific repo and uses language-neutral file patterns, so the same script works
in any bootstrapped repo. Set the REVIEW_PROJECT_NAME env var (the workflow does)
to give the reviewer the repo name; everything else has safe defaults.
"""

import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

MAX_REVIEW_CHARS = 120_000
# Must match the default in .github/workflows/claude-review.yml.
DEFAULT_CLAUDE_REVIEW_MODEL = "claude-sonnet-5"
# Tunable per repo without editing a vendored file. This script is copied into
# every managed repo, so a hardcoded ceiling would mean editing N copies to
# change one number; this mirrors CLAUDE_REVIEW_MODEL instead.
DEFAULT_CLAUDE_REVIEW_MAX_TOKENS = 8192
DEFAULT_INPUT_PRICE_USD_PER_MILLION = 3.0
DEFAULT_OUTPUT_PRICE_USD_PER_MILLION = 15.0
DEFAULT_CACHE_CREATION_INPUT_PRICE_MULTIPLIER = 1.25
DEFAULT_CACHE_READ_INPUT_PRICE_MULTIPLIER = 0.10

# Language-neutral: review source, config, and docs across common stacks.
ALLOW_PATTERNS = [
    "*.py", "*.js", "*.ts", "*.jsx", "*.tsx", "*.mjs", "*.cjs",
    "*.go", "*.rs", "*.rb", "*.java", "*.kt", "*.cs", "*.php", "*.swift",
    "*.c", "*.h", "*.cpp", "*.hpp", "*.sh", "*.ps1",
    "*.md", "*.yml", "*.yaml", "*.toml", "*.json", "*.sql",
    "**/*.py", "**/*.js", "**/*.ts", "**/*.tsx", "**/*.go", "**/*.rs",
    "**/*.rb", "**/*.java", "**/*.cs", "**/*.sql", ".github/**/*.yml",
]
EXCLUDE_PATTERNS = [
    "**/node_modules/**", "**/.next/**", "**/dist/**", "**/build/**",
    "**/.venv/**", "**/venv/**", "**/vendor/**", "**/__pycache__/**",
    "*.lock", "**/package-lock.json", "**/pnpm-lock.yaml", "**/yarn.lock",
    "**/uv.lock", "**/poetry.lock", "**/Cargo.lock", "**/*.min.js", "**/*.map",
]


def run_git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8", errors="replace")


def base_head() -> tuple[str, str]:
    base = os.getenv("BASE_SHA") or ""
    head = os.getenv("HEAD_SHA") or ""
    if not base:
        base = run_git(["rev-parse", "HEAD~1"]).strip()
    if not head:
        head = run_git(["rev-parse", "HEAD"]).strip()
    return base, head


SECRET_PATTERNS = [
    (
        re.compile(
            r"(?i)(api[_-]?key|token|secret|password|passwd|client[_-]?secret)"
            r"\s*[:=]\s*[^\s'\"]+"
        ),
        r"\1=<REDACTED>",
    ),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "sk-<REDACTED>"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA<REDACTED>"),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S
        ),
        "<REDACTED_PRIVATE_KEY>",
    ),
]


def redact(text: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def include_file(path: str) -> bool:
    allowed = any(fnmatch.fnmatch(path, pattern) for pattern in ALLOW_PATTERNS)
    excluded = any(fnmatch.fnmatch(path, pattern) for pattern in EXCLUDE_PATTERNS)
    return allowed and not excluded


def pr_diff() -> str:
    pr_number = os.getenv("PR_NUMBER") or ""
    repo = os.getenv("GITHUB_REPOSITORY") or ""
    token = os.getenv("GH_TOKEN") or ""
    if not pr_number or not repo:
        return ""

    patches = []
    page = 1
    while True:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}",
            headers={
                "accept": "application/vnd.github+json",
                "authorization": f"Bearer {token}",
                "x-github-api-version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            files = json.loads(response.read().decode("utf-8"))
        if not files:
            break
        for file_info in files:
            filename = file_info.get("filename", "")
            patch = file_info.get("patch", "")
            if filename and patch and include_file(filename):
                patches.append(f"diff --git a/{filename} b/{filename}\n{patch}")
        page += 1
    return "\n".join(patches)


def diff_text(base: str, head: str) -> str:
    diff = pr_diff()
    if not diff:
        pathspecs = [*ALLOW_PATTERNS, *[f":!{pattern}" for pattern in EXCLUDE_PATTERNS]]
        diff = run_git(["diff", "--unified=80", base, head, "--", *pathspecs])
    return redact(diff)[:MAX_REVIEW_CHARS]


def codebase_snapshot() -> str:
    paths = sorted(path for path in run_git(["ls-files"]).splitlines() if include_file(path))
    sections = []
    total = 0
    manifest = redact("--- REVIEW SNAPSHOT ORDER ---\n" + "\n".join(paths) + "\n")
    sections.append(manifest[: min(len(manifest), 8_000)])
    total += len(sections[-1])
    for path in paths:
        try:
            content = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        section = redact(f"--- FILE: {path} ---\n{content}\n")
        if total + len(section) > MAX_REVIEW_CHARS:
            remaining = MAX_REVIEW_CHARS - total
            if remaining > 200:
                sections.append(section[:remaining] + "\n--- SNAPSHOT TRUNCATED ---\n")
            break
        sections.append(section)
        total += len(section)
    return "\n".join(sections)


def write_review(text: str) -> None:
    with open("claude-review.md", "w", encoding="utf-8") as handle:
        handle.write(text.strip() + "\n")


def price_env(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def usage_summary(model: str, usage: dict[str, int] | None) -> str:
    if not usage:
        return ""
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    cache_creation_tokens = int(usage.get("cache_creation_input_tokens", 0) or 0)
    cache_read_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
    input_price = price_env(
        "CLAUDE_REVIEW_INPUT_PRICE_USD_PER_MILLION", DEFAULT_INPUT_PRICE_USD_PER_MILLION
    )
    output_price = price_env(
        "CLAUDE_REVIEW_OUTPUT_PRICE_USD_PER_MILLION", DEFAULT_OUTPUT_PRICE_USD_PER_MILLION
    )
    cache_creation_multiplier = price_env(
        "CLAUDE_REVIEW_CACHE_CREATION_INPUT_PRICE_MULTIPLIER",
        DEFAULT_CACHE_CREATION_INPUT_PRICE_MULTIPLIER,
    )
    cache_read_multiplier = price_env(
        "CLAUDE_REVIEW_CACHE_READ_INPUT_PRICE_MULTIPLIER",
        DEFAULT_CACHE_READ_INPUT_PRICE_MULTIPLIER,
    )
    estimated_cost = (
        (input_tokens * input_price)
        + (cache_creation_tokens * input_price * cache_creation_multiplier)
        + (cache_read_tokens * input_price * cache_read_multiplier)
        + (output_tokens * output_price)
    ) / 1_000_000
    lines = [
        "## Claude Review Usage", "",
        f"- Model: `{model}`",
        f"- Input tokens: `{input_tokens}`",
        f"- Output tokens: `{output_tokens}`",
    ]
    if cache_creation_tokens or cache_read_tokens:
        lines.extend([
            f"- Cache creation input tokens: `{cache_creation_tokens}`",
            f"- Cache read input tokens: `{cache_read_tokens}`",
        ])
    lines.extend([
        f"- Approximate estimated cost: `${estimated_cost:.6f}`",
        f"- Pricing assumption: `${input_price:g}/M input`, `${output_price:g}/M output`, "
        f"`{cache_creation_multiplier:g}x` cache creation, `{cache_read_multiplier:g}x` cache read",
        "- Anthropic billing is the source of truth.",
    ])
    return "\n".join(lines)


def review_text_from_body(body: dict) -> str:
    """Turn an API response body into the review text to post.

    Pure and network-free ON PURPOSE: this is the logic that silently failed,
    and the point of extracting it is that it can be tested against recorded
    response shapes without an API key or a live call.

    FIND the text block; do not assume it is content[0]. The original read
    `content[0].get("text", "")`, which is only correct when the first block IS
    the answer. Newer models emit a `thinking` block first, and a thinking
    block has no "text" field, so the review came back empty while the API had
    already generated (and billed) the full thing: PRs #23 and #24 posted
    "No review text returned" on ~2,048 output tokens of real spend, and both
    went GREEN. A check that pays, reports success, and verifies nothing is the
    exact failure this repo's CI exists to refuse.
    """
    content = body.get("content", [])
    text = "\n\n".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()

    stop_reason = body.get("stop_reason", "unknown")

    if not text:
        kinds = ", ".join(
            b.get("type", "?") for b in content if isinstance(b, dict)
        ) or "none"
        # A truncated answer and an empty response are different problems with
        # different fixes, so say which one happened.
        return (
            "**No review text came back from the API.** This is a tooling "
            f"failure, not a clean bill of health.\n\n- stop_reason: `{stop_reason}`\n"
            f"- content block types: `{kinds}`\n\n"
            "If stop_reason is `max_tokens`, raise the "
            "`CLAUDE_REVIEW_MAX_TOKENS` environment variable."
        )

    # A TRUNCATED REVIEW IS NOT A CLEAN REVIEW. The empty-text branch above
    # only catches a review that returned nothing; one that ran to the ceiling
    # comes back NON-empty and looks complete while its last finding is cut
    # mid-sentence, which reads as "all clear" to anyone skimming.
    if stop_reason == "max_tokens":
        return (
            "> **Truncated: this review hit the output-token ceiling and is "
            "incomplete.** Findings below may be cut off mid-thought, and "
            "later findings may be missing entirely. Raise the "
            "`CLAUDE_REVIEW_MAX_TOKENS` environment variable.\n\n" + text
        )

    return text


def call_claude(review_text: str, review_scope: str = "diff") -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return "## Claude Code Review\n\nSkipped: `ANTHROPIC_API_KEY` is not configured."

    project = (os.getenv("REVIEW_PROJECT_NAME") or "this repository").strip()
    common = (
        "Focus on correctness, security, secret handling, deployment risk, tests, "
        "input validation, error handling, and maintainability. Use any CONTRIBUTING, "
        "AGENTS.md, or docs/standards guidance present in the repo. Do not ask for or "
        "reveal secrets; if a value appears redacted, treat that as intentional. "
        "Prioritize concrete, specific findings over generic advice."
    )
    if review_scope == "full":
        instructions = (
            f"You are reviewing a production-bound full codebase snapshot for {project}. "
            "The snapshot is size-limited and begins with a file-order manifest. " + common
        )
        review_block = f"Codebase snapshot:\n```text\n{review_text}\n```"
    else:
        instructions = (
            f"You are reviewing a production-bound pull-request diff for {project}. "
            + common
            + " Be concise."
        )
        review_block = f"Diff:\n```diff\n{review_text}\n```"
    model = os.getenv("CLAUDE_REVIEW_MODEL", DEFAULT_CLAUDE_REVIEW_MODEL)
    try:
        max_tokens = int(
            os.getenv("CLAUDE_REVIEW_MAX_TOKENS", DEFAULT_CLAUDE_REVIEW_MAX_TOKENS)
        )
    except ValueError:
        max_tokens = DEFAULT_CLAUDE_REVIEW_MAX_TOKENS
    payload = {
        # 2048 was cutting it close enough to matter: an observed review used
        # 2,036 output tokens of the 2,048 available and another landed on
        # exactly 2,048, so a slightly longer diff gets truncated mid-finding.
        # Review length should be bounded by the instruction to be concise,
        # not by the ceiling.
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": instructions},
                    {"type": "text", "text": review_block, "cache_control": {"type": "ephemeral"}},
                ],
            }
        ],
    }
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        # 60s was sized when max_tokens was 2048 and every review came back
        # truncated. Raising the ceiling to 8192 made a COMPLETE review take
        # longer than the timeout allowed, so the job started dying on
        # TimeoutError instead of posting. The read has to outlast the
        # generation it asked for.
        with urllib.request.urlopen(request, timeout=300) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        return (
            f"## Claude Code Review\n\nClaude API call failed: HTTP {exc.code}."
            f"\n\n```text\n{detail}\n```"
        )

    text = review_text_from_body(body)

    parts = ["## Claude Code Review\n\n" + text]
    usage = usage_summary(model, body.get("usage"))
    if usage:
        parts.append(usage)
    return "\n\n".join(parts)


def main() -> int:
    review_scope = os.getenv("REVIEW_SCOPE", "diff").strip().lower()
    if review_scope == "full":
        review_text = codebase_snapshot()
    else:
        base, head = base_head()
        review_text = diff_text(base, head)
    if not review_text.strip():
        write_review("## Claude Code Review\n\nSkipped: no reviewable diff.")
        return 0
    write_review(call_claude(review_text, review_scope=review_scope))
    return 0


if __name__ == "__main__":
    sys.exit(main())

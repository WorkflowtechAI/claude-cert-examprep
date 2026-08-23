# AUTO-SYNCED from the LLM Builder Kit. Do not edit here; edit the kit
# source and re-run sync-standards.ps1.

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
# Must match Anthropic's model ID and the default in .github/workflows/claude-review.yml.
DEFAULT_CLAUDE_REVIEW_MODEL = "claude-sonnet-5"
# Tunable per repo without editing a vendored file. This script is copied into
# every managed repo, so a hardcoded ceiling would mean editing N copies to
# change one number; this mirrors CLAUDE_REVIEW_MODEL instead.
#
# 8192 was NOT enough. This ceiling covers thinking AND the answer, and thinking
# is not bounded by "be concise": observed reviews spent the entire 8,192-token
# budget on reasoning and emitted ten words of text, billing real money for the
# phrase "This diff" and passing green, while others truncated mid-finding. The
# answer is the tail of the budget, so the budget has to be sized for the
# reasoning in front of it. Repos that raised CLAUDE_REVIEW_MAX_TOKENS in their
# own workflow to work around this no longer need to.
DEFAULT_CLAUDE_REVIEW_MAX_TOKENS = 32000
DEFAULT_INPUT_PRICE_USD_PER_MILLION = 3.0
DEFAULT_OUTPUT_PRICE_USD_PER_MILLION = 15.0
DEFAULT_CACHE_CREATION_INPUT_PRICE_MULTIPLIER = 1.25
DEFAULT_CACHE_READ_INPUT_PRICE_MULTIPLIER = 0.10

# Language-neutral: review source, config, and docs across common stacks.
# fnmatch's `*` matches `/` too, so "*.py" already covers every depth; a
# "**/*.py" form would be the same pattern written twice.
ALLOW_PATTERNS = [
    "*.py", "*.js", "*.ts", "*.jsx", "*.tsx", "*.mjs", "*.cjs",
    "*.go", "*.rs", "*.rb", "*.java", "*.kt", "*.cs", "*.php", "*.swift",
    "*.c", "*.h", "*.cpp", "*.hpp", "*.sh", "*.ps1",
    "*.md", "*.yml", "*.yaml", "*.toml", "*.json", "*.sql",
]
EXCLUDE_PATTERNS = [
    "**/node_modules/**", "**/.next/**", "**/dist/**", "**/build/**",
    "**/.venv/**", "**/venv/**", "**/vendor/**", "**/__pycache__/**",
    "*.lock", "**/package-lock.json", "**/pnpm-lock.yaml", "**/yarn.lock",
    "**/uv.lock", "**/poetry.lock", "**/Cargo.lock", "**/*.min.js", "**/*.map",
]
# A closed set of type words. None is ever a secret, so a bare one after
# `password:` is an annotation; the SECRET_PATTERNS comment says how it is used.
_TYPE_WORD = (
    r"(?:str|int|float|boolean|bool|bytes|string|number|any|unknown|object"
    r"|dict|list|null|None|undefined)\b"
)
_TYPE = _TYPE_WORD + r"(?:[ \t]*\|[ \t]*" + _TYPE_WORD + r")*"

SECRET_PATTERNS = [
    # The key=value rule. A heuristic last line in front of the model, not a
    # substitute for keeping secrets out of commits: the name list is short on
    # purpose, and every shape below is pinned by an exact-output test in
    # test_claude_review.py, in both copies; the kit's suite also checks that
    # this table and the template's are identical.
    #
    # NAMES. Unanchored at the start, so `db_password` and `STRIPE_SECRET_KEY`
    # match on their tail; closed at the end by the quote, space or separator
    # that has to follow, so `passwordless`, `token_url` and `tokens` are other
    # names and stay as written. The `_key` family is spelled out because the
    # tail rule cannot reach it: `SECRET_KEY` (Django), `SECRET_KEY_BASE`
    # (Rails), `AWS_SECRET_ACCESS_KEY`, `PRIVATE_KEY`, `ACCESS_KEY`. A bare
    # `key` is not a name: `primary_key=True` and `for key, value` are code.
    #
    # SEPARATOR. `:`, `=`, `:=` (walrus) or `=>` (PHP array, match arm), and
    # never the first character of `==`, `===` or `=>`: `password == other` is
    # a comparison, and matching its first `=` redacted the second and left
    # the line unable to parse. `'password' => 'hunter2'` used to slip through
    # the same gap as the JSON key below. A match arm, `token => x` or
    # `token => f(x)`, is redacted, an accepted over-redaction.
    #
    # A `${{ secrets.X }}` expression names a secret without containing one.
    # Redacting it rewrote `ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}`
    # into `API_KEY=<REDACTED> secrets... }}` before the model saw the diff, and
    # the model then reported the workflow as broken YAML, as a blocking
    # finding, on a line that was fine. The lookahead leaves expressions alone,
    # quoted or bare, and with the stray space of `" ${{ secrets.X }}"`: that
    # is still an expression, and the space is a workflow bug the model can
    # only flag if it gets to see it.
    #
    # A quoted value is a value too, and it runs to its closing quote, spaces
    # included: `password: "abc123"` never matched when the class excluded the
    # opening quote, and `password: "correct horse battery"` lost only its first
    # word. An escaped quote inside the value does not close it: `"hun\"ter2"`
    # matched up to the backslash and leaked `ter2"}` to the model, the one
    # leak the downstream reviews of the JSON-key fix found. `\\.` takes any
    # backslash escape, `""` / `''` the doubled quote of YAML single quotes,
    # SQL and PowerShell. An unterminated quote falls back to the bare-token
    # form.
    #
    # A JSON key carries its closing quote between the name and the colon, so
    # `"password": "hunter2"` never matched: `\s*[:=]` had to follow the name
    # directly, and the whole value went to the model as written (verified with
    # a probe on 2026-08-22). A quote after the name (group `q`) admits the JSON
    # and Python-dict forms, and that form stays on one line, separator and
    # value alike, which the conditionals on `q` enforce: no serializer breaks
    # the line between a key and its colon, a JSON value always follows on the
    # key's line, and a quoted name that does end a line is code: `if kind ==
    # "token":` above a line of code, or a formatted ternary with `? "token"`
    # above `: "cookie"`.
    #
    # The unquoted form spans ONE line break, since YAML allows `password:`
    # with its scalar on the next line. It reads through the `+`, `-` or space
    # that prefixes every line of a diff, which is what this function mostly
    # sees, and which the old `\s*` took as the value, leaving the real one on
    # the wire. It stops at a blank line (`password:` in prose, then a code
    # fence) and at a sibling key (`password:` left empty, `username:` below
    # it), both of which used to be folded into one mangled line. The one
    # shape it leaves on purpose is a next-line value that is itself `word:`
    # at the end of its line, which cannot be told from a key; on the key's
    # own line a value ending in `:` is a value and is redacted.
    #
    # A value that opens a call is redacted WHOLE, through its closing paren.
    # `brokerApiKey: resolveKey("LITELLM_API_KEY"),` used to come out as
    # `brokerApiKey=<REDACTED>LITELLM_API_KEY"),`: the bare branch stopped at
    # the first quote and left a dangling fragment, which the model reported
    # as a "broken hunk" on a line that compiles, as a blocking finding,
    # round after round. Leaving calls alone was tried first and is wrong:
    # `password=hunter2(prod)` is call-shaped too, and a redactor that skips
    # it leaks. So `ident(...)`, `ident[...]`, `$(...)`, a bare `(...)` and
    # any run of those suffixes are consumed, three paren levels deep (a call
    # in a call in the value's own call), and replaced like any other value:
    # nothing dangles, nothing leaks, and a literal passed as an argument goes
    # with it. A call that breaks across lines, or nests a fourth level, falls
    # back to the bare form: the first fragment is redacted and the rest stays,
    # a display cost, with a literal argument that deep left visible. The
    # token ends before `,`, `;` and `)`, which no secret contains and which
    # the code around a value does: `login(password=pw, user=u)` used to lose
    # its comma and `connect(host=h, password=pw)` its closing paren. A
    # trailing quote is the value's own only when a leading one opened it
    # (the unterminated-quote fallback); otherwise it closes the ENCLOSING
    # literal and stays, so `x('api_key=abc123')` keeps its closing quote
    # instead of reading as an unterminated string in the reviewed diff. A
    # lone `-` or `+` before a space is a list marker or a diff marker, not a
    # value, so `password:` above `- item` stays as written.
    #
    # A bare value that is a type word is an annotation, not a secret: every
    # typed Python signature (`def login(user: str, password: str)`) and TS
    # parameter (`(_token: string) => {}`) matched here, and the model then
    # reported the file as syntactically broken. A closed set of type words
    # (_TYPE), optionally `| None`, with no `=` after it, is left as written,
    # and so is an absent value (`password = None`, `token = null`). A typed
    # DEFAULT is a value and goes whole: `password: str = "hunter2"` redacts
    # type and default together, so nothing that was masked before becomes
    # visible but the type word itself. A quoted literal under a secret name
    # (`{ token: "h" }`) stays redacted, and typing generics (`Optional[str]`)
    # are not in the set and still redact as a subscript.
    #
    # re.VERBOSE, so the branches can sit one per line: whitespace outside a
    # character class is layout, not pattern. Named groups, so the conditionals
    # and the replacement read as what they test rather than as a number.
    (
        re.compile(
            r"""
            (?P<key>
                api[_-]?key | access[_-]?key | private[_-]?key
              | secret(?:[_-]?access)?[_-]?key(?:[_-]?base)?
              | token | secret | password | passwd | client[_-]?secret
            )
            (?P<q>["'])?
            (?(q)[ \t]*|\s*)(?:=>|:=|[:=](?![=>]))
            (?(q)[ \t]*
              |[ \t]*(?:\r?\n(?:[-+ ]|(?![-+ ]))(?![ \t]*[\w.-]+:(?:[ \t]|\r?\n|$)))?[ \t]*)
            (?:(?P<type>%(T)s))?(?(type)[ \t]*=[ \t]*|)
            (?:
                "(?![ \t]*\$\{\{)(?:[^"\\\n]|\\.|"")*"
              | '(?![ \t]*\$\{\{)(?:[^'\\\n]|\\.|'')*'
              | (?![-+](?:[ \t]|\r?\n|$))
                (?(type)|(?!%(T)s[ \t]*(?:[,;)\]}:|]|\r?\n|$)))
                (?:
                    (?:[A-Za-z_$][\w.]*)?
                    (?:\((?:[^()\n]|\((?:[^()\n]|\([^()\n]*\))*\))*\)|\[[^\[\]\n]*\])+
                    [^\s'",;)]*
                  | ["'](?!\$\{\{)[^\s'",;)]+["']?
                  | (?!\$\{\{)[^\s'",;)]+
                )
            )
            """ % {"T": _TYPE},
            re.I | re.X,
        ),
        r"\g<key>=<REDACTED>",
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


def redact(text: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def include_file(path: str) -> bool:
    # fnmatch has no `**`. Its `*` does match `/`, so "**/dist/**" reads as
    # "*/dist/*" and needs a slash BEFORE dist, which a root-level path does not
    # have. Measured: "web/dist/x.js" was excluded and "dist/x.js" was reviewed,
    # and a root package-lock.json went to the model in every PR that touched
    # it. Matching against the path with a leading slash gives every depth,
    # the root included, the same shape.
    candidate = "/" + path
    allowed = any(fnmatch.fnmatch(candidate, pattern) for pattern in ALLOW_PATTERNS)
    excluded = any(fnmatch.fnmatch(candidate, pattern) for pattern in EXCLUDE_PATTERNS)
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


# Machine-readable outcome, written beside the review so the workflow can decide
# the CHECK COLOUR from what actually happened rather than from whether the
# script crashed. Grepping the markdown for a banner would couple CI to prose.
REVIEW_STATUS_PATH = "claude-review.status"
STATUS_OK = "ok"
STATUS_TRUNCATED = "truncated"
STATUS_EMPTY = "empty"
STATUS_SKIPPED = "skipped"

# The banners review_text_from_body writes and review_status reads. One
# definition for both, so the classifier cannot drift from the prose it keys
# on; four separate reviews of this file flagged the two literals as a pair
# that had to stay byte-identical by hand.
EMPTY_BANNER = "**No review text came back from the API.**"
TRUNCATED_BANNER = (
    "> **Truncated: this review hit the output-token ceiling and is incomplete.**"
)


def write_status(status: str) -> None:
    with open(REVIEW_STATUS_PATH, "w", encoding="utf-8") as handle:
        handle.write(status + "\n")


def review_status(text: str) -> str:
    """Classify a finished review body.

    A REVIEW THAT STOPPED EARLY DID NOT REVIEW THE REST. The banner tells a
    human, but the green check is what gets trusted and what feeds the
    auto-merge verdict, and the workflow already refuses that trade for a
    missing API key. Same rule here: a review that verified an unknown fraction
    of the diff must not report success.
    """
    if text.startswith(EMPTY_BANNER):
        return STATUS_EMPTY
    if text.startswith(TRUNCATED_BANNER):
        return STATUS_TRUNCATED
    return STATUS_OK


def price_env(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def max_tokens_from_env() -> int:
    """The output ceiling: CLAUDE_REVIEW_MAX_TOKENS, or the script default.

    The workflow always sets the variable now, from a repository variable that
    is usually unset, so the value this usually sees is the EMPTY STRING, not
    an absent key. Empty is the normal path, not the error path, and it is
    tested as such. A value that is set but not a number falls back too: a
    typo in a repo variable should cost one review its tuning, not the review.
    The fallback is said out loud on stderr, because a silent one would leave
    a mistyped repository variable unnoticed for as long as nobody reads the
    job log closely.
    """
    raw = os.getenv("CLAUDE_REVIEW_MAX_TOKENS", "").strip()
    if not raw:
        return DEFAULT_CLAUDE_REVIEW_MAX_TOKENS
    try:
        value = int(raw)
    except ValueError:
        value = 0
    # Zero and negatives are typos too: the API would reject them and the
    # review would fail for a reason unrelated to the diff. Too high is left
    # to the API, which knows the model's real limit and names it in the error.
    if value > 0:
        return value
    print(
        f"::warning::CLAUDE_REVIEW_MAX_TOKENS={raw!r} is not a positive integer; "
        f"using the script default {DEFAULT_CLAUDE_REVIEW_MAX_TOKENS}.",
        file=sys.stderr,
    )
    return DEFAULT_CLAUDE_REVIEW_MAX_TOKENS


def usage_summary(model: str, usage: dict[str, int] | None) -> str:
    if not usage:
        return ""
    # Per Anthropic's response schema, input_tokens is non-cached input;
    # cache creation and cache read tokens are billed separately.
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
    if model != DEFAULT_CLAUDE_REVIEW_MODEL:
        lines.append(
            f"- Pricing defaults are for `{DEFAULT_CLAUDE_REVIEW_MODEL}`; "
            "override pricing env vars if needed."
        )
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
            f"{EMPTY_BANNER} This is a tooling "
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
            f"{TRUNCATED_BANNER} Findings below may be cut off mid-thought, and "
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
    max_tokens = max_tokens_from_env()
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
        write_status(STATUS_SKIPPED)
        return 0

    body = call_claude(review_text, review_scope=review_scope)
    write_review(body)
    # The status is read from the REVIEW TEXT, which is the part
    # review_text_from_body already classified, not from the wrapper.
    write_status(review_status(body.split("## Claude Code Review\n\n", 1)[-1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
import urllib.parse
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
    "*.c", "*.h", "*.cpp", "*.hpp", "*.sh", "*.ps1", "*.vbs",
    "*.md", "*.yml", "*.yaml", "*.toml", "*.json", "*.sql",
    # Markup and styles are content in a language-neutral list. A repo whose
    # only UI is a single .html file had its entire surface unreviewed, and the
    # review said so in the confident voice of one that had read everything.
    # Build output arrives here as .min.css or under dist/, both excluded.
    "*.html", "*.css",
    # Deployment and secret surfaces. None of these is specific to one repo:
    # any project can ship a systemd unit, an nginx config, an .env.example
    # that is the shape of every secret it holds, or a Windows manifest, and
    # every one of them decides how the thing runs or what it trusts. They were
    # invisible in the kit until 2026-08-27 and are invisible in every repo
    # bootstrapped from this file until it syncs.
    "*.service", "*.conf", "*.example", "*.manifest", "*.cmd",
]
EXCLUDE_PATTERNS = [
    "**/node_modules/**", "**/.next/**", "**/dist/**", "**/build/**",
    "**/.venv/**", "**/venv/**", "**/vendor/**", "**/__pycache__/**",
    "*.lock", "**/package-lock.json", "**/pnpm-lock.yaml", "**/yarn.lock",
    "**/uv.lock", "**/poetry.lock", "**/Cargo.lock", "**/*.min.js", "**/*.map",
    "**/*.min.css",
]
# A closed set of type words. None is ever a secret, so a bare one after
# `password:` is an annotation; the SECRET_PATTERNS comment says how it is used.
_TYPE_WORD = (
    r"(?:str|int|float|boolean|bool|bytes|string|number|any|unknown|object"
    r"|dict|list|null|None|undefined)\b"
)
_TYPE = _TYPE_WORD + r"(?:[ \t]*\|[ \t]*" + _TYPE_WORD + r")*"

# An env-var lookup NAMES a secret without containing one, the same category as
# the `${{ secrets.X }}` expression the pattern already leaves alone, and it is
# exempted for the same reason: redacting it rewrites working code into
# something that does not parse. `token = os.environ.get("GITHUB_TOKEN") or ""`
# reached the model as `token=<REDACTED> or ""`, and the model reported a
# SyntaxError as a BLOCKING finding, twice in a row on kit #69, spending the
# whole review on an artifact. claude_review.py's own source hits this.
#
# NARROWED THREE WAYS, SO IT CANNOT HIDE A VALUE AND OVERTURNS NOTHING.
# (1) The call takes ONE string argument, so `os.environ.get("PW", "hunter2")`
# is not a lookup here and its default is still redacted. (2) The exemption is
# withdrawn if the rest of the line holds a non-empty quoted literal, so
# `... or "hunter2"` is still redacted while `... or ""` is left alone. (3) It
# does not apply where a type annotation was consumed, so
# `password: str = os.getenv("X")` still redacts type and default together.
# (4) The argument must look like an env var NAME -- an identifier, so
# `os.getenv("sk-ant-real-secret")` is not a lookup here and is redacted. An env
# var name is an identifier; a key generally is not, and matching on call shape
# alone would have let one through (caught in review on #74).
#
# The withdrawal in (2) reads ONE line, like every other branch in this table,
# so a fallback literal on a continuation line is not seen. That is the
# pre-existing limit of a line-oriented heuristic, not something this exemption
# widens: the same is true of every value shape here.
#
# `os.environ["X"]` is likewise left OUT: the subscript rule already redacts it
# and `token=<REDACTED>` reads as valid code, which is all this exemption is
# for. The shape that broke was the trailing ` or ""`, not the lookup itself.
# Every one of those is pinned by an exact-output test.
_ENV_LOOKUP = (
    r"(?:(?:os\.environ\.get|os\.getenv)[ \t]*\([ \t]*[\"'][A-Za-z_][A-Za-z0-9_]*[\"'][ \t]*\)"
    r"|process\.env\.[A-Za-z_]\w*)"
    r"(?![^\n]*[\"'][^\"'\n]+[\"'])"
)

# A PATTERN OR A PLACEHOLDER NAMES A SECRET WITHOUT CONTAINING ONE -- the same
# category as `${{ secrets.X }}` and the env lookup above, and the same failure
# mode: redacting it rewrites working code into something the model reports as
# broken, and the review is spent on the artifact instead of the diff.
#
# Measured on gestalt-workframe-edu#605, four consecutive rounds:
#
#     KEY_LINE = re.compile(r"^OPENROUTER_API_KEY=(.*)$", re.M)
#
# reached the reviewer as `^OPENROUTER_API_KEY=<REDACTED>` and came back as a
# BLOCKING finding -- "there is no capture group, .group(1) will raise" -- each
# time answered with the pushed blob and py_compile, each time re-reported. The
# f-string form `f"OPENROUTER_API_KEY={new_key}"` drew the same verdict: "this
# lambda ignores new_key and writes a fixed string to production".
#
# NARROWED SO IT CANNOT HIDE A VALUE.
# (1) A GROUP OR A REGEX LITERAL must open the value AND contain a REGEX IDIOM,
#     not merely a
#     character that regexes also use. `.`, `+`, `*` and `|` all appear inside
#     ordinary base64 -- `token=(AbC123+/==)` is a SECRET in brackets, and a
#     metacharacter test alone would have exempted it, turning a false positive
#     into a false negative, which is the worse direction. So the group must
#     contain `.*`, `.+`, a character class `[`, or a backslash escape --
#     and an opening `?` is NOT one of them. `(?:` looks like regex syntax and
#     is free to type around a literal, so `token=(?:hunter2)` would have been
#     exempt while holding a secret. A `(?...)` group qualifies only when it
#     also carries an alternation, which is what a grouped pattern is for:
#     `(?:a|b)` yes, `(?:hunter2)` no. `(.*)`, `([^"]*)` and `(\w+)` qualify;
#     `(hunter2)`, `(AbC123+/==)` and `(a|b|c)` do not.
#
#     `(a|b|c)` IS THE KNOWN OVER-REDACTION, and it is deliberate: a bare
#     alternation is also what a short secret in brackets looks like, and `|`
#     appears in no base64 alphabet but plenty of passwords. So a plain
#     capturing alternation still redacts -- the same false positive this change
#     set out to remove, kept where removing it would open a hole. Wrap it as
#     `(?:a|b|c)` and it is exempt, which is how a regex is usually written
#     anyway. A value with an identifier before
#     the paren is a CALL and is untouched here; the call rule redacts it whole.
# (2) A PLACEHOLDER is `{name}` with a LOWERCASE identifier -- the shape a format
#     string uses to say "the value goes here" (`{new_key}`, `{value}`). Keeping
#     it lowercase is what separates it from a token that happens to be braced:
#     `{SomeVaultToken123}` has capitals and stays redacted. `(?-i:...)` is
#     load-bearing -- the whole table compiles with re.I, so `[a-z_]` matched
#     `Hunter2` and the distinction did nothing until the flag was turned off
#     for this branch alone. A fully lowercase
#     alphanumeric secret wrapped in braces is the residual case, and it is
#     accepted knowingly -- this is a heuristic in front of a model, not a
#     boundary. `{"k": "v"}` has a quote and is not a placeholder, nor is `{a}{b}`.
# (3) EACH of them must END the value -- the next character is a quote,
#     whitespace, or a separator -- so `api_key=(.*)hunter2` is not exempt.
# (4) A JAVASCRIPT REGEX LITERAL IS THE SAME CATEGORY ONE DELIMITER OVER, and
#     (1) reads the value's FIRST character, which for `/(?:a|b)/` is the slash
#     rather than the group. So the group branch never fired on one, and this
#     repo's own .claude/hooks/check-handoff-language.mjs:699
#
#         const DEPLOY_SCRIPT_TOKEN = /(?:^|[/\\])deploy\.(?:sh|ps1|py|mjs)$/i;
#
#     reached the model as `DEPLOY_SCRIPT_TOKEN="<REDACTED>")deploy\....$/i;`
#     -- the bare branch stopped at the first `)` INSIDE the group -- which is
#     invalid JS on a line that runs. Exactly the failure this comment block was
#     written about, in the file the reviewer reads most often, and it cost
#     three rounds on #179 for a different line.
#
#     IT OPENS NOTHING THE GROUP BRANCH HAD NOT. Same idiom bar, so `/hunter2/`
#     is a value in slashes and is redacted; and the literal must CLOSE -- a `/`
#     then JS flag letters then a value terminator -- so `/var/lib/secrets` is a
#     path, not a pattern, and goes too. A body that carries an idiom AND hides
#     a secret (`/hunter2[x]/`) is the residual `(a|b|c)` already has, inherited
#     rather than added.
_REGEX_IDIOM = r"(?:\.[*+]|\[|\\[wsdWSDbAZ]|\?[:P=!<][^)\n]{0,60}\|)"

# One regex-literal body character. `[` opens a CHARACTER CLASS and a `/` inside
# one does not close the literal -- `[/\\]` in the line above holds exactly that
# `/` -- so the class is spelled out rather than left to a `[^/]` shortcut that
# would stop dead on it. The three atoms start with different characters (`\`,
# `[`, anything else), so the repeat has one way to consume each character and
# stays linear, which RedactionIsLinear pins.
_REGEX_BODY = r"(?:[^/\\\[\n]|\\.|\[(?:[^\]\\\n]|\\.)*\])"

_PATTERN_OR_PLACEHOLDER = (
    r"(?:"
    r"\((?=[^)\n]{0,80}%(I)s)"
    r"(?:[^()\n]|\([^()\n]*\)){0,80}\)"
    # The trailing quantifier or anchor, and NOTHING else. The first cut was a
    # character class including A-Za-z, so up to eight letters rode along as
    # "suffix" -- `token=(?:a|b)SECRETAB` was exempt with the secret inside the
    # span. Explicit alternatives instead: a quantifier, a counted repeat, one
    # of the anchor escapes, or a dollar.
    r"(?:[*+?]|\{\d+(?:,\d*)?\}|\\[bBAZzG]|\$){0,6}"
    r"|(?-i:\{[a-z_][a-z0-9_]*\})"
    # The regex literal. The lookahead is the same bounded idiom scan the group
    # branch runs, over body atoms so it cannot walk past the closing slash; the
    # flag run is `(?-i:...)` because the whole table compiles with re.I and
    # `[dgimsuvy]` would otherwise take eight letters of anything.
    r"|/(?=%(B)s{0,120}?%(I)s)%(B)s{1,200}/(?-i:[dgimsuvy]{0,8})"
    r")"
    r"(?=[\"'\s,;)\]]|$)"
) % {"I": _REGEX_IDIOM, "B": _REGEX_BODY}

# A VARIABLE RESHAPING ITSELF CARRIES NOTHING NEW.
#
#     token = token.strip().strip("'\"")
#     ->  token="<REDACTED>""'\"")
#
# The name matches, so the rule fires; the right-hand side is the SAME variable
# with method calls on it. There is no literal to hide -- whatever the value is,
# it was already in the variable a line earlier -- and redacting it corrupts
# control logic in the text the model reads. On kit #200 the model then reported
# that corruption as a BLOCKING SyntaxError, accurately, on a file that compiles
# and whose suite was green in the same run. A correct reading of a wrong input
# is the worst failure this table has, because it is unanswerable without the
# blob.
#
# NARROWED SO IT CANNOT HIDE A VALUE.
# (1) The value must OPEN with the same identifier the key matched -- a
#     backreference, not a lookalike, so `token = token_source.strip()` and
#     `token = other.strip()` are ordinary values and still redact.
# (2) Only method calls and subscripts may follow, and the chain must END the
#     value. `token = token.strip() or "hunter2"` has a literal after the chain,
#     so it is not exempt and the literal still goes.
# (3) NO ARGUMENT MAY CARRY A LONG ALPHANUMERIC RUN. `.strip("'\"")` and
#     `.split(",")` are punctuation and stay exempt; `.replace("hunter2abc", "")`
#     is a literal in an argument and is redacted. Eight characters is the bar,
#     the same order as the vendor entries below, and deliberately low: an
#     argument that long is doing something other than trimming.
#
# `db_password = db_password.strip()` is NOT exempt, because the key group
# matches the tail (`password`) and the backreference then looks for `password`
# where the value says `db_password`. Over-redaction, and left alone: the safe
# direction, and narrowing it further would mean matching the whole name.
_SELF_RESHAPE = (
    r"(?P=key)"
    r"(?:\.\w+(?:\((?![^()\n]*[A-Za-z0-9]{8})[^()\n]*\))?|\[[^\[\]\n]*\])+"
)

# What the value rule refuses to treat as a value. One name so the branch that
# uses it reads as the question it asks.
# THE CHAIN MUST END THE VALUE, and the terminator is deliberately NOT the
# `\s` the other two branches accept. With whitespace allowed,
# `token = token.strip() or "hunter2"` matched the chain, hit the space, and
# went exempt WITH THE LITERAL STILL ON THE LINE -- an exemption written to stop
# a false finding, turning a redacted line into a leak. Measured before it
# shipped. A closer or the end of the line only, so anything following the chain
# means the value is an expression and is redacted whole.
_NAMES_NOT_VALUES = r"(?:%s|%s|%s(?=[,;)\]}]|[ \t]*$))" % (
    _ENV_LOOKUP, _PATTERN_OR_PLACEHOLDER, _SELF_RESHAPE,
)

# A STRING LITERAL'S PREFIX IS PART OF THE LITERAL, and the value branch read it
# as a bare value that happened to end where a quote began. So only the prefix
# was redacted and the literal went to the model intact:
#
#     password = f"Ab{x}AbCdEf0123456789ZzYyXx"
#     ->  password="<REDACTED>""Ab{x}AbCdEf0123456789ZzYyXx"
#
# Measured 2026-08-31, documented as a residual gap, and then raised twice in
# review on #182 as the one worth fixing rather than recording: f-strings are
# the ordinary way to build a string in the language most of this repo is
# written in, so "a secret in an f-string" is not an exotic shape here.
#
# `f`, `r`, `b`, `u` and their two-letter pairs (`rb`, `br`, `fr`), plus C#'s
# `$` and `@`. Spelled as a LENGTH rather than a letter list because the list is
# language-specific and grows, while the shape -- one or two letters welded to a
# quote -- does not.
#
# IT MUST ABUT THE QUOTE, no space, which is the whole safety argument. In prose
# a word before a quotation has a space after it, so `password: is "hunter2"`
# still reads `is` as the value and leaves the quote alone; only `is"hunter2"`
# would be taken, and that is not English. The prefix sits OUTSIDE the `qv`
# group on purpose: `_redact_assignment` reads `qv[0]` for the author's quote,
# and a prefix inside the group would make that quote the letter `f`.
_LITERAL_PREFIX = r"(?:[A-Za-z]{1,2}|[$@])"

# A CONCATENATED VALUE IS STILL ONE VALUE, and the value branch used to stop at
# the first closing quote. Measured on 2026-08-31, in the UNDER-redaction
# direction -- a leak, not a display cost, and the only one on this table:
#
#     const apiKey = "sk-ant-" + "AbCdEf0123456789ZzYyXx";
#     ->  const apiKey="<REDACTED>" + "AbCdEf0123456789ZzYyXx";
#
# The single-literal form of that line redacts correctly, so the whole gap was
# the `+`. It was named in neither this file nor HARNESS.md's list of residual
# gaps; the entry there now says what is still not covered.
#
# THE OPERATORS ARE THE ONES A LITERAL ARRIVES THROUGH, and each was measured on
# a line that leaked before it was added. Concatenation: `+` (JS, TS, Python,
# C#, PowerShell), `.` and `..` (PHP, Lua), `&` (VBScript, and *.vbs is reviewed
# here). Formatting: `%` (Python's old style). And FALLBACK: `||`, `??`, `or`,
# `and`.
#
# THE FALLBACKS ARE NOT CONCATENATION and are here anyway, because
# `const token = opts.token || "hunter2";` is the hardcoded-default-credential
# antipattern and one of the likelier ways a real key reaches a repo. The value
# is the whole expression, so redacting it whole is right rather than generous.
# They reach a literal or a call and never a bare name: `token = a || b` has no
# literal to hide, so consuming `b` would buy nothing and cost the reader the
# name. The word forms take `[ \t]+` on both sides so `password` and `passwordor`
# stay different names.
#
# `+` REACHES ANY OPERAND -- a literal, a call, a bare token. The others reach a
# literal or a CALL and not a bare token, because they are also attribute
# access, bitwise-and and an English full stop: `password: "hunter2". Then the
# user...` would otherwise take the sentence. Reaching the call is what makes
# `password = "Ab".concat("hunter2")` go whole; `token = "abc".strip()` goes
# with it, which is over-redaction on a line whose value was already gone.
#
# `[ \t]`, NEVER `\s`, on both sides of the operator. A `+` at the start of the
# next line is a DIFF MARKER, and a diff is mostly what this function sees; a
# chain that crossed the line break would join a redacted value to the next
# hunk line. Bounded at 64 links, and each link must consume its operator, so
# the repeat cannot spin. RedactionIsLinear pins the cost.
#
# A `${{ secrets.X }}` operand ENDS the chain, for the reason it is left alone
# everywhere else here: it names a secret without holding one, and consuming it
# rewrites a workflow into something the model reports as broken.
# THE PREFIX REACHES THE OPERANDS TOO, and for one round it did not. #182 put
# `_LITERAL_PREFIX` in front of the `qv` group, so a prefixed literal was
# recognised as THE VALUE -- and left `_CONCAT_LITERAL` alone, so a prefixed
# literal as a CHAIN OPERAND still was not, and the chain stopped in front of it:
#
#     secret = b"kit-" + b"SECRET"   ->  secret="<REDACTED>""SECRET"
#     password = "postgres://" + f"{user}:SECRET"
#     var apiKey = "sk-" + $"{env}-SECRET";
#
# The same shape one position over, missed in the commit that fixed the shape.
# Recorded plainly because it is the exact failure the repo's own "fix the class,
# not the instance" constraint names, committed while fixing that class -- a
# prefix is part of a literal wherever a literal is allowed, and there are two
# places this table says "a literal".
#
# ONE DEFINITION, REFERENCED. The first cut re-spelled the prefix here and
# claimed sharing it would mean moving three blocks; review on #190 doubted that
# and was right -- `_LITERAL_PREFIX` depends on nothing, so it moves above with
# no reordering at all, and the two constants that DO have an order
# (`_CONCAT_LITERAL` -> `_BARE_CHAR` -> `_CONCAT_CALL`) simply follow it. A
# duplicate pinned by a test is still a duplicate; the test only reports the
# drift after it has happened.
_CONCAT_LITERAL = (
    r"(?:%(P)s?\"(?![ \t]*\$\{\{)(?:[^\"\\\n]|\\.|\"\")*\""
    r"|%(P)s?'(?![ \t]*\$\{\{)(?:[^'\\\n]|\\.|'')*'"
    r"|%(P)s?`(?![ \t]*\$\{\{)(?:[^`\\\n]|\\.)*`)"
) % {"P": _LITERAL_PREFIX}
# ONE CHARACTER OF A BARE VALUE -- AND NOT AN OPERATOR THE CHAIN IS WAITING FOR.
# The chain above hangs off the END of the value, so it only ever sees what the
# value branch declined to eat. The bare class `[^\s'",;)]+` excludes neither
# `+` nor `.` nor `&`, so with no space around the operator it swallowed it and
# left the chain nothing to attach to. Found in review on #182, measured the
# same day, six shapes and every one a leak of a whole literal:
#
#     apiKey=prefix+"SECRET"        ->  apiKey="<REDACTED>""SECRET"
#     local password=a.."SECRET"    ->  local password="<REDACTED>""SECRET"
#     $password=$a."SECRET";        ->  $password="<REDACTED>""SECRET";
#     token=x&"SECRET"              ->  token="<REDACTED>""SECRET"
#     token=f()+"SECRET"            ->  token="<REDACTED>""SECRET"
#
# The SPACED forms of all five were already correct, because the class stops at
# a space and the chain took it from there -- which is exactly why the tests
# written for the chain missed this: every one of them had spaces.
#
# THE STOP IS CONDITIONAL ON A COMPLETE LITERAL FOLLOWING, not on the operator
# alone. `+` and `.` are ordinary base64, so refusing them unconditionally would
# truncate a bare secret one character early and leak the rest to the model:
# `x('token=AbC+')` must still go whole. Requiring the chain's own literal
# pattern to match after the operator means the class only yields where the
# chain will actually pick up, and `AbC+` followed by an unterminated `'` is
# still one value.
#
# TWO ALTERNATIVES ON DISJOINT CHARACTER SETS, so the ordinary characters -- all
# but three of them -- take the first branch with no lookahead at all, and only
# a literal `+`, `.` or `&` pays for one. RedactionIsLinear pins the cost.
# THE SYMBOL OPERATORS, ONCE. The chain below matches these, and the bare class
# above has to stop in front of exactly these -- two spellings of one set, and
# when the set grew (`%`, `||`, `??`) only one of them grew. That is the fourth
# instance this week of an atom spelled twice and fixed once, so it is a shared
# constant rather than a matching pair. The word forms (`or`, `and`) are not
# here: they need whitespace on both sides, which a character-wise class cannot
# express, and a bare value cannot contain one without the space that ends it.
# `+` IS IN HERE FOR THE BOUNDARY, NOT FOR THE CHAIN. The chain spells its
# `+` branch separately because `+` alone reaches a BARE operand and the
# others do not, so the two cannot share one alternative. `+` still belongs in
# this set because `_BARE_CHAR` has to stop in front of every operator the
# chain can follow, `+` included. The chain's shared-constant branch does
# re-match `+`, harmlessly -- the `+` alternative is first and wins. Said out
# loud because it reads like the "spelled twice" defect this constant was
# introduced to end, and it is the one place that is deliberate. Raised in
# review on #192.
_CHAIN_OP = r"(?:\.{1,2}|\|\||\?\?|[+&%])"

# `[ \t]*` INSIDE THE LOOKAHEAD, because the chain allows it and this has to
# agree with the chain or the two disagree about where a value ends. It demanded
# the literal be GLUED to the operator, so `token = pre+ "SECRET"` -- operator
# glued left, space right -- failed the lookahead, the `+` was eaten as part of
# the bare token, and the chain had no operator left to attach to. Five
# operators, five leaks, raised in review on #192.
#
# The generator could not find it: `OPERAND_SPACING` varied the two sides
# TOGETHER (`{opspace}{op}{opspace}`), so every case it produced was symmetric.
# That is the same defect as a hand-written pin inheriting the shape that
# motivated it, one level up -- an axis that cannot express the asymmetry cannot
# find it. The axes vary independently now.
_BARE_CHAR = (
    r"(?:[^\s'\",;)+.&%%|?]|(?!%(O)s[ \t]*%(Q)s)[+.&%%|?])"
) % {"O": _CHAIN_OP, "Q": _CONCAT_LITERAL}

# A call or a subscript, as the value branch spells one, without its named group
# -- AND ENDING THE SAME WAY IT DOES. For one round it did not: the value branch
# ends its call form with the tempered `_BARE_CHAR`, this one ended with the raw
# `[^\s'",;)]*`, and so a call operand mid-chain ate the next operator and the
# literal behind it survived:
#
#     token = "a" + f()+"SECRET"      ->  token="<REDACTED>""SECRET"
#     token = "a" + a[0]+"SECRET"     ->  token="<REDACTED>""SECRET"
#     token = "a" + f(x).g+"SECRET"   ->  token="<REDACTED>""SECRET"
#
# `token = f()+"SECRET"` -- the same shape as the VALUE rather than as an
# operand -- was already correct, which is what named the asymmetry.
#
# Raised in review on #190, which asked whether the prefix asymmetry it was
# fixing had siblings in the other chain-operand branches. It did, in the branch
# next door. That question is the one worth keeping: two constants that mean
# "the same thing the value branch means" must be checked against the value
# branch, not against each other.
_CONCAT_CALL = (
    r"(?:[A-Za-z_$][\w.]*)?"
    r"(?:\((?:[^()\n]|\((?:[^()\n]|\([^()\n]*\))*\))*\)|\[[^\[\]\n]*\])+%(V)s*"
) % {"V": _BARE_CHAR}
_CONCAT_CHAIN = (
    r"(?:"
    r"[ \t]*\+[ \t]*(?:%(Q)s|%(F)s|(?!\$\{\{)[^\s'\",;)]+)"
    r"|[ \t]*%(O)s[ \t]*(?:%(Q)s|%(F)s)"
    r"|[ \t]+(?:or|and)[ \t]+(?:%(Q)s|%(F)s)"
    r"){0,64}"
) % {"Q": _CONCAT_LITERAL, "F": _CONCAT_CALL, "O": _CHAIN_OP}


class _LineQuoteParity:
    """Is a double quote already OPEN on this line, at this offset?

    The bare-value branch of `_redact_assignment` asks this to pick a placeholder
    quote that does not close the string the value is sitting inside. The obvious
    way to answer it is a back-scan to the start of the line, per match:

        line_start = m.string.rfind("\\n", 0, m.start()) + 1
        m.string[line_start:m.start()].count('"') % 2

    That is correct and it is QUADRATIC: one long line with many keys rescans the
    whole line for every one of them. It is not a theoretical cost. Measured on
    the shapes `RedactionIsLinear` already pins, `"password: " * 200_000` took
    119 seconds against that test's 10-second ceiling, which is why the fix was
    reverted the first time and the broken output pinned as a wart instead
    (#177).

    `re.sub` hands its matches over left to right and non-overlapping, so the
    answer can be CARRIED FORWARD rather than recomputed: each call counts only
    the gap since the previous one. The gaps are disjoint, so one pass reads the
    subject once no matter how many matches it holds. Same answer, linear time.

    THE GAP IS TAKEN FROM THE ORIGINAL SUBJECT, not from the redacted output, and
    it spans the previous match's own text -- `m.string` is what `re.sub` is
    scanning, and quotes the previous match consumed are quotes the author wrote
    on that line. This is the same span the back-scan above counted, which is
    what makes the two agree rather than merely look similar.

    Counting the SUBJECT rather than the OUTPUT is exact wherever a replacement
    balances what it replaced, which is everywhere but one shape: an earlier
    value on the same line holding an ODD number of quotes -- an unterminated
    literal, or an escaped `\\"` inside a quoted one -- where the output's pair
    is even and the input's was not. Accepted, and named rather than left to be
    found: it needs a malformed value AND a second key on the same line, and
    what it degrades to is the flat `"` this whole change improves on.

    Two rules follow from being stateful. It RESETS ITSELF whenever the subject
    changes or a match arrives behind the cursor, so correctness never depends on
    a caller remembering to reset -- `redact()` runs once per file in the
    snapshot path, and the fallback tests drive `redact_assignment` through their
    own `re.sub`. And it assumes ONE THREAD, which this script is: `python
    claude_review.py`, one process per PR.
    """

    __slots__ = ("_text", "_pos", "_odd", "in_use")

    def __init__(self) -> None:
        self.in_use = False
        self.restart()

    def restart(self) -> None:
        # Drops the subject reference along with the position. A review snapshot
        # is megabytes and there is no reason to pin the last one alive for the
        # rest of the process.
        self._text = None
        self._pos = 0
        self._odd = False

    def odd_before(self, text: str, index: int) -> bool:
        if text is not self._text or index < self._pos:
            self._text = text
            self._pos = 0
            self._odd = False
        gap = text[self._pos:index]
        newline = gap.rfind("\n")
        if newline < 0:
            # Same line as the last answer: the gap's quotes flip it or do not.
            self._odd ^= gap.count('"') % 2 == 1
        else:
            # The gap crossed into a new line, so the carried parity is stale and
            # only what follows the LAST newline is on this match's line.
            self._odd = gap.count('"', newline + 1) % 2 == 1
        self._pos = index
        return self._odd


_LINE_QUOTE_PARITY = _LineQuoteParity()


def _redact_assignment(m: "re.Match[str]") -> str:
    """Hide the value, and leave what is left PARSEABLE.

    The old replacement was the literal `\\g<key>=<REDACTED>`, which flattened
    every separator the pattern accepts -- `:`, `=`, `:=`, `=>` -- down to `=`,
    and dropped the quotes. So a perfectly ordinary object literal

        const poisoned = { baseUrl: "http://127.0.0.1:9", apiKey: "abc\\ndef" };

    reached the reviewer as `apiKey=<REDACTED>`, which is not valid in an object
    literal, and the model reported a SyntaxError as a BLOCKING finding on a file
    that parses and whose suite is green in the same CI run. Three rounds on kit
    #169, and it is the third time this class has cost a whole review: the env
    lookup exemption and the regex literal exemption above were both added for
    the same failure with different inputs.

    Those two are EXEMPTIONS -- they decide not to redact something. This is not.
    The value is still gone. What changes is that the hole left behind is shaped
    like the language it sits in:

        apiKey: "abc\\ndef"     ->  apiKey: "<REDACTED>"      (parses)
        f("KEY=" + "abc123")   ->  f("KEY=<REDACTED>")      (parses)
        TOKEN = "abc123"       ->  TOKEN = "<REDACTED>"      (parses)
        token => "abc123"      ->  token => "<REDACTED>"     (parses)
        OPENROUTER_KEY=abc123  ->  OPENROUTER_KEY="<REDACTED>"

    THE PLACEHOLDER IS ALWAYS QUOTED, and in the SOURCE'S OWN QUOTE CHARACTER.
    Both halves were raised on review of #177 and both are corrections to the
    first cut of this function.

    Always, because `<REDACTED>` is not a valid token in any language where `<`
    and `>` are operators. Quoting only the values that ARRIVED quoted made
    exactly the shapes that already had quotes parse, and left `f(token=abc123)`
    coming out as `f(token=<REDACTED>)` -- the same broken-syntax report, one
    shape over, in a function written to stop them. A bare .env line gains a pair
    of quotes it did not have; .env, YAML and every shell accept them, and the
    redacted text is read, never executed.

    In the source's quote, because `'` and `"` are not interchangeable
    everywhere: in SQL a double-quoted token is an IDENTIFIER and a single-quoted
    one is a string, so rewriting `password = \'x\'` as `password="<REDACTED>"`
    changes what the line means rather than how it looks. `qv` holds the whole
    literal, so its first character is the quote the author chose.

    WHITESPACE AROUND THE SEPARATOR IS STILL DROPPED, deliberately and as before:
    `TOKEN = "x"` becomes `TOKEN="<REDACTED>"`, not `TOKEN = "<REDACTED>"`. The
    match consumes that whitespace and this is a redactor, not a formatter -- the
    contract is that the result PARSES and hides the value, never that it is
    pretty. Said out loud because it is the kind of lossy detail a reviewer
    reasonably flags as a bug. Raised on review of #177.
    """
    # The author's own quote where the value had one -- `\'` and `"` are not
    # interchangeable everywhere, and in SQL a double-quoted token is an
    # IDENTIFIER, so swapping them changes meaning rather than formatting.
    #
    # A BARE VALUE HAS NO QUOTE TO PRESERVE, so it takes the one that does not
    # close the string it is sitting inside. `f("token=abc123")` came out as
    # `f("token="<REDACTED>"")`, which closes the enclosing literal and reopens
    # it. THE DAMAGE IS NARROWER THAN IT LOOKS AND WAS MEASURED, because the
    # obvious claim -- "that is a syntax error" -- is mostly false and would have
    # been the wrong reason to change this: in JS and in Python alike the line
    # re-balances into a comparison chain (`"token=" < REDACTED > ""`) and
    # PARSES, which is exactly how it survived #177. What it stops being is a
    # STRING. The author's literal is now a comparison against an undeclared
    # name, and where `<` is not an operator -- JSON, YAML, .env, the formats a
    # diff is full of -- it does not parse at all: `{"note": "token=abc123"}`
    # redacted to invalid JSON, and no longer does. With a `"` already open on
    # the line the placeholder is single-quoted instead, and
    # `f("token='<REDACTED>'")` is still one string holding one hidden value.
    #
    # This was the known wart of #177 and it was NOT a wart of taste. The first
    # fix asked the question with a back-scan per match and took 119 seconds
    # against the 10-second ceiling in
    # RedactionIsLinear.test_a_pathological_input_redacts_in_linear_time, so it
    # was reverted. `_LineQuoteParity` is the same answer carried forward across
    # matches instead of rescanned per match; the cost is one extra pass over
    # the subject, and the whole shape is pinned by
    # test_a_quote_that_closes_an_enclosing_string_is_not_eaten.
    #
    # Nothing is open on the line of an ordinary `TOKEN=abc123`, which still gets
    # the double quote it always had.
    #
    # ASKED AT THE OFFSET THE PLACEHOLDER LANDS ON, which is past the key's own
    # closing quote, not at the start of the key. `"brokerApiKey": resolveKey(x)`
    # has a `"` open in front of `ApiKey` -- the JSON key's -- but group `q`
    # closes it and the replacement puts it back, so the value is NOT inside a
    # string and single-quoting it would emit `"brokerApiKey":'<REDACTED>'`,
    # which is not JSON. Nothing between `q` and the value can hold a quote (the
    # key, the separator, the type word and whitespace), so this offset is exact.
    # The one shape it reads on the wrong line is the YAML value that sits on the
    # line BELOW its key, which has no quote of its own to be endangered by.
    key_quote = m.group("q") or ""
    placeholder_at = m.end("q") if key_quote else m.start()
    quote = m.group("qv")[0] if m.group("qv") else (
        "'" if _LINE_QUOTE_PARITY.odd_before(m.string, placeholder_at) else '"'
    )
    # THE SEAM DROPS THE OPENING QUOTE AND KEEPS THE ENCLOSING STRING'S.
    # `f("API_KEY=" + "hunter2")` opens its value with the ENCLOSING literal's
    # CLOSING quote, and the match consumed it; emitting one here would close
    # that string a second time and leave the `f("API_KEY="<REDACTED>")` shape
    # this function exists to stop. Leaving it off rejoins the halves into the
    # single literal the line already was, `f("API_KEY=<REDACTED>")`.
    #
    # The closer then has to be the SEAM's quote, not the value's: the two are
    # different in `f("API_KEY=" + 'hunter2')`, and taking the value's would
    # close a double-quoted string with an apostrophe. Nothing but the seam can
    # consume that opening quote, so every other shape still gets both of its own.
    opener = quote
    if m.group("seam"):
        opener, quote = "", m.group("seamq")
    # NOT PUT BACK: the `type` group. `password: str = "x"` redacts the
    # annotation along with the default, which is pre-existing and deliberate --
    # putting the type back would need the `=` back with it, and the two are one
    # span in the pattern. Noted because it is invisible otherwise and this
    # patch is not what caused it.
    return "{key}{q}{sep}{opener}<REDACTED>{quote}".format(
        key=m.group("key"),
        opener=opener,
        # The key's own closing quote, for `"token": "x"`. Dropping it left a
        # dangling quote behind -- the same unparseable-output bug, one character
        # over. Python re gives "" for a group that did not participate, which is
        # what `key_quote` normalised above.
        q=key_quote,
        sep=m.group("sep"),
        quote=quote,
    )


# Set the first time the fallback below fires, so the warning is emitted once per
# process rather than once per match. A large diff has thousands of matches.
#
# ONCE PER PROCESS IS ONCE PER PR, because this script is `python
# claude_review.py` in a job that exits. Stated because it stops being true the
# day something reuses the process across diffs -- a worker handling several PRs
# would warn for the first and stay quiet for the rest, which is the silent
# degradation the warning exists to prevent.
_REDACT_FALLBACK_WARNED = False


def redact_assignment(m: "re.Match[str]") -> str:
    """`_redact_assignment`, but a broken pattern degrades instead of exploding.

    The function above reads `key`, `q`, `sep`, `qv`, `seam` and `seamq` BY
    NAME. A future regex edit that renames or drops one raises IndexError
    inside re.sub -- and
    `redact()` runs before anything is sent, so the whole review step dies and NO
    review is posted at all. That is the worst outcome available: a review that
    ran and over-redacted is a bad review, a review that never ran is a green
    check on unread code, which is the failure this repo names most often.

    So the pair-with-a-fallback, and the fallback is total: `<REDACTED>` for the
    whole match. It cannot raise, it cannot leak -- everything matched is gone,
    key and separator included -- and it is deliberately the UGLY output, because
    an unparseable line in a review is a symptom someone chases, while a silently
    absent review is not. Raised on review of #177.
    """
    global _REDACT_FALLBACK_WARNED
    try:
        return _redact_assignment(m)
    except Exception as exc:  # noqa: BLE001 -- any failure here must hide, not raise
        # SAY SO, ONCE. Hiding the value is the right default; hiding the FACT
        # that the redactor is broken is not. A silent fallback degrades
        # permanently and invisibly, and the operator would meet it as "the
        # reviews got ugly a while back" rather than as a defect with a date --
        # the same rule the hooks follow, where a check that could not run must
        # never look like one that passed.
        #
        # Once per process, not per match: a large diff would otherwise emit
        # thousands of identical lines and bury the CI log it is trying to
        # annotate. Raised on review of #177.
        if not _REDACT_FALLBACK_WARNED:
            _REDACT_FALLBACK_WARNED = True
            print(
                f"WARNING: the assignment redactor fell back to a total redaction "
                f"({type(exc).__name__}: {exc}). Secrets are still hidden, but the "
                f"named groups in SECRET_PATTERNS no longer match _redact_assignment. "
                f"Run .github/scripts/test_claude_review.py.",
                file=sys.stderr,
            )
        return "<REDACTED>"


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
    # SEPARATOR. `:`, `=`, `:=` (walrus), `=>` (PHP array, match arm), or the
    # string-append `+=` and `.=`. `password += "hunter2"` matched NOTHING and
    # went to the model whole -- the same measured leak as the concatenation
    # chain below, one operator to the left of it, since `\s*` cannot cross the
    # `+`. The separator is never the first character of `==`, `===` or `=>`:
    # `password == other` is a comparison, and matching its first `=` redacted
    # the second and left the line unable to parse. `'password' => 'hunter2'` used to slip through
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
    # A CONCATENATION SEAM sits between the separator and the value. Inside an
    # enclosing string the operator hides between two quotes belonging to
    # DIFFERENT literals -- `f("API_KEY=" + "hunter2")` -- so the value branch
    # read `" + "` as a complete string, redacted that, and left the secret in
    # plain text after it. The bridge consumes `" + ` when ANY quote follows --
    # not only the same one, because `f("API_KEY=" + 'hunter2')` switches
    # character across the seam and leaked when it had to match. The chain below
    # cannot reach this shape at all: the operator it needs is already inside a
    # literal. `_redact_assignment` drops the opening quote when the bridge
    # fired, since the one it ate was the enclosing literal's closing quote, and
    # closes with the bridge's quote rather than the value's.
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
            (?(q)[ \t]*|\s*)(?P<sep>=>|:=|[+.]=|[:=](?![=>]))
            (?(q)[ \t]*
              |[ \t]*(?:\r?\n(?:[-+ ]|(?![-+ ]))(?![ \t]*[\w.-]+:(?:[ \t]|\r?\n|$)))?[ \t]*)
            (?P<seam>(?P<seamq>["'])[ \t]*(?:\+|\.{1,2}|&)[ \t]*(?=["']))?
            (?:(?P<type>%(T)s))?(?(type)[ \t]*=[ \t]*|)
            (?(type)|(?!%(E)s))
            (?:
                %(P)s?
                (?P<qv>
                  "(?![ \t]*\$\{\{)(?:[^"\\\n]|\\.|"")*"
                | '(?![ \t]*\$\{\{)(?:[^'\\\n]|\\.|'')*'
                | `(?![ \t]*\$\{\{)(?:[^`\\\n]|\\.)*`
                )
              | (?![-+](?:[ \t]|\r?\n|$))
                (?(type)|(?!%(T)s[ \t]*(?:[,;)\]}:|]|\r?\n|$)))
                (?:
                    (?:[A-Za-z_$][\w.]*)?
                    (?:\((?:[^()\n]|\((?:[^()\n]|\([^()\n]*\))*\))*\)|\[[^\[\]\n]*\])+
                    %(V)s*
                  | ["'](?!\$\{\{)[^\s'",;)]+["']?
                  | (?!\$\{\{)%(V)s+
                )
            )
            %(C)s
            """ % {"T": _TYPE, "E": _NAMES_NOT_VALUES, "C": _CONCAT_CHAIN,
                   "V": _BARE_CHAR, "P": _LITERAL_PREFIX},
            re.I | re.X,
        ),
        redact_assignment,
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

# THE OTHER HALF OF THE TABLE, AND THE ONE THAT SCALES.
#
# Everything above this line PARSES SYNTAX to find a value position, and every
# leak found in the week of 2026-08-31 was there: spacing, prefixes, operands,
# backticks, escaped quotes, fallback operators, subscript assignment,
# positional arguments. That half is unbounded -- each language, quoting form
# and operator is another branch -- and eight classes of it are still open,
# recorded in HARNESS.md and #188.
#
# The three entries above ask a different question: not "where is the value"
# but "is this string a key". That question needs no syntax at all, so no
# quoting form can hide from it. There were three of them; there should be
# these.
#
# A PREFIX, NOT AN ENTROPY THRESHOLD, and the difference was measured rather
# than assumed. Against the shapes the parser misses, carrying real credential
# formats:
#
#     tuned entropy rule   14% caught,  0.079% of repo lines redacted
#     loose entropy rule   57% caught,  9.9%   of repo lines redacted
#     vendor prefixes      75% caught,  0.008% of repo lines redacted
#
# The prefix rule beats both entropy variants on BOTH axes -- five times the
# recall of the affordable one, at a tenth of its cost. The reason is
# structural: a prefix is a literal string, so nothing that is not a GitHub
# token begins `ghp_`, while entropy is a proxy that collides with git SHAs,
# UUIDs, content hashes, base64 assets and minified code. An earlier draft of
# this comment recommended entropy on the strength of a 34-of-34 result; that
# measurement used a sentinel chosen to look exactly like what the rule
# detected, and against real key formats the same rule scored 6 of 16.
#
# WHAT IT STILL CANNOT SEE, and why that is not an argument against it: an AWS
# SECRET access key (no prefix, by design), a raw hex or UUID key, and any
# passphrase. Those are the 25%, and no regex reaches them -- TruffleHog does,
# because it VERIFIES candidates against the vendor rather than matching them,
# which is a thing a redactor cannot do. This is a heuristic in front of a
# model; that is the boundary.
#
# The replacement keeps the prefix, as the three entries above do, because
# "you leaked a GitHub token" is worth more to a reviewer than "you leaked
# something".
_VENDOR_KEYS = (
    # GitHub: personal, OAuth, user-to-server, server-to-server, refresh.
    r"(gh[pousr]_)[A-Za-z0-9]{36,}",
    r"(github_pat_)[A-Za-z0-9_]{50,}",
    r"(glpat-)[A-Za-z0-9_-]{20,}",
    # Slack bot, user, app, refresh, legacy and configuration tokens.
    r"(xox[baprse]-)[A-Za-z0-9-]{10,}",
    # Google API keys and OAuth access tokens.
    r"(AIza)[A-Za-z0-9_-]{35}",
    r"(ya29\.)[A-Za-z0-9_-]{20,}",
    # AWS temporary credentials; the permanent id is AKIA, above.
    r"(ASIA)[0-9A-Z]{16}",
    # Stripe SECRET and restricted keys. The publishable `pk_` key is public by
    # design and is deliberately absent: redacting it would hide nothing and
    # cost the reviewer a line it can legitimately read.
    r"(sk_(?:live|test)_)[A-Za-z0-9]{16,}",
    r"(rk_(?:live|test)_)[A-Za-z0-9]{16,}",
    r"(npm_)[A-Za-z0-9]{30,}",
    r"(dop_v1_)[a-f0-9]{64}",
    r"(shpat_)[a-fA-F0-9]{32}",
    r"(SG\.)[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}",
    r"(hf_)[A-Za-z0-9]{30,}",
    r"(r8_)[A-Za-z0-9]{37,}",
    # A JWT, which is two dotted base64 segments after the fixed header prefix.
    # `eyJ` is `{"` in base64, so this is the header of every one of them.
    r"(eyJ)[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*",
    # Raised in review on #196, which asked for the package and infrastructure
    # vendors a repo like this actually holds credentials for.
    r"(pypi-)[A-Za-z0-9_-]{16,}",
    r"(dckr_pat_)[A-Za-z0-9_-]{20,}",
    r"(sbp_)[a-f0-9]{40,}",
    # Twilio's API Key SID. Its ACCOUNT SID (`AC` + 32 hex) is deliberately
    # absent: an account SID is an identifier, published in dashboards and
    # request URLs, and redacting it would cost a reviewer a line it can
    # legitimately read for no gain. Twilio's auth TOKEN is 32 bare hex with no
    # prefix at all and is unreachable here -- one of the shapes named below.
    r"(SK)[0-9a-f]{32}",
)

# WHAT NO PREFIX LIST CAN REACH, named so the list is not read as coverage.
# Anthropic and OpenAI keys are already caught by the `sk-` entry above, so they
# are absent here rather than missing. But an AWS SECRET access key, a
# Cloudflare API token, a Twilio auth token and any raw hex or UUID credential
# carry no distinguishing prefix by design -- they are indistinguishable from a
# git SHA or a content hash by shape alone, which is exactly the collision that
# made an entropy rule unaffordable. TruffleHog reaches them because it VERIFIES
# a candidate against the vendor rather than matching it; a redactor cannot.
#
# The fixed lengths on DigitalOcean and Shopify are current-format specific and
# will stop matching if either vendor changes them. Raised in review on #196 and
# accepted: a loose length invites collisions, and a vendor changing its token
# format is a thing someone notices.

# NO VENDOR PREFIX MATCHES MID-IDENTIFIER.
#
# Every entry above names a prefix and none of them anchored it, so each one
# also fired inside a longer word: `TASK` + 32 hex matched the Twilio rule and
# came back `TASK<REDACTED>`, as did `MASK` and `FLASK`. That is the safe
# direction -- a benign token blanked, not a real one leaked -- but a redactor
# that eats identifiers hands the model a diff with holes in it, and the model
# then reviews the holes. Raised in review on #196 against the `SK` entry
# alone; measured across the table it was 20 of 20, so the boundary is applied
# once here rather than spelled twenty times.
#
# The class is word characters only. `-` is deliberately out: after a hyphen a
# prefix genuinely begins a new token, and matching there keeps the bias toward
# over-redaction on the one case where the two directions disagree.
#
# Zero-width, so `\1` is still the prefix and the one-group invariant holds.
_NOT_MID_IDENTIFIER = r"(?<![A-Za-z0-9_])"

# Built rather than written out, so a vendor is one line and the replacement
# cannot drift from the pattern -- the failure this file met four times in one
# week was an atom spelled twice and fixed once.
SECRET_PATTERNS += [
    (re.compile(_NOT_MID_IDENTIFIER + pattern), r"\1<REDACTED>")
    for pattern in _VENDOR_KEYS
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
    # ONE CALL AT A TIME. `_LINE_QUOTE_PARITY` is a single process-wide cursor,
    # so two threads redacting two files would interleave their gaps and answer
    # each other's question about which quote is open. The snapshot path calls
    # this in a loop, one file at a time, which is the only shape it is written
    # for -- parallelising that loop needs a cursor per call, not a shared one.
    # The class says the same thing, but this is the call site a future refactor
    # edits, so it is said where that edit happens. Raised on review of #181.
    #
    # AND THE COMMENT IS BACKED BY A TRIPWIRE, because two independent reviews
    # asked for one and both gave the same reason: the failure is SILENT. A
    # second concurrent caller does not crash and does not under-redact -- it
    # gets the wrong quote character, which is a mangled diff, which is the
    # phantom-syntax-error class this whole function exists to stop. A comment
    # does not stop that; it only explains it afterwards.
    #
    # It is a TRIPWIRE, NOT A LOCK, and the difference matters: the check and
    # the set are not atomic, so two threads arriving together can both pass.
    # It catches the case worth catching -- someone parallelises the snapshot
    # loop and the second call arrives while the first is mid-pass -- and it is
    # not a licence to call this concurrently. It raises OUT of `redact()`
    # rather than inside the `re.sub`, deliberately: `redact_assignment` would
    # swallow it into a total redaction and the caller would never learn.
    if _LINE_QUOTE_PARITY.in_use:
        raise RuntimeError(
            "redact() was re-entered while a pass was still running. The "
            "quote-parity cursor is process-wide state and cannot serve two "
            "passes at once; give each caller its own _LineQuoteParity, or "
            "serialise the calls."
        )
    _LINE_QUOTE_PARITY.in_use = True
    try:
        for pattern, replacement in SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text
    finally:
        _LINE_QUOTE_PARITY.in_use = False
        # The quote-parity cursor is per-pass state. Dropping it here keeps the
        # last subject from being pinned alive (the snapshot path calls this once
        # per file, with megabytes each) and leaves no position to carry into the
        # next call. Hygiene, not the correctness mechanism: `_LineQuoteParity`
        # resets itself on a new subject, so a caller that reaches for the
        # pattern table directly gets the same answer without this.
        _LINE_QUOTE_PARITY.restart()


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
# A review that could not run for want of a key. SEPARATE FROM STATUS_SKIPPED
# on purpose: `skipped` means there was nothing to review and the gate passes
# it, which is right for an empty diff and catastrophic for a missing key.
# Measured before this existed: call_claude() with no key returned a "Skipped:"
# body, review_status() fell through to STATUS_OK, and the gate accepted it --
# a green review check over a diff nothing read.
STATUS_NO_KEY = "no-key"
NO_KEY_BANNER = "Skipped: `ANTHROPIC_API_KEY` is not configured."
STATUS_FAILED = "failed"

# The banners review_text_from_body writes and review_status reads. One
# definition for both, so the classifier cannot drift from the prose it keys
# on; four separate reviews of this file flagged the two literals as a pair
# that had to stay byte-identical by hand.
EMPTY_BANNER = "**No review text came back from the API.**"
TRUNCATED_BANNER = (
    "> **Truncated: this review hit the output-token ceiling and is incomplete.**"
)
# An HTTP error was the one outcome review_status could not see. The call site
# returned a body reading "Claude API call failed: HTTP 429", which starts with
# neither banner above, so it classified as `ok` and the check went GREEN on a
# review that never happened. Measured in the kit on 2026-08-27: the gateway
# answered {"type":"budget_exceeded"} with 429, the posted comment said exactly
# that in plain text, and the job passed in 14 seconds.
FAILED_BANNER = "**The review did not run.**"


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
    if text.startswith(FAILED_BANNER):
        return STATUS_FAILED
    if text.startswith(EMPTY_BANNER):
        return STATUS_EMPTY
    if text.startswith(TRUNCATED_BANNER):
        return STATUS_TRUNCATED
    # Before this case existed the missing-key body matched none of the banners
    # above and fell through to STATUS_OK. THIS FUNCTION ONLY STATES THE FACT.
    # Whether "could not run" blocks is the gate's decision, because that is
    # where github.actor is available -- and the actor, never the key's absence,
    # is what may excuse it.
    if text.startswith(NO_KEY_BANNER):
        return STATUS_NO_KEY
    return STATUS_OK


def status_for(body: str) -> str:
    """Classify a body as written for the comment, header and all.

    THE SEAM THAT WAS NOT TESTED. call_claude returns the posted comment, which
    is prefixed with "## Claude Code Review", while review_status classifies the
    review TEXT and keys on its opening banner. main() reconciled the two with an
    inline split, so the classifier and the formatter were each covered by tests
    and their composition was covered by none: a banner could be correct, the
    formatting could be correct, and the status could still come out `ok`.
    Raised in review on kit #130, where the reviewer could not see main() in the
    diff and asked whether the fix fired at all. It does, and now a test says so
    rather than an argument.
    """
    return review_status(body.split("## Claude Code Review\n\n", 1)[-1])


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


ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"


def messages_endpoint() -> str:
    """Where the review request goes.

    CI is an unattended spender. With the host hardcoded, every review billed
    ANTHROPIC_API_KEY directly: no virtual key, no team ceiling, no daily cap,
    no attribution, and a runaway review loop invisible to every control the
    repo owner has.

    Pointing this at a LiteLLM broker needs no other change: the broker serves
    /v1/messages in Anthropic's own shape and accepts a virtual key through the
    same x-api-key header. A capped key in ANTHROPIC_API_KEY plus a base URL
    here is the whole migration.

    Unset, this is exactly the previous behaviour, so a repo that does not run a
    broker is unaffected.
    """
    base = (os.getenv("ANTHROPIC_BASE_URL") or ANTHROPIC_DEFAULT_BASE).strip().rstrip("/")
    if not base:
        base = ANTHROPIC_DEFAULT_BASE
    # THE KEY TRAVELS WITH THIS URL, so the host is not a formatting detail.
    # ANTHROPIC_BASE_URL is a repo VARIABLE, which is a lower bar than a secret:
    # anyone who can set one could point the reviewer at a box they control and
    # ANTHROPIC_API_KEY would go with the request. Refuse rather than warn --
    # a warning in an unattended CI log is a leak nobody reads.
    #
    # A scheme check, not an allowlist. This variable exists so the broker can
    # move without editing a file vendored into every repo; an allowlist would
    # need editing in all the same places. https is the property that matters:
    # no plaintext egress of the key, and no http:// to an arbitrary host.
    # LOOPBACK over plain http is allowed; nothing else is. The key cannot
    # leave the machine to reach 127.0.0.1, and the enforcer's own endpoint
    # check binds a loopback socket to stay network-free -- a blanket https
    # rule breaks that test, which makes the rule wrong rather than the test.
    #
    # urlsplit and a hostname SET, never a prefix: "http://127.0.0.1.evil.test"
    # starts with "http://127.0.0.1" and is not loopback. A prefix check would
    # ship a bypass inside the fix for a bypass.
    #
    # ONE PARSE ANSWERS BOTH QUESTIONS. The scheme test used to be
    # `base.startswith("https://")`, a byte comparison -- but URL schemes are
    # case-insensitive (RFC 3986 s3.1), so HTTPS:// failed it, fell into the
    # loopback branch, was not loopback, and SystemExited on a legitimate
    # endpoint. It failed closed, so never a leak; it just produced
    # "must be https:// (got: 'HTTPS://...')", which reads as nonsense to
    # whoever hits it. Reading scheme and host from the SAME urlsplit also means
    # the two can never disagree about what string they parsed.
    parts = urllib.parse.urlsplit(base)
    if parts.scheme.lower() != "https":
        host = parts.hostname  # urlsplit lower-cases this already
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise SystemExit(
                f"ANTHROPIC_BASE_URL must be https:// or loopback (got: {base!r}). "
                "The API key is sent with every request to it, so any other "
                "non-https base is refused rather than used."
            )
    return f"{base}/v1/messages"


# THE KEY MUST NOT LEAVE THE HOST THAT WAS VALIDATED.
#
# `messages_endpoint()` checks the scheme and host of the URL you CONFIGURED.
# Nothing checked where that URL sends you next, and urllib's default redirect
# handler strips only `content-length` and `content-type` -- every other header
# is copied onto the new request, including `x-api-key`. So a validated https
# endpoint answering 302 hands the API key to whatever host it names.
#
# MEASURED ON 2026-08-31, three ways rather than argued:
#   HTTPRedirectHandler.redirect_request(..., "https://evil.example/collect")
#     returned a Request for evil.example carrying {'X-api-key': '<sentinel>'}
#   end to end over two loopback servers, the redirect TARGET received the
#     sentinel value verbatim
#   with this opener, the same call raises HTTPError 302 and the target
#     receives nothing
#
# Returning None from redirect_request makes urllib raise rather than follow,
# which lands in the `except urllib.error.HTTPError` below and becomes
# STATUS_FAILED -- blocking, and visible in the posted comment. A redirect the
# operator actually wants is then a deliberate configuration change rather than
# a silent key egress, which is the right way round.
#
# This is the residual half of the threat `messages_endpoint()` was written for:
# it validated the destination and not the journey.
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# build_opener replaces a default handler when the class given subclasses it,
# so this opener is the default stack with redirects refused and nothing else
# changed -- proxies, cookies and TLS verification all behave as before.
_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def is_upstream_credit_exhausted(detail: str | None) -> bool:
    """Did the PROVIDER refuse for lack of credit, behind the broker's status?

    Read on the BODY rather than the status code, because the code is the
    broker's and the reason is the provider's. LiteLLM forwards an Anthropic
    refusal as HTTP 400 -- not 402, not 429 -- with the provider's own JSON
    embedded as an escaped string inside its own. Nothing about the status
    distinguishes it from a malformed request.

    Measured 2026-09-01, when it stopped every review and every agent
    constraint across the repo and the posted comment said only "HTTP 400 from
    the API".

    Deliberately narrow: `credit balance` and `insufficient credit` are the
    provider's phrasings for an empty account. `budget` is NOT matched here --
    that is the per-key ceiling the branch above already owns, and folding the
    two together would tell the operator to top up an account when the actual
    fix is to raise a cap.
    """
    lowered = (detail or "").lower()
    return "credit balance" in lowered or "insufficient credit" in lowered


def call_claude(review_text: str, review_scope: str = "diff") -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        # Built from NO_KEY_BANNER rather than repeating the string, so the
        # producer and the classifier cannot drift apart -- the whole defect was
        # a body no classifier case matched.
        return f"## Claude Code Review\n\n{NO_KEY_BANNER}"

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
        messages_endpoint(),
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
        with _NO_REDIRECT_OPENER.open(request, timeout=300) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        hint = ""
        # THE STATUS-AUTHORITATIVE BRANCH GOES FIRST, because two branches
        # below classify on the BODY and a body can say anything. A 3xx is a
        # redirect by definition -- the broker never reached the provider, so
        # no provider verdict can be in that body -- yet `"budget" in detail`
        # and `is_upstream_credit_exhausted(detail)` would both happily claim
        # one that merely contained the words, and the operator would be told
        # to top up an account over what is a base-URL mistake. Raised in
        # review on #204 against the credit branch alone; the `budget` branch
        # has the same hole, and ordering closes both rather than bolting a
        # code guard onto each.
        if 300 <= exc.code < 400:
            hint = (
                " The endpoint answered with a redirect, which is REFUSED rather"
                " than followed: urllib copies every header but content-length"
                " and content-type onto the new request, so following it would"
                " hand the API key to whatever host the redirect names. The key"
                " was NOT sent onward. Point ANTHROPIC_BASE_URL at the final"
                " host instead."
            )
        elif exc.code in (401, 403):
            hint = " The key is not accepted at this endpoint."
        elif exc.code == 402 or "budget" in detail.lower():
            hint = " This reads as a spending ceiling on the key rather than a transient error."
        elif exc.code == 429:
            hint = " Rate limited, or a spending ceiling; the body below says which."
        elif is_upstream_credit_exhausted(detail):
            # UPSTREAM CREDIT IS NOT THE KEY, THE CAP, OR A 429, and until
            # 2026-09-01 nothing here said so. The broker forwards the
            # provider's refusal as HTTP 400 with the reason nested two JSON
            # levels down -- `{"error":{"message":"{\"error\":{\"message\":
            # \"Your credit balance is too low...\"}}"}}` -- so the operator
            # saw "HTTP 400 from the API" and a wall of escaped JSON, while the
            # hint list offered "401 or 403 is the key, 402 is the budget",
            # none of which matched. Diagnosing it meant reading the nested
            # string by hand.
            #
            # Three different money failures now reach this branch table and
            # each needs its own sentence: a team DAILY CAP (429,
            # budget_exceeded), a per-key ceiling (402), and the provider
            # account being empty (400, here). They are fixed in different
            # places by different people, which is the whole reason the message
            # has to distinguish them.
            hint = (
                " The BROKER reached the provider and the provider refused for"
                " lack of credit -- this is the upstream account being empty,"
                " not the key, not a per-key ceiling, and not a rate limit."
                " No re-run will clear it and no ceiling can be raised past it."
                " Add credit to the provider account."
            )
        return (
            f"## Claude Code Review\n\n{FAILED_BANNER} HTTP {exc.code} from the API,"
            f" so nothing in this diff was reviewed.{hint}"
            f"\n\n```text\n{detail}\n```"
        )
    except OSError as exc:
        # A NETWORK FAILURE THAT IS NOT AN HTTP ERROR STILL HAS TO POST.
        #
        # Only HTTPError was caught, so a read timeout, a DNS failure, a reset
        # connection or a TLS error propagated out of main() and killed the
        # process before write_status() ran. The workflow caught THAT correctly
        # -- "No Claude review status was written, so nothing proves a review
        # ran", red rather than green -- but the promise this branch makes,
        # that the posted comment carries the reason, was not kept: there was
        # no comment at all, and the reason lived in a stack trace in the job
        # log. Measured on kit #192 on 2026-08-31: `TimeoutError: The read
        # operation timed out` after 5m17s, no comment, no status file.
        #
        # THE COMMENT ABOVE THIS ONE ALREADY DESCRIBES THIS FAILURE HAPPENING
        # ONCE BEFORE, and the fix chosen then was to raise the timeout from 60s
        # to 300s. That treated the symptom -- the review got slower, so the
        # ceiling moved -- and left the class: any network exception still took
        # the status file with it. It fired again at 300s. A timeout is not a
        # thing a ceiling can be raised past, only made rarer.
        #
        # OSError is the right net rather than a list of names: TimeoutError,
        # socket.timeout, ConnectionResetError and urllib's URLError are all
        # subclasses of it, and a new one will be too. HTTPError is a subclass
        # of URLError and therefore of OSError, so the handler above MUST stay
        # first -- it is more specific and carries the status code this one
        # cannot.
        return (
            f"## Claude Code Review\n\n{FAILED_BANNER} The API call did not"
            f" complete ({type(exc).__name__}), so nothing in this diff was"
            f" reviewed. This is a transport failure rather than a rejection:"
            f" there is no status code because no response arrived. A re-run is"
            f" the first thing to try.\n\n```text\n{exc}\n```"
        )

    # A BODY THAT ARRIVED BUT DOES NOT PARSE IS THE SAME BUG, ONE INPUT OVER.
    #
    # The handlers above catch the call failing. They do not catch the ANSWER
    # being unreadable: `json.loads` raises JSONDecodeError and `.decode` raises
    # UnicodeDecodeError, both ValueError and neither an OSError, so a proxy
    # error page, a truncated response or a gateway's HTML would propagate out
    # of main() and take `write_status()` with it -- the identical
    # crash-before-status-write this commit's parent fixed for transport.
    #
    # Raised in review on #197, on the commit that fixed the transport half. It
    # is the same defect wearing a different exception type, which is the fourth
    # time this file has met "fixed the shape, missed its neighbour" in a week.
    #
    # The read moved out of the try above so the raw bytes survive to be shown:
    # "the endpoint returned something unparseable" is not actionable, and the
    # first 1000 characters of it usually are -- a Cloudflare page and a
    # truncated JSON body look nothing alike.
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        detail = raw.decode("utf-8", errors="replace")[:1000]
        return (
            f"## Claude Code Review\n\n{FAILED_BANNER} The endpoint answered,"
            f" but the body did not parse as JSON ({type(exc).__name__}), so"
            f" nothing in this diff was reviewed. A proxy error page or a"
            f" truncated response reads like this; the first 1000 characters"
            f" are below.\n\n```text\n{detail}\n```"
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
    write_status(status_for(body))
    return 0


if __name__ == "__main__":
    sys.exit(main())

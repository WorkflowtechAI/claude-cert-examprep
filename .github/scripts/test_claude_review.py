# AUTO-SYNCED from the LLM Builder Kit. Do not edit here; edit the kit
# source and re-run sync-standards.ps1.

"""Tests for the review-response parsing in claude_review.py.

WHY THIS FILE EXISTS. The parsing it covers failed silently in production:
`content[0].get("text", "")` returns nothing when the first content block is a
`thinking` block, so PRs #23 and #24 posted "No review text returned" while the
API had already generated and billed ~2,048 output tokens of real review, and
both PRs went GREEN on that. The reviewer itself flagged the missing tests on
the fix's own PR. A parsing bug in the trust boundary of CI deserves recorded
response shapes, not a narrative.

Pure: no network, no API key, no subprocess. Run with `python -m unittest
discover .github/scripts` or `python .github/scripts/test_claude_review.py`.
"""

import contextlib
import importlib.util
import io
import itertools
import os
import re
import time
import unittest
from pathlib import Path
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "claude_review", Path(__file__).with_name("claude_review.py")
)
claude_review = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(claude_review)
review_text_from_body = claude_review.review_text_from_body


class ThinkingBlockFirst(unittest.TestCase):
    """The exact shape that caused the outage."""

    def test_text_after_thinking_block_is_found(self):
        body = {
            "content": [
                {"type": "thinking", "thinking": "let me look at the diff"},
                {"type": "text", "text": "Finding: the retry has no backoff."},
            ],
            "stop_reason": "end_turn",
        }
        self.assertEqual(
            review_text_from_body(body), "Finding: the retry has no backoff."
        )

    def test_the_old_naive_read_would_have_missed_it(self):
        """Documents the regression this file guards against."""
        body = {
            "content": [
                {"type": "thinking", "thinking": "reasoning"},
                {"type": "text", "text": "Real findings."},
            ],
            "stop_reason": "end_turn",
        }
        naive = body["content"][0].get("text", "")
        self.assertEqual(naive, "")  # the bug
        self.assertIn("Real findings.", review_text_from_body(body))  # the fix


class OrdinaryShapes(unittest.TestCase):
    def test_text_only(self):
        body = {
            "content": [{"type": "text", "text": "All clear."}],
            "stop_reason": "end_turn",
        }
        self.assertEqual(review_text_from_body(body), "All clear.")

    def test_multiple_text_blocks_are_joined_in_order(self):
        body = {
            "content": [
                {"type": "text", "text": "First."},
                {"type": "text", "text": "Second."},
            ],
            "stop_reason": "end_turn",
        }
        self.assertEqual(review_text_from_body(body), "First.\n\nSecond.")

    def test_unknown_block_types_are_ignored_not_fatal(self):
        body = {
            "content": [
                {"type": "some_future_block", "data": {"x": 1}},
                {"type": "text", "text": "Still found it."},
            ],
            "stop_reason": "end_turn",
        }
        self.assertEqual(review_text_from_body(body), "Still found it.")

    def test_malformed_blocks_do_not_crash(self):
        body = {
            "content": ["not a dict", None, {"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
        }
        self.assertEqual(review_text_from_body(body), "ok")


class EmptyIsReportedAsFailure(unittest.TestCase):
    """An empty result must never read as a clean bill of health."""

    def test_empty_content_says_tooling_failure_and_why(self):
        body = {"content": [], "stop_reason": "end_turn"}
        out = review_text_from_body(body)
        self.assertIn("tooling failure", out)
        self.assertIn("not a clean bill of health", out)
        self.assertIn("end_turn", out)

    def test_thinking_only_reports_the_block_types_it_got(self):
        body = {
            "content": [{"type": "thinking", "thinking": "..."}],
            "stop_reason": "max_tokens",
        }
        out = review_text_from_body(body)
        self.assertIn("tooling failure", out)
        self.assertIn("thinking", out)
        self.assertIn("max_tokens", out)

    def test_whitespace_only_text_counts_as_empty(self):
        body = {
            "content": [{"type": "text", "text": "   \n  "}],
            "stop_reason": "end_turn",
        }
        self.assertIn("tooling failure", review_text_from_body(body))

    def test_missing_stop_reason_says_unknown_rather_than_guessing(self):
        body = {"content": []}
        self.assertIn("unknown", review_text_from_body(body))


class TruncationIsFlagged(unittest.TestCase):
    """A NON-empty review cut off at the ceiling still is not a clean review."""

    def test_truncated_review_gets_a_banner_above_the_findings(self):
        body = {
            "content": [{"type": "text", "text": "Finding one. Finding tw"}],
            "stop_reason": "max_tokens",
        }
        out = review_text_from_body(body)
        self.assertIn("Truncated", out)
        self.assertIn("incomplete", out)
        self.assertIn("CLAUDE_REVIEW_MAX_TOKENS", out)
        # The findings survive; the banner is added, not substituted.
        self.assertIn("Finding one.", out)

    def test_complete_review_gets_no_banner(self):
        body = {
            "content": [{"type": "text", "text": "Finding one. Finding two."}],
            "stop_reason": "end_turn",
        }
        self.assertNotIn("Truncated", review_text_from_body(body))


class StatusDecidesTheCheckColour(unittest.TestCase):
    """The banner informs a human; the STATUS decides whether CI goes green.

    A review that stopped at the ceiling verified an unknown fraction of the
    diff. It used to pass anyway: the PR #32 review spent all 8,192 output
    tokens on reasoning, emitted the words "This diff", billed $0.15, and went
    green. Same rule the missing-key step already enforces at the top of the
    job.
    """

    def test_a_truncated_review_is_not_ok(self):
        text = review_text_from_body({
            "content": [{"type": "text", "text": "## Summary\n\nThis diff"}],
            "stop_reason": "max_tokens",
        })
        self.assertEqual(claude_review.review_status(text), claude_review.STATUS_TRUNCATED)

    def test_an_empty_review_is_not_ok(self):
        text = review_text_from_body({"content": [{"type": "thinking"}], "stop_reason": "end_turn"})
        self.assertEqual(claude_review.review_status(text), claude_review.STATUS_EMPTY)

    def test_a_complete_review_is_ok(self):
        text = review_text_from_body({
            "content": [{"type": "text", "text": "Finding one. Finding two."}],
            "stop_reason": "end_turn",
        })
        self.assertEqual(claude_review.review_status(text), claude_review.STATUS_OK)

    def test_a_review_that_merely_mentions_truncation_is_still_ok(self):
        # The classifier keys on the banner this script writes at the START of
        # the body, not on the word appearing anywhere. A review discussing a
        # truncation bug in the diff under review must not fail the build.
        text = review_text_from_body({
            "content": [{"type": "text", "text": "The snapshot is Truncated here, which is fine."}],
            "stop_reason": "end_turn",
        })
        self.assertEqual(claude_review.review_status(text), claude_review.STATUS_OK)

    def test_the_ceiling_leaves_room_for_reasoning_before_the_answer(self):
        # 8192 covered thinking AND the answer, and thinking is not bounded by
        # "be concise", so the answer was the part that got cut. Pinning the
        # floor so a future trim has to argue with this comment.
        self.assertGreaterEqual(claude_review.DEFAULT_CLAUDE_REVIEW_MAX_TOKENS, 16000)


class TheCheckoutFollowsTheBaseBranch(unittest.TestCase):
    """The workflow checks out the base BRANCH, not the base SHA in the event.

    `github.event.pull_request.base.sha` is the base tip as it stood when the
    PR was opened, and GitHub does not refresh it when the base moves. So a fix
    merged after a PR was opened never reached that PR's reviews: one managed
    repo had a PR opened against the 8192-token reviewer, master moved to the
    32000-token one, and every run AND re-run on that PR still checked out the
    old script and died on max_tokens. Rebasing was the only way in. A branch
    name is resolved when the job runs.

    Still the trusted side of pull_request_target: the base branch is history
    that only push access can change, and fork PRs never reach the job.
    Checking out head.sha with secrets in the environment is the thing that
    must never happen, so that is pinned here as well.
    """

    @staticmethod
    def _workflow():
        here = Path(__file__).resolve()
        # Deployed, the test sits in .github/scripts/ beside .github/workflows/.
        # In the kit's template directory the two files are siblings.
        for candidate in (
            here.parents[1] / "workflows" / "claude-review.yml",
            here.with_name("claude-review.yml"),
        ):
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        return None

    def setUp(self):
        workflow = self._workflow()
        if workflow is None:
            self.fail(
                "claude-review.yml was not found beside this test. The workflow and "
                "this file ship together (standards-map.ps1, Bootstrap-Repo.ps1)."
            )
        refs = re.findall(r"^\s*ref:\s*(\S.*?)\s*$", workflow, flags=re.M)
        self.assertEqual(len(refs), 1, f"expected exactly one checkout ref:, found {refs}")
        self.ref = refs[0]

    def test_the_ref_is_the_base_branch_resolved_when_the_job_runs(self):
        self.assertIn("github.event.pull_request.base.ref", self.ref, self.ref)

    def test_the_ref_is_not_the_sha_frozen_when_the_pr_was_opened(self):
        self.assertNotIn("base.sha", self.ref, self.ref)

    def test_the_ref_is_never_the_pull_request_head(self):
        for untrusted in ("head.sha", "head.ref", "head_ref", "merge_commit_sha"):
            with self.subTest(untrusted=untrusted):
                self.assertNotIn(untrusted, self.ref, self.ref)

    def test_workflow_dispatch_still_falls_back_to_the_dispatched_sha(self):
        self.assertIn("github.sha", self.ref, self.ref)


class FileSelectionTreatsRootLikeNested(unittest.TestCase):
    """The exclude list is written as `**/dist/**`, and fnmatch has no `**`.

    Its `*` does match `/`, so the pattern reads as `*/dist/*` and needs a slash
    BEFORE `dist`. A nested path has one; a root-level path does not. Measured
    before the fix: `web/package-lock.json` excluded, `package-lock.json`
    reviewed, and the same for node_modules, dist, vendor, .venv and *.min.js.
    A root lockfile is the common case, and it went to the model in every PR
    that touched it.
    """

    def test_root_level_build_artifacts_are_excluded_like_nested_ones(self):
        for path in (
            "package-lock.json", "web/package-lock.json",
            "pnpm-lock.yaml", "uv.lock",
            "node_modules/x/index.js", "web/node_modules/x/index.js",
            "dist/bundle.js", "web/dist/bundle.js",
            "vendor/lib.py", ".venv/lib/site.py",
            "app.min.js", "static/app.min.js",
        ):
            with self.subTest(path=path):
                self.assertFalse(claude_review.include_file(path), path)

    def test_source_is_reviewed_at_every_depth(self):
        for path in (
            "app.py", "src/a/b/c.py", "web/src/index.tsx",
            ".github/workflows/ci.yml", "README.md",
        ):
            with self.subTest(path=path):
                self.assertTrue(claude_review.include_file(path), path)


class RedactionLeavesActionsExpressionsAlone(unittest.TestCase):
    """`${{ secrets.X }}` names a secret; it does not contain one.

    The redactor rewrote `ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}`
    into `ANTHROPIC_API_KEY=<REDACTED> secrets.ANTHROPIC_API_KEY }}` before the
    model saw the diff, and the model then reported the workflow as broken
    YAML, as a blocking finding, on a line that was fine.
    """

    def test_a_workflow_secret_reference_survives(self):
        line = "      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}"
        self.assertEqual(claude_review.redact(line), line)

    def test_a_quoted_workflow_expression_survives_too(self):
        # "${{ ... }}" is ordinary YAML quoting; it names a secret, contains none.
        for line in ('      TOKEN: "${{ secrets.TOKEN }}"', "      LIMIT: '${{ vars.LIMIT }}'"):
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), line)

    def test_a_real_value_is_still_redacted(self):
        self.assertEqual(
            claude_review.redact("api_key = fake_abc123def456"),
            "api_key=<REDACTED>",
        )
        self.assertNotIn("hunter2", claude_review.redact("password: hunter2"))

    def test_a_quoted_value_is_redacted_too(self):
        # The value class excluded the opening quote, so `password: "abc123"`
        # never matched and went to the model as written.
        cases = {
            'password: "hunter2"': "password=<REDACTED>",
            "api_key='fake_abc123'": "api_key=<REDACTED>",
            'TOKEN = "abc123"': "TOKEN=<REDACTED>",
            # Spaces inside the quotes are part of the value; the tail used to leak.
            'password: "correct horse battery"': "password=<REDACTED>",
            # An unterminated quote still hides the token after it.
            'password: "unterminated': "password=<REDACTED>",
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                # Exact, so a dangling closing quote fails here too: an
                # unbalanced quote is the kind of artifact the model then
                # reads as broken syntax.
                self.assertEqual(claude_review.redact(line), want)

    def test_a_json_quoted_key_is_redacted_too(self):
        # A JSON key carries its closing quote between the name and the colon,
        # so `"password": "hunter2"` never matched: `\s*[:=]` had to follow the
        # name directly, and the value went to the model as written. Verified
        # with a probe on 2026-08-22.
        cases = {
            '{"password": "hunter2", "user": "bob"}': '{"password=<REDACTED>, "user": "bob"}',
            '{"api_key":"fake_abc123"}': '{"api_key=<REDACTED>}',
            # A Python dict quotes the same way, with the other quote.
            "{'client_secret': 'abc123'}": "{'client_secret=<REDACTED>}",
            # The common JSON shape is a longer key that ENDS in a secret name.
            '"db_password": "correct horse battery"': '"db_password=<REDACTED>',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), want)

    def test_a_json_quoted_expression_survives(self):
        # The same key shape naming a workflow secret still contains no value.
        line = '{"TOKEN": "${{ secrets.TOKEN }}"}'
        self.assertEqual(claude_review.redact(line), line)

    def test_a_quoted_name_that_ends_its_line_is_code(self):
        # `if kind == "token":` ends a line; the value of a JSON key never does.
        # Spanning the line break here ate the next line's first word and would
        # hand the model a hunk that does not parse.
        line = 'if kind == "token":\n    return x'
        self.assertEqual(claude_review.redact(line), line)
        # The unquoted form still spans it: YAML allows the scalar on the next line.
        self.assertNotIn("hunter2", claude_review.redact("password:\n  hunter2"))

    def test_a_comparison_is_code_not_an_assignment(self):
        # `==` and `===` compare. Only the first `=` used to match, and the rest
        # of the line was taken as the value. Quoted or bare, the line survives.
        for line in (
            "if password == other_var:",
            'if "password" == other_var:',
            "if (token === expected) {",
        ):
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), line)

    def test_the_other_assignment_shapes_are_redacted_too(self):
        cases = {
            # PHP array: the same closing-quote gap as a JSON key.
            "'password' => 'hunter2',": "'password=<REDACTED>,",
            # Python walrus: `:=` is one separator, not a colon and a stray `=`.
            'if (password := "hunter2"):': "if (password=<REDACTED>):",
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), want)

    def test_an_escaped_quote_does_not_end_the_value(self):
        # `"hun\"ter2"` matched up to the backslash and leaked `ter2"}` to the
        # model: the value class stopped at any quote. A backslash escape is
        # part of the value, and so is the doubled quote that YAML single
        # quotes, SQL and PowerShell use. An empty value is two quotes, not a
        # doubled one.
        cases = {
            '{"password": "hun\\"ter2"}': '{"password=<REDACTED>}',
            "password = 'it\\'s'": "password=<REDACTED>",
            # A regex literal is the everyday shape of an escaped quote.
            'token = "[^\\"]+"': "token=<REDACTED>",
            # An escaped backslash before the closing quote does not escape it.
            'password: "C:\\\\"': "password=<REDACTED>",
            "password: 'it''s'": "password=<REDACTED>",
            '$password = "say ""hi"""': "$password=<REDACTED>",
            '{"password": "", "user": "bob"}': '{"password=<REDACTED>, "user": "bob"}',
            '"password": ""': '"password=<REDACTED>',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), want)

    def test_a_quoted_expression_survives_a_leading_space(self):
        # `" ${{ secrets.X }}"` is still an expression. It was redacted because
        # the lookahead sat right after the opening quote, and the stray space
        # is a workflow bug the model can only flag if it gets to see it.
        for line in (
            '      TOKEN: " ${{ secrets.TOKEN }}"',
            '{"TOKEN": "  ${{ secrets.TOKEN }}"}',
            "      TOKEN: '\t${{ secrets.TOKEN }}'",
        ):
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), line)

    def test_a_quoted_key_and_its_separator_share_a_line(self):
        # The quoted-key form is one line: key, separator, value. No serializer
        # breaks the line between a key and its colon; the shape that does put
        # a quoted name above a `:` is a formatted ternary, which used to be
        # folded into one mangled line.
        line = 'const kind = isToken\n  ? "token"\n  : "cookie";'
        self.assertEqual(claude_review.redact(line), line)
        self.assertEqual(claude_review.redact('"password"\n: "x"'), '"password"\n: "x"')

    def test_the_unquoted_form_spans_one_line_break_through_a_diff_prefix(self):
        # YAML allows `password:` with its scalar on the next line. In a diff,
        # which is what redact() mostly sees, that next line starts with `+`,
        # `-` or a space, and the old `\s*` took the prefix as the value and
        # left the real one on the wire.
        redacted = {
            "+password:\n+  hunter2": "+password=<REDACTED>",
            "-password:\n-  hunter2": "-password=<REDACTED>",
            ' password:\n   "hunter2"': " password=<REDACTED>",
            "password:\n\thunter2": "password=<REDACTED>",
            # A value with a colon in it is a value, not a sibling key: the key
            # shape is `word:` followed by a space or the end of the line.
            "password:\n  redis://user:hunter2@host": "password=<REDACTED>",
            "password:\n  db.internal:5432": "password=<REDACTED>",
            # On the key's own line a value ending in `:` is a value.
            "token: abc123:": "token=<REDACTED>",
        }
        for text, want in redacted.items():
            with self.subTest(text=text):
                self.assertEqual(claude_review.redact(text), want)
        for text in (
            # One line break. A blank line ends the search: `password:` in
            # prose, then a code fence, used to lose the fence.
            "Set the password:\n\n```bash\nlogin",
            "password:\n\nhunter2",
            # A sibling key is not the value of an empty `password:`.
            "+  password:\n+  username: bob",
            "password:\n  username: bob",
            # The accepted gap: a next-line value that is itself `word:` at the
            # end of its line cannot be told from a sibling key, and the key is
            # the common shape.
            "password:\n  abc123:",
            # A lone dash is a list marker (or a diff marker), not a value.
            "password:\n  - item",
        ):
            with self.subTest(text=text):
                self.assertEqual(claude_review.redact(text), text)

    def test_the_shapes_combine(self):
        cases = {
            # Unanchored name, PHP arrow and quoted key at once.
            "'db_password' => 'hunter2',": "'db_password=<REDACTED>,",
            '"password" => "hunter2",': '"password=<REDACTED>,',
            # The match is case-insensitive and the replacement keeps the case.
            '"Password": "hunter2"': '"Password=<REDACTED>',
            'Api-Key = "x"': "Api-Key=<REDACTED>",
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), want)

    def test_the_key_family_is_named_and_a_near_miss_is_another_name(self):
        # The name is unanchored at the start (`db_password`) and closed at the
        # end by the quote, space or separator that follows it. `SECRET_KEY`
        # and its relatives fail that closing rule on `_KEY`, so the family is
        # spelled out; `passwordless`, `token_url` and `tokens` fail it too,
        # and are other names. Pinned so a widening is a conscious change.
        redacted = {
            'SECRET_KEY = "django-insecure-fake"': "SECRET_KEY=<REDACTED>",
            "STRIPE_SECRET_KEY=fake_abc": "STRIPE_SECRET_KEY=<REDACTED>",
            "AWS_SECRET_ACCESS_KEY=fake_abc": "AWS_SECRET_ACCESS_KEY=<REDACTED>",
            "SECRET_KEY_BASE=fake_abc": "SECRET_KEY_BASE=<REDACTED>",
            "MINIO_ACCESS_KEY=fake_abc": "MINIO_ACCESS_KEY=<REDACTED>",
            "private_key: fake_abc": "private_key=<REDACTED>",
        }
        for line, want in redacted.items():
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), want)
        for line in (
            "passwordless=true",
            'token_url = "https://example.test/oauth/token"',
            "tokens = text.split()",
            "secrets: inherit",
            "primary_key=True",
        ):
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), line)

    def test_no_literal_survives_any_combination_of_shapes(self):
        # The cases above are the incidents. This is the general claim they
        # stand in for: a name in any key shape, any separator, any quoting
        # and any terminator after the value, and the literal never reaches
        # the model, the name stays, the terminator stays. Deterministic, so
        # a failure names its line.
        names = ("password", "API_KEY", "db_password", "SECRET_KEY", "client_secret", "Token")
        key_quotes = ("", '"', "'")
        separators = (":", ": ", "=", " = ", ":=", " => ")
        value_quotes = ("", '"', "'")
        terminators = ("", ",", ";", ")", " # note", "\n")
        for name, kq, sep, vq, term in itertools.product(
            names, key_quotes, separators, value_quotes, terminators
        ):
            line = f"{kq}{name}{kq}{sep}{vq}hunter2{vq}{term}"
            with self.subTest(line=line):
                out = claude_review.redact(line)
                self.assertNotIn("hunter2", out)
                self.assertTrue(out.startswith(f"{kq}{name}=<REDACTED>"), out)
                self.assertTrue(out.endswith(term), out)


class RedactionSparesEnvLookups(unittest.TestCase):
    """An env-var lookup NAMES a secret without containing one, so it is left
    alone -- the same category as a `${{ secrets.X }}` expression, and exempted
    for the same reason.

    `token = os.environ.get("GITHUB_TOKEN") or ""` reached the model as
    `token=<REDACTED> or ""`, which is not valid Python, and the model reported
    a SyntaxError as a BLOCKING finding on kit #69 -- twice in a row, spending
    the whole review on an artifact and citing corroborating "evidence" (`import
    os` is unused) that was also the artifact. claude_review.py's own source
    hits this shape.

    THE EXEMPTION IS NARROWED SO IT CANNOT HIDE A VALUE, which is the only
    reason it is safe. The call form takes ONE string argument, so a default
    argument is not an env lookup and is still redacted; and the exemption is
    withdrawn entirely if the rest of the line carries a non-empty quoted
    literal, so an `or "hunter2"` fallback is still redacted while `or ""` is
    not. Both halves are pinned below: the MUST-REDACT cases matter more than
    the tidy ones, because that is the direction that leaks.
    """

    def test_a_bare_lookup_is_left_exactly_as_written(self):
        for line in (
            'token = os.environ.get("GITHUB_TOKEN")',
            'token = os.getenv("GH_TOKEN") or ""',
            'token = os.environ.get("GITHUB_TOKEN") or ""',
            "const token = process.env.GITHUB_TOKEN;",
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))

    def test_a_default_argument_is_a_value_and_is_still_redacted(self):
        # os.environ.get(NAME, DEFAULT): the default can be a real secret, so
        # the two-argument form is deliberately not an env lookup here.
        self.assertEqual(
            "password=<REDACTED>",
            claude_review.redact('password = os.environ.get("PW", "hunter2")'),
        )
        self.assertEqual(
            "token=<REDACTED>", claude_review.redact('token = os.getenv("T", "sk-live-abc123")')
        )

    def test_a_literal_fallback_on_the_line_withdraws_the_exemption(self):
        # The line is REDACTED rather than left verbatim, which is the whole
        # guarantee: the exemption never applies where a literal is in reach.
        # What survives after the redacted value is unchanged from before this
        # exemption existed and is not this rule's to fix -- a trailing literal
        # after ANY redacted call value stays visible (`resolveKey("X") or
        # "hunter2"` reads the same way), because the value token ends at the
        # closing paren. The redactor is a heuristic last line, not a
        # guarantee, as SECRET_PATTERNS says at the top.
        for line in (
            'token = os.environ.get("X") or "hunter2"',
            'token = os.getenv("X") if x else "hunter2"',
            'const token = process.env.X || "hunter2";',
        ):
            with self.subTest(line=line):
                out = claude_review.redact(line)
                self.assertIn("<REDACTED>", out)
                self.assertNotEqual(line, out)

    def test_the_exemption_matches_the_unexempted_baseline_on_those_lines(self):
        # Same input, same output as a non-env call value: proof the exemption
        # withdrew completely rather than half-applying.
        self.assertEqual(
            claude_review.redact('token = resolveKey("X") or "hunter2"'),
            claude_review.redact('token = os.environ.get("X") or "hunter2"'),
        )

    def test_a_non_identifier_argument_is_not_a_lookup(self):
        # Matching on call shape alone would exempt this and hand the model a
        # real key. An env var name is an identifier; a key generally is not.
        for line in (
            'token = os.getenv("sk-ant-real-secret-value")',
            'api_key = os.environ.get("AKIA-NOT/AN.IDENT")',
            "token = os.getenv(\'hunter two\')",
        ):
            with self.subTest(line=line):
                self.assertEqual("<REDACTED>", claude_review.redact(line).split("=", 1)[1])

    def test_a_lookalike_that_is_not_an_env_lookup_is_still_redacted(self):
        # Only the exempted forms are exempt; anything that merely resembles
        # one is an ordinary call and is consumed whole.
        self.assertEqual("token=<REDACTED>", claude_review.redact('token = myenv.get("X")'))
        self.assertEqual("token=<REDACTED>", claude_review.redact('token = get_environ("X")'))

    def test_the_subscript_form_is_deliberately_not_exempt(self):
        # `os.environ["X"]` stays on the subscript rule, which predates this
        # exemption and is pinned by RedactionSparesCode. Nothing is gained by
        # exempting it: `token=<REDACTED>` already reads as valid code. What
        # broke was the trailing ` or ""` after a consumed CALL, not the lookup.
        self.assertEqual(
            "token=<REDACTED>", claude_review.redact('token = os.environ["TOKEN"]')
        )

    def test_a_typed_default_is_deliberately_not_exempt(self):
        # A type annotation means type and default are redacted together, which
        # predates this exemption and is pinned by RedactionSparesTypeAnnotations.
        self.assertEqual(
            "password=<REDACTED>", claude_review.redact('password: str = os.getenv("X")')
        )


class RedactionSparesCode(unittest.TestCase):
    """A key-name followed by a FUNCTION CALL is redacted whole, never left dangling.

    `brokerApiKey: resolveKey("LITELLM_API_KEY"),` was being rewritten to
    `brokerApiKey=<REDACTED>LITELLM_API_KEY"),` and handed to the model, which
    then reported a "broken hunk" on a line that compiles, as a blocking
    finding, round after round. Leaving calls alone was the first fix and was
    wrong: `password=hunter2(prod)` is call-shaped too, and a redactor that
    skips it leaks. So a call is consumed through its closing paren and
    replaced like any other value. Nothing dangles; nothing leaks.
    """

    def test_identifier_call_is_redacted_whole_with_nothing_dangling(self):
        out = claude_review.redact('brokerApiKey: resolveKey("LITELLM_API_KEY"),')
        self.assertEqual("brokerApiKey=<REDACTED>,", out)

    def test_dotted_call_is_redacted_whole(self):
        out = claude_review.redact('apiKey: settings.resolve("X"),')
        self.assertEqual("apiKey=<REDACTED>,", out)

    def test_a_nested_call_is_consumed_two_levels_deep(self):
        self.assertEqual("token=<REDACTED>;", claude_review.redact('token = resolveKey(env("X"));'))
        self.assertEqual("password=<REDACTED>", claude_review.redact("password = a(b(c(d)))"))

    def test_a_secret_abutting_a_paren_is_redacted_not_leaked(self):
        # The case that made "leave calls alone" wrong: call-shaped, and a secret.
        self.assertEqual("password=<REDACTED>", claude_review.redact("password=hunter2(prod)"))
        self.assertEqual("PASSWORD=<REDACTED>", claude_review.redact("PASSWORD=Summer(2024)!"))
        self.assertEqual("password=<REDACTED>", claude_review.redact("password=hunter2[prod]"))

    def test_a_subscript_a_suffix_run_and_a_command_substitution_go_the_same_way(self):
        # Consume, never skip: a subscript, a run of suffixes, `$(cmd)` and a
        # parenthesised value are redacted whole, not left dangling and not
        # left to the model.
        cases = {
            'token = os.environ["TOKEN"]': "token=<REDACTED>",
            'token = d["a"]["b"]': "token=<REDACTED>",
            "token = f(x)[0]": "token=<REDACTED>",
            "token = f(x)(y), z": "token=<REDACTED>, z",
            "TOKEN=$(gcloud auth print-access-token)": "TOKEN=<REDACTED>",
            "password=(x)": "password=<REDACTED>",
            # `=>` before a call must not fall back to `=` plus a `>` value.
            "'password' => getenv(\"DB_PASSWORD\"),": "'password=<REDACTED>,",
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), want)

    def test_literals_are_still_redacted(self):
        self.assertIn("<REDACTED>", claude_review.redact("api_key=abc123"))
        self.assertIn("<REDACTED>", claude_review.redact('password: "hunter the second"'))
        self.assertNotIn("hunter", claude_review.redact('password: "hunter the second"'))

    def test_env_ref_value_is_still_redacted(self):
        # $FOO / ${FOO} are values, not calls; a secret pulled from env in a
        # shell line is still a secret on the wire.
        self.assertIn("<REDACTED>", claude_review.redact("token=$MY_TOKEN"))
        self.assertEqual(claude_review.redact("token=${MY_TOKEN}"), "token=<REDACTED>")

    def test_a_json_quoted_key_whose_value_is_a_call_is_redacted_whole(self):
        # The JSON form (`"apiKey": ...`) takes the same path; the key's own
        # closing quote is consumed with the separator, as for every JSON value.
        out = claude_review.redact('"brokerApiKey": resolveKey("LITELLM_API_KEY"),')
        self.assertEqual('"brokerApiKey=<REDACTED>,', out)

    def test_a_secret_followed_by_a_parenthetical_is_still_redacted(self):
        # A space before the paren is not a call; the word is the secret and the
        # parenthetical is prose that stays.
        self.assertEqual(
            "password=<REDACTED> (rotated weekly)",
            claude_review.redact("password=hunter2 (rotated weekly)"),
        )

    def test_a_literal_default_inside_a_call_goes_with_the_call(self):
        # Redacting the call whole closes the gap that skipping it left open: a
        # literal passed as an argument is inside the redacted span.
        out = claude_review.redact('apiKey = getEnv("API_KEY", "hunter2")')
        self.assertEqual("apiKey=<REDACTED>", out)
        self.assertNotIn("hunter2", out)

    def test_a_call_broken_across_lines_falls_back_to_the_bare_form(self):
        # Rare, and a display cost only: the first line's fragment is redacted,
        # nothing on it leaks, the continuation is left as it was.
        out = claude_review.redact('apiKey: resolveKey(\n  "X"),')
        self.assertNotIn("resolveKey(", out)
        self.assertIn("<REDACTED>", out)

    def test_the_nesting_boundary_is_three_paren_levels(self):
        # Three levels are consumed whole. Four fall back to the bare form:
        # the closing parens dangle and a literal argument at that depth stays
        # visible. Pinned so the depth limit is a stated number, not a guess.
        self.assertEqual("apiKey=<REDACTED>", claude_review.redact("apiKey: a(b(c(ENV)))"))
        four_deep = claude_review.redact("password = a(b(c(d(e))))")
        self.assertEqual("password=<REDACTED>))))", four_deep)
        literal_four_deep = claude_review.redact('apiKey: a(b(c(d("X"))))')
        self.assertEqual('apiKey=<REDACTED>"X"))))', literal_four_deep)

    def test_a_quote_that_closes_an_enclosing_string_is_not_eaten(self):
        # The bare-token branch took any trailing quote, so `x('api_key=abc123')`
        # came out as `x('api_key=<REDACTED>)`: an unterminated literal in the
        # diff the model sees, which it reported as "the test files are
        # syntactically broken" on every PR whose tests carry a fixture. A
        # trailing quote is the value's own only when a leading one opened it.
        self.assertEqual("x('api_key=<REDACTED>')", claude_review.redact("x('api_key=abc123')"))
        self.assertEqual('f("token=<REDACTED>")', claude_review.redact('f("token=abc123")'))
        # The unterminated-quote fallback still takes its own leading quote.
        self.assertEqual("password=<REDACTED>", claude_review.redact('password="hunter2'))

    def test_the_punctuation_after_a_bare_value_is_code(self):
        # No secret contains `,`, `;` or `)`; the code around a value does.
        cases = {
            "login(password=pw, user=u)": "login(password=<REDACTED>, user=u)",
            "login(password=get_pw(), user=u)": "login(password=<REDACTED>, user=u)",
            "connect(host=h, password=pw)": "connect(host=h, password=<REDACTED>)",
            "$password = hunter2;": "$password=<REDACTED>;",
            "password: hunter2, user: bob": "password=<REDACTED>, user: bob",
            # A match arm is redacted, an accepted over-redaction, call or not.
            "token => x,": "token=<REDACTED>,",
            "token => parse(x),": "token=<REDACTED>,",
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), want)


class RedactionIsLinear(unittest.TestCase):
    """redact() runs on every PR diff before the model sees it, so a pathological
    hunk must cost time proportional to its size, never a hang.

    The three value alternatives start with different characters and every
    lookahead is one bounded scan, so no quantifier has two ways to consume a
    character. The bound here is loose on purpose (a 1 MB input takes well
    under a second on a laptop); catastrophic backtracking takes minutes, and
    that is the regression this pins.
    """

    def test_a_pathological_input_redacts_in_linear_time(self):
        shapes = {
            "unbalanced parens after the key": "password=" + "(" * 200_000,
            "unbalanced parens before the key": "(" * 200_000 + "password=x",
            "deep nesting": "password=a" + "(" * 100_000 + ")" * 100_000,
            "unterminated quote then a megabyte": 'password: "' + "a" * 1_000_000,
            "a megabyte of backslashes": 'password: "' + "\\" * 1_000_000,
            "a megabyte of doubled quotes": 'password: "' + '""' * 500_000,
            "a call with a megabyte of arguments": "password=f(" + "a," * 500_000 + ")",
            "many keys, many spaces": "password: " * 200_000,
            "a long type union": "password: " + "str | " * 100_000 + "x",
            "many bare type words": "password: " + "str " * 200_000,
        }
        for label, text in shapes.items():
            with self.subTest(shape=label):
                started = time.perf_counter()
                claude_review.redact(text)
                self.assertLess(time.perf_counter() - started, 10.0, label)


class RedactionSparesTypeAnnotations(unittest.TestCase):
    """`password: str` is an annotation, not a secret.

    Every typed Python signature and TS parameter matched the key=value rule,
    and the reviewer then reported the file as syntactically broken
    (`async (_token: string) => {}` came out as `_token=<REDACTED>`). A closed
    set of type words is never a secret; a typed DEFAULT is a value and goes
    whole, so nothing that was masked before becomes visible but the word.
    """

    def test_a_bare_type_word_is_an_annotation(self):
        for line in (
            "def login(user: str, password: str) -> None:",
            "async (_token: string) => {}",
            "password: str | None",
            "password: string;",
            "token: bytes",
            "password: Boolean,",
            # An absent value is not a secret either.
            "password = None",
            "token = null;",
            "token = undefined",
            "password: null",
        ):
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), line)

    def test_a_typed_default_is_redacted_whole(self):
        cases = {
            'password: str = "hunter2"': "password=<REDACTED>",
            "token: str | None = None": "token=<REDACTED>",
            'password: str = os.getenv("X")': "password=<REDACTED>",
            "password: int = 5)": "password=<REDACTED>)",
            "api_key: str | None = None,": "api_key=<REDACTED>,",
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), want)

    def test_only_the_whole_word_is_a_type(self):
        cases = {
            "password: strong_pw": "password=<REDACTED>",
            "password: stringy": "password=<REDACTED>",
            'password: "str"': "password=<REDACTED>",
            # A quoted literal under a secret name stays redacted: the
            # fail-closed side of this rule.
            '{ token: "h", keyName: "k" }': '{ token=<REDACTED>, keyName: "k" }',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), want)


class TheCeilingComesFromTheEnvironment(unittest.TestCase):
    """The workflow always sets CLAUDE_REVIEW_MAX_TOKENS now, from a repository
    variable that is usually unset. So the value the script usually sees is the
    EMPTY STRING, not an absent key, and that has to mean the default."""

    DEFAULT = claude_review.DEFAULT_CLAUDE_REVIEW_MAX_TOKENS

    @staticmethod
    def _ceiling(value):
        """max_tokens_from_env() with the variable set to `value`, stderr captured."""
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_REVIEW_MAX_TOKENS": value}):
            with contextlib.redirect_stderr(err):
                got = claude_review.max_tokens_from_env()
        return got, err.getvalue()

    def test_empty_string_means_the_default(self):
        self.assertEqual(self._ceiling("")[0], self.DEFAULT)

    def test_unset_means_the_default(self):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_REVIEW_MAX_TOKENS"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(claude_review.max_tokens_from_env(), self.DEFAULT)

    def test_a_repo_value_wins(self):
        self.assertEqual(self._ceiling(" 24000 ")[0], 24000)

    def test_a_typo_costs_the_tuning_not_the_review(self):
        self.assertEqual(self._ceiling("lots")[0], self.DEFAULT)

    def test_zero_and_negatives_are_typos_too(self):
        for bad in ("0", "-5"):
            with self.subTest(value=bad):
                self.assertEqual(self._ceiling(bad)[0], self.DEFAULT)

    def test_a_typo_is_said_out_loud(self):
        # The fallback is the right outcome. A SILENT fallback would leave a
        # mistyped repository variable unnoticed for as long as nobody read the
        # job log closely, so it is a workflow warning, which the Actions UI
        # surfaces as an annotation on the run.
        for bad in ("lots", "0", "-5"):
            with self.subTest(value=bad):
                _, err = self._ceiling(bad)
                self.assertIn("::warning::", err)
                self.assertIn(bad, err)
                self.assertIn(str(self.DEFAULT), err)

    def test_the_normal_paths_are_quiet(self):
        # Empty is the usual value and a valid number is a deliberate one.
        # Neither is a warning, or every run would carry one.
        for fine in ("", " 24000 "):
            with self.subTest(value=fine):
                self.assertEqual(self._ceiling(fine)[1], "")


class TheRunnerFollowsRepoVisibility(unittest.TestCase):
    """A PUBLIC repo runs the review on GitHub-hosted runners; everything else
    runs on the egi-vps self-hosted pool.

    The org runner group excludes public repositories, so a public repo's job
    on [self-hosted, ...] queues forever: correct labels, idle runners, and
    cancel/reopen cannot help. Measured on claude-cert-examprep, 2026-08-22:
    every review run after the pool move sat queued (8 h, then 12 h) while the
    six private siblings' runs completed. GitHub-hosted minutes are free for
    public repos, and opening the pool to them would let fork PRs run code on
    the VPS, so public goes hosted.

    The test is the string 'public' on purpose: a missing or unknown visibility
    falls to the pool (the private default), which stays a visibly queued,
    never-green check on a public repo rather than silently billing a private
    one. (Queued is loud in the sense that it never goes green, not in the
    sense of an immediate failure.) A bare `ubuntu-latest` is the
    regression that spent the org's hosted minutes; a bare self-hosted list is
    the one that stranded the public repo. Both fail here.
    """

    EXPR = (
        "${{ github.event.repository.visibility == 'public' && 'ubuntu-latest'"
        " || fromJSON('[\"self-hosted\",\"Linux\",\"X64\"]') }}"
    )

    @staticmethod
    def _runs_on(path):
        text = path.read_text(encoding="utf-8")
        return re.findall(r"^\s*runs-on:\s*(\S.*?)\s*$", text, flags=re.M)

    @staticmethod
    def _workflows():
        """claude-review.yml (required) and ci-standards.yml (when the repo carries
        it). Deployed, both live in .github/workflows/, next to this test's
        parent directory. In the kit, claude-review.yml is this file's sibling in
        pipeline/templates/ci-bootstrap/ and ci-standards.yml is one level up in
        pipeline/templates/: that is what the second candidate of each pair is.
        A repo's own workflows are never read: only the two vendored files are."""
        here = Path(__file__).resolve()
        candidates = {
            "claude-review.yml": (
                here.parents[1] / "workflows" / "claude-review.yml",
                here.with_name("claude-review.yml"),
            ),
            "ci-standards.yml": (
                here.parents[1] / "workflows" / "ci-standards.yml",
                here.parents[1] / "ci-standards.yml",
            ),
        }
        found = {}
        for name, paths in candidates.items():
            for path in paths:
                if path.is_file():
                    found[name] = path
                    break
        return found

    def setUp(self):
        self.found = self._workflows()
        if "claude-review.yml" not in self.found:
            self.fail("claude-review.yml was not found beside this test")

    def test_the_review_workflow_has_exactly_one_runs_on(self):
        lines = self._runs_on(self.found["claude-review.yml"])
        self.assertEqual(len(lines), 1, lines)

    def test_ci_standards_has_two_jobs_on_the_same_line(self):
        if "ci-standards.yml" not in self.found:
            self.skipTest("this repo declines ci-standards")
        lines = self._runs_on(self.found["ci-standards.yml"])
        self.assertEqual(len(lines), 2, lines)

    def test_public_goes_hosted_and_everything_else_to_the_pool(self):
        for name, path in self.found.items():
            for line in self._runs_on(path):
                with self.subTest(workflow=name):
                    self.assertEqual(line, self.EXPR)

    def test_neither_bare_runner_is_accepted(self):
        for name, path in self.found.items():
            for line in self._runs_on(path):
                for bare in ("ubuntu-latest", "[self-hosted, Linux, X64]"):
                    with self.subTest(workflow=name, bare=bare):
                        self.assertNotEqual(line, bare)


if __name__ == "__main__":
    unittest.main(verbosity=2)

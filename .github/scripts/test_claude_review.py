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

import ast
import contextlib
import http.server
import importlib.util
import io
import itertools
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
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

    def test_the_local_diff_excludes_with_the_long_pathspec_form(self):
        # Without a PR the review diffs base..head with git, excluding every
        # EXCLUDE_PATTERNS entry. In the short `:!` form git reads the next
        # character as more magic: the kit's own reviewer stopped on
        # `:!__pycache__/*` with "Unimplemented pathspec magic '_'" (kit #323).
        calls = []

        def run_git(args):
            calls.append(args)
            return ""

        with mock.patch.object(
            claude_review, "pr_diff", return_value=("", [], ("", ""))
        ), mock.patch.object(claude_review, "run_git", side_effect=run_git):
            claude_review.review_plan("base", "head")
        (call,) = calls
        excludes = [arg for arg in call if arg.startswith(":")]
        self.assertEqual(excludes, [f":(exclude){p}" for p in claude_review.EXCLUDE_PATTERNS])

    def test_source_is_reviewed_at_every_depth(self):
        for path in (
            "app.py", "src/a/b/c.py", "web/src/index.tsx",
            ".github/workflows/ci.yml", "README.md",
        ):
            with self.subTest(path=path):
                self.assertTrue(claude_review.include_file(path), path)

    def test_markup_and_styles_are_reviewed(self):
        # A repo whose whole interface is one .html file had that file dropped
        # from the diff, and the review reported on the rest as though the diff
        # were complete. Measured in the kit on 2026-08-27, PR #124.
        for path in (
            "index.html", "web/templates/base.html", "dashboard.html",
            "styles.css", "src/app/globals.css",
        ):
            with self.subTest(path=path):
                self.assertTrue(claude_review.include_file(path), path)

    def test_minified_styles_stay_out_at_every_depth(self):
        # Same root-versus-nested rule as the lockfiles above.
        for path in ("app.min.css", "static/app.min.css", "dist/site.css"):
            with self.subTest(path=path):
                self.assertFalse(claude_review.include_file(path), path)

    def test_the_deployment_surfaces_are_reviewed(self):
        # Any project can ship these, and each one decides how the thing runs
        # or what it trusts. They were invisible until 2026-08-27.
        for path in (
            "deploy/app.service", "systemd/worker.service",
            "nginx.conf", "deploy/nginx/site.conf",
            ".env.example", "config/.env.example",
            "app.manifest", "Open-Console.cmd",
        ):
            with self.subTest(path=path):
                self.assertTrue(claude_review.include_file(path), path)

    def test_an_excluded_directory_beats_every_allow_pattern(self):
        # Raised in review on kit #125: the allow list grows by extension, and
        # each new bare glob leans entirely on EXCLUDE_PATTERNS to keep build
        # and dependency trees out. Enumerating a couple of examples per PR is
        # how one gets missed, so this crosses every excluded directory with
        # every allowed extension. It fails the moment a new pattern outruns
        # the exclusions instead of a PR later.
        directories = (
            "node_modules", ".next", "dist", "build", ".venv", "venv",
            "vendor", "__pycache__",
        )
        extensions = sorted(
            pattern[2:] for pattern in claude_review.ALLOW_PATTERNS if pattern.startswith("*.")
        )
        self.assertGreater(
            len(extensions), 20, "the allow list shrank; this test is measuring nothing"
        )
        for directory in directories:
            for extension in extensions:
                for path in (
                    f"{directory}/pkg/file.{extension}",
                    f"web/{directory}/pkg/file.{extension}",
                ):
                    with self.subTest(path=path):
                        self.assertFalse(claude_review.include_file(path), path)


class TheEndpointRefusesToLeakTheKey(unittest.TestCase):
    """ANTHROPIC_BASE_URL decides where ANTHROPIC_API_KEY is sent.

    It is a repo VARIABLE, not a secret, which is a lower bar: anyone who can
    set one could point the reviewer at a host they control and the key would go
    with the request. messages_endpoint() took it verbatim.

    Found by a review on benesseremedestetica#12 during the rollout, and it was
    NEW reach rather than a pre-existing gap -- the vendored copy those repos
    carried referenced ANTHROPIC_BASE_URL zero times, so the sync is what gave
    the variable this power.

    A scheme check rather than an allowlist, deliberately: the variable exists so
    the broker can move without editing a file vendored into every repo, and an
    allowlist would need editing in all the same places. https is the property
    that matters -- no plaintext egress, no http:// to an arbitrary box.
    """

    def setUp(self):
        self._saved = os.environ.get("ANTHROPIC_BASE_URL")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("ANTHROPIC_BASE_URL", None)
        else:
            os.environ["ANTHROPIC_BASE_URL"] = self._saved

    def test_the_default_is_https_and_is_accepted(self):
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        self.assertEqual(claude_review.messages_endpoint(),
                         "https://api.anthropic.com/v1/messages")

    def test_an_https_override_is_accepted(self):
        os.environ["ANTHROPIC_BASE_URL"] = "https://llm.example.test/"
        self.assertEqual(claude_review.messages_endpoint(),
                         "https://llm.example.test/v1/messages")

    def test_plain_http_is_refused_not_warned(self):
        # Refused, because a warning in an unattended CI log is a leak nobody
        # reads. The message names the offending value so the fix is obvious.
        os.environ["ANTHROPIC_BASE_URL"] = "http://attacker.example.test"
        with self.assertRaises(SystemExit) as caught:
            claude_review.messages_endpoint()
        self.assertIn("https://", str(caught.exception))

    def test_a_schemeless_host_is_refused_too(self):
        # The shape most likely to be typed by accident, and the one that would
        # otherwise produce a request to a relative-looking host.
        os.environ["ANTHROPIC_BASE_URL"] = "llm.example.test"
        with self.assertRaises(SystemExit):
            claude_review.messages_endpoint()

    def test_loopback_over_plain_http_is_allowed(self):
        # The carve-out exists because the enforcer's own endpoint check binds
        # a loopback socket to stay network-free. The key cannot leave the
        # machine to reach 127.0.0.1, so http there is not egress.
        for base in ("http://127.0.0.1:8931", "http://localhost:8931"):
            with self.subTest(base=base):
                os.environ["ANTHROPIC_BASE_URL"] = base
                self.assertEqual(claude_review.messages_endpoint(), f"{base}/v1/messages")

    def test_the_scheme_test_is_case_insensitive(self):
        # RFC 3986 s3.1: schemes are case-insensitive. The old check was
        # `base.startswith("https://")`, so these all fell through to the
        # loopback branch and SystemExited on a legitimate endpoint. It failed
        # closed -- a spurious refusal, never a leak -- but the message it
        # produced named https:// while rejecting an https URL.
        for base in ("HTTPS://api.anthropic.com", "HttpS://api.anthropic.com",
                     "HTTPS://llm.workflowtech.ai"):
            with self.subTest(base=base):
                os.environ["ANTHROPIC_BASE_URL"] = base
                self.assertEqual(claude_review.messages_endpoint(), f"{base}/v1/messages")

    def test_a_case_variant_scheme_does_not_smuggle_a_non_loopback_host(self):
        # The case fix must not become a way past the host check: HTTP:// is
        # still not https, so it still has to be loopback to be allowed.
        for base in ("HTTP://evil.example", "HtTp://api.anthropic.com",
                     "HTTP://127.0.0.1.evil.example"):
            with self.subTest(base=base):
                os.environ["ANTHROPIC_BASE_URL"] = base
                with self.assertRaises(SystemExit):
                    claude_review.messages_endpoint()

    def test_loopback_over_a_case_variant_http_is_still_allowed(self):
        for base in ("HTTP://127.0.0.1:8931", "HTTP://localhost:8931",
                     "Http://LOCALHOST:8931"):
            with self.subTest(base=base):
                os.environ["ANTHROPIC_BASE_URL"] = base
                self.assertEqual(claude_review.messages_endpoint(), f"{base}/v1/messages")

    def test_a_loopback_LOOKALIKE_is_still_refused(self):
        # THE CARVE-OUT MUST NOT BE A PREFIX MATCH. Every one of these starts
        # with a loopback-looking string and resolves somewhere else entirely.
        # Shipping a bypass inside the fix for a bypass is the failure this
        # case exists to make impossible.
        for base in (
            "http://127.0.0.1.evil.example",
            "http://localhost.evil.example",
            "http://127.0.0.1@evil.example",
            "http://evil.example/127.0.0.1",
        ):
            with self.subTest(base=base):
                os.environ["ANTHROPIC_BASE_URL"] = base
                with self.assertRaises(SystemExit):
                    claude_review.messages_endpoint()


class RedactionLeavesTheCodeParseable(unittest.TestCase):
    """Every exact-output case above pins ONE line. This pins the PROPERTY they
    were all supposed to have, and that three of them silently asserted the
    negation of.

    The redaction table hides values; it must not rewrite the code around them
    into something that no longer parses, because the reviewer then spends the
    round reporting a SyntaxError that does not exist. That has now happened four
    times on this repo with three different inputs -- an env lookup (kit #69), a
    regex literal (gestalt-workframe-edu#605, four rounds), and an ordinary
    object literal (kit #169, three rounds) -- and each time the answer was a new
    exemption or a new anchor for the ONE shape that had been reported.

    This is the general form, so the fifth shape fails here instead of in a
    review. It uses Python's own parser rather than a hand-rolled shape check:
    a value that is a syntax error before redaction is not this table's problem,
    so each case is asserted to parse BOTH before and after.
    """

    # Real Python, each carrying a secret under a name the table matches.
    CASES = (
        'config = {"apiKey": "abc123def456", "url": "https://example.test"}',
        'config = {"apiKey": "abc\\ndef", "url": "https://example.test"}',
        "TOKEN = \"abc123def456\"",
        "api_key = 'fake_abc123'",
        'SECRET_KEY = "django-insecure-fake"',
        'password: str = "hunter2"',
        'd = {"client_secret": "abc123", "keep": 1}',
        "if (password := \"hunter2\"):\n    pass",
        'settings = dict(api_key="abc123def456")',
        # BARE, not a quoted literal. This is the shape the first cut of the fix
        # left broken -- it quoted the placeholder only where the source already
        # had quotes, so the values with no quotes to preserve kept coming out as
        # a bare `<REDACTED>`, which is `<` and `>` around an identifier and
        # parses nowhere. A bare value is hidden only when it is a token
        # (capaz#107), so these two carry one that starts with the old literal.
        "call(api_key=abc123def456ghi789)",
        "OPENROUTER_API_KEY = abc123def456ghi789",
    )

    # Every literal the cases above carry. Named here so the leak check below
    # cannot drift from the cases by being edited in only one of two places.
    SECRETS = (
        "abc123def456", "hunter2", "fake_abc123", "django-insecure-fake",
        "abc123", "abc\\ndef",
    )

    def test_the_two_lists_actually_cover_each_other(self):
        # A CHECK THAT SCANNED NOTHING IS NOT A PASS.
        #
        # `test_and_the_value_is_actually_gone` loops SECRETS and skips any entry
        # that is not in the case. So an entry naming nothing still passes, and a
        # case whose literal nobody listed is checked for parseability and for
        # nothing else. Both were true when this class was written: `abc\\ndef`,
        # the literal from the incident that started all this, was in a case and
        # in no list, so the one case that mattered most was the one the leak
        # check silently skipped.
        for secret in self.SECRETS:
            with self.subTest(secret=secret):
                self.assertTrue(any(secret in c for c in self.CASES),
                                f"{secret!r} is in no case, so it pins nothing")
        for case in self.CASES:
            with self.subTest(case=case):
                self.assertTrue(any(s in case for s in self.SECRETS),
                                f"no listed literal in {case!r}, so only parsing is checked")

    def test_a_redacted_line_still_parses(self):
        for src in self.CASES:
            with self.subTest(src=src):
                ast.parse(src)  # the case itself is valid Python, or it proves nothing
                out = claude_review.redact(src)
                self.assertNotEqual(out, src, "nothing was redacted, so this pins nothing")
                try:
                    ast.parse(out)
                except SyntaxError as exc:
                    self.fail(f"redaction broke the syntax\n  in : {src}\n  out: {out}\n  {exc}")

    def test_and_the_value_is_actually_gone(self):
        # The half that matters more. A redaction that parses and leaks is worse
        # than one that does not parse, so the property above never stands alone.
        for src in self.CASES:
            with self.subTest(src=src):
                out = claude_review.redact(src)
                for secret in self.SECRETS:
                    if secret in src:
                        self.assertNotIn(secret, out)

    # THE MOTIVATING BUG WAS NOT PYTHON, and ast.parse cannot see anything else.
    # These are the languages the incidents were actually in. A full parser for
    # each is not available here, so the check is delimiter BALANCE -- weaker
    # than parsing, and precisely the property every one of these incidents
    # broke: `{"password": "hunter2"}` came out as `{"password=<REDACTED>,` with
    # a quote opened and never closed, which is what the model then reported as
    # a broken file. Raised on review of #177.
    BALANCED_CASES = (
        # THE KNOWN WART, under the LEAK assertion and not only the parse one.
        # Review of #177 asked whether an unbalanced quote could pull trailing
        # content into what looks like a new string and widen what is visible.
        # It cannot here -- the value is consumed by the match either way -- and
        # that is asserted rather than argued.
        #
        # The double-quoted one is text inside a literal and is hidden for
        # that. The single-quoted one is not seen as text (only `"` is counted),
        # so it carries a token, as do the other bare values below (capaz#107).
        'f("token=abc123def456")',
        "x('api_key=abc123def456ghi789')",
        # JS/TS object literal -- the shape from #169.
        'const c = { apiKey: "abc123def456", url: "https://example.test" };',
        'const c = { apiKey: \'abc123def456\' };',
        # JSON.
        '{"api_key": "abc123def456", "user": "bob"}',
        # YAML, quoted and bare.
        '  password: "hunter2"',
        "  api_key: abc123def456ghi789",
        # Shell, and shell inside single quotes -- the enclosing-string shape.
        "export OPENROUTER_API_KEY=abc123def456ghi789",
        "sh -c 'TOKEN=abc123def456ghi789'",
        # PHP arrow, which the separator fix is what made survivable at all.
        "$config = ['password' => 'hunter2'];",
    )

    def test_delimiters_stay_balanced_outside_python(self):
        for src in self.BALANCED_CASES:
            with self.subTest(src=src):
                out = claude_review.redact(src)
                self.assertNotEqual(out, src, "nothing was redacted, so this pins nothing")
                for literal in ("abc123def456", "hunter2"):
                    if literal in src:
                        self.assertNotIn(literal, out)
                for opener, closer in (("{", "}"), ("[", "]"), ("(", ")")):
                    self.assertEqual(out.count(opener), out.count(closer),
                                     f"{opener}{closer} unbalanced in {out!r}")
                for quote in ('"', "'"):
                    self.assertEqual(out.count(quote) % 2, 0,
                                     f"odd number of {quote} in {out!r}")


class RedactionFailsSoftOnABrokenPattern(unittest.TestCase):
    """A redactor that RAISES posts no review at all, which is the worse failure.

    `_redact_assignment` reads `key`, `q`, `sep` and `qv` by name. A future regex
    edit that renames or drops one raises inside `re.sub`, and `redact()` runs
    before anything is sent -- so the review step dies and the check goes red with
    no review, or worse, green with none. A review that over-redacted is a bad
    review; a review that never ran is a green check on unread code.

    So the table calls `redact_assignment`, which falls back to a TOTAL redaction
    of the match. Raised on review of #177.
    """

    def setUp(self):
        # THE FLAG IS PROCESS-GLOBAL, so a test that trips it changes what the
        # next one sees. Reset before and restore after -- addCleanup runs on a
        # failure and on an exception, which a trailing assignment in the test
        # body does not. Raised on review of #177, which noted the hand-reset was
        # fine sequentially and fragile under anything else.
        self._warned = claude_review._REDACT_FALLBACK_WARNED
        claude_review._REDACT_FALLBACK_WARNED = False
        self.addCleanup(setattr, claude_review, "_REDACT_FALLBACK_WARNED", self._warned)

    def test_a_pattern_missing_a_group_redacts_instead_of_raising(self):
        # A pattern with `key` and nothing else -- exactly what a careless regex
        # edit leaves behind.
        broken = re.compile(r"(?P<key>token)=(\S+)")
        out = broken.sub(claude_review.redact_assignment, "token=abc123def456")
        self.assertEqual(out, "<REDACTED>")
        self.assertNotIn("abc123def456", out)

    def test_the_fallback_hides_everything_it_matched(self):
        # Total, not partial: the key and the separator go too. Ugly on purpose --
        # an unparseable line is a symptom someone chases, a missing review is not.
        broken = re.compile(r"(?P<key>password)\s*=\s*(?P<value>\S+)")
        out = broken.sub(claude_review.redact_assignment, "password = hunter2")
        self.assertNotIn("hunter2", out)
        self.assertNotIn("password", out)

    def test_a_pattern_missing_only_the_seam_group_still_degrades(self):
        """Raised in review on #182: confirm the NEW groups are covered too.

        The test above drops every group at once, which is the careless-edit
        case. This one is the subtler one the seam introduced -- a pattern that
        still has `key`, `q`, `sep` and `qv`, so the old replacement would have
        worked, and lacks only `seam`. The failure mode that must NOT happen is
        a partial match: the seam logic silently skipped while the rest of the
        replacement proceeds, because that emits a line that looks redacted and
        can leave the value's own quote behind.

        It must be the TOTAL redaction, same as any other broken pattern.
        """
        broken = re.compile(
            r"(?P<key>token)(?P<q>[\"'])?(?P<sep>=)(?P<qv>\"[^\"]*\")"
        )
        out = broken.sub(claude_review.redact_assignment, 'token="abc123def456"')
        self.assertEqual(out, "<REDACTED>")
        self.assertNotIn("abc123def456", out)

    def test_the_working_pattern_is_unaffected(self):
        # The wrapper must be invisible when nothing is broken.
        self.assertEqual(claude_review.redact('api_key = "abc123def456"'),
                         'api_key="<REDACTED>"')

    def test_the_fallback_says_so_on_stderr(self):
        # HIDING THE VALUE IS RIGHT. HIDING THE BREAKAGE IS NOT.
        #
        # A silent fallback degrades permanently and invisibly, and the operator
        # would meet it as "the reviews got ugly a while back" rather than as a
        # defect with a date. Same rule the hooks follow: a check that could not
        # run must never look like one that passed. Raised on review of #177,
        # where the first cut of this fallback was silent.
        broken = re.compile(r"(?P<key>token)=(\S+)")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = broken.sub(claude_review.redact_assignment, "token=abc123def456")
        self.assertEqual(out, "<REDACTED>")
        self.assertIn("fell back to a total redaction", err.getvalue())
        # And it names what to run, because a warning nobody can act on is noise.
        self.assertIn("test_claude_review", err.getvalue())

    def test_it_says_so_ONCE_and_not_per_match(self):
        # A large diff has thousands of matches; one warning per match buries the
        # log it is trying to annotate.
        broken = re.compile(r"(?P<key>token)=(\S+)")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            broken.sub(claude_review.redact_assignment, "token=a token=b token=c")
        self.assertEqual(err.getvalue().count("fell back to a total redaction"), 1)


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
            'api_key="<REDACTED>"',
        )
        # A bare value is hidden when it is a token (capaz#107). This one starts
        # with the literal the older tests used, so the check below still bites.
        self.assertNotIn("hunter2", claude_review.redact("password: hunter2Xk9mP2qR7vL4"))

    def test_a_quoted_value_is_redacted_too(self):
        # The value class excluded the opening quote, so `password: "abc123"`
        # never matched and went to the model as written.
        cases = {
            'password: "hunter2"': 'password:"<REDACTED>"',
            "api_key='fake_abc123'": "api_key='<REDACTED>'",
            'TOKEN = "abc123"': 'TOKEN="<REDACTED>"',
            # Spaces inside the quotes are part of the value; the tail used to leak.
            'password: "correct horse battery"': 'password:"<REDACTED>"',
            # An unterminated quote still hides the token after it.
            'password: "unterminated': 'password:"<REDACTED>"',
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
            '{"password": "hunter2", "user": "bob"}': '{"password":"<REDACTED>", "user": "bob"}',
            '{"api_key":"fake_abc123"}': '{"api_key":"<REDACTED>"}',
            # A Python dict quotes the same way, with the other quote.
            "{'client_secret': 'abc123'}": "{'client_secret':'<REDACTED>'}",
            # The common JSON shape is a longer key that ENDS in a secret name.
            '"db_password": "correct horse battery"': '"db_password":"<REDACTED>"',
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
        self.assertNotIn("hunter2", claude_review.redact("password:\n  hunter2Xk9mP2qR7vL4"))

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
            "'password' => 'hunter2',": "'password'=>'<REDACTED>',",
            # Python walrus: `:=` is one separator, not a colon and a stray `=`.
            'if (password := "hunter2"):': 'if (password:="<REDACTED>"):',
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
            '{"password": "hun\\"ter2"}': '{"password":"<REDACTED>"}',
            "password = 'it\\'s'": "password='<REDACTED>'",
            # A regex literal is the everyday shape of an escaped quote.
            'token = "[^\\"]+"': 'token="<REDACTED>"',
            # An escaped backslash before the closing quote does not escape it.
            'password: "C:\\\\"': 'password:"<REDACTED>"',
            "password: 'it''s'": "password:'<REDACTED>'",
            '$password = "say ""hi"""': '$password="<REDACTED>"',
            '{"password": "", "user": "bob"}': '{"password":"<REDACTED>", "user": "bob"}',
            '"password": ""': '"password":"<REDACTED>"',
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
        #
        # The bare values are tokens: a short bare word under a secret name is
        # code by shape and comes back as written (capaz#107).
        redacted = {
            "+password:\n+  hunter2Xk9mP2qR7vL4": '+password:"<REDACTED>"',
            "-password:\n-  hunter2Xk9mP2qR7vL4": '-password:"<REDACTED>"',
            ' password:\n   "hunter2"': ' password:"<REDACTED>"',
            "password:\n\thunter2Xk9mP2qR7vL4": 'password:"<REDACTED>"',
            # A value with a colon in it is a value, not a sibling key: the key
            # shape is `word:` followed by a space or the end of the line.
            "password:\n  redis://user:hunter2@host": 'password:"<REDACTED>"',
            "password:\n  db.internal:5432": 'password:"<REDACTED>"',
            # On the key's own line a value ending in `:` is a value.
            "token: abc123def456ghi789:": 'token:"<REDACTED>"',
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
            "'db_password' => 'hunter2',": "'db_password'=>'<REDACTED>',",
            '"password" => "hunter2",': '"password"=>"<REDACTED>",',
            # The match is case-insensitive and the replacement keeps the case.
            '"Password": "hunter2"': '"Password":"<REDACTED>"',
            'Api-Key = "x"': 'Api-Key="<REDACTED>"',
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
        # The bare values are tokens, since a short bare word is code by shape
        # (capaz#107).
        redacted = {
            'SECRET_KEY = "django-insecure-fake"': 'SECRET_KEY="<REDACTED>"',
            "STRIPE_SECRET_KEY=fake_abc123def456": 'STRIPE_SECRET_KEY="<REDACTED>"',
            "AWS_SECRET_ACCESS_KEY=fake_abc123def456": 'AWS_SECRET_ACCESS_KEY="<REDACTED>"',
            "SECRET_KEY_BASE=fake_abc123def456": 'SECRET_KEY_BASE="<REDACTED>"',
            "MINIO_ACCESS_KEY=fake_abc123def456": 'MINIO_ACCESS_KEY="<REDACTED>"',
            "private_key: fake_abc123def456": 'private_key:"<REDACTED>"',
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
        #
        # AND THE SEPARATOR SURVIVES AS WRITTEN, which is the part this used to
        # assert the opposite of. It pinned `{name}=<REDACTED>` for every one of
        # the six separators, so the suite REQUIRED the flattening that made
        # `apiKey: "x"` come out as `apiKey=<REDACTED>` -- unparseable in the
        # object literal it came from, and reported as a blocking SyntaxError on
        # a file whose own suite was green in the same run.
        #
        # A BARE VALUE IS A TOKEN HERE. Since capaz#107 a short bare word under
        # a secret name is code by shape and comes back as written, so the
        # unquoted cases carry a token that starts with the quoted sentinel;
        # `assertNotIn("hunter2")` then checks the whole value either way.
        names = ("password", "API_KEY", "db_password", "SECRET_KEY", "client_secret", "Token")
        key_quotes = ("", '"', "'")
        separators = (":", ": ", "=", " = ", ":=", " => ")
        value_quotes = ("", '"', "'")
        terminators = ("", ",", ";", ")", " # note", "\n")
        for name, kq, sep, vq, term in itertools.product(
            names, key_quotes, separators, value_quotes, terminators
        ):
            value = "hunter2" if vq else "hunter2Xk9mP2qR7vL4"
            line = f"{kq}{name}{kq}{sep}{vq}{value}{vq}{term}"
            with self.subTest(line=line):
                out = claude_review.redact(line)
                self.assertNotIn("hunter2", out)
                # Whitespace around the separator is still dropped; the
                # separator itself is not. The placeholder is always quoted, in
                # the source's own quote where it had one, so what is left parses
                # as the language it was in and a SQL string stays a string.
                q = vq or '"'
                placeholder = f"{q}<REDACTED>{q}"
                self.assertTrue(
                    out.startswith(f"{kq}{name}{kq}{sep.strip()}{placeholder}"), out
                )
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
    argument is not an env lookup; and the exemption is withdrawn entirely if
    the rest of the line carries a non-empty quoted literal, so an
    `or "hunter2"` fallback is still redacted while `or ""` is not.

    SINCE capaz#107 A REFUSED LOOKUP IS DECIDED LIKE ANY OTHER VALUE: a call is
    code, so it comes back as written unless it holds a literal in its chain or
    a token anywhere. A default that is a token still goes; a short one is the
    named residual.
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

    def test_a_default_argument_is_hidden_when_it_is_a_token(self):
        # os.environ.get(NAME, DEFAULT): the two-argument form is deliberately
        # not an env lookup here, so it is decided as a value. A token default
        # goes with the call; a short one reads as code (capaz#107).
        self.assertEqual(
            'token="<REDACTED>"',
            claude_review.redact('token = os.getenv("T", "hunter2Xk9mP2qR7vL4")'),
        )
        for line in (
            'password = os.environ.get("PW", "hunter2")',
            'token = os.getenv("T", "sk-live-abc123")',
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))

    def test_a_literal_fallback_on_the_line_withdraws_the_exemption(self):
        # The line is REDACTED rather than left verbatim, which is the whole
        # guarantee: the exemption never applies where a literal is in reach.
        # The fallback literal is part of the chain, and a chain that carries a
        # literal is hidden whole. The redactor is a heuristic last line, not a
        # guarantee, as SECRET_PATTERNS says at the top.
        for line in (
            'token = os.environ.get("X") or "hunter2"',
            'const token = process.env.X || "hunter2";',
        ):
            with self.subTest(line=line):
                out = claude_review.redact(line)
                self.assertIn("<REDACTED>", out)
                self.assertNotIn("hunter2", out)
        # A ternary is not a chain, so its literal was never in the value: the
        # old call redaction hid `os.getenv("X")` and left `"hunter2"` visible
        # all the same. Now the call is code and the line comes back whole;
        # nothing reaches the model that did not before.
        line = 'token = os.getenv("X") if x else "hunter2"'
        self.assertEqual(line, claude_review.redact(line))

    def test_the_exemption_matches_the_unexempted_baseline_on_those_lines(self):
        # Same input, same output as a non-env call value: proof the exemption
        # withdrew completely rather than half-applying.
        self.assertEqual(
            claude_review.redact('token = resolveKey("X") or "hunter2"'),
            claude_review.redact('token = os.environ.get("X") or "hunter2"'),
        )

    def test_a_non_identifier_argument_is_not_a_lookup(self):
        # Matching on call shape alone would exempt this and hand the model a
        # real key. An env var name is an identifier; a key generally is not,
        # so these are decided as values, and a key is a token that goes with
        # its call.
        for line in (
            'token = os.getenv("sk-ant-api03-AbCdEf0123456789ZzYyXx")',
            'api_key = os.environ.get("wJalrXUtnFEMI/K7MDENG/bPxRfi")',
        ):
            with self.subTest(line=line):
                self.assertEqual('"<REDACTED>"', claude_review.redact(line).split("=", 1)[1])
        # Two words are not a token: the named residual (capaz#107).
        line = "token = os.getenv('hunter two')"
        self.assertEqual(line, claude_review.redact(line))

    def test_a_lookalike_that_is_not_an_env_lookup_is_an_ordinary_call(self):
        # Only the exempted forms are exempt; anything that merely resembles
        # one is an ordinary call, and an ordinary call is code (capaz#107).
        for line in ('token = myenv.get("X")', 'token = get_environ("X")'):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))

    def test_the_subscript_form_is_not_exempt_and_is_code(self):
        # `os.environ["X"]` was never exempt: the subscript rule hid it whole.
        # Since capaz#107 a subscript with no token in it is code.
        line = 'token = os.environ["TOKEN"]'
        self.assertEqual(line, claude_review.redact(line))

    def test_a_typed_lookup_comes_back_annotation_and_all(self):
        # The annotation is never hidden, and the lookup behind it is code.
        line = 'password: str = os.getenv("X")'
        self.assertEqual(line, claude_review.redact(line))


class RedactionSparesCode(unittest.TestCase):
    """A key-name followed by a FUNCTION CALL is code unless it holds a token.

    `brokerApiKey: resolveKey("LITELLM_API_KEY"),` was being rewritten to
    `brokerApiKey=<REDACTED>LITELLM_API_KEY"),` and handed to the model, which
    then reported a "broken hunk" on a line that compiles, as a blocking
    finding, round after round. So a call is consumed through its closing
    paren, and nothing dangles.

    Until capaz#107 the consumed call was then hidden whole, which turned
    `token = request_context.set(ctx)` into `token="<REDACTED>"` and drew the
    same blocking report one shape over. A call is now hidden only when it
    holds a token or carries a literal in its chain; otherwise it comes back as
    written. `password=hunter2(prod)` is call-shaped and short, and is the
    named residual.
    """

    # A token by `_is_token`, starting with the sentinel the older tests used,
    # so an `assertNotIn("hunter2")` still checks the whole value.
    TOKEN = "hunter2Xk9mP2qR7vL4"

    def test_identifier_call_is_left_whole_with_nothing_dangling(self):
        line = 'brokerApiKey: resolveKey("LITELLM_API_KEY"),'
        self.assertEqual(line, claude_review.redact(line))

    def test_dotted_call_is_left_whole(self):
        line = 'apiKey: settings.resolve("X"),'
        self.assertEqual(line, claude_review.redact(line))

    def test_a_nested_call_is_consumed_two_levels_deep(self):
        line = 'token = resolveKey(env("X"));'
        self.assertEqual(line, claude_review.redact(line))
        self.assertEqual(
            'token="<REDACTED>";',
            claude_review.redact(f'token = resolveKey(env("{self.TOKEN}"));'),
        )
        self.assertEqual(
            'password="<REDACTED>"', claude_review.redact(f"password = a(b(c({self.TOKEN})))")
        )

    def test_a_call_shaped_secret_is_hidden_when_it_is_a_token(self):
        # The case that made "leave calls alone" wrong was a call-shaped
        # secret. A token in that position still goes whole.
        for line, want in {
            f"password={self.TOKEN}(prod)": 'password="<REDACTED>"',
            f"password={self.TOKEN}[prod]": 'password="<REDACTED>"',
        }.items():
            with self.subTest(line=line):
                self.assertEqual(want, claude_review.redact(line))
        # A short one reads as a call, the named residual of capaz#107.
        for line in ("password=hunter2(prod)", "PASSWORD=Summer(2024)!", "password=hunter2[prod]"):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))

    def test_a_subscript_a_suffix_run_and_a_command_substitution_go_the_same_way(self):
        # Consume, never skip: a subscript, a run of suffixes, `$(cmd)` and a
        # parenthesised value are taken whole. Holding no token, each is code
        # and comes back exactly as written.
        for line in (
            'token = os.environ["TOKEN"]',
            'token = d["a"]["b"]',
            "token = f(x)[0]",
            "token = f(x)(y), z",
            "TOKEN=$(gcloud auth print-access-token)",
            "password=(x)",
            "'password' => getenv(\"DB_PASSWORD\"),",
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))
        # Holding a token, each goes whole and leaves nothing dangling.
        cases = {
            f'token = d["{self.TOKEN}"]["b"]': 'token="<REDACTED>"',
            f"token = f({self.TOKEN})(y), z": 'token="<REDACTED>", z',
            f"TOKEN=$(echo {self.TOKEN})": 'TOKEN="<REDACTED>"',
            # `=>` before a call must not fall back to `=` plus a `>` value.
            f"'password' => getenv(\"{self.TOKEN}\"),": '\'password\'=>"<REDACTED>",',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), want)

    def test_literals_are_still_redacted(self):
        self.assertIn("<REDACTED>", claude_review.redact("api_key=abc123def456ghi789"))
        self.assertIn("<REDACTED>", claude_review.redact('password: "hunter the second"'))
        self.assertNotIn("hunter", claude_review.redact('password: "hunter the second"'))

    def test_an_env_ref_is_a_name(self):
        # $FOO / ${FOO} name a secret the shell reads at run time; neither holds
        # one. #56 pinned them as redacted; since capaz#107 a name comes back.
        for line in ("token=$MY_TOKEN", "token=${MY_TOKEN}"):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))

    def test_a_json_quoted_key_whose_value_is_a_call_takes_the_same_path(self):
        # The JSON form (`"apiKey": ...`) takes the same path; the key's own
        # closing quote is consumed with the separator, as for every JSON value.
        line = '"brokerApiKey": resolveKey("LITELLM_API_KEY"),'
        self.assertEqual(line, claude_review.redact(line))
        self.assertEqual(
            '"brokerApiKey":"<REDACTED>",',
            claude_review.redact(f'"brokerApiKey": resolveKey("{self.TOKEN}"),'),
        )

    def test_a_secret_followed_by_a_parenthetical_is_still_redacted(self):
        # A space before the paren is not a call; the word is the secret and the
        # parenthetical is prose that stays.
        self.assertEqual(
            'password="<REDACTED>" (rotated weekly)',
            claude_review.redact(f"password={self.TOKEN} (rotated weekly)"),
        )

    def test_a_token_default_inside_a_call_goes_with_the_call(self):
        # A literal argument that is a token is inside the span the call
        # consumed, so it goes with it. A short one is the named residual.
        out = claude_review.redact(f'apiKey = getEnv("API_KEY", "{self.TOKEN}")')
        self.assertEqual('apiKey="<REDACTED>"', out)
        line = 'apiKey = getEnv("API_KEY", "hunter2")'
        self.assertEqual(line, claude_review.redact(line))

    def test_a_call_broken_across_lines_falls_back_to_the_bare_form(self):
        # The first line's fragment is decided alone, and holds nothing to hide.
        line = 'apiKey: resolveKey(\n  "X"),'
        self.assertEqual(line, claude_review.redact(line))

    def test_the_nesting_boundary_is_three_paren_levels(self):
        # Three levels are consumed whole. Four fall back to the bare form: the
        # fragment up to the first closer is decided alone, the closing parens
        # dangle, and a literal argument at that depth stays visible. Pinned so
        # the depth limit is a stated number, not a guess.
        self.assertEqual(
            'apiKey:"<REDACTED>"', claude_review.redact(f"apiKey: a(b(c({self.TOKEN})))")
        )
        four_deep = claude_review.redact(f"password = a(b(c(d({self.TOKEN}))))")
        self.assertEqual('password="<REDACTED>"))))', four_deep)
        line = 'apiKey: a(b(c(d("X"))))'
        self.assertEqual(line, claude_review.redact(line))

    def test_a_quote_that_closes_an_enclosing_string_is_not_eaten(self):
        # The bare-token branch took any trailing quote, so `x('api_key=abc123')`
        # came out as `x('api_key=<REDACTED>)`: an unterminated literal in the
        # diff the model sees, which it reported as "the test files are
        # syntactically broken" on every PR whose tests carry a fixture. A
        # trailing quote is the value's own only when a leading one opened it.
        # The value is a token: only `"` is counted as an open string, so a
        # short value inside single quotes reads as code (capaz#107).
        self.assertEqual(
            'x(\'api_key="<REDACTED>"\')', claude_review.redact("x('api_key=abc123def456ghi789')")
        )
        # AND THE PLACEHOLDER DOES NOT CLOSE THE STRING IT LANDS IN. This was the
        # known wart of #177: a bare value inside an enclosing DOUBLE-quoted
        # string got a `"` that closed that string and reopened it. The first fix
        # answered "is a quote already open here" with a back-scan per match and
        # took 119 seconds against this suite's 10-second ceiling, so it was
        # reverted and the broken output pinned here instead. `_LineQuoteParity`
        # carries the answer forward across matches rather than rescanning per
        # match; the timing is still pinned by RedactionIsLinear.
        self.assertEqual('f("token=\'<REDACTED>\'")', claude_review.redact('f("token=abc123")'))
        # WHAT THE OLD OUTPUT COST, measured rather than assumed. `f("token="
        # <REDACTED>"")` re-balances into a comparison chain and PARSES in JS and
        # Python both, so "it was a syntax error" is the wrong reason to have
        # changed it. It stops being a STRING -- and in JSON, where `<` is not an
        # operator, it does not parse at all. That is the shape below: it is the
        # one that was demonstrably broken, so it is the one worth pinning.
        redacted = claude_review.redact('{"note": "token=abc123"}')
        self.assertEqual('{"note": "token=\'<REDACTED>\'"}', redacted)
        self.assertEqual({"note": "token='<REDACTED>'"}, json.loads(redacted))
        # SEVERAL KEYS INSIDE ONE STRING all stay inside it. Carrying the parity
        # forward is only equivalent to the back-scan if the gaps add up, so the
        # second key on the line is the case that would catch it drifting.
        self.assertEqual(
            'f("token=\'<REDACTED>\', password=\'<REDACTED>\'")',
            claude_review.redact('f("token=abc, password=def")'),
        )
        # A quote that OPENS and CLOSES between two keys leaves the line where it
        # was: the gap scan counts both, not the nearest one.
        self.assertEqual(
            'f("token=\'<REDACTED>\'" + x + "password=\'<REDACTED>\'")',
            claude_review.redact('f("token=abc" + x + "password=def")'),
        )
        # A NEW LINE STARTS THE COUNT OVER. The carried parity is per line, so an
        # odd line above must not make the line below single-quote.
        self.assertEqual(
            'f("token=\'<REDACTED>\'")\npassword="<REDACTED>"',
            claude_review.redact('f("token=abc123")\npassword=abc123def456ghi789'),
        )
        # AND A NEW SUBJECT STARTS IT OVER TOO. `redact()` runs once per file in
        # the snapshot path, so a cursor left odd by the previous file would
        # single-quote the first bare value of the next one.
        self.assertEqual('f("token=\'<REDACTED>\'")', claude_review.redact('f("token=abc123")'))
        self.assertEqual('token="<REDACTED>"', claude_review.redact("token=abc123def456ghi789"))
        # The key's OWN quote is not an open string. `"brokerApiKey":` has a `"`
        # in front of the key, and group `q` closes it before the value, so the
        # placeholder is double-quoted and the JSON stays JSON.
        self.assertEqual(
            '{"token":"<REDACTED>", "user": bob}',
            claude_review.redact('{"token": abc123def456ghi789, "user": bob}'),
        )
        # And so it is not read as text inside a literal either: a short bare
        # value after a JSON key is code (capaz#107).
        line = '{"token": abc123, "user": bob}'
        self.assertEqual(line, claude_review.redact(line))
        # The unterminated-quote fallback still takes its own leading quote.
        self.assertEqual('password="<REDACTED>"', claude_review.redact('password="hunter2'))

    def test_the_punctuation_after_a_bare_value_is_code(self):
        # No secret contains `,`, `;` or `)`; the code around a value does. The
        # values are tokens, so each one is hidden and the punctuation is what
        # is left to check (capaz#107).
        t = self.TOKEN
        cases = {
            f"login(password={t}, user=u)": 'login(password="<REDACTED>", user=u)',
            f"login(password=get_pw({t}), user=u)": 'login(password="<REDACTED>", user=u)',
            f"connect(host=h, password={t})": 'connect(host=h, password="<REDACTED>")',
            f"$password = {t};": '$password="<REDACTED>";',
            f"password: {t}, user: bob": 'password:"<REDACTED>", user: bob',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), want)
        # A name, a call and a match arm hold nothing, and come back whole.
        for line in (
            "login(password=pw, user=u)",
            "login(password=get_pw(), user=u)",
            "token => x,",
            "token => parse(x),",
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))


class ARequiredVariableExpansionReachesTheModelWhole(unittest.TestCase):
    """`${NAME:?message}` names a secret without holding one (capaz#21).

    The bare value stopped at the first space inside the braces, so the compose
    line below reached the model as `POSTGRES_PASSWORD:"<REDACTED>" POSTGRES_PASSWORD
    in infra/compose/.env}`, and the reviewer reported invalid YAML and a deploy
    blocker on a file `docker compose config` accepts. Exact output, both
    directions: what is left as written, and what is still redacted whole.
    """

    def test_a_required_variable_expansion_is_left_exactly_as_written(self):
        for line in (
            # The capaz#21 line, as the diff carried it.
            "+      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD"
            " in infra/compose/.env}",
            'POSTGRES_PASSWORD: "${POSTGRES_PASSWORD:?required}"',
            "POSTGRES_PASSWORD: '${POSTGRES_PASSWORD:?required}'",
            "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD?required}",
            "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?}",
            "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set it} # compose refuses to start",
            "      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}",
            '      - "POSTGRES_PASSWORD=${POSTGRES_PASSWORD:?set it}"',
            "environment: { POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set it} }",
            'export DB_PASSWORD="${DB_PASSWORD:?missing}"',
            "POSTGRES_PASSWORD:\n  ${POSTGRES_PASSWORD:?set it}",
            # Inside a longer value the inner key is refused at `:?` instead.
            # Split over two lines, so no line carries a credential-URL shape
            # for the secret scan to read.
            (
                "DATABASE_URL: postgresql://capaz:"
                "${POSTGRES_PASSWORD:?set it}@db:5432/capaz"
            ),
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))

    def test_an_expansion_that_can_hold_a_literal_is_hidden_when_it_holds_a_token(self):
        # Consumed whole, through the closing brace, so nothing after a space
        # inside it dangles; then decided like any other bare value (capaz#107).
        # `T` is a token that starts with the sentinel the older cases used.
        t = "hunter2Xk9mP2qR7vL4"
        whole = 'POSTGRES_PASSWORD:"<REDACTED>"'
        cases = {
            # A default or an alternate holds a literal, and the words after its
            # first space used to reach the model: `"<REDACTED>" horse}`.
            f"POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD:-{t}}}": whole,
            f"POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD-correct {t}}}": whole,
            f"POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD:={t} horse}}": whole,
            f"POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD:+{t}}}": whole,
            # A required-variable expansion that does not END the value.
            f"POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD:?set it}}{t}": whole,
            'POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set it} + "hunter2"': whole,
            # Text glued behind a closer is still the value (`_VALUE_END`).
            f"POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD:?set it}}]{t}": whole,
            f"POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD:?set it}}}}{t}": whole,
            # A default inside a connection URL is still the inner key's value.
            # Split like the URL above.
            (
                "DATABASE_URL: postgresql://capaz:"
                f"${{POSTGRES_PASSWORD:-{t}}}@db:5432/capaz"
            ): 'DATABASE_URL: postgresql://capaz:${POSTGRES_PASSWORD:"<REDACTED>"',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertEqual(want, claude_review.redact(line))
        # A name holds nothing, and a short default reads as code: the named
        # residual of capaz#107. #56 had pinned the two names as redacted.
        for line in (
            "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}",
            "POSTGRES_PASSWORD: $POSTGRES_PASSWORD",
            "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-correct horse}",
            "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set it}]hunter2",
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))

    def test_a_key_glued_behind_the_closer_is_still_redacted(self):
        """Text glued behind the closer makes the expansion a value.

        Declining it once sent these lines to a bare branch that read through
        the glued second key, and the second value reached the model (review
        of kit #327). The bare value now stops in front of the key, so the
        key gets its own match. Since capaz#107 the expansion in front holds
        no token and comes back as written; the glued value is a token or a
        literal and is hidden.
        """
        t = "hunter2Xk9mP2qR7vL4"
        for line, want in (
            (
                f"password: ${{PW:?m}}]password: {t}",
                'password: ${PW:?m}]password:"<REDACTED>"',
            ),
            (
                f"DB={{password: ${{DB_PASSWORD:?set it}}}}password: {t}",
                'DB={password: ${DB_PASSWORD:?set it}}password:"<REDACTED>"',
            ),
            (
                f"password: ${{PW:?m}}]#password: {t}",
                'password: ${PW:?m}]#password:"<REDACTED>"',
            ),
            (
                'password: ${PW:?m}]password="hunter2"',
                'password: ${PW:?m}]password="<REDACTED>"',
            ),
        ):
            with self.subTest(line=line):
                self.assertEqual(want, claude_review.redact(line))

    def test_the_residuals_are_pinned_not_assumed(self):
        """What the exemption costs, asserted so HARNESS.md cannot drift.

        A word typed as the MESSAGE reaches the model, and so does a value glued
        to its colon that opens with `?` when a `}` follows on the same line,
        since that `:?` reads as an expansion operator. A key in a vendor format
        is still caught in the message by the prefix half.
        """
        message = "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?hunter2}"
        self.assertEqual(message, claude_review.redact(message))
        for braced in ("{password:?hunter2}", "cfg = {password:?hunter2, user: u}"):
            with self.subTest(braced=braced):
                self.assertEqual(braced, claude_review.redact(braced))
        # With no closing brace, `:?` is a separator like any other: on review
        # of #306 these two reached the model, where main had redacted them.
        # The values are tokens, which a bare value has to be (capaz#107).
        for glued, want in (
            ("password:?hunter2Xk9mP2qR7vL4", 'password:"<REDACTED>"'),
            ("token:?hunter2Xk9mP2qR7vL4", 'token:"<REDACTED>"'),
        ):
            with self.subTest(glued=glued):
                self.assertEqual(want, claude_review.redact(glued))
        # Split, like WRITTEN_OUT_KEYS: this source line carries no key shape.
        keyed = "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?" + "sk-" + "abcdefghij0123456789}"
        self.assertEqual(
            "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?sk-<REDACTED>}",
            claude_review.redact(keyed),
        )


class ANumberUnderATokenNameReachesTheModel(unittest.TestCase):
    """`CHARS_PER_TOKEN = 4` is a count, not a credential (kit #314).

    The name rule matches `token` on its tail, so the constant reached the model
    as `CHARS_PER_TOKEN="<REDACTED>"`, and the reviewer asked whether it was a
    string used in arithmetic. Exact output, both directions: what is left as
    written, and what is still redacted whole.

    THESE CASES ARE REDACTED IN THE REVIEWED DIFF, AND THAT IS THE REDACTOR
    WORKING. Each input is a `token` assignment, so the review of #317 read
    lines such as `'token = "123456"': whole` as unbalanced quotes and as
    duplicate keys. Both files compile and every case is distinct. Verify with
    `git show <sha>:<path>`, not the diff.
    """

    def test_the_constants_from_kit_314_reach_the_model_as_written(self):
        # Measured before the change: only the first was redacted. `TOKENS` is
        # a different name, and its `S` ends the match before the separator.
        for line in (
            "CHARS_PER_TOKEN = 4",
            "OUTPUT_TOKENS_PER_CALL = 12_000",
            "DEFAULT_CLAUDE_REVIEW_MAX_TOKENS = 32000",
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))

    def test_a_bare_number_that_ends_the_value_is_left_as_written(self):
        for line in (
            "+CHARS_PER_TOKEN = 4",
            "CHARS_PER_TOKEN = 4  # a rough average",
            "COST_PER_TOKEN = 0.000_003",
            "MAX_TOKEN = 4.",
            # The annotation is read through. Without that, the retry with no
            # annotation redacted `int` and left `= 4` dangling after it.
            "CHARS_PER_TOKEN: int = 4",
            "CHARS_PER_TOKEN: int | None = 4",
            "const CHARS_PER_TOKEN = 4;",
            '{"token": 4}',
            "f(token=4, x=1)",
            "token:\n+  4",
            # A closer ends the number when the value ends with it.
            "{token: 4}",
            "{ a: { token: 4 } }",
            "f({token: 4}, x)",
            "x = [token=4];",
            "{token: 4}  # a rough average",
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))

    def test_anything_more_than_a_number_is_decided_as_a_value(self):
        whole = 'token="<REDACTED>"'
        # Pairs, not a dict keyed on the input: a dict keeps only the last of
        # two equal keys, so a repeated case would drop out without a sound.
        cases = (
            # A quote makes it a literal.
            ('token = "123456"', whole),
            ("token = 4hunter2Xk9mP2qR7vL4", whole),
            # A number that does not END the value takes the chain with it.
            ('token = 4 + "hunter2"', whole),
            ('token=4+"hunter2"', whole),
            ('token = 4 or "hunter2"', whole),
            ('token=4.."hunter2"', whole),
            # And combined with the conditionals that were already there: an
            # annotation with a union, a quoted JSON key, and the YAML value on
            # the line below its key behind a diff marker. The annotation goes
            # back as written.
            ('token: int = 4 or "hunter2"', 'token: int = "<REDACTED>"'),
            ('token: int | None = 4 + "hunter2"', 'token: int | None = "<REDACTED>"'),
            ('{"token": 4 + "hunter2"}', '{"token":"<REDACTED>"}'),
            ('token:\n+  4 + "hunter2"', 'token:"<REDACTED>"'),
            # The Telegram bot token shape opens with digits and runs on past
            # the colon. Split, so this source line carries no key shape.
            (
                "BOT_TOKEN = 123456789:" + "AbCdEf0123456789" * 2 + "ZzY",
                'BOT_TOKEN="<REDACTED>"',
            ),
        )
        for line, want in cases:
            with self.subTest(line=line):
                self.assertEqual(want, claude_review.redact(line))
        # A short bare value holds no token under any name, so these come back
        # as written (capaz#107). Before it, every one of them was hidden.
        for line in (
            "password = 1234",
            "secret = 42",
            # Escaped, so this source stays ASCII: fullwidth and Arabic-Indic
            # digits, which `\d` would call digits.
            "token = １２３４５６",
            "token = ١٢٣٤",
            "token = 0x1F",
            "token = 3e-6",
            "token = -1",
            "token = 4hunter2",
            "token: int = 4 + x",
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))

    def test_a_closer_ends_the_number_only_where_the_value_ends(self):
        """A `]` or `}` with more of the value glued behind it is not an end.

        The bare branch reads through both, so before #317 `4]` and what
        followed it redacted as one value. The exemption stopped at the
        closer and handed the rest to the model, which the review of
        capaz#65 found and #317's own generator could not express.
        """
        whole = 'token="<REDACTED>"'
        cases = (
            ("token = 4]wJalrXUtnFEMIK7MDENG", whole),
            ("token = 4}wJalrXUtnFEMIK7MDENG", whole),
            ('token = 4] + "hunter2"', whole),
            ('token = 4}+"hunter2"', whole),
            # Split, so this source line carries no credential-URL shape. The
            # glued text is a token, which a bare value has to be (capaz#107).
            (
                "url = postgres://u:${TOKEN:=0}" + "hunter2Xk9mP2qR7vL4@host/db",
                'url = postgres://u:${TOKEN:="<REDACTED>"',
            ),
            # One string holding a JSON object: the closer and then the
            # string's quote end the value, as the bare value ends there.
            ('\'{"token": 4}\'', '\'{"token": 4}\''),
        )
        for line, want in cases:
            with self.subTest(line=line):
                self.assertEqual(want, claude_review.redact(line))

    def test_no_glued_closer_hands_the_rest_of_the_value_over(self):
        # ZERO IS THE BASELINE: every case here redacted before #317, and
        # every one leaked after it. A closer followed by `)` is not
        # generated: the bare branch stops at `)` as well, the adjacency gap
        # HARNESS.md records, so those leaked before #317 too.
        secret = "AbCdEf0123456789ZzYyXx"
        assignments = (
            "token={}", "token = {}", "token: {}", "token:={}",
            "CHARS_PER_TOKEN = {}", '"token": {}', "CHARS_PER_TOKEN: int = {}",
        )
        closers = ("]", "}", "]]", "}]")
        tails = (
            "{s}", ' + "{s}"', '+"{s}"', ' or "{s}"', '.."{s}"', ":{s}",
            "@{s}", "/{s}",
        )
        contexts = (("", ""), ("f(", ")"), ("{ ", " }"), ("+  ", ""))
        leaked, count = [], 0
        for a, num, closer, tail, (before, after) in itertools.product(
            assignments, ("4", "12_000.5"), closers, tails, contexts,
        ):
            line = before + a.format(num + closer + tail.format(s=secret)) + after
            count += 1
            if secret in claude_review.redact(line):
                leaked.append(line)
        # A check that scanned nothing is not a pass.
        self.assertEqual(1792, count)
        self.assertEqual([], leaked[:12])

    def test_a_vendor_key_made_of_digits_is_still_redacted(self):
        # No vendor entry is all digits. These three come nearest: a fixed
        # prefix, then nothing but digits. Under a `token` name the value opens
        # with a letter, so the exemption never reads it, and on its own the
        # prefix half takes it. Built at runtime, like the keyed message above.
        for prefix, body in (
            ("SK", "0123456789" * 3 + "01"),
            ("dop_v1_", "0123456789" * 6 + "0123"),
            ("sbp_", "0123456789" * 4),
        ):
            key = prefix + body
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    'API_TOKEN="<REDACTED>"', claude_review.redact(f"API_TOKEN = {key}")
                )
                self.assertEqual(
                    f"key {prefix}<REDACTED>", claude_review.redact(f"key {key}")
                )

    def test_the_residual_is_pinned_not_assumed(self):
        """What the exemption costs, asserted so HARNESS.md cannot drift.

        A credential made only of digits under a `token` name, such as a
        six-digit one-time code, reaches the model as written.
        """
        line = "OTP_TOKEN = 839201"
        self.assertEqual(line, claude_review.redact(line))


class AKeyGluedIntoABareValueGetsItsOwnMatch(unittest.TestCase):
    """A bare value ends in front of a key the table knows.

    It read through a glued key and its separator like any other text, so one
    match swallowed the next key and the scan resumed past that key's value.
    `token = abc]password: hunter2` reached the model as
    `token="<REDACTED>" hunter2` on main; on review of #327 the same path sent
    a declined exemption's second key to the model. Exact output, both
    directions: the glued key is redacted on its own, and a value that merely
    contains a key name stays one value.

    Since capaz#107 a bare value is hidden only when it holds a token, so the
    glued values below are tokens. A short first value is code and comes back
    as written in front of the glued key.
    """

    T = "hunter2Xk9mP2qR7vL4"

    def test_a_glued_key_is_redacted_on_its_own(self):
        t = self.T
        second = 'password:"<REDACTED>"'
        cases = (
            (f"token = {t}]password: {t}", 'token="<REDACTED>"' + second),
            (f"token = abc]password: {t}", "token = abc]" + second),
            (f"token = abcpassword: {t}", "token = abc" + second),
            (f"token=x#password: {t}", "token=x#" + second),
            # The key matches on its tail, so the head goes with the first value.
            (f"token=x]db_password: {t}", "token=x]db_" + second),
            (f"token=x]STRIPE_SECRET_KEY={t}", 'token=x]STRIPE_SECRET_KEY="<REDACTED>"'),
            ('token = abc]password="hunter2"', 'token = abc]password="<REDACTED>"'),
            (f"token = abc]password = {t}", 'token = abc]password="<REDACTED>"'),
            (f"token=x.api_key => {t}", 'token=x.api_key=>"<REDACTED>"'),
            # A declined number took this path between #317 and this change.
            (f"token = 4]password: {t}", "token = 4]" + second),
            (f"password: token: {t}", 'password: token:"<REDACTED>"'),
            (
                f"url = https://x/?token=abc&password={t}",
                'url = https://x/?token=abc&password="<REDACTED>"',
            ),
        )
        for line, want in cases:
            with self.subTest(line=line):
                self.assertEqual(want, claude_review.redact(line))

    def test_a_key_name_with_no_separator_stays_inside_the_value(self):
        # A name that contains a key name is a name, and comes back whole.
        for line in (
            "token = my_password_hash",
            "token = secretkeybase",
            "password = token_abc",
            "password = get_token()",
            "api_key = tokenizer.secret",
            "token: secret_value",
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))
        # A token that contains one is one token, hidden whole.
        for line, want in (
            ("token = Xk9mP2qR7password4vL", 'token="<REDACTED>"'),
            ("token: secret9Xk2mP7qR4vL", 'token:"<REDACTED>"'),
        ):
            with self.subTest(line=line):
                self.assertEqual(want, claude_review.redact(line))

    def test_no_glued_key_hands_its_value_over(self):
        # ZERO IS THE BASELINE. At kit #327's head, 34a153e, most of these
        # leaked the second value; on main the plain values did too.
        secret = "AbCdEf0123456789ZzYyXx"
        firsts = ("token = {}", "password: {}")
        values = ("abc", "4", "f(x)", "a[0]", "${X:-d}", "x.y")
        glues = ("", "]", "}", "]]", "#", ".", "/", ":")
        keys = ("password", "db_password", "STRIPE_SECRET_KEY", "api_key", "x.token", "Password")
        seconds = (": {s}", "={s}", " = {s}", " => {s}", '="{s}"', ":={s}")
        contexts = (("", ""), ("f(", ")"), ("{ ", " }"), ("+  ", ""))
        leaked, count = [], 0
        for first, value, glue, key, second, (before, after) in itertools.product(
            firsts, values, glues, keys, seconds, contexts,
        ):
            line = before + first.format(value + glue + key + second.format(s=secret)) + after
            count += 1
            if secret in claude_review.redact(line):
                leaked.append(line)
        # A check that scanned nothing is not a pass.
        self.assertEqual(13824, count)
        self.assertEqual([], leaked[:12])

    def test_the_residual_is_pinned_not_assumed(self):
        # The chain's bare operand after a spaced `+` still reads through a
        # key, so the second value reaches the model, as it did on main. The
        # first value holds nothing and comes back with it.
        line = f"token = a + password: {self.T}"
        self.assertEqual(line, claude_review.redact(line))


class AnExemptNameEndsWhereItsValueDoes(unittest.TestCase):
    """The name exemptions end at `_VALUE_END`, the end every exemption shares.

    The bare value reads through `]` and `}`, and the self-reshape, pattern and
    env-lookup exemptions stopped at either, so text glued behind the closer
    reached the model. Each shape below did so on main, at kit #327's head and
    at dba33db, before #317. Exact output, both directions.
    """

    SECRET = "wJalrXUtnFEMIK7MDENG"

    def test_text_glued_behind_a_closer_is_still_the_value(self):
        whole = 'token="<REDACTED>"'
        for value in (
            "token = token.strip()]",
            "token = token.strip()}",
            "token = token[0]]",
            "token=(?:a|b)]",
            "token={value}]",
            "token=/a.*b/]",
            "token = os.environ.get('TOKEN')]",
            "token = process.env.TOKEN]",
            "token = process.env.TOKEN}",
        ):
            line = value + self.SECRET
            with self.subTest(line=line):
                self.assertEqual(whole, claude_review.redact(line))
        # A word glued behind the env call is the value's too.
        self.assertEqual(whole, claude_review.redact('token = os.getenv("X")' + self.SECRET))

    def test_a_key_glued_behind_the_closer_gets_its_own_match(self):
        # The first value holds no token and comes back as written; the glued
        # key's value is a token or a literal and is hidden (capaz#107).
        s = self.SECRET
        for line, want in (
            (f"token = token.strip()]password: {s}", 'token = token.strip()]password:"<REDACTED>"'),
            (
                'token = process.env.TOKEN]password="hunter2"',
                'token = process.env.TOKEN]password="<REDACTED>"',
            ),
            (f"token={{value}}]password: {s}", 'token={value}]password:"<REDACTED>"'),
        ):
            with self.subTest(line=line):
                self.assertEqual(want, claude_review.redact(line))

    def test_an_exempt_name_that_ends_its_value_is_left_as_written(self):
        for line in (
            "token = token.strip()",
            "f(token=token.strip())",
            "{token: token.strip()}",
            "{token: process.env.TOKEN}",
            "f({token: token.strip()}).then(x)",
            "new Client({token: process.env.TOKEN}).connect()",
            'log(f"[token={token}]")',
            "const token = process.env.GITHUB_TOKEN!;",
            "const token = process.env.TOKEN||'';",
            'token = os.getenv("TOKEN").strip()',
            # Main redacted these five. A blank already ends the value, so a
            # closer or a comment after it does too; the chain's old end read
            # `$` without re.M and matched only at the end of the whole text.
            "{ a: { token: token.strip() } }",
            "token = token.strip()  # trim",
            "token = token.strip()\nnext = 1",
            "m = {token: (?:a|b)}",
            "f({token: /(?:a|b)/}).x",
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))

    def test_no_glued_closer_hands_text_to_the_model(self):
        # ZERO IS THE BASELINE. Every exemption kind, every glued closer run,
        # a secret or a second key glued behind it, four enclosing contexts.
        # Not generated, and pinned below instead: a regex literal holding a
        # group, whose bare value stops at the group's `)`, and a quote or `)`
        # glued behind the closer, where the bare value stops as well.
        secret = "AbCdEf0123456789ZzYyXx"
        values = (
            "token = token.strip()", "token: token.strip()", "token = token[0]",
            "token = token.split(',')[0]", "token=(?:a|b)", "token=(.*)",
            "token={value}", "token=/a.*b/", "token = os.environ.get('TOKEN')",
            'token = os.getenv("X")', "token = process.env.TOKEN",
            "token: process.env.TOKEN", "token = 4", "CHARS_PER_TOKEN: int = 12_000.5",
            "password: ${PW:?set it}",
        )
        closers = ("]", "}", "]]", "}]", "]}", "}}")
        tails = (
            "{s}", ":{s}", "@{s}", "/{s}", "-{s}", "#{s}", ".{s}", "+{s}",
            ' + "{s}"', '+"{s}"', ' or "{s}"', "password: {s}", "password:{s}",
            "token={s}", "#password: {s}", 'password="{s}"', "db_password: {s}",
            "x.password = {s}", "api_key => {s}", "STRIPE_SECRET_KEY={s}",
            "{s}password: {s}",
        )
        contexts = (("", ""), ("f(", ")"), ("{ ", " }"), ("+  ", ""))
        leaked, count = [], 0
        for value, closer, tail, (before, after) in itertools.product(
            values, closers, tails, contexts,
        ):
            line = before + value + closer + tail.format(s=secret) + after
            count += 1
            if secret in claude_review.redact(line):
                leaked.append(line)
        # A check that scanned nothing is not a pass.
        self.assertEqual(7560, count)
        self.assertEqual([], leaked[:12])

    def test_the_residuals_are_pinned_not_assumed(self):
        """What survives, asserted so HARNESS.md cannot drift.

        Each reaches the model exactly as on main. `)` and a quote glued
        behind the value lie past the point where the bare value stops; the
        env lookup has no end but a closer or a word character; a type word
        before a closer is an annotation; a pattern ends at whitespace even
        when a chain follows; and the self-reshape chain reads an attribute
        name, which an identifier-shaped secret can be.
        """
        for line in (
            "token = token.strip())" + self.SECRET,
            'token = token.strip()]"' + self.SECRET + '"',
            "token = process.env.TOKEN-" + self.SECRET,
            "password = str]" + self.SECRET,
            "token=(?:a|b) + '" + self.SECRET + "'",
            "token = token." + self.SECRET,
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))
        # A regex literal holding a group: the bare value stops at the group's
        # `)`, so the glued tail survives. The fragment before it holds no
        # token, so since capaz#107 the line comes back whole.
        line = "token=/(?:a|b)/i]" + self.SECRET
        self.assertEqual(line, claude_review.redact(line))
        # A call on the literal itself reads as more of the value. It holds no
        # token, so the over-redaction main had here is gone (capaz#107).
        line = 'parts = {token: token.split(",")[0]}.items()'
        self.assertEqual(line, claude_review.redact(line))


class ReenteringRedactIsLoud(unittest.TestCase):
    """`redact()` cannot serve two passes at once, and says so.

    The quote-parity cursor is process-wide, so a second concurrent caller does
    not crash and does not under-redact -- it gets the wrong quote character.
    That is a mangled diff, which is the phantom-syntax-error class the redactor
    exists to stop, arriving silently. Two independent reviews of #181 asked for
    a loud failure for exactly that reason.

    A tripwire, not a lock: check and set are not atomic, so this pins the case
    worth catching (a re-entrant call arriving mid-pass) and claims nothing
    about genuine thread safety.
    """

    def test_a_reentrant_call_raises_instead_of_corrupting(self):
        seen = []

        def reenter(m):
            # Called from inside the first pass, i.e. exactly the position a
            # parallelised snapshot loop would put a second caller in.
            try:
                claude_review.redact("token=abc123")
            except RuntimeError as exc:
                seen.append(str(exc))
            return "<REDACTED>"

        pattern = re.compile(r"token=\w+")
        original = claude_review.SECRET_PATTERNS[:]
        claude_review.SECRET_PATTERNS[:] = [(pattern, reenter)]
        try:
            claude_review.redact("token=abc123")
        finally:
            claude_review.SECRET_PATTERNS[:] = original

        self.assertEqual(1, len(seen), "the re-entrant call should have raised")
        self.assertIn("re-entered", seen[0])

    def test_the_flag_is_cleared_even_when_a_pass_raises(self):
        # A pass that dies must not wedge every later call into the tripwire.
        def boom(m):
            raise ValueError("pass exploded")

        pattern = re.compile(r"token=\w+")
        original = claude_review.SECRET_PATTERNS[:]
        claude_review.SECRET_PATTERNS[:] = [(pattern, boom)]
        try:
            with self.assertRaises(ValueError):
                claude_review.redact("token=abc123")
        finally:
            claude_review.SECRET_PATTERNS[:] = original

        # The next ordinary call still works rather than raising RuntimeError.
        self.assertEqual('token="<REDACTED>"', claude_review.redact("token=abc123def456ghi789"))


class NoCombinationOfShapesLeaksALiteral(unittest.TestCase):
    """THE ANSWER TO "EVERY FIX FOUND ITS NEIGHBOUR ONE ROUND LATER".

    Four leaks were fixed in this file in one week, and each was found only
    after the previous fix shipped: spaced concatenation missed the unspaced
    form, the prefixed VALUE missed the prefixed OPERAND, the tempered bare
    class missed the chain's call form, and the operator set missed the bare
    class that has to stop in front of it. Every one was the NEIGHBOUR of
    something already tested, missed because the pins were written from the
    example that motivated the change and inherited its incidental properties.
    Every pin in this file had spaces around the operator, because the shape
    that prompted them did.

    A generator does not have incidental properties. This walks the product of
    the axes the pattern actually branches on and asserts, for every one, that
    the sentinel does not survive.

    THE ORACLE IS ONE-SIDED, which is what makes this cheap and worth having.
    There is no need to know the correct output for 392,400 lines -- only that
    the secret is gone from each. Over-redaction is not tested here (the exact
    -output classes above do that); this asks the single question the file
    exists to answer.

    ZERO IS THE BASELINE, not a recorded count. Every combination these axes
    produce is covered, so a leak here is a regression rather than a known gap.
    Shapes that are still known to leak -- adjacency with no operator,
    subscript assignment, positional secrets, escaped quotes inside an
    enclosing string -- are deliberately NOT generated: they are recorded in
    HARNESS.md and #188, and generating them would mean pinning a nonzero
    baseline that hides a real regression inside an accepted one.
    """

    SECRET = "AbCdEf0123456789ZzYyXx"

    NAMES = ["api_key", "token", "password", "client_secret", '"api_key"']
    SEPARATORS = [":", "=", ":=", "=>", "+=", ".="]
    SPACING = ["", " "]
    PREFIXES = ["", "f", "b", "$"]
    QUOTES = ['"', "'", "`"]
    OPERATORS = [None, "+", ".", "..", "&", "%", "||", "??", " or ", " and "]
    # TWO AXES, NOT ONE. This was a single `OPERAND_SPACING` applied to both
    # sides of the operator, so every generated case was symmetric and the
    # asymmetric leak (`pre+ "SECRET"` -- glued left, spaced right) was outside
    # the product entirely. Review on #192 found by hand what the generator
    # could not express. An axis that cannot represent the asymmetry cannot
    # find it, which is the hand-written pin's blind spot one level up.
    SPACE_BEFORE_OP = ["", " "]
    SPACE_AFTER_OP = ["", " "]
    # A NUMBER IS A LEAD TOO. A bare number under `token` is exempt
    # (`_NUMBER`), and the exemption has to stop where the value does: every
    # operator and spacing here must still take the literal behind it.
    LEADS = ["", "pre", "f()", "12_000.5"]
    CONTEXTS = [("", ""), ("f(", ")"), ('f("', '")'), ("{ ", " }"), ("+  ", "")]

    def cases(self):
        # itertools.product, not nine nested `for`s. The nested form worked and
        # then pushed its innermost line past the vendored-file column limit the
        # moment an axis was split in two -- a layout that cannot survive its own
        # axes growing. The product also makes the axis list the only place a
        # dimension is declared, so adding one cannot silently skip a level.
        axes = itertools.product(
            self.NAMES, self.SEPARATORS, self.SPACING, self.PREFIXES,
            self.QUOTES, self.OPERATORS, self.SPACE_BEFORE_OP,
            self.SPACE_AFTER_OP, self.LEADS, self.CONTEXTS,
        )
        for name, sep, sp, prefix, quote, op, pre_sp, post_sp, lead, ctx in axes:
            before, after = ctx
            literal = f"{prefix}{quote}{self.SECRET}{quote}"
            if op is None:
                # No operator means no operand and no spacing around one; the
                # other combinations would be the same case many times over.
                if lead or pre_sp or post_sp:
                    continue
                value = literal
            else:
                left = lead or f"{prefix}{quote}pre{quote}"
                value = f"{left}{pre_sp}{op}{post_sp}{literal}"
            yield f"{before}{name}{sp}{sep}{sp}{value}{after}"

    def test_the_generator_actually_generates(self):
        # A CHECK THAT SCANNED NOTHING IS NOT A PASS. The loop above is nine
        # deep and one wrong `continue` empties it silently.
        count = sum(1 for _ in self.cases())
        self.assertGreater(count, 100_000, "the axes stopped producing cases")

    def test_the_sentinel_survives_nothing(self):
        leaked = []
        for line in self.cases():
            if self.SECRET in claude_review.redact(line):
                leaked.append(line)
                if len(leaked) >= 12:
                    break
        if leaked:
            self.fail(
                f"{len(leaked)}+ generated shapes leak the literal; first few:\n  "
                + "\n  ".join(f"{shape}\n    -> {claude_review.redact(shape)}" for shape in leaked)
            )

    def test_it_finishes_in_a_time_a_suite_can_afford(self):
        # It runs on every PR alongside everything else. Measured at ~1.2s for
        # the full product; the ceiling is loose so a slow machine is not a
        # red build, and a pattern that went quadratic still shows up here.
        started = time.perf_counter()
        for line in self.cases():
            claude_review.redact(line)
        self.assertLess(time.perf_counter() - started, 30.0)
class ATransportFailurePostsRatherThanCrashing(unittest.TestCase):
    """A read timeout killed the process before it could say so.

    `call_claude` caught only `urllib.error.HTTPError`, so a read timeout, a
    reset connection, a DNS failure or a TLS error propagated out of `main()`
    and took `write_status()` with it. The workflow caught THAT correctly -- "No
    Claude review status was written, so nothing proves a review ran", red
    rather than green -- but the promise the error branch makes, that the posted
    comment carries the reason, was not kept: there was no comment, and the
    reason lived in a stack trace in the job log.

    Measured on kit #192 on 2026-08-31: `TimeoutError: The read operation timed
    out` after 5m17s, no comment, no status file, and a red check whose message
    said only that nothing proved a review ran.

    THE SAME LINE HAD ALREADY BEEN FIXED TWICE BY RAISING THE TIMEOUT, 60s then
    300s. Both treated the symptom -- the review got slower, so the ceiling
    moved -- and left the class, which is that any network exception took the
    status file with it. A third raise would have been the third symptom fix in
    a row on one line.
    """

    def _call_with(self, exc):
        # No response body here on purpose: `side_effect=exc` makes the call
        # RAISE, so nothing is ever read back. A leftover payload dict from the
        # returning version of this test sat here unused until ruff named it.
        with mock.patch.object(claude_review._NO_REDIRECT_OPENER, "open", side_effect=exc):
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}, clear=False):
                return claude_review.call_claude("diff text")

    def test_a_read_timeout_returns_a_failure_banner(self):
        out = self._call_with(TimeoutError("The read operation timed out"))
        self.assertIn(claude_review.FAILED_BANNER, out)
        self.assertIn("TimeoutError", out)
        self.assertIn("transport failure", out)

    def test_every_transport_failure_takes_the_same_path(self):
        # OSError is the net rather than a list of names, so a shape nobody has
        # met yet lands here too.
        for exc in (
            TimeoutError("timed out"),
            ConnectionResetError("reset by peer"),
            claude_review.urllib.error.URLError("name resolution failed"),
            OSError("something the stdlib has not named yet"),
        ):
            with self.subTest(exc=type(exc).__name__):
                out = self._call_with(exc)
                self.assertIn(claude_review.FAILED_BANNER, out)

    def test_the_banner_is_the_one_the_status_reader_recognises(self):
        # The whole point: the workflow decides the check colour from the
        # status, and the status is read off this banner. A failure that does
        # not carry it reads as a review that ran.
        #
        # THROUGH `status_for`, WHICH IS WHAT main() CALLS -- not
        # `review_status`. The first draft of this test used the latter and
        # failed with 'ok', because call_claude returns the posted COMMENT
        # (heading and all) while review_status classifies the review TEXT.
        # That is the seam status_for's own docstring was written about, and
        # asserting the wrong half of it would have passed a green check on an
        # unreviewed diff straight through.
        out = self._call_with(TimeoutError("timed out"))
        self.assertEqual(claude_review.STATUS_FAILED, claude_review.status_for(out))

    def test_an_http_error_still_takes_the_more_specific_handler(self):
        # HTTPError subclasses URLError and therefore OSError, so the order of
        # the two handlers is load-bearing: catching OSError first would swallow
        # every HTTP failure and lose the status code with it.
        exc = claude_review.urllib.error.HTTPError(
            "https://example.invalid", 429, "Too Many Requests", {},
            io.BytesIO(b'{"error":{"type":"budget_exceeded"}}'),
        )
        out = self._call_with(exc)
        self.assertIn("HTTP 429", out)
        self.assertIn("budget_exceeded", out)
        self.assertNotIn("transport failure", out)


class AnUnparseableBodyReportsRatherThanCrashing(unittest.TestCase):
    """The answer arriving is not the same as the answer being readable.

    The transport handlers catch the CALL failing. They do not catch the BODY
    being unreadable: `json.loads` raises JSONDecodeError and `.decode` raises
    UnicodeDecodeError, both ValueError and neither an OSError. So a proxy error
    page, a truncated response or a gateway's HTML propagated out of `main()`
    and took `write_status()` with it -- the identical crash-before-status-write
    that the transport fix in this same PR closed for sockets.

    Raised in review on #197, on the commit that fixed the transport half. The
    same defect wearing a different exception type, which is the fourth time
    this file has met "fixed the shape, missed its neighbour" in one week.
    """

    def _answer_with(self, payload):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return payload

        with mock.patch.object(
            claude_review._NO_REDIRECT_OPENER, "open", return_value=Response()
        ):
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}, clear=False):
                return claude_review.call_claude("diff")

    def test_a_proxy_error_page_reports_instead_of_crashing(self):
        body = self._answer_with(b"<html><body>502 Bad Gateway</body></html>")
        self.assertIn(claude_review.FAILED_BANNER, body)
        self.assertEqual(claude_review.STATUS_FAILED, claude_review.status_for(body))

    def test_the_body_that_did_not_parse_is_shown(self):
        # "unparseable" is not actionable; a Cloudflare page and a truncated
        # JSON body look nothing alike, and the difference is the diagnosis.
        body = self._answer_with(b"<html>upstream connect error</html>")
        self.assertIn("upstream connect error", body)

    def test_a_truncated_json_body_takes_the_same_path(self):
        body = self._answer_with(b'{"content":[{"type":"text","text":"half')
        self.assertIn(claude_review.FAILED_BANNER, body)
        self.assertEqual(claude_review.STATUS_FAILED, claude_review.status_for(body))

    def test_undecodable_bytes_take_it_too(self):
        # UnicodeDecodeError is a ValueError, so one handler covers both without
        # naming either -- the same reason OSError is the net for transport.
        body = self._answer_with(b"\xff\xfe\x00not utf-8 at all")
        self.assertIn(claude_review.FAILED_BANNER, body)
        self.assertEqual(claude_review.STATUS_FAILED, claude_review.status_for(body))

    def test_a_good_body_still_reviews(self):
        # The guard must not swallow the happy path.
        body = self._answer_with(
            b'{"content":[{"type":"text","text":"looks fine"}],'
            b'"stop_reason":"end_turn"}'
        )
        self.assertIn("looks fine", body)
        self.assertEqual(claude_review.STATUS_OK, claude_review.status_for(body))


class ThreeMoneyFailuresAreThreeDifferentSentences(unittest.TestCase):
    """A cap, a ceiling and an empty account are fixed by different people.

    They arrive as three different statuses and the message has to tell them
    apart, because the operator's next action differs in each case:

        429 + budget_exceeded  the team DAILY cap  -> wait, or raise the cap
        402                    a per-key ceiling   -> raise the key's ceiling
        400 + credit balance   the provider account is EMPTY -> add credit

    The third had no branch until 2026-09-01, when it stopped every review and
    every agent constraint across the repo and the posted comment said only
    "HTTP 400 from the API" with the reason nested two JSON levels down inside
    an escaped string. The hint list on offer was "401 or 403 is the key, 402
    is the budget", none of which matched. Diagnosing it meant reading the
    nested string by hand.
    """

    # The body as the broker actually sent it during the outage, escaping and
    # all -- a fixture invented from the docs would not have the nesting that
    # made this hard to read in the first place.
    REAL_BODY = (
        '{"error":{"message":"{\\"type\\":\\"error\\",\\"error\\":'
        '{\\"type\\":\\"invalid_request_error\\",\\"message\\":'
        '\\"Your credit balance is too low to access the Anthropic API. '
        'Please go to Plans & Billing to upgrade or purchase credits.\\"}}. '
        'Received Model Group=claude-sonnet-5","code":"400"}}'
    )

    def _hint_for(self, code, body):
        error = urllib.error.HTTPError(
            "https://llm.example.invalid/v1/messages", code, "nope", {},
            io.BytesIO(body.encode("utf-8")),
        )
        with mock.patch.object(
            claude_review._NO_REDIRECT_OPENER, "open", side_effect=error
        ):
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}, clear=False):
                return claude_review.call_claude("diff")

    def test_an_empty_provider_account_says_so(self):
        out = self._hint_for(400, self.REAL_BODY)
        self.assertIn(claude_review.FAILED_BANNER, out)
        self.assertEqual(claude_review.STATUS_FAILED, claude_review.status_for(out))
        self.assertIn("upstream account being empty", out)
        self.assertIn("Add credit", out)

    def test_it_says_what_will_NOT_help(self):
        # The two things an operator reaches for first, named as useless, so
        # nobody spends an hour re-running a job that cannot pass.
        out = self._hint_for(400, self.REAL_BODY)
        self.assertIn("No re-run will clear it", out)
        self.assertIn("no ceiling can be raised past it", out)

    def test_a_per_key_ceiling_is_still_its_own_sentence(self):
        out = self._hint_for(402, '{"error":{"message":"budget exceeded"}}')
        self.assertIn("spending ceiling on the key", out)
        self.assertNotIn("Add credit", out)

    def test_the_daily_cap_is_still_its_own_sentence(self):
        out = self._hint_for(
            429, '{"error":{"type":"budget_exceeded","message":"Budget has been exceeded"}}'
        )
        self.assertIn("spending ceiling", out)
        self.assertNotIn("Add credit", out)

    def test_the_detector_reads_the_body_not_the_status(self):
        # The status is the BROKER's; the reason is the PROVIDER's. Nothing
        # about 400 distinguishes an empty account from a malformed request.
        self.assertTrue(claude_review.is_upstream_credit_exhausted(self.REAL_BODY))
        self.assertTrue(
            claude_review.is_upstream_credit_exhausted("insufficient credit remaining")
        )
        self.assertFalse(claude_review.is_upstream_credit_exhausted(""))
        self.assertFalse(
            claude_review.is_upstream_credit_exhausted("model not found: claude-x"),
        )

    def test_a_redirect_is_a_redirect_whatever_its_body_says(self):
        """Status beats body when the status is the one thing that cannot lie.

        Both `"budget" in detail` and `is_upstream_credit_exhausted(detail)`
        classify on text, and a 3xx body is not a provider verdict -- the
        broker never reached the provider. With the credit branch sitting
        above the redirect branch, a redirect whose body happened to carry
        either word was reported as a money problem, and the operator would
        top up an account over a base-URL mistake. Raised in review on #204
        against the credit branch; the `budget` branch had the same hole, so
        the fix is the ordering rather than a code guard on each.
        """
        for phrase in ("Your credit balance is too low", "budget exceeded"):
            for code in (301, 302, 307, 308):
                with self.subTest(code=code, phrase=phrase):
                    out = self._hint_for(code, f'{{"message":"{phrase}"}}')
                    self.assertIn("answered with a redirect", out)
                    self.assertNotIn("Add credit", out)
                    self.assertNotIn("spending ceiling", out)

    def test_the_money_branches_still_fire_on_their_own_statuses(self):
        # Reordering fixes by exclusion, so prove it excluded only redirects:
        # the same two phrases on a non-3xx status must still be classified.
        self.assertIn("Add credit", self._hint_for(400, self.REAL_BODY))
        self.assertIn(
            "spending ceiling", self._hint_for(402, '{"message":"budget exceeded"}')
        )

    def test_a_budget_body_is_not_a_credit_body(self):
        # Folding them together would tell the operator to top up an account
        # when the fix is to raise a cap. Different money, different person.
        self.assertFalse(
            claude_review.is_upstream_credit_exhausted(
                '{"error":{"type":"budget_exceeded","message":"Budget has been exceeded!"}}'
            )
        )


class TheKeyDoesNotFollowARedirect(unittest.TestCase):
    """`messages_endpoint()` validated the destination and not the journey.

    urllib's default redirect handler copies every header but `content-length`
    and `content-type` onto the new request, so `x-api-key` rides along. A
    validated https endpoint answering 302 therefore hands the API key to
    whatever host the redirect names -- the residual half of the exact threat
    the endpoint check was written for.

    MEASURED THREE WAYS on 2026-08-31 before the fix: the handler in isolation
    returned a Request for `evil.example` carrying the sentinel; two loopback
    servers showed the redirect TARGET receiving it verbatim; and the opener
    below raised instead, with the target receiving nothing.

    These tests use REAL SOCKETS on the loopback interface rather than a mock,
    because the claim is about what urllib does and a mock of urllib cannot
    testify to that. They bind port 0, serve one request, and shut down.
    """

    def _serve(self, handler_cls):
        server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server.server_address[1]

    def _endpoints(self):
        """A redirector that points at a collector which records its headers."""
        seen = {}

        class Collector(http.server.BaseHTTPRequestHandler):
            def _record(inner):
                seen.update({k.lower(): v for k, v in inner.headers.items()})
                inner.send_response(200)
                inner.send_header("content-type", "application/json")
                inner.end_headers()
                inner.wfile.write(b'{"content":[{"type":"text","text":"x"}]}')

            # A 302 replays a POST as a GET, so both have to answer or the test
            # fails on its own shape rather than on the property.
            do_POST = _record
            do_GET = _record

            def log_message(inner, *a):
                pass

        collector_port = self._serve(Collector)

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_POST(inner):
                inner.send_response(302)
                inner.send_header(
                    "Location", f"http://127.0.0.1:{collector_port}/collect"
                )
                inner.end_headers()

            def log_message(inner, *a):
                pass

        return seen, self._serve(Redirector)

    def test_the_default_handler_would_have_forwarded_the_key(self):
        # The vulnerability, asserted rather than described, so the fix below is
        # not protecting against something nobody demonstrated.
        request = urllib.request.Request(
            "https://llm.example.invalid/v1/messages",
            data=b"{}",
            headers={"x-api-key": "SENTINEL", "content-type": "application/json"},
            method="POST",
        )
        forwarded = urllib.request.HTTPRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, "https://evil.example/collect"
        )
        self.assertEqual("evil.example", forwarded.host)
        self.assertIn("SENTINEL", str(dict(forwarded.headers)))

    def test_the_opener_refuses_the_redirect_and_the_target_gets_nothing(self):
        seen, redirector_port = self._endpoints()
        request = urllib.request.Request(
            f"http://127.0.0.1:{redirector_port}/v1/messages",
            data=b"{}",
            headers={"x-api-key": "SENTINEL", "content-type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            claude_review._NO_REDIRECT_OPENER.open(request, timeout=10)
        self.assertEqual(302, caught.exception.code)
        self.assertEqual({}, seen, "the redirect target received a request at all")

    def test_a_refused_redirect_reports_as_a_failure_and_says_why(self):
        # It lands in the HTTPError handler, so it is STATUS_FAILED -- blocking,
        # visible in the posted comment, and carrying the reason rather than a
        # bare status code.
        error = urllib.error.HTTPError(
            "https://llm.example.invalid/v1/messages", 302, "Found", {},
            io.BytesIO(b""),
        )
        with mock.patch.object(
            claude_review._NO_REDIRECT_OPENER, "open", side_effect=error
        ):
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}, clear=False):
                body = claude_review.call_claude("diff")
        self.assertIn(claude_review.FAILED_BANNER, body)
        self.assertEqual(claude_review.STATUS_FAILED, claude_review.status_for(body))
        self.assertIn("redirect", body.lower())
        self.assertIn("NOT sent onward", body)


class TheTokenDoesNotFollowARedirect(unittest.TestCase):
    """The GitHub token rode along on a redirect, as the API key did above.

    pr_diff() sends `authorization: Bearer $GH_TOKEN` to the GitHub API, and it
    went through urllib's default opener, which copies that header onto a
    redirect to any host. Found in review on capaz#65. Measured on CPython
    3.12.13 and 3.14.0 before the fix: a 302 from one loopback server to a
    second one handed the second the token verbatim, and the handler copied it
    onto an https-to-http redirect too.

    Real sockets, for the same reason as the class above: the claim is about
    what urllib does.
    """

    def _serve(self, handler_cls):
        server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # Cleanups run last first: stop serve_forever, then close its socket.
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def _endpoints(self):
        """A GitHub API stand-in that redirects every GET to a collector."""
        seen = {}

        class Collector(http.server.BaseHTTPRequestHandler):
            def do_GET(inner):
                seen.update({k.lower(): v for k, v in inner.headers.items()})
                inner.send_response(200)
                inner.send_header("content-type", "application/json")
                inner.end_headers()
                inner.wfile.write(b"[]")

            def log_message(inner, *a):
                pass

        collector_port = self._serve(Collector)

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_GET(inner):
                inner.send_response(302)
                inner.send_header(
                    "Location", f"http://127.0.0.1:{collector_port}/collect"
                )
                inner.end_headers()

            def log_message(inner, *a):
                pass

        return seen, f"http://127.0.0.1:{self._serve(Redirector)}"

    def test_the_default_opener_would_have_sent_the_token_on(self):
        # The leak, through these same two servers, so the test below cannot
        # pass because the redirect never reached the collector.
        seen, api = self._endpoints()
        request = urllib.request.Request(
            f"{api}/repos/o/r/pulls/1/files",
            headers={"authorization": "Bearer SENTINEL"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
        self.assertEqual("Bearer SENTINEL", seen.get("authorization"))

    def test_the_pr_diff_refuses_the_redirect_and_the_target_gets_nothing(self):
        seen, api = self._endpoints()
        env = {"PR_NUMBER": "1", "GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "SENTINEL"}
        with mock.patch.object(claude_review, "GITHUB_API", api), mock.patch.dict(
            os.environ, env
        ), contextlib.redirect_stderr(io.StringIO()):
            _, _, (unlisted, _) = claude_review.pr_diff()
        # The refused redirect is a failed page: named, not followed.
        self.assertIn("failed on page 1 (HTTP 302)", unlisted)
        self.assertEqual({}, seen, "the redirect target received a request at all")

    def test_no_request_in_the_reviewer_uses_the_default_opener(self):
        # Every request the reviewer sends carries the GitHub token or the API
        # key, so every one goes through an opener built on _NoRedirect. This
        # finds a new urlopen() call wherever it is added, not only the one
        # fixed here.
        tree = ast.parse(Path(claude_review.__file__).read_text(encoding="utf-8"))
        lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and "urlopen" in (getattr(node.func, "attr", None), getattr(node.func, "id", None))
        ]
        self.assertEqual([], lines, "claude_review.py calls urlopen() on these lines")


class AnUnhandledErrorStillPostsAReason(unittest.TestCase):
    """Whatever escapes main() posts a failed review instead of no status at all.

    Each crash-before-status found so far was fixed at its own call site
    (transport, IncompleteRead, the PR request, a files page on co-dm#80), and
    the next site stayed open. run() is the net under all of them, so these
    tests raise from main() itself rather than from any one site.
    """

    SECRET = 'password = "hunter2-correct-horse"'

    def _run(self, exc):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            claude_review, "main", side_effect=exc
        ), contextlib.redirect_stderr(io.StringIO()) as err:
            os.chdir(tmp)
            try:
                code = claude_review.run()
                status = Path(claude_review.REVIEW_STATUS_PATH).read_text(encoding="utf-8")
                comment = Path("claude-review.md").read_text(encoding="utf-8")
            finally:
                os.chdir(cwd)
        return code, status.strip(), comment, err.getvalue()

    def test_an_unhandled_error_fails_the_check_and_says_what_it_was(self):
        code, status, comment, err = self._run(KeyError("changed_files"))
        self.assertEqual((code, status), (0, claude_review.STATUS_FAILED))
        self.assertEqual(claude_review.status_for(comment), claude_review.STATUS_FAILED)
        self.assertIn("KeyError: 'changed_files'", comment)
        self.assertIn("The job log has the traceback", comment)
        self.assertIn("Traceback (most recent call last)", err)

    def test_the_message_is_redacted_before_it_is_posted(self):
        # Precondition: the redactor changes this line, or the test proves nothing.
        self.assertNotIn("hunter2-correct-horse", claude_review.redact(self.SECRET))
        _, status, comment, _ = self._run(ValueError(self.SECRET))
        self.assertEqual(status, claude_review.STATUS_FAILED)
        self.assertNotIn("hunter2-correct-horse", comment)

    def test_a_fence_in_the_message_cannot_close_the_code_block(self):
        _, _, comment, _ = self._run(RuntimeError("```\n## Approved"))
        self.assertEqual(comment.count("```"), 2)

    def test_the_script_entry_point_is_the_net(self):
        source = Path(claude_review.__file__).read_text(encoding="utf-8")
        self.assertIn('if __name__ == "__main__":\n    sys.exit(run())', source)


class ErrorTextIsRedactedWhereverItGoes(unittest.TestCase):
    """Exception and response text reaches the comment and the log through error_text().

    Before error_text(), only run()'s net redacted. API error bodies, git and
    urllib messages and the files-page reason went to the comment or the log
    as they came, and an API body holding ``` could close the code fence it
    was shown in (review of #338).
    """

    SECRET = 'password = "hunter2-correct-horse"'

    def test_error_text_redacts_caps_and_defuses_fences(self):
        self.assertNotIn("hunter2-correct-horse", claude_review.redact(self.SECRET))
        self.assertNotIn("hunter2-correct-horse", claude_review.error_text(self.SECRET))
        self.assertEqual(len(claude_review.error_text("x" * 5000)), 1000)
        self.assertEqual(len(claude_review.error_text("x" * 5000, limit=None)), 5000)
        self.assertNotIn("```", claude_review.error_text("```\n## Approved"))

    def test_an_api_error_body_is_redacted_in_the_comment(self):
        refused = urllib.error.HTTPError(
            "https://api.anthropic.com/v1/messages", 400, "bad", {},
            io.BytesIO(f"invalid request: {self.SECRET}\n```\n## Approved".encode()),
        )
        self.addCleanup(refused.close)
        with mock.patch.object(
            claude_review._NO_REDIRECT_OPENER, "open", side_effect=refused
        ), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            body = claude_review.post_review({"model": "m", "messages": []}, "test-key")
        self.assertTrue(body.startswith(claude_review.FAILED_BANNER), body)
        self.assertNotIn("hunter2-correct-horse", body)
        self.assertEqual(body.count("```"), 2, body)

    def test_no_error_text_is_printed_or_posted_raw(self):
        # The guard for the class: a new `{exc}` or `{detail}` in an f-string,
        # or a str(exc), fails here until it goes through error_text(). The
        # redactor's own fallback warning is the one exemption, because
        # redact() is what failed when it prints.
        source = Path(claude_review.__file__).read_text(encoding="utf-8")
        raw = [
            line.strip()
            for line in source.splitlines()
            if re.search(r"\{(?:exc|detail)\}|str\(exc\)", line)
            and "Secrets are still hidden" not in line
            and "error_text(" not in line
        ]
        self.assertEqual([], raw, "error text printed or posted without error_text()")
        self.assertNotIn("traceback.print_exc()", source)


class ATruncatedBodyStillPostsAReason(unittest.TestCase):
    """A response cut off mid-body raised straight out of call_claude().

    The `except OSError` branch was added so a transport failure -- timeout,
    reset, DNS, TLS -- still posts a comment with the reason instead of killing
    the process before write_status() runs. It does not cover the one failure
    that happens AFTER a response arrives: the server sends a status line and a
    Content-Length, then the connection closes early. http.client raises
    IncompleteRead from response.read(), and IncompleteRead is not an OSError:

        HTTPException -> Exception -> BaseException

    So the same crash-before-status shape came back through a class the net did
    not name. Raised by the reviewer on claude-cert-examprep#9, confirmed on
    kit main, kit issue #211.

    REAL SOCKET, like the redirect tests above, and for the same reason: the
    claim is about what http.client does when a body is short, and a mock of
    http.client cannot testify to that. The handler promises 4096 bytes and
    sends 20. The only thing faked is messages_endpoint(), so the request
    reaches the loopback port; that function has its own tests.
    """

    def _serve_truncated(self):
        class Truncator(http.server.BaseHTTPRequestHandler):
            def do_POST(inner):
                inner.send_response(200)
                inner.send_header("content-type", "application/json")
                inner.send_header("content-length", "4096")
                inner.end_headers()
                inner.wfile.write(b'{"content":[{"type":')
                inner.wfile.flush()
                # Returning closes the connection with 4076 bytes still owed.

            def log_message(inner, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Truncator)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server.server_address[1]

    def test_a_short_body_posts_a_failure_instead_of_raising(self):
        port = self._serve_truncated()
        with mock.patch.object(
            claude_review, "messages_endpoint",
            return_value=f"http://127.0.0.1:{port}/v1/messages",
        ):
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}, clear=False):
                # Before the fix this line raises http.client.IncompleteRead.
                body = claude_review.call_claude("diff")
        self.assertIn(claude_review.FAILED_BANNER, body)
        self.assertEqual(claude_review.STATUS_FAILED, claude_review.status_for(body))
        self.assertIn("IncompleteRead", body)
        # The OSError sentence says no response arrived. One did. The reason
        # posted has to say what actually happened: a body cut short.
        self.assertIn("cut off", body.lower())
        self.assertNotIn("no response arrived", body)

    def test_the_gap_is_a_class_hierarchy_fact_not_an_opinion(self):
        # If this ever flips, the separate branch is redundant and can fold
        # into the OSError one. It will not flip; it is here so the reason the
        # branch exists is a test, not a comment.
        self.assertFalse(issubclass(http.client.IncompleteRead, OSError))
        self.assertFalse(issubclass(http.client.HTTPException, OSError))


class AFailureBeforeAnyBodyIsNotACutOffBody(unittest.TestCase):
    """#213 added `except http.client.HTTPException` with one sentence: the
    endpoint answered and the body was cut off. Measured on kit #217, that is
    true for exactly one of thirteen subclasses. A garbage status line raises
    BadStatusLine -- nothing answered -- and was told it had. The fix keeps the
    broad net (every one of the thirteen would otherwise crash the reviewer
    before write_status(), which is what #213 closed) and chooses the sentence
    by type.

    The real-socket case is BadStatusLine, because http.client raising it from
    a real read is the thing a mock cannot testify to. RemoteDisconnected is
    tested by type through a mocked opener: which exception a closed socket
    surfaces as is platform-bound (RST on Windows is ConnectionResetError, a
    clean FIN on Linux is RemoteDisconnected), and the claim under test is the
    wording for the type, not the socket.
    """

    def _serve_garbage(self):
        import socketserver

        class Garbage(socketserver.BaseRequestHandler):
            def handle(inner):
                # The whole request is read before the answer. http.client
                # writes the headers and the body separately, so one recv()
                # often got only the headers; the body then reached a closed
                # socket, Windows reset the connection, and the client raised
                # ConnectionResetError or ConnectionAbortedError instead of
                # reading the status line: 58 of 300 runs (kit #315).
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = inner.request.recv(65536)
                    if not chunk:
                        return
                    data += chunk
                head, _, body = data.partition(b"\r\n\r\n")
                length = re.search(rb"(?im)^content-length:[ \t]*(\d+)", head)
                remaining = int(length.group(1)) - len(body) if length else 0
                while remaining > 0:
                    chunk = inner.request.recv(remaining)
                    if not chunk:
                        return
                    remaining -= len(chunk)
                inner.request.sendall(b"<html>502 Bad Gateway</html>\r\n\r\n")
                inner.request.close()

        server = socketserver.TCPServer(("127.0.0.1", 0), Garbage)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server.server_address[1]

    def _call(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}, clear=False):
            return claude_review.call_claude("diff")

    def test_a_garbage_status_line_is_not_an_answer(self):
        port = self._serve_garbage()
        with mock.patch.object(
            claude_review, "messages_endpoint",
            return_value=f"http://127.0.0.1:{port}/v1/messages",
        ):
            body = self._call()
        self.assertIn(claude_review.FAILED_BANNER, body)
        self.assertEqual(claude_review.STATUS_FAILED, claude_review.status_for(body))
        self.assertIn("BadStatusLine", body)
        self.assertNotIn("answered", body.lower())
        self.assertNotIn("cut off", body.lower())

    def test_a_peer_that_hung_up_did_not_answer(self):
        exc = http.client.RemoteDisconnected("Remote end closed connection without response")
        with mock.patch.object(claude_review._NO_REDIRECT_OPENER, "open", side_effect=exc):
            body = self._call()
        self.assertIn(claude_review.FAILED_BANNER, body)
        self.assertIn("RemoteDisconnected", body)
        self.assertIn("without sending a response", body)
        self.assertNotIn("answered", body.lower())
        self.assertNotIn("cut off", body.lower())

    def test_a_cut_off_body_still_says_so_with_its_counts(self):
        exc = http.client.IncompleteRead(b"x" * 20, 4076)
        with mock.patch.object(claude_review._NO_REDIRECT_OPENER, "open", side_effect=exc):
            body = self._call()
        self.assertIn("cut off", body.lower())
        self.assertIn("20 bytes arrived of 4096 promised", body)

    def test_no_other_subclass_claims_a_body(self):
        seen = 0
        for name in sorted(dir(http.client)):
            cls = getattr(http.client, name)
            if not (isinstance(cls, type) and issubclass(cls, http.client.HTTPException)):
                continue
            if cls in (http.client.HTTPException, http.client.IncompleteRead,
                       http.client.RemoteDisconnected):
                continue
            try:
                exc = cls("x")
            except TypeError:
                continue
            seen += 1
            with self.subTest(exception=name):
                with mock.patch.object(
                    claude_review._NO_REDIRECT_OPENER, "open", side_effect=exc
                ):
                    body = self._call()
                self.assertIn(claude_review.FAILED_BANNER, body)
                self.assertIn(name, body)
                self.assertNotIn("answered", body.lower())
                self.assertNotIn("cut off", body.lower())
        self.assertGreater(seen, 5, "the table found almost nothing to test, which is not a pass")


class AVendorKeyIsRecognisedWithoutParsingTheLine(unittest.TestCase):
    """The half of the table that does not care where the value sits.

    Everything in the key=value rule PARSES SYNTAX to find a value position, and
    every leak found in the week of 2026-08-31 was there -- spacing, prefixes,
    operands, backticks, escaped quotes, fallback operators, subscript
    assignment, positional arguments. Eight classes of it are still open.

    These entries ask a different question: not "where is the value" but "is
    this string a key". No quoting form can hide from that, which is why a
    secret the parser cannot reach is still caught here.

    A PREFIX, NOT AN ENTROPY THRESHOLD, and the difference was measured. Against
    the shapes the parser misses, carrying real credential formats: a tuned
    entropy rule caught 14% and redacted 0.079% of the repo's lines, a loose one
    caught 57% and redacted 9.9%, and vendor prefixes caught 75% at 0.008%.
    Prefixes win on BOTH axes because a prefix is a literal string -- nothing
    that is not a GitHub token begins `ghp_` -- while entropy collides with git
    SHAs, UUIDs, content hashes and base64 assets.
    """

    # THESE USED TO READ AS `"<REDACTED>"` IN A REVIEWED DIFF. MOSTLY THEY NO
    # LONGER DO, AND THAT IS A FIX, NOT A REGRESSION.
    #
    # The history matters because it cost real review time. These fixtures are
    # vendor keys and the code under test detects vendor keys, so `redact()` ate
    # them on the way to the model. Two review rounds on #196 were shown
    # `"<REDACTED>"`, reasonably concluded the fixture was placeholder text, and
    # the second reported it as a BLOCKING bug. Both readings were correct about
    # what they had been shown, which is what made it expensive.
    #
    # SPLITTING THE LITERALS FIXED IT, as a side effect of fixing something else.
    # Written as `"ghp_" + "16C7..."` the SOURCE line carries no contiguous
    # vendor shape, so the redactor leaves it alone and the reviewer sees the
    # real fixture -- while the RUNTIME value is byte-identical, so the detectors
    # still fire and not one assertion moves. The split was forced by GitHub push
    # protection refusing the push (#209); this fell out of it. Measured against
    # main's redactor: all seventeen reach the model intact. (A first pass
    # measured eight lines and found seven; the six that push protection never
    # forced -- JWT, npm, HuggingFace, Replicate, PyPI, Docker Hub -- were still
    # contiguous and still eaten, and were split in the same commit.)
    #
    # ONE REDACTED ON ITS LABEL, NOT ITS VALUE. `redact()` fires on names
    # as well as values, so `"Stripe secret"` was eaten for the word `secret`
    # however the value was written. It is now `"Stripe live key"`. Nothing keys
    # on the label -- VENDOR_SAMPLES is only ever walked as
    # `for label, value in ...` -- so this changes no assertion, and it is the
    # rename that takes the class to seventeen of seventeen.
    #
    # WHAT WOULD ACTUALLY BE WRONG, and is the thing to check instead: a literal
    # `"<REDACTED>"` fixture would FAIL these tests, not pass them. That string
    # matches no vendor prefix, so it survives `redact()` unchanged, and the
    # assertion is `assertNotIn(value, redact(line))`. A placeholder cannot hide
    # here -- the suite going green is itself the proof the fixtures are real.
    #
    # If a fixture DOES still reach you redacted, that is the redactor working,
    # not a placeholder. Verify with
    # `git show <sha>:.github/scripts/test_claude_review.py` rather than by
    # asking for the value to be changed.
    #
    # Structure is real; the bytes are not. None of these is a live key.
    VENDOR_SAMPLES = {
        "Stripe live key": "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
        "GitHub PAT": "ghp_" + "16C7e42F292c6912E7710c838347Ae178B4a",
        "GitLab PAT": "glpat-" + "ABCdef123456789012345",
        "Slack bot": "xoxb-" + "123456789012-1234567890123-aB3dEfGhIjKl",
        "Google API": "AIza" + "SyD-aBcDeFgHiJkLmNoPqRsTuVwXyZ12345",
        "AWS temporary": "ASIA" + "IOSFODNN7EXAMPLE",
        "JWT": "eyJ" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVP",
        "npm": "npm_" + "abcdefghij1234567890ABCDEFGHIJ1234",
        "SendGrid": "SG." + "aBcDeFgHiJkLmNoPqRsTu.vWxYz1234567890abcdefghij",
        "HuggingFace": "hf_" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567",
        "DigitalOcean": "dop_v1_" + "a1b2c3d4" * 8,
        "Shopify": "shpat_" + "a1b2c3d4" * 4,
        "Replicate": "r8_" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890a",
        # Added on review of #196, which asked for the package and
        # infrastructure vendors a repo like this actually holds keys for.
        "PyPI": "pypi-" + "AgEIcHlwaS5vcmcCJDAxMjM0NTY3ODkwYWJjZGVm",
        "Docker Hub": "dckr_pat_" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ01",
        "Supabase": "sbp_" + "a1b2c3d4" * 5,
        "Twilio API key": "SK" + "0123456789abcdef" * 2,
    }

    # Lines the key=value rule does NOT reach, each measured on 2026-08-31 and
    # recorded in #188. This is where these entries earn their place: a shape
    # the parser cannot parse is still a string these can recognise.
    PARSER_CANNOT_REACH = (
        'config["api_key"] = "{}"',
        'vault.write("password", "{}")',
        "    proxy_set_header X-Api-Key {};",
        "ENV NPM_TOKEN {}",
        'apiKey?: string = "{}"',
        '  ansible_password: !unsafe {}',
        'const apiKey = /* dev only */ "{}";',
    )

    def test_every_vendor_key_is_redacted_wherever_it_sits(self):
        for label, value in self.VENDOR_SAMPLES.items():
            for shape in self.PARSER_CANNOT_REACH:
                line = shape.format(value)
                with self.subTest(vendor=label, shape=shape):
                    self.assertNotIn(value, claude_review.redact(line))

    def test_the_shapes_really_are_beyond_the_key_value_rule(self):
        # A CHECK THAT SCANNED NOTHING IS NOT A PASS. If the parser started
        # covering these, the test above would pass without the vendor entries
        # doing anything, and would quietly stop testing them. A value with no
        # vendor prefix must still survive every one of these shapes.
        plain = "correcthorsebatterystaple"
        for shape in self.PARSER_CANNOT_REACH:
            line = shape.format(plain)
            with self.subTest(shape=shape):
                self.assertIn(
                    plain, claude_review.redact(line),
                    "the key=value rule now reaches this shape, so it no longer "
                    "proves the vendor entries did the work",
                )

    def test_the_vendor_is_still_named_in_the_output(self):
        # The replacement keeps the prefix, as `sk-<REDACTED>` and
        # `AKIA<REDACTED>` already did: "you leaked a GitHub token" is worth
        # more to a reviewer than "you leaked something".
        sample = "ghp_" + "16C7e42F292c6912E7710c838347Ae178B4a"
        out = claude_review.redact(f'x = "{sample}"')
        self.assertIn("ghp_<REDACTED>", out)

    def test_an_identifier_is_not_a_secret(self):
        """Twilio's ACCOUNT SID is published in dashboards and request URLs.

        Redacting it would cost a reviewer a line it can legitimately read and
        hide nothing, which is the same reasoning that keeps Stripe's
        publishable `pk_` key out of the table. The API KEY SID above is a
        different string and is redacted.
        """
        sid = "AC" + "0123456789abcdef0123456789abcdef"
        line = f'account_sid = "{sid}"'
        self.assertEqual(line, claude_review.redact(line))

    def test_a_word_that_merely_ends_with_a_prefix_is_left_alone(self):
        """`TASK` + 32 hex is not a Twilio key, and used to be redacted as one.

        Raised in review on #196 against the `SK` entry, whose two characters
        make the collision easy to see. The measurement said it was not an `SK`
        bug: none of the twenty entries anchored its prefix, so every one of
        them matched mid-identifier. The boundary went on at the build site,
        and this pins the case that named it.
        """
        for word in ("TASK", "MASK", "FLASK", "SUBTASK"):
            line = f"{word}0123456789abcdef0123456789abcdef"
            with self.subTest(word=word):
                self.assertEqual(line, claude_review.redact(line))

    def test_no_vendor_prefix_matches_inside_a_longer_word(self):
        """The whole table, not the one entry review happened to look at.

        A real credential never acquires a leading identifier character, so a
        sample that still matches with one glued on is a rule that will blank
        benign tokens. This ran 20-of-20 before the fix.
        """
        for label, value in self.VENDOR_SAMPLES.items():
            with self.subTest(vendor=label):
                glued = f"ENV SOME_TOKEN X{value}"
                self.assertEqual(glued, claude_review.redact(glued))

    # Written out once and split, like VENDOR_SAMPLES: the source line carries
    # no key shape, so the reviewer reading a diff of this file sees it whole.
    WRITTEN_OUT_KEYS = (
        ("sk-", "sk-" + "abcdefghij0123456789", "sk-<REDACTED>"),
        ("AKIA", "AKIA" + "IOSFODNN7EXAMPLE", "AKIA<REDACTED>"),
    )

    def test_a_pack_code_ending_in_desk_reaches_the_model_whole(self):
        """The line capaz#16's reviewer was shown as `help-desk-<REDACTED>`.

        `sk-` and `AKIA` are written out in SECRET_PATTERNS, above the build
        site where `_VENDOR_KEYS` takes the boundary, so they matched inside
        words: the `sk-` in `desk-` plus twenty characters is an OpenAI key by
        shape. The reviewer then reported its own redaction as blocking data
        corruption in a pack file that was correct.
        """
        line = '+    "code": "msp.domain.help-desk' + '-and-end-user-support",'
        self.assertEqual(line, claude_review.redact(line))

    def test_the_written_out_key_entries_skip_a_longer_word_too(self):
        for label, key, _shown in self.WRITTEN_OUT_KEYS:
            for glued in (f"de{key}", f"X{key}", f"SOME_VAR_{key}"):
                with self.subTest(entry=label, glued=glued):
                    self.assertEqual(glued, claude_review.redact(glued))

    def test_the_written_out_key_entries_still_fire_on_a_real_key(self):
        """The boundary must not switch these two off, so assert both directions.

        An escape or a percent-encoded byte ends a word, though its last
        character is a word character: on review of #306, `"line1\\nsk-..."`,
        `"\\tAKIA..."` and `a%3Dsk-...` reached the model whole. These are the
        two-character escapes as a diff carries them, not real control bytes.
        """
        for label, key, shown in self.WRITTEN_OUT_KEYS:
            for line in (
                f"ENV SOME_TOKEN {key}",
                f"see the runbook: {key}",
                f"some-{key}",
                f'msg = "line1\\n{key}"',
                f'x = "\\t{key}"',
                f'x = "a\\r{key}"',
                f'q = "a%3D{key}"',
                f"q=a%3d{key}",
            ):
                with self.subTest(entry=label, line=line):
                    out = claude_review.redact(line)
                    self.assertNotIn(key, out)
                    self.assertIn(shown, out)

    def test_every_key_shape_entry_carries_the_boundary(self):
        """The whole table, enumerated, so the next written-out entry cannot miss it.

        The boundary went on at the `_VENDOR_KEYS` build site and skipped the two
        entries written out above it; a test pinned to those two would miss a
        third. Two kinds of entry are exempt by what they are: the key=value rule,
        whose replacement is a function because it parses syntax rather than
        matching a prefix, and the PEM block, which opens on `-----BEGIN`.
        Everything else is a key shape and opens on `_NOT_MID_IDENTIFIER`.
        """
        checked = 0
        for pattern, replacement in claude_review.SECRET_PATTERNS:
            if callable(replacement) or pattern.pattern.startswith("-----BEGIN"):
                continue
            checked += 1
            with self.subTest(pattern=pattern.pattern[:60]):
                self.assertTrue(
                    pattern.pattern.startswith(claude_review._NOT_MID_IDENTIFIER),
                    "a key-shape entry without the boundary matches inside words",
                )
        # A floor, so a table that moved out from under this loop cannot pass
        # it by checking nothing: the written-out entries plus every vendor.
        self.assertGreaterEqual(
            checked, len(self.WRITTEN_OUT_KEYS) + len(claude_review._VENDOR_KEYS)
        )

    def test_the_boundarys_own_blind_spot_is_pinned_not_assumed(self):
        """What the word boundary costs, asserted so the doc cannot drift.

        Raised in review on #196. Excluding `[A-Za-z0-9_]` means a key glued
        to a leading identifier is no longer seen by this half. That is the
        price of not blanking `TASK` + 32 hex, and it is worth pinning in
        BOTH directions so a future widening of the class shows up here as a
        failure rather than as a silent change of behaviour.

        `sk-` and `AKIA` took the boundary on #306, and its cost with it: on
        main they matched anywhere, so `key_sk-...` was redacted and now is
        not. An escape (`\\n`, `\\r`, `\\t`) or a percent-encoded byte (`%3D`)
        is not part of the word, so a key behind one IS caught; any other
        escape (`\\x41`) ends in a word character and reads as the word going on.
        """
        ghp = "ghp_" + "16C7e42F292c6912E7710c838347Ae178B4a"
        keys = [(ghp[:4], ghp)] + [(label, key) for label, key, _ in self.WRITTEN_OUT_KEYS]
        for label, key in keys:
            # Glued to an identifier or behind a word-ending escape: this half
            # does not reach it.
            for line in (
                f"SOMEVAR_{key}", f"SOMEVAR{key}", f"MYVAR=SOMEVAR_{key}",
                f"key_{key}", f'"\\x41{key}"',
            ):
                with self.subTest(entry=label, missed=line):
                    self.assertIn(key, claude_review.redact(line))
            # A hyphen is not an identifier character, so that one IS caught --
            # the deliberate asymmetry, kept biased toward over-redaction -- and
            # so is a key behind an escape or a percent-encoded byte.
            for line in (f"some-{key}", f'"a\\n{key}"', f'"\\t{key}"', f"q=a%3D{key}"):
                with self.subTest(entry=label, caught=line):
                    self.assertNotIn(key, claude_review.redact(line))
            # And the OTHER half still reaches a glued key under a known name.
            with self.subTest(entry=label, named=True):
                self.assertNotIn(key, claude_review.redact(f'token = "SOMEVAR_{key}"'))

    def test_the_boundary_did_not_switch_the_detectors_off(self):
        """The other half of the pin, because a boundary can fix by breaking.

        `assertEqual(line, redact(line))` above passes just as happily if the
        entry stopped matching anything at all. So assert the same samples in
        the same shape WITHOUT the glued character are still eaten.
        """
        for label, value in self.VENDOR_SAMPLES.items():
            with self.subTest(vendor=label):
                line = f"ENV SOME_TOKEN {value}"
                self.assertNotIn(value, claude_review.redact(line))

    def test_the_prefixless_credentials_are_named_not_covered(self):
        """The honest half of the claim, asserted rather than assumed.

        An AWS SECRET access key, a Twilio auth token and a raw hex credential
        carry no distinguishing prefix, so nothing here reaches them and the
        table must not be read as if it did. If one of these ever starts being
        redacted, the comment above `_VENDOR_KEYS` has become wrong and should
        be corrected rather than left to flatter the coverage.
        """
        for label, value in (
            ("AWS secret access key", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
            ("Twilio auth token", "0123456789abcdef0123456789abcdef"),
            ("raw hex credential", "9f86d081884c7d659a2feaa0c55ad015"),
        ):
            with self.subTest(label=label):
                line = f"# see the runbook, value {value}"
                self.assertEqual(line, claude_review.redact(line))

    def test_every_entry_has_exactly_one_group_for_the_replacement(self):
        # The replacement is `\1<REDACTED>`, so an entry with no group raises at
        # substitution time and an entry with two silently keeps the wrong half.
        for pattern in claude_review._VENDOR_KEYS:
            with self.subTest(pattern=pattern):
                self.assertEqual(
                    1, re.compile(pattern).groups,
                    "each vendor entry captures exactly the prefix",
                )

    # NO CROSS-COPY TEST HERE, AND THAT IS NOT AN OVERSIGHT. The kit's suite
    # carries `test_the_two_copies_carry_the_same_vendors`, which imports this
    # file and compares the compiled `_VENDOR_KEYS`. This copy cannot mirror it:
    # it travels to bootstrapped repos that have no kit tree to compare against,
    # and a test that silently finds nothing to check is worse than no test --
    # it is the "scanned nothing and passed" shape this suite exists to refuse.
    # The comparison belongs in the copy that can see both. Raised in review on
    # #196, which noticed the asymmetry and was right to ask.
    def test_what_it_deliberately_leaves_alone(self):
        # A git SHA and a UUID are the collision an entropy rule cannot resolve
        # and a prefix rule never meets. Stripe's PUBLISHABLE key is public by
        # design, so redacting it would hide nothing and cost the reviewer a
        # line it may legitimately need.
        for line in (
            "# pinned at da39a3ee5e6b4b0d3255bfef95601890afd80709",
            "id = 550e8400-e29b-41d4-a716-446655440000",
            'stripe.publishable = "pk_live_4eC39HqLyjWDarjtT1zdp7dc"',
            "Bearer tokens are described in the README.",
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))


class AVariableReshapingItselfCarriesNothingNew(unittest.TestCase):
    r"""`token = token.strip()` has no literal to hide.

    The name matches, so the rule fired; the right-hand side is the SAME
    variable with methods called on it. Whatever the value is, it was already in
    the variable a line earlier -- and redacting it corrupts control logic in
    the text the model reads:

        token = token.strip().strip("'\"")
        ->  token="<REDACTED>""'\"")

    Measured on kit #200, where the model then reported that corruption as a
    BLOCKING SyntaxError -- accurately -- on a file that compiles and whose
    suite was green in the same CI run. A CORRECT READING OF A WRONG INPUT is
    the worst failure this table has, because nothing in the review can answer
    it: the reviewer is right about what it was shown.
    """

    def assertUnchanged(self, line):
        self.assertEqual(line, claude_review.redact(line))

    def assertRedacted(self, line):
        self.assertIn("<REDACTED>", claude_review.redact(line))

    def test_a_reshape_of_the_same_name_survives(self):
        for line in (
            "token = token.strip()",
            "key = key.lower()",
            "token = token[1:]",
            "secret = secret.split(\",\")[0]",
            "api_key = api_key.replace(\"-\", \"\")",
            "password = password.strip().strip(\"'\\\"\")",
        ):
            with self.subTest(line=line):
                self.assertUnchanged(line)

    def test_a_different_name_is_an_ordinary_value(self):
        # The backreference is the whole safety argument: a LOOKALIKE is not
        # the same variable, so it is not exempt and is decided as an ordinary
        # value. Since capaz#107 an ordinary call with no token in it is code,
        # so it comes back as written; with a token, it goes.
        for line in (
            "token = other.strip()",
            "token = token_source.strip()",
            "token = source_token.strip()",
        ):
            with self.subTest(line=line):
                self.assertUnchanged(line)
        self.assertRedacted("token = other.strip()]AbCdEf0123456789ZzYyXx")

    def test_a_long_literal_in_a_subscript_is_not_a_reshape(self):
        """Rule (3) says no ARGUMENT may carry a long alphanumeric run. The
        regex applied that to the call branch and never to the subscript
        branch, so `token = token["AbCdEfGh12345678"]` was read as a harmless
        reshape and left on the line for the model. Raised by the reviewer on
        gestalt-workframe-edu#609; measured on kit main before filing (#212):
        three of the seven lines below leaked, and the call-branch control
        showed the guard working where it existed.

        An index, a slice and a short key are still what the exemption is FOR.
        """
        literal = "AbCdEfGh12345678"
        for line in (
            f'token = token["{literal}"]',
            f"token = token['{literal}']",
            f'token = token.strip()["{literal}"]',
        ):
            with self.subTest(line=line):
                self.assertRedacted(line)
        for line in (
            "token = token[0]",
            "token = token[4:]",
            'token = token["id"]',
            'secret = secret.split(",")[0]',
        ):
            with self.subTest(line=line):
                self.assertUnchanged(line)

    def test_a_literal_after_the_chain_is_still_a_literal(self):
        """The regression this exemption nearly shipped.

        With whitespace allowed as a terminator, `token = token.strip() or
        "hunter2"` matched the chain, hit the space, and went exempt WITH THE
        LITERAL STILL ON THE LINE -- an exemption written to stop a false
        finding, turning a redacted line into a leak. Caught by measuring
        before pushing, and pinned so it cannot come back.
        """
        for line in (
            'token = token.strip() or "AbCdEf0123456789ZzYyXx"',
            'token = token.strip() + "AbCdEf0123456789ZzYyXx"',
            'token = token.strip() & "AbCdEf0123456789ZzYyXx"',
        ):
            with self.subTest(line=line):
                self.assertNotIn("AbCdEf0123456789ZzYyXx", claude_review.redact(line))

    def test_a_ternary_still_leaks_and_that_is_not_this_exemptions_doing(self):
        """A PRE-EXISTING gap, pinned here so the next reader does not blame the
        exemption above for it.

        The chain follows `||`, `??`, `or` and `and`; it does not follow a
        ternary. So the value ends at the condition and the else-branch literal
        survives. MEASURED IDENTICAL BEFORE AND AFTER this exemption, and it
        happens for `other.lower()` and a bare `x` just the same, which is what
        proves the exemption is not involved -- the first draft of the test
        above asserted this line was redacted, and it never was.

        Recorded in HARNESS.md's residual list rather than fixed here: a
        ternary is a third operand shape and belongs with the other unfollowed
        forms, not bolted onto a fix for something else.
        """
        for line in (
            'password = password.lower() if x else "AbCdEf0123456789ZzYyXx"',
            'password = other.lower() if x else "AbCdEf0123456789ZzYyXx"',
            'password = x if y else "AbCdEf0123456789ZzYyXx"',
        ):
            with self.subTest(line=line):
                self.assertIn(
                    "AbCdEf0123456789ZzYyXx", claude_review.redact(line),
                    "the ternary is now followed -- good, but HARNESS.md and "
                    "this test still say it is not, and one of them is wrong",
                )

    def test_a_long_literal_argument_is_not_punctuation(self):
        # `.strip("'\"")` and `.split(",")` are punctuation and stay exempt. An
        # argument carrying an eight-character alphanumeric run is doing
        # something other than trimming, and is redacted.
        self.assertRedacted('token = token.replace("AbCdEf0123456789ZzYyXx", "")')
        self.assertUnchanged('token = token.split(",")')

    def test_the_name_must_match_whole_and_a_prefix_does_not(self):
        # The key group matches the TAIL of a name, so `client_secret` is
        # matched as `secret` and the backreference then looks for `secret`
        # where the value says `client_secret`. Not exempt, so it is decided as
        # an ordinary value; it holds no token, so since capaz#107 it comes back
        # as written and the over-redaction main had here is gone.
        self.assertUnchanged('client_secret = client_secret.encode("utf-8")')

    def test_an_ordinary_secret_is_untouched_by_any_of_this(self):
        for line in (
            'token = "AbCdEf0123456789ZzYyXx"',
            "password: hunter2Xk9mP2qR7vL4",
            'api_key = get_key("AbCdEf0123456789ZzYyXx")',
        ):
            with self.subTest(line=line):
                self.assertRedacted(line)


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
            # THE SPACES AFTER THE SEPARATOR. Two `[ \t]*` around the optional
            # line break could share one run of spaces in about K*K/2 ways, and
            # a value the rule then declined made the engine try every one:
            # 8,000 spaces took 36 to 41 seconds. An exempt number and no value
            # at all are the two ways to decline.
            "spaces before an exempt number": "token =" + " " * 50_000 + "4",
            "spaces and no value": "password = " + " " * 50_000,
            # `_VALUE_END` reads a run of closers before the value is ended or
            # declined, so each reading of the spaces paid for the whole run:
            # 1,000 spaces and a million closers took 55 seconds. The run ends
            # the number at the end of the text and is declined before `x`,
            # and every exemption that shares the end gets the declined shape.
            "spaces before an exempt number and a closer run": (
                "token = " + " " * 1_000 + "4" + "]" * 1_000_000
            ),
            "spaces, a number and a closer run it declines": (
                "token = " + " " * 1_000 + "4" + "]" * 1_000_000 + "x"
            ),
            "spaces, a self-reshape and a closer run it declines": (
                "token = " + " " * 1_000 + "token.strip()" + "]" * 1_000_000 + "x"
            ),
            "spaces, an env lookup and a closer run it declines": (
                "token = " + " " * 1_000 + "process.env.TOKEN" + "}" * 1_000_000 + "x"
            ),
            "spaces, a pattern and a closer run it declines": (
                "token = " + " " * 1_000 + "(?:a|b)" + "]" * 1_000_000 + "x"
            ),
            # The exemption added a bounded group match with one level of
            # nested-paren tolerance, so these are its own pathological shapes.
            "a huge alternation under a secret name": "password=(?:" + "a|" * 200_000 + "b)",
            "a huge character class": "password=([" + "a" * 500_000 + "])",
            "many groups on one line": "password=" + "(.*)" * 200_000,
            "a placeholder repeated": "password=" + "{v}" * 200_000,
            # The placeholder's quote depends on what is open on the line, and
            # asking that per match with a back-scan is quadratic: it is how the
            # first version of the fix took 119 seconds on "many keys, many
            # spaces" above. These two are the same trap aimed at the carried
            # cursor -- many bare values on ONE line, and many on their own
            # lines, so a cursor that failed to advance or reset per line would
            # be timed here rather than found in a review.
            "many bare values on one line": "token=a " * 200_000,
            "many bare values on their own lines": "token=a\n" * 200_000,
            # The config-file cursor is the same kind of carried answer, so the
            # same trap: many file headers, each with keys under it, code and
            # config in turn, and many keys under one header far below it.
            "many files with keys under each": (
                "diff --git a/a.env b/a.env\n+token=a\n"
                "diff --git a/a.py b/a.py\n+token=a\n"
            ) * 50_000,
            "many keys far below one config header": (
                "diff --git a/a.yml b/a.yml\n" + "+token: a\n" * 200_000
            ),
            # And with a quote actually open, so the counting runs rather than
            # finding nothing to count.
            "many bare values inside one string": 'f("' + "token=a " * 200_000 + '")',
            # The concatenation chain and the regex-literal exemption are the
            # newest bounded repeats, so these are their own pathological shapes.
            "a concatenation with no end": 'password="a"' + ' + "b"' * 200_000,
            "a concatenation of bare tokens": "password=a" + " + b" * 200_000,
            "an unterminated regex literal": "password=/" + "a" * 1_000_000,
            "a regex literal that never closes its class": "password=/[" + "a" * 500_000,
            "a regex body of escapes": "password=/" + "\\d" * 200_000,
            # The concat OPERAND is a call or subscript, which stacks a nested
            # paren matcher under a repeat. Raised in review on #182: the shapes
            # above stress flat repetition and none of them stress this. They
            # are cheap and they are the file's own bar -- the newest bounded
            # repeats get their own pathological shapes.
            "unbalanced open parens after a concat": 'password="x" + f' + "(" * 200_000,
            "a deeply nested call in a concat operand": (
                'password="x" + f' + "(" * 50_000 + ")" * 50_000
            ),
            "many call operands chained": 'password="x"' + " + f(a)" * 100_000,
            "an unbalanced subscript operand": 'password="x" + a[' + "(" * 200_000,
            # The subscript branch of the reshape exemption gained the same
            # lookahead the call branch has (kit #212). Each bracket's scan is
            # bounded by its own closing bracket, so a chain of them must stay
            # linear; these are the shapes that would show it if it did not.
            "a reshape of two hundred thousand subscripts": "token = token" + "[0]" * 200_000,
            "a reshape of many short-keyed subscripts": "token = token" + '["ab"]' * 100_000,
            "a reshape subscript that never closes": 'token = token["' + "a" * 1_000_000,
            "a reshape subscript with a megabyte inside": 'token = token["' + "a" * 500_000 + '"]',
            "a concat operand with a megabyte of arguments": (
                'password="x" + f(' + "a," * 500_000 + ")"
            ),
            # And the bare-value tempering, which asks a lookahead per operator
            # character. Ordinary characters take the branch that asks nothing.
            "a megabyte of operators in a bare value": "password=" + "+.&" * 300_000,
            "operators each followed by a near-literal": "password=" + '+"a' * 200_000,
            # The operand prefix adds an optional two-letter run in front of
            # every literal the chain can reach, so a run of near-operands that
            # each fail late is its own shape.
            "prefixed near-operands": "password=" + '+ab"x' * 200_000,
            "prefixes with no literal after them": "password=" + "+ab" * 300_000,
            # The operator set grew (`%`, `||`, `??`, `or`, `and`) and the
            # bare-boundary lookahead grew with it, so the new members get their
            # own shapes rather than inheriting the confidence of the old ones.
            # Raised in review on #192; measured worst case 0.40s.
            #
            # THESE LINES ARE UNREADABLE IN A REVIEWED DIFF, AND THAT IS THE
            # REDACTOR WORKING. Each one is `password=` followed by a
            # concatenation, so `redact()` takes it whole and the line reaches
            # the model as `"label": "password=<REDACTED>" * 300_000` -- with
            # the operator it exists to stress edited out. Two review rounds on
            # #192 read that and reported the payloads as stale copy-paste,
            # which is the correct reading of what they were shown.
            #
            # It cannot be fixed by renaming: a shape that does not start with a
            # key the table knows never enters the rule these tests exist to
            # stress. So it is said here instead, in the hunk itself, where the
            # next reviewer will be looking. Verify with
            # `git show <sha>:.github/scripts/test_claude_review.py`, not the diff.
            "a megabyte of || with no literal": "password=" + "a||" * 300_000,
            "|| each followed by a near-literal": "password=" + '||"a' * 200_000,
            "?? repeated": "password=" + "a??" * 300_000,
            "% repeated": "password=" + "a%" * 400_000,
            "word operators repeated": "password=" + "a or " * 200_000,
            "and repeated": "password=" + "a and " * 200_000,
            "mixed operator soup": "password=" + "a||b??c%d or " * 80_000,
            # And the backtick, the newest quote character.
            "a backtick never closed": "password=`" + "a" * 1_000_000,
            "many backtick literals": "password=" + "`a`+" * 200_000,
            "nested backtick interpolation": "password=`${`" * 100_000,
            # THE SELF-RESHAPE CHAIN, whose `(?:...|...)+` is the one nested
            # quantifier in this file and so the only shape here that could
            # backtrack exponentially. Raised in review on #201, which asked
            # for a linear-time test before the alternation merged.
            #
            # The last two are the ones that matter. A chain that MATCHES is
            # cheap however long it is; a chain that matches and then fails at
            # the terminator is what makes an engine try every way of splitting
            # the `+` -- so they end in `!`, which `(?=[,;)\]}]|[ \t]*$)` does
            # not accept, and in an unclosed call the inner group cannot close.
            "a long self-reshape chain": "token=token" + ".strip" * 100_000,
            "subscript chain": "token=token" + "[0]" * 100_000,
            "alternating attribute and subscript": "token=token" + ".a[0]" * 60_000,
            "chain that fails at the terminator": "token=token" + ".a" * 100_000 + "!",
            "chain of unclosed calls": "token=token" + ".a(" * 100_000,
            # THE PARAMETER EXPANSION (capaz#21). Both of its branches read to a
            # closing brace under a bound of 200, so a brace that never closes, a
            # message that never ends and a line of them are its own shapes.
            "an expansion that never closes": "password=${A" + "b" * 1_000_000,
            "a message that never ends": "password: ${A:?" + "b" * 1_000_000,
            "many expansions that do not end the value": "password: ${A:?m}x " * 100_000,
            "many inner keys refused at the separator": "x:${PASSWORD:?m}" * 100_000,
            # THE NUMBER EXEMPTION (`_NUMBER`). Its lookahead reads a digit run
            # and an optional annotation, so a run that never ends its value, a
            # long union in front of one and a file of constants are its shapes.
            "a number that never ends its value": "token=" + "1" * 1_000_000 + "x",
            "a number of underscores": "token=1" + "_" * 1_000_000 + "x",
            "a long type union before a number": "token: " + "int | " * 100_000 + "= 4",
            "many numeric token constants": "CHARS_PER_TOKEN = 4\n" * 200_000,
            # `_VALUE_END` reads a run of closers, spaced or not, before it
            # decides; a run that never reaches an end is its shape.
            "a closer run that never ends its value": "token=4" + "] " * 300_000 + "x",
            "a closer run of spaces": "token=4]" + " " * 1_000_000 + "x",
            # `_BARE_CHAR` asks at every character whether a key and its
            # separator start there. Key names that never reach a separator,
            # a run of names with prefixes in common, a name before a megabyte
            # of blanks, and a run of glued keys that each end a value.
            "key names that never reach a separator": "token=4]" + "password" * 150_000,
            "a run of overlapping key names": "token=x" + "secret_key" * 100_000,
            "a key name before a megabyte of blanks": "token=xpassword" + " " * 1_000_000 + "y",
            "a run of glued keys": "token=x" + "password:" * 100_000,
            "a closer then a megabyte of bare text": "token=x]" + "a" * 1_000_000,
            "a closer run then a key": "token=x" + "]" * 300_000 + "password: y",
            # `_VALUE_END` reads a glued closer run, then blanks, before it
            # decides, and the env lookup's name can give letters back.
            "a glued closer run that never ends": "token=token.strip()" + "]" * 1_000_000 + "x",
            "closers then blanks then a word": "token=4" + "}" * 300_000 + " " * 300_000 + "x",
            "a closer run then a quote": "token={v}" + "}" * 1_000_000 + "'",
            "a long env name glued to a closer": "token=process.env." + "A" * 1_000_000 + "]x",
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
    set of type words is never a secret. A typed DEFAULT is decided like any
    other value, and since capaz#107 the annotation in front of it always goes
    back as written: `password: str = "hunter2"` is
    `password: str = "<REDACTED>"`, and `secret: bytes = field(repr=False)` is
    unchanged.
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

    def test_a_typed_default_keeps_its_annotation(self):
        # A literal or a token default is hidden; the annotation is not.
        cases = {
            'password: str = "hunter2"': 'password: str = "<REDACTED>"',
            "api_key: str = abc123def456ghi789": 'api_key: str = "<REDACTED>"',
            'token: str | None = "x",': 'token: str | None = "<REDACTED>",',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), want)
        # A default that is a name, a call or a short number is code.
        for line in (
            "token: str | None = None",
            'password: str = os.getenv("X")',
            "password: int = 5)",
            "api_key: str | None = None,",
            "secret: bytes = field(repr=False)",
        ):
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), line)

    def test_only_the_whole_word_is_a_type(self):
        # A word that starts with a type word is a value, decided as one: a
        # name comes back (capaz#107), a token goes.
        for line in ("password: strong_pw", "password: stringy"):
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), line)
        cases = {
            "password: strXk9mP2qR7vL4wN8": 'password:"<REDACTED>"',
            'password: "str"': 'password:"<REDACTED>"',
            # A quoted literal under a secret name stays redacted: the
            # fail-closed side of this rule.
            '{ token: "h", keyName: "k" }': '{ token:"<REDACTED>", keyName: "k" }',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), want)


class CodeUnderASecretNameReachesTheModelAsWritten(unittest.TestCase):
    """A name, a call, an `await` and a type annotation hold no secret.

    The assignment rule hid every value under a secret name, so on capaz#107
    three ordinary lines reached the model as `kid, secret="<REDACTED>"
    owner.fetchrow(...)`, `secret:"<REDACTED>"` and `token="<REDACTED>"`, and the
    reviewer reported each one as a syntax error and a blocking issue. A value
    is now hidden only when it sits in a config file, is a quoted literal, sits
    inside a double-quoted string, carries a literal in its chain, or holds a
    high-entropy token. Exact output, both directions.
    """

    def test_the_three_lines_from_capaz_107_come_back_as_written(self):
        for line in (
            '        kid, secret = await owner.fetchrow("SELECT kid, secret FROM signing_keys")',
            "    secret: bytes = field(repr=False)",
            "    token = request_context.set(ctx)",
            # A keyword argument is a name and a value, not one 16-character
            # token: `=` splits it before the entropy test reads it.
            "    token = client.auth(api_version=2023)",
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))

    def test_the_real_secret_shapes_are_still_masked(self):
        # Built at runtime, like WRITTEN_OUT_KEYS: this source carries no key
        # shape for the secret scan to read.
        sk = "sk-" + "ant-api03-" + "AbCdEf0123456789ZzYyXx"
        hex40 = "0123456789abcdef" * 2 + "01234567"
        pem = (
            "-----BEGIN " + "RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn\n"
            "-----END " + "RSA PRIVATE KEY-----"
        )
        cases = (
            # A quoted literal is always hidden, whatever it holds.
            (f'OPENAI_API_KEY = "{sk}"', 'OPENAI_API_KEY="<REDACTED>"'),
            (f'client = Anthropic(api_key="{sk}")', 'client = Anthropic(api_key="<REDACTED>")'),
            (f'GITHUB_TOKEN = "{hex40}"', 'GITHUB_TOKEN="<REDACTED>"'),
            # A bare token is hidden by the entropy test.
            (f"GITHUB_TOKEN={hex40}", 'GITHUB_TOKEN="<REDACTED>"'),
            # With no key name at all, the vendor prefix still catches it.
            (f"see {sk} above", "see sk-<REDACTED> above"),
            # The PEM rule needs no key name either.
            (pem, "<REDACTED_PRIVATE_KEY>"),
            (f"key: |\n{pem}\nnext: 1", "key: |\n<REDACTED_PRIVATE_KEY>\nnext: 1"),
        )
        for line, want in cases:
            with self.subTest(line=line):
                self.assertEqual(want, claude_review.redact(line))

    def test_a_key_inside_a_declined_value_is_still_redacted(self):
        # The declined value was consumed by the match, so it is scanned again:
        # without that, the inner literal would reach the model.
        cases = (
            ('token = login(password="hunter2")', 'token = login(password="<REDACTED>")'),
            (
                'secret = connect(host=h, api_key="hunter2").session()',
                'secret = connect(host=h, api_key="<REDACTED>").session()',
            ),
            ("token = a(password=b(secret='x'))", "token = a(password=b(secret='<REDACTED>'))"),
        )
        for line, want in cases:
            with self.subTest(line=line):
                self.assertEqual(want, claude_review.redact(line))

    def test_a_bare_value_inside_a_double_quoted_string_is_text(self):
        # A connection string is literal text, not code, so a short password in
        # it is hidden; the placeholder keeps the enclosing string whole.
        self.assertEqual(
            "conn = \"Server=db;Password='<REDACTED>';\"",
            claude_review.redact('conn = "Server=db;Password=hunter2;"'),
        )

    def test_a_bare_value_in_a_config_file_is_the_data_itself(self):
        # Rule (0): in a dotenv, YAML, INI or properties file a bare value is
        # the secret, not code that reads one, so it is hidden as it was before
        # capaz#107. The file is the one the nearest header above names. One
        # diff, config and code in turn, so the cursor has to switch back.
        diff = (
            "diff --git a/infra/.env.example b/infra/.env.example\n"
            "+DB_PASSWORD=hunter2\n"
            "diff --git a/infra/compose.yml b/infra/compose.yml\n"
            "+      POSTGRES_PASSWORD: changeme\n"
            "+      DB_PASSWORD: ${DB_PASSWORD:?set it}\n"
            "diff --git a/app/db.py b/app/db.py\n"
            "+    secret: bytes = field(repr=False)\n"
            "+    password = settings_password\n"
            # A declined call is scanned again for a key of its own, and that
            # inner pass must read this file as code too: its bare inner value
            # stays, its quoted one goes.
            "+    token = login(password=hunter2)\n"
            '+    token = login(password="hunter2")\n'
            "diff --git a/app/settings.ini b/app/settings.ini\n"
            "+password = hunter2\n"
        )
        want = (
            "diff --git a/infra/.env.example b/infra/.env.example\n"
            '+DB_PASSWORD="<REDACTED>"\n'
            "diff --git a/infra/compose.yml b/infra/compose.yml\n"
            '+      POSTGRES_PASSWORD:"<REDACTED>"\n'
            "+      DB_PASSWORD: ${DB_PASSWORD:?set it}\n"
            "diff --git a/app/db.py b/app/db.py\n"
            "+    secret: bytes = field(repr=False)\n"
            "+    password = settings_password\n"
            "+    token = login(password=hunter2)\n"
            '+    token = login(password="<REDACTED>")\n'
            "diff --git a/app/settings.ini b/app/settings.ini\n"
            '+password="<REDACTED>"\n'
        )
        self.assertEqual(want, claude_review.redact(diff))
        # The codebase snapshot names its file the other way.
        for section, want in (
            (
                "--- FILE: src/app.properties ---\ndb.password=hunter2\n",
                '--- FILE: src/app.properties ---\ndb.password="<REDACTED>"\n',
            ),
            (
                "--- FILE: src/db.py ---\npassword = settings_password\n",
                "--- FILE: src/db.py ---\npassword = settings_password\n",
            ),
        ):
            with self.subTest(section=section):
                self.assertEqual(want, claude_review.redact(section))

    def test_the_config_file_list_is_what_it_says(self):
        for path in (
            ".env", ".env.local", "infra/.env.example", "key-broker/litellm.env.example",
            "compose.yml", "deploy/values.YAML", ".github/workflows/ci.yml",
            "setup.cfg", "app/settings.ini", "nginx/site.conf", "src/app.properties",
        ):
            with self.subTest(path=path):
                self.assertTrue(claude_review._CONFIG_FILE.search(path))
        for path in (
            ".envrc", "env.py", "config.env.ts", "src/environment.ts",
            "settings.json", "Dockerfile", "deploy.sh", "app/config.py",
        ):
            with self.subTest(path=path):
                self.assertFalse(claude_review._CONFIG_FILE.search(path))

    def test_is_token_draws_the_line_where_it_says(self):
        tokens = (
            "0123456789abcdef",  # sixteen hex characters
            "wJalrXUtnFEMI/K7MDENG",
            "hunter2Xk9mP2qR7vL4",
            "123e4567-e89b-12d3-a456-426614174000",
            # A dot inside a token: a segment that opens with a digit is not
            # an identifier, so the whole run is not a dotted name.
            "aB3dE5fG.9hJkL1mN",
        )
        words = (
            "0123456789abcde",  # fifteen
            "request_context.set",  # no digit
            "settings.SIGNING_KEY_V2",  # a dotted name
            "2026-01-01T00:00:00Z",  # a timestamp, 2.5 bits per character
            "aaaaaaaa11111111",  # repetitive
            "correct-horse-battery",  # a passphrase: no digit
            # THE DOTTED RESIDUAL: a random run whose every dotted segment opens
            # with a letter is shaped exactly like `settings.SIGNING_KEY_V2`, so
            # it reads as a name. Base64, hex and UUIDs carry no dot, and a JWT
            # is caught by its `eyJ` prefix, so this is the shape left.
            "aB3dE5fG.hJ9kL1mN",
        )
        for piece in tokens:
            with self.subTest(piece=piece):
                self.assertTrue(claude_review._is_token(piece))
        for piece in words:
            with self.subTest(piece=piece):
                self.assertFalse(claude_review._is_token(piece))

    def test_the_residuals_are_pinned_not_assumed(self):
        """What the narrowing costs, asserted so HARNESS.md cannot drift.

        Outside a config file, a bare value under a secret name that is shorter
        than 16 characters or looks like a word reads as code by shape, and so
        does a short literal passed as an argument. A generated
        credential is caught by the token test or a vendor prefix. A hand-typed
        one is caught by nothing in CI: TruffleHog matches known credential
        formats, and `hunter2` has none.
        """
        for line in (
            "DB_PASSWORD=hunter2",
            "  password: changeme",
            "DB_PASSWORD=correct-horse-battery",
            "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-correct horse}",
            'password = decrypt("hunter2")',
            'token = os.environ.get("TOKEN", "hunter2")',
            "x('api_key=abc123')",
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))
        # Outside a config file the same shapes are code by rule, whatever the
        # file really holds: a shell script, a Dockerfile, a README's example.
        for path, line in (
            ("deploy.sh", "+export DB_PASSWORD=hunter2"),
            ("Dockerfile", "+ENV DB_PASSWORD=hunter2"),
            ("README.md", "+    password: changeme"),
        ):
            text = f"diff --git a/{path} b/{path}\n{line}\n"
            with self.subTest(path=path):
                self.assertEqual(text, claude_review.redact(text))


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
    # THE FORK-GATED FORM, for jobs that run repo code rather than only calling
    # an API. Keying on visibility alone let a fork PR against a PRIVATE repo
    # run on the self-hosted pool. claude-review.yml does not need it -- it
    # gates forks out at the job level instead. Whitespace-normalised, because
    # it is written as a folded scalar to stay inside the column limit.
    FORK_GATED = (
        "${{ (github.event.repository.visibility == 'public'"
        " || ((github.event_name == 'pull_request'"
        " || github.event_name == 'pull_request_target')"
        " && github.event.pull_request.head.repo.full_name != github.repository))"
        " && 'ubuntu-latest'"
        " || fromJSON('[\"self-hosted\",\"Linux\",\"X64\"]') }}"
    )
    ACCEPTED = (EXPR, FORK_GATED)

    @staticmethod
    def _runs_on(path):
        """Every runs-on value, with folded scalars joined as YAML would.

        STDLIB ONLY, deliberately. This file is vendored into repos that are not
        guaranteed to have PyYAML, so a `yaml.safe_load` here would make a
        travelling single-file test depend on a package its host may not
        install. Raised in review on kit #193.

        A raw regex is not enough either: the fork-gated jobs write the
        expression as `runs-on: >-` over several lines, and a line match returns
        `>-` instead of the value. Folding those continuation lines with spaces
        is precisely what the scalar means, so this compares what the runner
        actually receives.
        """
        found = []
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("runs-on:"):
                continue
            value = stripped[len("runs-on:"):].strip()
            if value not in (">-", ">", "|", "|-"):
                found.append(value)
                continue
            indent = len(line) - len(line.lstrip())
            parts = []
            for cont in lines[i + 1:]:
                if not cont.strip():
                    break
                if len(cont) - len(cont.lstrip()) <= indent:
                    break
                parts.append(cont.strip())
            found.append(" ".join(parts))
        return found

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
                    self.assertIn(line, self.ACCEPTED)

    def test_neither_bare_runner_is_accepted(self):
        for name, path in self.found.items():
            for line in self._runs_on(path):
                for bare in ("ubuntu-latest", "[self-hosted, Linux, X64]"):
                    with self.subTest(workflow=name, bare=bare):
                        self.assertNotEqual(line, bare)


class MessagesEndpointTests(unittest.TestCase):
    """CI is an unattended spender, and its host was hardcoded.

    Every review billed ANTHROPIC_API_KEY straight at Anthropic: no virtual key,
    no team ceiling, no daily cap, no attribution. A runaway review loop would
    have been invisible to every spend control that exists.

    The fix is one env var, so these pin two things: that an unset var behaves
    exactly as before (a repo with no broker must not break), and that a set one
    is composed correctly rather than double-slashed.
    """

    def setUp(self):
        self._saved = os.environ.get("ANTHROPIC_BASE_URL")
        os.environ.pop("ANTHROPIC_BASE_URL", None)

    def tearDown(self):
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        if self._saved is not None:
            os.environ["ANTHROPIC_BASE_URL"] = self._saved

    def test_unset_is_anthropic_direct(self):
        # the pre-existing behaviour, unchanged, for every repo without a broker
        self.assertEqual(
            claude_review.messages_endpoint(), "https://api.anthropic.com/v1/messages"
        )

    def test_set_routes_to_the_broker(self):
        os.environ["ANTHROPIC_BASE_URL"] = "https://llm.example.invalid"
        self.assertEqual(
            claude_review.messages_endpoint(), "https://llm.example.invalid/v1/messages"
        )

    def test_trailing_slashes_do_not_double_up(self):
        for base in ("https://llm.example.invalid/", "https://llm.example.invalid///"):
            with self.subTest(base=base):
                os.environ["ANTHROPIC_BASE_URL"] = base
                self.assertEqual(
                    claude_review.messages_endpoint(),
                    "https://llm.example.invalid/v1/messages",
                )

    def test_blank_falls_back_rather_than_building_a_bare_path(self):
        # an unset repo variable arrives as "", not as absent -- and "/v1/messages"
        # would be a relative URL that fails somewhere far from the cause
        for base in ("", "   "):
            with self.subTest(base=repr(base)):
                os.environ["ANTHROPIC_BASE_URL"] = base
                self.assertEqual(
                    claude_review.messages_endpoint(),
                    "https://api.anthropic.com/v1/messages",
                )

    def test_the_host_is_no_longer_hardcoded_at_the_call_site(self):
        source = Path(__file__).with_name("claude_review.py").read_text(encoding="utf-8")
        # the constant may name it; the request must not
        self.assertNotIn('"https://api.anthropic.com/v1/messages"', source)
        self.assertIn("messages_endpoint()", source)


class AReviewThatDidNotRunIsNotAPass(unittest.TestCase):
    """The one API outcome the classifier could not see.

    Measured on 2026-08-27, kit #129. The broker answered the review call with
    HTTP 429 and a body reading `{"type":"budget_exceeded"}`: the key's $5.00
    ceiling. call_claude returned a body starting "Claude API call failed", which
    matches neither banner, so review_status classified it `ok`, the workflow's
    case branch treated it as green, and the job passed in 14 SECONDS with a
    posted comment saying in plain text that no review had happened.

    That is the same trade this file already refuses twice, for a missing key and
    for a truncated answer: a check that verified an unknown fraction of the diff
    must not report success. A review that verified NONE of it least of all.
    """

    BODIES = (
        (429, '{"error":{"message":"Budget has been exceeded! Key=ci-review",'
              '"type":"budget_exceeded","code":"429"}'),
        (401, '{"error":{"message":"invalid x-api-key","type":"authentication_error"}'),
        (402, '{"error":{"message":"payment required","type":"billing_error"}'),
        (500, '{"error":{"message":"internal server error"}'),
    )

    def _failed_body(self, code, detail):
        return (
            f"{claude_review.FAILED_BANNER} HTTP {code} from the API, so nothing in this"
            f" diff was reviewed.\n\n```text\n{detail}\n```"
        )

    def test_every_http_failure_classifies_as_failed(self):
        for code, detail in self.BODIES:
            with self.subTest(code=code):
                status = claude_review.review_status(self._failed_body(code, detail))
                self.assertEqual(status, claude_review.STATUS_FAILED)

    def test_the_old_body_would_have_passed(self):
        # The exact string this replaces, kept so the regression is documented
        # rather than described. It classified as ok, which is the bug.
        old = "Claude API call failed: HTTP 429.\n\n```text\nbudget_exceeded\n```"
        self.assertEqual(claude_review.review_status(old), claude_review.STATUS_OK)

    def test_a_real_review_is_still_ok(self):
        self.assertEqual(
            claude_review.review_status("## Findings\n\nLine 3 does two things."),
            claude_review.STATUS_OK,
        )

    def test_the_other_two_failure_states_are_unchanged(self):
        self.assertEqual(
            claude_review.review_status(claude_review.EMPTY_BANNER + " stop_reason: end_turn"),
            claude_review.STATUS_EMPTY,
        )
        self.assertEqual(
            claude_review.review_status(claude_review.TRUNCATED_BANNER + " findings follow"),
            claude_review.STATUS_TRUNCATED,
        )

    def test_the_banner_the_script_writes_is_the_banner_it_reads(self):
        # One definition for both directions. A hand-edited literal in either
        # place is how the classifier silently stops matching.
        source = Path(__file__).with_name("claude_review.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('FAILED_BANNER = "'), 1)
        self.assertIn("{FAILED_BANNER} HTTP {exc.code}", source)

    def test_a_real_http_failure_ends_as_a_failed_status(self):
        """The composition, not the two halves.

        Raised in review on kit #130, and it was the right question: the
        classifier was tested alone, the HTTP-error formatting was tested alone,
        and the path between them was an expression inside main(). The reviewer
        could not see main() in the diff and asked whether the fix fired at all.
        This drives the real call_claude with a raised HTTPError and runs its
        actual return value through the real classifier.
        """
        for code in (401, 402, 429, 500):
            with self.subTest(code=code):
                error = urllib.error.HTTPError(
                    url="https://example.invalid/v1/messages",
                    code=code,
                    msg="nope",
                    hdrs=None,
                    fp=io.BytesIO(b'{"error":{"type":"budget_exceeded"}}'),
                )
                raised = mock.patch.object(
                    claude_review._NO_REDIRECT_OPENER, "open", side_effect=error
                )
                keyed = mock.patch.dict(
                    os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False
                )
                with raised, keyed:
                    body = claude_review.call_claude("diff --git a/x b/x\n+1\n")
                self.assertIn(claude_review.FAILED_BANNER, body)
                self.assertEqual(claude_review.status_for(body), claude_review.STATUS_FAILED)

    def test_a_real_review_ends_as_ok_through_the_same_path(self):
        # The other direction, so the seam cannot be fixed by classifying
        # everything as failed.
        payload = json.dumps({
            "content": [{"type": "text", "text": "## Findings\n\nLine 3 does two things."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }).encode()

        class Response:
            def read(self):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(
            claude_review._NO_REDIRECT_OPENER, "open", return_value=Response()
        ), mock.patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False
        ):
            body = claude_review.call_claude("diff --git a/x b/x\n+1\n")
        self.assertEqual(claude_review.status_for(body), claude_review.STATUS_OK)

    def test_a_review_with_no_key_is_not_reported_as_a_pass(self):
        # Measured before the fix: this body matched no banner case and
        # status_for returned "ok", which the workflow gate accepts. A Dependabot
        # pull_request is served the Dependabot secret store and never receives
        # ANTHROPIC_API_KEY, so "green over an unread diff" was that event's
        # permanent state.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            body = claude_review.call_claude("diff --git a/x b/x\n+1\n")
        self.assertIn(claude_review.NO_KEY_BANNER, body)
        self.assertEqual(claude_review.status_for(body), claude_review.STATUS_NO_KEY)
        self.assertNotEqual(claude_review.status_for(body), claude_review.STATUS_OK)

    def test_every_banner_constant_is_known_to_the_classifier(self):
        """THE CLASS, not the instance.

        EMPTY_BANNER, TRUNCATED_BANNER and FAILED_BANNER were constants and all
        three had a case. The missing-key banner was a bare inline string at the
        top of call_claude, and it was the only one the classifier did not know
        about -- which is the entire defect, and has nothing to do with keys.

        The default cannot be flipped to make this safe: a real review body is
        arbitrary text, so OK is not positively identifiable and the fall-through
        must stay OK. Enumerating the banners is therefore the only defence, and
        it fails the next time someone inlines one instead of naming it.
        """
        banners = {
            name: value
            for name, value in vars(claude_review).items()
            if name.endswith("_BANNER") and isinstance(value, str)
        }
        self.assertGreaterEqual(
            len(banners), 4,
            f"expected at least the four known banners, found {sorted(banners)}",
        )
        for name, banner in sorted(banners.items()):
            with self.subTest(banner=name):
                status = claude_review.status_for(
                    "## Claude Code Review\n\n" + banner + " trailing detail"
                )
                self.assertNotEqual(
                    status, claude_review.STATUS_OK,
                    f"{name} classifies as OK, so a review that hit it would pass. "
                    "Give it a case in review_status().",
                )

    def test_the_workflow_fails_the_check_on_that_status(self):
        # BOTH LAYOUTS, because the suite really does travel and they differ.
        # In the kit's template directory the workflow sits beside this file; after
        # sync-standards installs it, the test is in <repo>/.github/scripts/ and the
        # workflow in <repo>/.github/workflows/. Checking only the first meant this
        # test could never pass in a managed repo -- and since a failed suite writes
        # no review status, the gate then failed every PR with 'nothing proves a
        # review ran'. Kit CI could not see it: it runs this copy in place.
        here = Path(__file__).resolve()
        candidates = [
            here.with_name("claude-review.yml"),
            here.parent.parent / "workflows" / "claude-review.yml",
        ]
        workflow = next((c for c in candidates if c.is_file()), None)
        self.assertIsNotNone(
            workflow, "claude-review.yml not found in either layout: %s" % candidates
        )
        text = workflow.read_text(encoding="utf-8")
        case = text.split('case "$status" in', 1)[1].split("esac", 1)[0]
        self.assertIn("failed)", case, "the workflow has no branch for a failed review")
        branch = case.split("failed)", 1)[1].split(";;", 1)[0]
        self.assertIn("exit 1", branch, "a failed review does not fail the check")
        # And it must not be swept in with the green ones.
        green = case.split(")", 1)[0]
        self.assertNotIn("failed", green)



class ARegexNamesASecretWithoutContainingOne(unittest.TestCase):
    """Measured on gestalt-workframe-edu#605, four consecutive review rounds.

        KEY_LINE = re.compile(r"^OPENROUTER_API_KEY=(.*)$", re.M)

    reached the model as `^OPENROUTER_API_KEY=<REDACTED>` and came back as a
    BLOCKING finding -- "there is no capture group, .group(1) will raise" --
    four times, each answered with the pushed blob and py_compile output, each
    time re-reported by the next round. The f-string form drew the same verdict:
    "this lambda ignores new_key and writes a fixed string to production".

    Same category as `${{ secrets.X }}`: the line names a secret and holds none,
    and redacting it turns working code into something the model reports as
    broken, spending the review on the artifact rather than the diff.
    """

    def assertUnchanged(self, text):
        self.assertEqual(claude_review.redact(text), text)

    def assertRedacted(self, text):
        self.assertIn("<REDACTED>", claude_review.redact(text))

    # ---- the shapes that broke -------------------------------------------
    def test_a_capture_group_survives(self):
        self.assertUnchanged(r'KEY_LINE = re.compile(r"^OPENROUTER_API_KEY=(.*)$", re.M)')

    def test_a_character_class_group_survives(self):
        self.assertUnchanged(r'PAT = re.compile(r"api_key=([^\"]*)")')

    def test_a_non_capturing_group_survives(self):
        self.assertUnchanged(r'PAT = re.compile(r"token=(?:abc|def)")')

    def test_an_fstring_placeholder_survives(self):
        self.assertUnchanged('return f"OPENROUTER_API_KEY={new_key}"')

    def test_a_format_placeholder_survives(self):
        self.assertUnchanged('line = "password={value}".format(value=v)')

    def test_a_replacement_template_survives(self):
        self.assertUnchanged(r'text = re.sub(r"^SECRET=(.*)$", f"SECRET={new}", text)')

    # ---- and it still cannot hide anything --------------------------------
    #
    # Since capaz#107 a bare value is hidden only when it holds a token, so the
    # values below are tokens. What each case pins is that the exemption does
    # not swallow them; a short word in the same place is code either way.
    def test_a_parenthesised_literal_is_still_redacted(self):
        """No metacharacter, so it is a value in brackets, not a pattern."""
        self.assertRedacted("password=(hunter2Xk9mP2qR7vL4)")

    def test_a_braced_json_value_is_still_redacted(self):
        self.assertRedacted("token={k:hunter2Xk9mP2qR7vL4}")

    def test_a_placeholder_with_a_suffix_is_still_redacted(self):
        """The exemption ends the value; anything after it is a value."""
        self.assertRedacted('api_key="(.*)hunter2"')

    def test_two_placeholders_are_not_one_name(self):
        self.assertRedacted("secret={a}{hunter2Xk9mP2qR7vL4}")

    def test_a_call_is_code(self):
        """The call rule owns this shape: a call with no token comes back."""
        self.assertUnchanged('brokerApiKey: resolveKey("LITELLM_API_KEY"),')

    def test_an_ordinary_secret_is_untouched_by_any_of_this(self):
        for line in [
            "password: hunter2Xk9mP2qR7vL4",
            'API_KEY="sk-abcdefghijklmnopqrst"',
            "client_secret => abc123def456ghi789",
            'DB_PASSWORD="correct-horse-battery"',
        ]:
            with self.subTest(line=line):
                self.assertRedacted(line)

    def test_a_base64_secret_in_brackets_is_not_a_regex(self):
        """The hole a metacharacter test left open.

        `+`, `/`, `.` and `=` are ordinary base64, so "contains a metacharacter"
        exempted a SECRET in brackets -- turning a false positive into a false
        negative, which is the worse direction. A group now has to contain a
        regex IDIOM, not a character regexes happen to use.
        """
        for line in [
            "token=(AbC123+/XyZ789qW==)",
            "api_key=(a1+b2+c3+d4+e5+f6+g7+h8)",
            "password=(some.9Xk2mP7qR4vL.here)",
            "secret=(a1|b2|c3|Xk9mP2qR7vL4)",
            "client_secret=(hunter2Xk9mP2qR7vL4*)",
        ]:
            with self.subTest(line=line):
                self.assertRedacted(line)

    def test_a_secret_cannot_ride_along_as_a_suffix(self):
        """The suffix class accepted up to eight LETTERS.

        So `token=(?:a|b)SECRETAB` was exempt with the secret inside the matched
        span -- the exemption hiding a value, which is the one thing it must not
        do. The suffix is now a quantifier, a counted repeat, an anchor escape or
        a dollar, and nothing else.
        """
        for line in [
            "token=(?:a|b)SECRETAB9Xk2mP7qR4",
            "api_key=(.*)hunter2Xk9mP2qR7vL4",
            "password=([a-z])abcdefgh9Xk2mP7q",
        ]:
            with self.subTest(line=line):
                self.assertRedacted(line)

    def test_a_real_quantified_group_still_survives(self):
        for line in [
            r'PAT = re.compile(r"token=(.*)+")',
            r'PAT = re.compile(r"api_key=(\w){2,4}")',
            r'PAT = re.compile(r"secret=(.*)$")',
            r'PAT = re.compile(r"password=(.+)\b")',
        ]:
            with self.subTest(line=line):
                self.assertUnchanged(line)

    def test_a_group_prefix_is_not_evidence_of_a_pattern(self):
        """`(?:` looks like regex syntax and is free to type around a secret.

        The first cut exempted any group opening with `?`, so `token=(?:hunter2)`
        was exempt while holding a literal. A `(?...)` group qualifies only when
        it also carries an alternation, which is what grouping is FOR.
        """
        for line in [
            "token=(?:hunter2Xk9mP2qR7vL4)",
            "api_key=(?:AbC123XyZ789qW)",
            "password=(?=hunter2Xk9mP2qR7vL4)",
            "secret=(?P<x>hunter2Xk9mP2qR7vL4)",
        ]:
            with self.subTest(line=line):
                self.assertRedacted(line)

    def test_a_grouped_alternation_is_still_a_pattern(self):
        for line in [
            r'PAT = re.compile(r"token=(?:abc|def)")',
            r'PAT = re.compile(r"api_key=(?:a|b|c)")',
            r'PAT = re.compile(r"secret=(?P<v>.*)")',
        ]:
            with self.subTest(line=line):
                self.assertUnchanged(line)

    def test_a_braced_token_with_capitals_is_not_a_placeholder(self):
        """A placeholder is a variable name; this is a value that looks like one."""
        for line in [
            "token={SomeVaultToken123}",
            "api_key={ABCDEF123456GHIJ78}",
            "password={Hunter2Xk9mP2qR7vL4}",
        ]:
            with self.subTest(line=line):
                self.assertRedacted(line)

    def test_the_real_placeholders_still_survive(self):
        for line in [
            'return f"OPENROUTER_API_KEY={new_key}"',
            'line = "password={value}"',
            'x = f"token={t}"',
        ]:
            with self.subTest(line=line):
                self.assertUnchanged(line)

    def test_the_real_patterns_still_survive(self):
        for line in [
            r'PAT = re.compile(r"^OPENROUTER_API_KEY=(.*)$")',
            r'PAT = re.compile(r"api_key=([^\"]*)")',
            r'PAT = re.compile(r"token=(?:abc|def)")',
            r'PAT = re.compile(r"secret=(\w+)")',
            r'PAT = re.compile(r"password=(.+)$")',
        ]:
            with self.subTest(line=line):
                self.assertUnchanged(line)

    def test_the_env_lookup_exemption_still_holds(self):
        """The exemption this one was modelled on must not have moved."""
        self.assertUnchanged('token = os.environ.get("GITHUB_TOKEN") or ""')

    # ---- and one delimiter over, the same thing in JavaScript --------------
    def test_a_javascript_regex_literal_survives(self):
        r"""The branch above needs the group to OPEN the value, and a JS regex
        literal keeps it behind a `/`. Measured on this repo's own
        .claude/hooks/check-handoff-language.mjs:699, which reached the model as
        `DEPLOY_SCRIPT_TOKEN="<REDACTED>")deploy\.(?:sh|ps1|py|mjs)$/i;` -- the
        bare branch stopped at the first `)` INSIDE the group, leaving invalid
        JS on a line that runs. That is the failure this whole class exists to
        prevent, and it cost three rounds on #179 for a different line.
        """
        self.assertUnchanged(
            r"const DEPLOY_SCRIPT_TOKEN = /(?:^|[/\\])deploy\.(?:sh|ps1|py|mjs)$/i;"
        )

    def test_the_other_regex_literal_shapes_survive_too(self):
        for line in [
            r"const token = /^\d+$/;",
            r"const token = /[a-z]+/g;",
            r"const token = /^(?:sh|ps1)$/iu;",
            r"const api_key = /.*KEY=(.*)$/;",
            # A class holding the delimiter is why the body atom spells one out.
            r"const secret = /[/\\]tmp/;",
        ]:
            with self.subTest(line=line):
                self.assertUnchanged(line)

    def test_a_value_in_slashes_is_not_a_pattern(self):
        """Same bar as the group branch: no idiom, no exemption."""
        for line in [
            "const token = /hunter2Xk9mP2qR7vL4/;",
            "token = /var/lib/Xk9mP2qR7vL4",
            "password = /abc123def456ghi789/",
        ]:
            with self.subTest(line=line):
                self.assertRedacted(line)

    def test_an_unterminated_slash_is_not_a_literal(self):
        """The literal has to CLOSE, or every path under a secret name is one."""
        self.assertRedacted(r"token = /^\d+abc123def456")


class AConcatenatedSecretGoesWholeNotHalf(unittest.TestCase):
    """`api_key = "a" + "b"` was redacted down to its FIRST fragment.

    Measured on 2026-08-31 against the table as it stood:

        const apiKey = "sk-ant-" + "AbCdEf0123456789ZzYyXx";
        ->  const apiKey="<REDACTED>" + "AbCdEf0123456789ZzYyXx";

    The single-literal form of the same line redacts correctly, so this was
    specific to concatenation: the value ended at the first closing quote and
    the rest of the expression reached the model verbatim. That is the
    UNDER-redaction direction -- a leak, not a display cost -- and it was named
    in neither this file nor HARNESS.md's list of known residual gaps, so
    nothing recorded it in either direction.

    Two pieces answer it. A CHAIN, which carries the value through `+` (JS, TS,
    Python, C#, PowerShell) and through `.`/`..` to a quoted operand (PHP, Lua).
    A BRIDGE, for the same expression written INSIDE an enclosing string, where
    the operator hides between two quotes belonging to different literals and
    the chain never sees it.
    """

    SECRET = "AbCdEf0123456789ZzYyXx"

    def assertRedactsTo(self, line, want):
        self.assertEqual(want, claude_review.redact(line))

    # ---- the three measured shapes ----------------------------------------
    def test_a_js_const_built_from_two_literals(self):
        self.assertRedactsTo(
            'const apiKey = "sk-ant-" + "AbCdEf0123456789ZzYyXx";',
            'const apiKey="<REDACTED>";',
        )

    def test_the_same_expression_inside_an_enclosing_string(self):
        # The bridge. The quote that opens the value is the ENCLOSING literal's
        # CLOSING quote, so the replacement leaves it off and the two halves
        # rejoin into the one string the line already was.
        self.assertRedactsTo(
            'writeFileSync(f, "ANTHROPIC_API_KEY=" + "AbCdEf0123456789ZzYyXx");',
            'writeFileSync(f, "ANTHROPIC_API_KEY=<REDACTED>");',
        )

    def test_a_python_assignment_built_from_two_literals(self):
        self.assertRedactsTo(
            'password = "Ab" + "AbCdEf0123456789ZzYyXx"',
            'password="<REDACTED>"',
        )

    def test_not_one_of_them_leaves_the_literal_behind(self):
        # THE HALF THAT MATTERS MORE, asserted apart from the exact outputs
        # above so that editing an expected string cannot quietly turn a leak
        # back on while the suite stays green.
        for line in (
            'const apiKey = "sk-ant-" + "AbCdEf0123456789ZzYyXx";',
            'writeFileSync(f, "ANTHROPIC_API_KEY=" + "AbCdEf0123456789ZzYyXx");',
            'password = "Ab" + "AbCdEf0123456789ZzYyXx"',
            "password = 'Ab' + 'AbCdEf0123456789ZzYyXx'",
            'token = "a" + "b" + "AbCdEf0123456789ZzYyXx"',
            '$password = "Ab" . "AbCdEf0123456789ZzYyXx";',
            'local password = "Ab" .. "AbCdEf0123456789ZzYyXx"',
            'x("client_secret=" + "AbCdEf0123456789ZzYyXx")',
        ):
            with self.subTest(line=line):
                self.assertIn(self.SECRET, line, "the case carries no literal, so it pins nothing")
                self.assertNotIn(self.SECRET, claude_review.redact(line))

    # ---- the rest of the chain --------------------------------------------
    def test_the_authors_own_quote_is_still_the_one_that_comes_back(self):
        self.assertRedactsTo(
            "password = 'Ab' + 'AbCdEf0123456789ZzYyXx'",
            "password='<REDACTED>'",
        )

    def test_a_chain_of_three_goes_whole(self):
        self.assertRedactsTo(
            'token = "a" + "b" + "AbCdEf0123456789ZzYyXx"',
            'token="<REDACTED>"',
        )

    def test_a_call_on_the_far_side_goes_with_it(self):
        self.assertRedactsTo('token = "Bearer " + getToken()', 'token="<REDACTED>"')

    def test_the_php_and_lua_operators_reach_a_quoted_operand(self):
        self.assertRedactsTo(
            '$password = "Ab" . "AbCdEf0123456789ZzYyXx";',
            '$password="<REDACTED>";',
        )
        self.assertRedactsTo(
            'local password = "Ab" .. "AbCdEf0123456789ZzYyXx"',
            'local password="<REDACTED>"',
        )

    # ---- and what the chain must NOT swallow ------------------------------
    def test_a_full_stop_in_prose_is_not_a_concatenation(self):
        # `.` is also attribute access and an English full stop, so it reaches a
        # QUOTED operand only. Without that narrowing the sentence after a
        # redacted value in a markdown diff went with it.
        self.assertRedactsTo(
            'The password: "hunter2". Then the user logs in.',
            'The password:"<REDACTED>". Then the user logs in.',
        )

    def test_a_method_call_on_a_literal_goes_with_the_value(self):
        # `.` reaches a literal or a CALL, which is what makes the Java-style
        # `"Ab".concat("hunter2")` go whole. `"abc123".strip()` goes the same
        # way -- over-redaction on a line whose value had already gone, and the
        # price of not leaking the argument.
        self.assertRedactsTo('token = "abc123".strip()', 'token="<REDACTED>"')
        self.assertRedactsTo(
            'password = "Ab".concat("AbCdEf0123456789ZzYyXx");',
            'password="<REDACTED>";',
        )

    def test_the_vbscript_operator_reaches_a_quoted_operand(self):
        # *.vbs is in the reviewer's allow-list, so `&` is a concatenation
        # operator it actually meets.
        self.assertRedactsTo(
            'password = "Ab" & "AbCdEf0123456789ZzYyXx"',
            'password="<REDACTED>"',
        )

    def test_a_seam_that_switches_quote_character(self):
        # The closer comes from the SEAM, not the value: taking the value's
        # would close a double-quoted string with an apostrophe.
        self.assertRedactsTo(
            '''f("password=" + \'AbCdEf0123456789ZzYyXx\')''',
            'f("password=<REDACTED>")',
        )

    def test_a_second_link_after_a_seam(self):
        self.assertRedactsTo(
            'f("password=" + "Ab" + "AbCdEf0123456789ZzYyXx")',
            'f("password=<REDACTED>")',
        )


    def test_the_chain_does_not_cross_a_line_break(self):
        # `+` at the start of the next line is a DIFF MARKER, and a diff is
        # mostly what this function sees.
        self.assertRedactsTo(
            'password: "hunter2"\n+const other = 1',
            'password:"<REDACTED>"\n+const other = 1',
        )

    def test_a_closing_brace_is_not_an_operator(self):
        self.assertRedactsTo('{apiKey:"hunter2"}', '{apiKey:"<REDACTED>"}')

    # ---- the operand the chain could not see -----------------------------
    def test_a_bare_first_operand_butted_against_the_operator(self):
        """The chain hangs off the END of the value, so it only sees what the
        value branch declined to eat -- and the bare class excluded neither `+`
        nor `.` nor `&`, so with no space it swallowed the operator and left the
        chain nothing to attach to. Found in review on #182 and measured the
        same day: five languages, five leaks of a whole literal.

        THE SPACED FORMS WERE ALREADY CORRECT, which is precisely why the tests
        written for the chain missed it -- every one of them had spaces. That is
        the lesson worth more than the fix: a pin written from the shape that
        motivated the change inherits its blind spot.
        """
        cases = {
            'apiKey=prefix+"AbCdEf0123456789ZzYyXx"': 'apiKey="<REDACTED>"',
            'local password=a.."AbCdEf0123456789ZzYyXx"': 'local password="<REDACTED>"',
            '$password=$a."AbCdEf0123456789ZzYyXx";': '$password="<REDACTED>";',
            'token=x&"AbCdEf0123456789ZzYyXx"': 'token="<REDACTED>"',
            'token=f()+"AbCdEf0123456789ZzYyXx"': 'token="<REDACTED>"',
            "password=pre+'AbCdEf0123456789ZzYyXx'": 'password="<REDACTED>"',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertNotIn(self.SECRET, claude_review.redact(line))
                self.assertRedactsTo(line, want)

    def test_the_spaced_forms_of_those_five_still_agree(self):
        # The same five with spaces, which took a different path through the
        # pattern before this change and must still land in the same place.
        for line in (
            'apiKey = prefix + "AbCdEf0123456789ZzYyXx"',
            'local password = a .. "AbCdEf0123456789ZzYyXx"',
            '$password = $a . "AbCdEf0123456789ZzYyXx";',
            'token = x & "AbCdEf0123456789ZzYyXx"',
            'token = f() + "AbCdEf0123456789ZzYyXx"',
        ):
            with self.subTest(line=line):
                self.assertNotIn(self.SECRET, claude_review.redact(line))

    def test_a_bare_value_ENDING_in_an_operator_still_goes_whole(self):
        """The regression the conditional stop exists to avoid.

        `+`, `.` and `=` are ordinary base64, so refusing an operator
        unconditionally would truncate a bare secret one character early and
        send the rest to the model -- turning a fixed leak into a smaller one.
        The stop fires only where a COMPLETE quoted literal follows, which is
        the only place the chain could pick up anyway.

        Each value is a token, which a bare value has to be (capaz#107).
        """
        cases = {
            "x('token=AbC123def456ghi789+')": 'x(\'token="<REDACTED>"\')',
            "password=AbC+dEf/123XyZ789qW==": 'password="<REDACTED>"',
            "token=abc.9Xk2mP7qR4vL.ghi": 'token="<REDACTED>"',
            "token=a1+b2+c3+Xk9mP2qR7vL4": 'token="<REDACTED>"',
            "secret=x1&y2&z3&Xk9mP2qR7vL4": 'secret="<REDACTED>"',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertRedactsTo(line, want)

    def test_an_unspaced_workflow_expression_still_ends_the_chain(self):
        # The `${{ }}` guard lives in the chain's literal, and the bare class
        # consults that same literal -- so an expression after the operator
        # neither stops the value early nor gets consumed. The value in front
        # is a token, which a bare value has to be (capaz#107).
        self.assertRedactsTo(
            'token=hunter2Xk9mP2qR7vL4+"${{ secrets.TOKEN }}"',
            'token="<REDACTED>""${{ secrets.TOKEN }}"',
        )

    # ---- the prefix that made a literal look like a bare value -----------
    def test_a_prefixed_string_literal_is_a_quoted_value(self):
        """`f"..."` read as a bare value ending where the quote began, so only
        the `f` was redacted and the literal went to the model intact.

        Documented as a residual gap first, then raised twice in review on #182
        as the one worth fixing rather than recording -- f-strings are the
        ordinary way to build a string in the language most of this repo is
        written in, so a secret in one is not an exotic shape here.
        """
        cases = {
            'password = f"Ab{x}AbCdEf0123456789ZzYyXx"': 'password="<REDACTED>"',
            'password = r"AbCdEf0123456789ZzYyXx"': 'password="<REDACTED>"',
            'token = b"AbCdEf0123456789ZzYyXx"': 'token="<REDACTED>"',
            "secret = rb'AbCdEf0123456789ZzYyXx'": "secret='<REDACTED>'",
            'var apiKey = $"p{x}AbCdEf0123456789ZzYyXx";': 'var apiKey="<REDACTED>";',
            'var token = @"AbCdEf0123456789ZzYyXx";': 'var token="<REDACTED>";',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertNotIn(self.SECRET, claude_review.redact(line))
                self.assertRedactsTo(line, want)

    def test_a_prefixed_literal_is_a_literal_in_the_CHAIN_too(self):
        """The half the prefix fix missed the first time.

        #182 put the prefix in front of the `qv` group, so a prefixed literal
        was recognised as THE VALUE, and left `_CONCAT_LITERAL` alone -- so a
        prefixed literal as a CHAIN OPERAND still was not, and the chain stopped
        in front of it. The same shape one position over, missed in the commit
        that fixed the shape, and found by an adversarial sweep rather than by
        reading. Recorded because it is the repo's own "fix the class, not the
        instance" constraint failing inside the fix for that class.
        """
        cases = {
            'secret = b"kit-" + b"AbCdEf0123456789ZzYyXx"': 'secret="<REDACTED>"',
            'password = "postgres://" + f"{user}:AbCdEf0123456789ZzYyXx"':
                'password="<REDACTED>"',
            'var apiKey = "sk-" + $"{env}-AbCdEf0123456789ZzYyXx";':
                'var apiKey="<REDACTED>";',
            '"password": "pg-" + f"AbCdEf0123456789ZzYyXx",':
                '"password":"<REDACTED>",',
            'password += "sk-" + b"AbCdEf0123456789ZzYyXx"':
                'password+="<REDACTED>"',
            "token = 'a' + r'AbCdEf0123456789ZzYyXx'": "token='<REDACTED>'",
            'password = "a" . f"AbCdEf0123456789ZzYyXx";': 'password="<REDACTED>";',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertNotIn(self.SECRET, claude_review.redact(line))
                self.assertRedactsTo(line, want)

    def test_a_word_before_a_CHAIN_operands_quote_is_not_a_prefix_either(self):
        """The symmetric half of the abutting rule, raised in review on #190.

        The value branch's boundary was pinned; the chain operand's was only
        implied by the two sharing a spelling. Two branches with the same
        guarantee should have the same test, or one of them can lose the
        guarantee while the shared-spelling assertion stays green.

        `.` and `&` reach a literal or a call, so a spaced word is neither and
        the chain stops with the quotation intact. `+` reaches any operand, so
        it takes the bare word and stops at the quote -- different route, same
        outcome: the quotation is never eaten as though the word were a prefix.
        """
        cases = {
            'The password: "a" . So "hunter2" matters':
                'The password:"<REDACTED>" . So "hunter2" matters',
            'The api_key: "a" & to "hunter2" now':
                'The api_key:"<REDACTED>" & to "hunter2" now',
            'The token: "a" + is "hunter2" here':
                'The token:"<REDACTED>" "hunter2" here',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertRedactsTo(line, want)

    def test_only_one_or_two_letters_abutting_are_a_chain_operands_prefix(self):
        # Abutting and short enough IS a prefix and goes whole; three letters
        # is a word, not a prefix, and the chain stops in front of the quote.
        self.assertRedactsTo('token = "a" + f"hunter2"', 'token="<REDACTED>"')
        self.assertRedactsTo('token = "a" . b"hunter2"', 'token="<REDACTED>"')
        self.assertRedactsTo(
            'token = "a" + abc"hunter2"', 'token="<REDACTED>""hunter2"'
        )

    def test_every_chain_operand_is_built_from_the_shared_constant(self):
        """The first cut re-spelled both of these and pinned the duplicates.
        Review on #190 doubted the stated reason -- that sharing would mean
        reordering three blocks -- and was right: `_LITERAL_PREFIX` depends on
        nothing. So the constants are shared now and this asserts the sharing
        rather than the duplicate, which is the stronger check: a duplicate test
        reports drift after it happens, a reference cannot drift at all.

        Kept as a test because the failure it guards is silent either way -- the
        value branch goes on working while an operand quietly stops matching,
        and nothing else in the suite would name the cause.
        """
        # The prefix reaches EVERY quote character of the chain's literal.
        # Asserted per character rather than as a count: the count was
        # hardcoded at two and went stale the moment the backtick was added,
        # which is the same staleness this test exists to catch, one level up.
        prefix = claude_review._LITERAL_PREFIX
        for quote in ('\\"', "'", "`"):
            with self.subTest(quote=quote):
                self.assertIn(
                    prefix + "?" + quote, claude_review._CONCAT_LITERAL,
                    f"the {quote} form of the chain's literal lost its prefix",
                )
        # DERIVED, NOT HARDCODED. This assertion has now gone stale once (it
        # said two, and the backtick made it three), and review on #192 pointed
        # out that replacing one hardcoded number with another only moves the
        # staleness. Each quote branch carries exactly one prefix and exactly
        # one `${{ }}` guard, so counting one against the other needs no
        # number at all -- and a FOURTH quote style added without a prefix
        # fails here, which the per-character loop above cannot catch.
        self.assertEqual(
            claude_review._CONCAT_LITERAL.count(r"(?![ \t]*\$\{\{)"),
            claude_review._CONCAT_LITERAL.count(prefix),
            "a quote branch has a ${{ }} guard but no literal prefix, or the "
            "reverse -- the two are one per quote character",
        )
        # And the chain's CALL ends the way the value branch's call ends.
        self.assertIn(claude_review._BARE_CHAR, claude_review._CONCAT_CALL)
        self.assertNotIn(
            r"])+[^\s'\",;)]*", claude_review._CONCAT_CALL,
            "the chain's call form is back on the raw bare class, so a call "
            "operand will eat the operator in front of the next literal",
        )

    def test_a_call_operand_ends_where_the_value_branchs_call_ends(self):
        """The asymmetry review on #190 asked about, and it was a live leak.

        The value branch ends its call form with the tempered `_BARE_CHAR`;
        the chain's ended with the raw class, so a call operand mid-chain ate
        the operator in front of the next literal and that literal survived.
        The same shape written as the VALUE was already correct, which is what
        named it as an asymmetry rather than a missing feature.
        """
        cases = {
            'token = "a" + f()+"AbCdEf0123456789ZzYyXx"': 'token="<REDACTED>"',
            'token = "a" + a[0]+"AbCdEf0123456789ZzYyXx"': 'token="<REDACTED>"',
            'token = "a" + f(x).g+"AbCdEf0123456789ZzYyXx"': 'token="<REDACTED>"',
            'token = "a" + f()."AbCdEf0123456789ZzYyXx"': 'token="<REDACTED>"',
            'password = "a" + g()&"AbCdEf0123456789ZzYyXx"': 'password="<REDACTED>"',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertNotIn(self.SECRET, claude_review.redact(line))
                self.assertRedactsTo(line, want)
        # The value-branch twin, which was already right and must stay so.
        self.assertRedactsTo(
            'token = f()+"AbCdEf0123456789ZzYyXx"', 'token="<REDACTED>"'
        )

    def test_the_exemptions_survive_the_operand_prefix(self):
        for line in (
            'token = os.environ.get("GITHUB_TOKEN") or ""',
            r'PAT = re.compile(r"token=(?:abc|def)")',
            'return f"OPENROUTER_API_KEY={new_key}"',
            'token: "${{ secrets.TOKEN }}"',
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))

    def test_a_workflow_expression_operand_still_ends_the_chain(self):
        # The `${{ }}` guard sits after the prefix in both spellings, so a
        # prefix cannot be used to walk past it.
        out = claude_review.redact('token = "abc123" + "${{ secrets.TOKEN }}"')
        self.assertIn("${{ secrets.TOKEN }}", out)
        self.assertNotIn("abc123", out)

    def test_the_prefix_must_abut_the_quote_so_prose_is_untouched(self):
        """The whole safety argument for accepting one or two letters.

        In prose a word before a quotation has a space after it, so the value is
        still the bare word and the quotation is left alone. Only `is"hunter2"`
        would be taken, and that is not English. Since capaz#107 the bare word
        is not hidden either, so the sentence comes back whole.
        """
        line = 'The password: is "hunter2" today'
        self.assertRedactsTo(line, line)

    def test_the_three_exemptions_survive_the_prefix(self):
        # A prefix sits where an exemption's value starts, so each is re-checked
        # rather than assumed: an env lookup, a regex, and an f-string
        # placeholder -- the last being a PREFIXED literal that must still be
        # read as naming a secret rather than holding one.
        for line in (
            'token = os.environ.get("GITHUB_TOKEN") or ""',
            r'PAT = re.compile(r"token=(?:abc|def)")',
            'return f"OPENROUTER_API_KEY={new_key}"',
            'token: "${{ secrets.TOKEN }}"',
        ):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))

    def test_a_workflow_expression_still_ends_the_chain(self):
        # `${{ secrets.X }}` names a secret without holding one, everywhere else
        # in this table; concatenating onto one does not change that.
        self.assertRedactsTo(
            'token = "abc123" + "${{ secrets.TOKEN }}"',
            'token="<REDACTED>" + "${{ secrets.TOKEN }}"',
        )

    def test_the_ordinary_shapes_are_exactly_as_they_were(self):
        # The chain is optional and matches empty, so every pin in this file
        # that has no operator after the value must be untouched by it. These
        # are copied from the classes above on purpose.
        #
        # NOT `f("token=abc123")`, which has no operator either and belongs to
        # test_a_quote_that_closes_an_enclosing_string_is_not_eaten. Re-pinning
        # its exact output here would mean two classes to edit the day the
        # placeholder quote changes, and one of them would be this one, which is
        # not about that.
        for line, want in {
            'password = "hunter2"': 'password="<REDACTED>"',
            'const c = { apiKey: "abc123def456" };': 'const c = { apiKey:"<REDACTED>" };',
            "password: hunter2Xk9mP2qR7vL4, user: bob": 'password:"<REDACTED>", user: bob',
            "login(password=hunter2Xk9mP2qR7vL4, user=u)": 'login(password="<REDACTED>", user=u)',
        }.items():
            with self.subTest(line=line):
                self.assertRedactsTo(line, want)

    def test_the_enclosing_string_wart_is_still_only_a_wart(self):
        # The bare value inside an enclosing string has no operator, so the
        # chain must leave it exactly where the class above found it. Asserted
        # as "the value is gone and nothing rode along", not as an exact string,
        # because the placeholder quote is that class's to decide.
        out = claude_review.redact('f("token=abc123")')
        self.assertNotIn("abc123", out)
        self.assertIn("<REDACTED>", out)
        self.assertTrue(out.startswith('f("token=') and out.endswith(")"), out)


class AnAppendIsAnAssignment(unittest.TestCase):
    r"""`password += "hunter2"` matched NOTHING and went to the model whole.

    Measured on 2026-08-31 alongside the concatenation chain, and it is the same
    leak one operator to the left: the separator ran `\s*` up to `:` or `=`, and
    `\s` does not cross the `+`. So the name matched, the separator did not, and
    the rule that would have redacted the literal never fired at all.

    `+=` and `.=` only -- the two string-append operators. `-=`, `*=` and the
    rest do not append and are not assignments a secret literal arrives through.
    """

    def test_the_python_and_js_append_is_redacted(self):
        self.assertEqual(
            'password+="<REDACTED>"',
            claude_review.redact('password += "AbCdEf0123456789ZzYyXx"'),
        )

    def test_the_php_append_is_redacted(self):
        self.assertEqual(
            "$password.='<REDACTED>'",
            claude_review.redact("$password .= 'AbCdEf0123456789ZzYyXx'"),
        )

    def test_a_comparison_is_still_not_an_assignment(self):
        # The `==` guard is what the new alternative must not have moved.
        for line in ("password == other", "token === other", "if (password == x) {"):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))

    def test_the_arithmetic_augmentations_are_not_appends(self):
        for line in ("password -= 1", "token *= 2"):
            with self.subTest(line=line):
                self.assertEqual(line, claude_review.redact(line))


class ALongDiffIsReadWholeInParts(unittest.TestCase):
    """A diff longer than one call is read whole, in parts, and never cut.

    It was cut at MAX_REVIEW_CHARS with no marker and passed (EGI_bot#117), then
    cut, named and failed (kit #284, #292), because the author orders the diff
    and padding the head carried a change past the cut (capaz#14). Failing named
    the gap and still read none of it: capaz#57 is 1,124,890 characters, and its
    review read 120,000. Now every chunk is reviewed and the part reviews are
    merged. The size is patched small so the fixtures stay readable, and the
    model is a fake that answers by what it is asked.
    """

    CAP = 1000

    def setUp(self):
        patcher = mock.patch.object(claude_review, "MAX_REVIEW_CHARS", self.CAP)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The review job runs this suite with the repo's variables in its
        # environment, and a set value would override the patched size, the
        # cap or the prices.
        env = {
            k: v for k, v in os.environ.items()
            if not k.startswith("CLAUDE_REVIEW_") and k != "ANTHROPIC_BASE_URL"
        }
        env_patcher = mock.patch.dict(os.environ, env, clear=True)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        self.requests = []
        self.order = []

    @staticmethod
    def _file(name, lines, hunks=1):
        """A file's diff: its header lines, then `hunks` hunks of `lines` added lines."""
        header = f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n"
        return header + "".join(
            f"@@ -{h * 100},0 +{h * 100},{lines} @@\n"
            + "".join(f"+{name} hunk {h} line {i}\n" for i in range(lines))
            for h in range(hunks)
        )

    def _diff(self, *files):
        return "".join(self._file(*spec) for spec in files)

    def _long_diff(self):
        # a.py and c.py fit in one chunk each; b.py is four hunks of ~650.
        return self._diff(("a.py", 10, 3), ("b.py", 30, 4), ("c.py", 3))

    @staticmethod
    def _hunks(text):
        """Every hunk in `text`, from its @@ line to the next hunk or file."""
        return re.findall(r"^@@.*?(?=^@@|^diff --git |\Z)", text, flags=re.M | re.S)

    def _chunks(self, diff):
        return claude_review.chunk_diff(diff, self.CAP)

    def test_a_diff_that_fits_is_one_chunk_untouched(self):
        diff = self._diff(("a.py", 5))
        self.assertEqual(self._chunks(diff), [diff])

    def test_no_hunk_is_split_lost_or_repeated(self):
        diff = self._diff(("a.py", 10, 3), ("b.py", 30, 4), ("c.py", 3), ("d.py", 12, 6))
        chunks = self._chunks(diff)
        self.assertGreater(len(chunks), 2)
        read = [hunk for chunk in chunks for hunk in self._hunks(chunk)]
        self.assertEqual(read, self._hunks(diff))

    def test_every_chunk_opens_on_a_file_header(self):
        for chunk in self._chunks(self._long_diff()):
            with self.subTest(chunk=chunk[:40]):
                self.assertTrue(chunk.startswith("diff --git a/"), chunk[:80])

    def test_a_file_that_fits_is_in_exactly_one_chunk(self):
        chunks = self._chunks(self._long_diff())
        for name in ("a.py", "c.py"):
            with self.subTest(name=name):
                self.assertEqual(sum(f"diff --git a/{name} " in chunk for chunk in chunks), 1)

    def test_a_long_file_continues_under_its_header(self):
        chunks = self._chunks(self._diff(("b.py", 30, 4)))
        self.assertEqual(len(chunks), 4)
        for chunk in chunks:
            with self.subTest(chunk=chunk[:60]):
                self.assertTrue(chunk.startswith("diff --git a/b.py b/b.py\n--- a/b.py\n"))
                self.assertLessEqual(len(chunk), self.CAP)

    def test_a_file_longer_than_a_chunk_fills_the_room_left_first(self):
        # On capaz#57 a 29,042-character first part was sent alone because the
        # next file started a fresh part: 13 calls where 10 hold the diff.
        chunks = self._chunks(self._diff(("c.py", 3), ("b.py", 30, 4)))
        self.assertEqual(len(chunks), 4)
        self.assertIn("diff --git a/c.py ", chunks[0])
        self.assertIn("+b.py hunk 0 line 0\n", chunks[0])

    def test_a_hunk_longer_than_a_chunk_goes_alone_and_whole(self):
        diff = self._diff(("a.py", 3), ("huge.py", 200), ("c.py", 3))
        chunks = self._chunks(diff)
        self.assertEqual(chunks, [self._file("a.py", 3), self._file("huge.py", 200),
                                  self._file("c.py", 3)])
        self.assertEqual("".join(chunks), diff)

    def _model(self, fail_parts=(), truncate_parts=(), empty_parts=(), barrier=None):
        """The Messages endpoint as a fake, recording every request.

        A part is answered with the files it shows, and the merge with a fixed
        review. A part in `fail_parts` answers HTTP 500, one in `empty_parts`
        answers with no text, and one in `truncate_parts` stops at max_tokens.
        With `barrier`, parts 2 to barrier.parties + 1 each wait on it, so none
        of them answers until all of them are in flight together.
        """
        def open_(request, timeout=None):
            sent = json.loads(request.data)
            self.requests.append(sent)
            task = sent["messages"][0]["content"][-1]["text"]
            part = re.match(r"Review part (\d+) of", task)
            number = int(part.group(1)) if part else 0
            self.order.append(("start", number))
            if barrier and 2 <= number <= barrier.parties + 1:
                barrier.wait()
            if number in fail_parts:
                raise urllib.error.HTTPError(
                    request.full_url, 500, "boom", {}, io.BytesIO(b"upstream down")
                )
            if part:
                files = sorted(set(re.findall(r"^diff --git a/\S+ b/(\S+)$", task, flags=re.M)))
                text = f"Part {number} read {', '.join(files)}."
            else:
                text = "Merged review: nothing to flag."
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({
                "content": [] if number in empty_parts else [{"type": "text", "text": text}],
                "stop_reason": "max_tokens" if number in truncate_parts else "end_turn",
                "usage": {"input_tokens": 100_000, "output_tokens": 5_000},
            }).encode()
            self.order.append(("end", number))
            return response

        return mock.patch.object(claude_review._NO_REDIRECT_OPENER, "open", side_effect=open_)

    def _main(self, diff, model=None, env=None):
        """Run main() over `diff` with the fake model. Returns the status and the comment.

        `diff` is also the codebase snapshot, read when `env` sets REVIEW_SCOPE=full.
        """
        os.environ.update({
            "ANTHROPIC_API_KEY": "test-key", "REVIEW_SCOPE": "diff",
            "BASE_SHA": "base", "HEAD_SHA": "head", **(env or {}),
        })
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            claude_review, "pr_diff", return_value=(diff, [], ("", ""))
        ), mock.patch.object(
            claude_review, "codebase_snapshot", return_value=diff
        ), model or self._model(), contextlib.redirect_stderr(io.StringIO()):
            os.chdir(tmp)
            try:
                self.assertEqual(claude_review.main(), 0)
                status = Path(claude_review.REVIEW_STATUS_PATH).read_text(encoding="utf-8")
                comment = Path("claude-review.md").read_text(encoding="utf-8")
            finally:
                os.chdir(cwd)
        return status.strip(), comment

    def _tasks(self):
        return [request["messages"][0]["content"][-1]["text"] for request in self.requests]

    def test_a_long_diff_is_reviewed_in_parts_and_merged(self):
        diff = self._long_diff()
        parts = len(self._chunks(diff))
        status, comment = self._main(diff)
        self.assertEqual(status, claude_review.STATUS_OK)
        self.assertEqual(len(self.requests), parts + 1)
        self.assertTrue(comment.startswith(
            "## Claude Code Review\n\nMerged review: nothing to flag."
        ))

    def test_every_hunk_reaches_the_model(self):
        diff = self._long_diff()
        self._main(diff)
        read = "".join(self._tasks()[:-1])
        for hunk in self._hunks(diff):
            with self.subTest(hunk=hunk[:30]):
                self.assertIn(hunk, read)

    def test_padding_the_head_cannot_carry_a_change_past_the_review(self):
        """The attack the cut allowed: filler first, the change that matters after it."""
        status, _ = self._main(self._diff(("aaa_filler.py", 200), ("zzz_payload.py", 3)))
        self.assertEqual(status, claude_review.STATUS_OK)
        self.assertTrue(any("+zzz_payload.py hunk 0 line 2" in task for task in self._tasks()))

    def test_every_call_opens_with_the_same_cached_context(self):
        self._main(self._long_diff())
        contexts = [request["messages"][0]["content"][0] for request in self.requests]
        self.assertEqual(len({json.dumps(c, sort_keys=True) for c in contexts}), 1)
        self.assertEqual(contexts[0]["cache_control"], {"type": "ephemeral"})
        for request in self.requests:
            self.assertNotIn("cache_control", request["messages"][0]["content"][-1])
        for name in ("a.py", "b.py", "c.py"):
            self.assertIn(f"- {name}\n", contexts[0]["text"] + "\n")

    def test_part_one_finishes_before_any_other_part_starts(self):
        # A cache entry is readable once the response writing it has begun.
        self._main(self._long_diff())
        self.assertEqual(self.order[:2], [("start", 1), ("end", 1)])

    def test_the_parts_after_the_first_run_together(self):
        # Each of parts 2 on waits for the others before it answers, so a
        # review that sent them one at a time breaks the barrier after 5 seconds.
        diff = self._long_diff()
        parts = len(self._chunks(diff))
        barrier = threading.Barrier(min(claude_review.REVIEW_WORKERS, parts - 1), timeout=5)
        self.assertGreater(barrier.parties, 1)
        status, _ = self._main(diff, model=self._model(barrier=barrier))
        self.assertEqual(status, claude_review.STATUS_OK)
        self.assertFalse(barrier.broken)
        self.assertEqual(self.order[:2], [("start", 1), ("end", 1)])

    def test_the_merge_reads_every_part_review(self):
        diff = self._long_diff()
        parts = len(self._chunks(diff))
        self._main(diff)
        merge = self._tasks()[-1]
        self.assertTrue(merge.startswith(claude_review.MERGE_TASK))
        for number in range(1, parts + 1):
            self.assertIn(f"Part {number} read ", merge)

    def test_a_failed_part_stops_the_merge_and_fails_the_check(self):
        status, comment = self._main(self._long_diff(), model=self._model(fail_parts=(2,)))
        self.assertEqual(status, claude_review.STATUS_FAILED)
        self.assertFalse(any(t.startswith(claude_review.MERGE_TASK) for t in self._tasks()))
        self.assertIn("Part 2 of", comment)
        # The parts already paid for are posted.
        self.assertIn("Part 1 read a.py", comment)

    def test_a_truncated_part_fails_as_truncated(self):
        status, _ = self._main(self._long_diff(), model=self._model(truncate_parts=(1,)))
        self.assertEqual(status, claude_review.STATUS_TRUNCATED)

    def test_the_most_severe_part_names_the_failure_whatever_its_place(self):
        # Failed outranks empty outranks truncated. Part 1 is the least severe
        # each time, so naming the first part that did not finish gets it wrong.
        diff = self._long_diff()
        parts = len(self._chunks(diff))
        for fail, empty, truncate, expected in (
            ((3,), (), (1,), claude_review.STATUS_FAILED),
            ((), (3,), (1,), claude_review.STATUS_EMPTY),
            ((3,), (2,), (1,), claude_review.STATUS_FAILED),
        ):
            with self.subTest(fail=fail, empty=empty, truncate=truncate):
                self.requests = []
                status, comment = self._main(diff, model=self._model(
                    fail_parts=fail, empty_parts=empty, truncate_parts=truncate
                ))
                self.assertEqual(status, expected)
                unfinished = ", ".join(map(str, sorted({*fail, *empty, *truncate})))
                self.assertIn(f"Part {unfinished} of {parts} did not finish", comment)
                self.assertFalse(
                    any(t.startswith(claude_review.MERGE_TASK) for t in self._tasks())
                )

    def test_one_part_is_one_call_as_before(self):
        status, _ = self._main(self._diff(("a.py", 5)))
        self.assertEqual((status, len(self.requests)), (claude_review.STATUS_OK, 1))
        self.assertNotIn("cache_control", json.dumps(self.requests[0]))

    def test_the_coverage_section_counts_files_parts_and_cost(self):
        diff = self._long_diff()
        parts = len(self._chunks(diff))
        _, comment = self._main(diff)
        coverage = comment.split("## Claude Review Coverage", 1)[1]
        self.assertIn("- Files: 3 of 3 reviewable changed files.", coverage)
        self.assertIn(
            f"- Diff: {len(diff):,} characters in {parts} parts of at most {self.CAP:,}", coverage
        )
        # Each fake call bills 100,000 input and 5,000 output tokens at $2/$10.
        self.assertIn(f"- Spent: ${(parts + 1) * 0.25:.2f},", coverage)
        self.assertIn("4 characters a token", coverage)

    def test_an_estimate_over_the_cap_sends_nothing_and_fails(self):
        diff = self._long_diff()
        status, comment = self._main(diff, env={"CLAUDE_REVIEW_MAX_USD": "0.01"})
        self.assertEqual((status, self.requests), (claude_review.STATUS_OVER_BUDGET, []))
        self.assertTrue(comment.startswith(
            "## Claude Code Review\n\n" + claude_review.OVER_BUDGET_BANNER
        ))
        self.assertIn(f"{len(diff):,} characters", comment)
        self.assertIn("over the $0.01 cap", comment)
        self.assertIn("raise the CLAUDE_REVIEW_MAX_USD repository variable", comment)
        self.assertIn("## Claude Review Coverage", comment)

    # 400,000 characters is 100,000 input tokens at $2 a million, plus 12,000
    # output tokens at $10: $0.32. The output alone is $0.12, under either cap.
    SNAPSHOT = "x" * 400_000

    def test_a_full_snapshot_over_the_cap_sends_nothing_and_fails(self):
        status, comment = self._main(
            self.SNAPSHOT, env={"REVIEW_SCOPE": "full", "CLAUDE_REVIEW_MAX_USD": "0.30"}
        )
        self.assertEqual((status, self.requests), (claude_review.STATUS_OVER_BUDGET, []))
        self.assertTrue(comment.startswith(
            "## Claude Code Review\n\n" + claude_review.OVER_BUDGET_BANNER
        ))
        self.assertIn("400,000 characters", comment)
        self.assertIn("estimated $0.32", comment)
        self.assertIn("over the $0.30 cap", comment)
        self.assertIn("raise the CLAUDE_REVIEW_MAX_USD repository variable", comment)

    def test_a_full_snapshot_under_the_cap_is_one_call(self):
        status, _ = self._main(
            self.SNAPSHOT, env={"REVIEW_SCOPE": "full", "CLAUDE_REVIEW_MAX_USD": "0.33"}
        )
        self.assertEqual((status, len(self.requests)), (claude_review.STATUS_OK, 1))
        self.assertIn("Codebase snapshot:", self._tasks()[0])

    def test_a_full_snapshot_without_a_key_says_so_before_the_cost(self):
        # Same order as a diff: no key is its own status, and the gate excuses
        # it for Dependabot alone.
        status, _ = self._main(self.SNAPSHOT, env={
            "REVIEW_SCOPE": "full", "CLAUDE_REVIEW_MAX_USD": "0.01", "ANTHROPIC_API_KEY": "",
        })
        self.assertEqual((status, self.requests), (claude_review.STATUS_NO_KEY, []))

    def test_a_diff_the_size_of_capaz_57_fits_the_default_cap(self):
        # 1,124,890 characters in 10 parts, measured end to end on kit #311.
        plan = claude_review.ReviewPlan(
            ["x" * 112_489] * 10, [f"docs/plan/{n}.md" for n in range(12)], [], 1_124_890
        )
        estimate = claude_review.estimate_cost_usd(plan)
        self.assertGreater(estimate, 1.0)
        self.assertLess(estimate, claude_review.DEFAULT_MAX_REVIEW_USD)

    def test_the_estimate_counts_no_more_output_than_the_ceiling_allows(self):
        # It counted 12,000 output tokens a call whatever CLAUDE_REVIEW_MAX_TOKENS
        # said, so a ceiling of 8,000 was estimated 50% over what it could cost.
        plan = claude_review.ReviewPlan(["x" * 4_000], ["a.py"], [], 4_000)
        _, output_price, _, _ = claude_review._prices()
        for ceiling, per_call in (("", 12_000), ("8000", 8_000), ("64000", 12_000)):
            with self.subTest(ceiling=ceiling), mock.patch.dict(
                os.environ, {"CLAUDE_REVIEW_MAX_TOKENS": ceiling}
            ):
                self.assertEqual(claude_review.output_tokens_per_call(), per_call)
                with mock.patch.object(claude_review, "OUTPUT_TOKENS_PER_CALL", 0):
                    no_output = claude_review.estimate_cost_usd(plan)
                self.assertAlmostEqual(
                    claude_review.estimate_cost_usd(plan) - no_output,
                    per_call * output_price / 1_000_000,
                )

    def test_the_cap_comes_from_the_environment(self):
        for value, expected in (("", 5.0), (" 12.5 ", 12.5), ("lots", 5.0), ("-1", 5.0)):
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {"CLAUDE_REVIEW_MAX_USD": value}
            ), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(claude_review.review_cap_usd(), expected)
                if value.strip() in ("lots", "-1"):
                    self.assertIn("CLAUDE_REVIEW_MAX_USD=", err.getvalue())

    def test_the_gate_fails_over_budget_and_partial(self):
        workflow = TheCheckoutFollowsTheBaseBranch._workflow()
        self.assertIsNotNone(workflow, "claude-review.yml not found beside this test")
        self.assertIn("CLAUDE_REVIEW_MAX_USD: ${{ vars.CLAUDE_REVIEW_MAX_USD }}", workflow)
        case = workflow.split('case "$status" in', 1)[1].split("esac", 1)[0]
        for status in ("over-budget", "partial"):
            with self.subTest(status=status):
                self.assertIn(f"{status})", case)
                branch = case.split(f"{status})", 1)[1].split(";;", 1)[0]
                self.assertIn("::error::", branch)
                self.assertIn("exit 1", branch)
                # The raw variable may be a typo the script fell back from; the
                # value actually used is in the PR comment.
                self.assertNotIn("${CLAUDE_REVIEW_MAX", branch)
        self.assertNotIn("partial", case.split(")", 1)[0])


class TheBudgetComesFromTheEnvironment(unittest.TestCase):
    """CLAUDE_REVIEW_MAX_CHARS, parsed exactly like CLAUDE_REVIEW_MAX_TOKENS.

    It sizes the parts a long diff is read in. A repo that raised it while it
    was a cut keeps working: its reviews make fewer, larger calls.
    """

    @staticmethod
    def _budget(value):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_REVIEW_MAX_CHARS": value}):
            with contextlib.redirect_stderr(err):
                got = claude_review.review_budget()
        return got, err.getvalue()

    def test_empty_string_means_the_default(self):
        self.assertEqual(self._budget("")[0], claude_review.MAX_REVIEW_CHARS)

    def test_a_repo_value_wins_and_the_parts_use_it(self):
        self.assertEqual(self._budget(" 400000 ")[0], 400000)
        diff = "".join(f"diff --git a/{name} b/{name}\n+x\n" for name in ("a.py", "b.py"))
        with mock.patch.dict(os.environ, {"CLAUDE_REVIEW_MAX_CHARS": "30"}), mock.patch.object(
            claude_review, "pr_diff", return_value=(diff, [], ("", ""))
        ):
            plan = claude_review.review_plan("base", "head")
        self.assertEqual(len(plan.chunks), 2)
        self.assertEqual("".join(plan.chunks), diff)

    def test_a_typo_falls_back_out_loud(self):
        got, err = self._budget("lots")
        self.assertEqual(got, claude_review.MAX_REVIEW_CHARS)
        self.assertIn("CLAUDE_REVIEW_MAX_CHARS='lots'", err)

    def test_the_workflow_passes_the_repository_variable(self):
        workflow = TheCheckoutFollowsTheBaseBranch._workflow()
        self.assertIn(
            "CLAUDE_REVIEW_MAX_CHARS: ${{ vars.CLAUDE_REVIEW_MAX_CHARS }}", workflow
        )


class AFileWithoutAPatchIsFetchedOrNamed(unittest.TestCase):
    """GitHub's files API leaves `patch` out of a file whose diff is too large.

    pr_diff() skipped such a file, so nobody reviewed it and nothing named it.
    Measured on capaz#57: 05-hard-parts.md, 11-data-model.md, 12-architecture.md,
    13-security-privacy-dr.md and 20-roadmap.md, 300 to 411 changed lines each,
    came back with no patch, and the posted comment listed only the three files
    the budget cut. Each such file is now diffed by git from the fetched PR
    head, or named in the comment and failed. The files API and git are fakes
    here: no network, no subprocess.
    """

    PR = "57"
    BIG = "docs/plan/05-hard-parts.md"
    PATCHED = {"filename": "CLAUDE.md", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"}

    def setUp(self):
        # The review job runs this suite with the repo's variables in its
        # environment, and a set value would override the patched size or cap.
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_REVIEW_")}
        env.update({"PR_NUMBER": self.PR, "GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "t"})
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.git_calls = []

    @staticmethod
    def _files_api(files, changed=None):
        """The opener as GitHub answers: `files` on page 1, then an empty page.

        The PR itself reports `changed` changed files, len(files) by default.
        """
        def open_(request, timeout=None):
            page = re.search(r"/files\?.*[?&]page=(\d+)", request.full_url)
            if page is None:
                body = {"changed_files": len(files) if changed is None else changed}
            else:
                body = files if page.group(1) == "1" else []
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(body).encode()
            return response

        return mock.patch.object(claude_review._GITHUB_OPENER, "open", side_effect=open_)

    def _git(self, diffs=None, fail=()):
        """run_git with no git behind it, recording every call.

        `diffs` maps the last path in a diff's pathspec to git's output for it.
        `fail` names the subcommands that exit non-zero.
        """
        diffs = diffs or {}

        def run_git(args):
            self.git_calls.append(args)
            command = "fetch" if "fetch" in args else "diff"
            if command in fail:
                raise claude_review.subprocess.CalledProcessError(128, ["git", *args])
            return "" if command == "fetch" else diffs.get(args[-1], "")

        return mock.patch.object(claude_review, "run_git", side_effect=run_git)

    @staticmethod
    def _git_diff(path, lines=3):
        return (
            f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n"
            f"--- a/{path}\n+++ b/{path}\n@@ -1,{lines} +1,{lines} @@\n"
            + "".join(f"+new line {i}\n" for i in range(lines))
        )

    def _pr_diff(self, files, git):
        with self._files_api(files), git, contextlib.redirect_stderr(io.StringIO()) as err:
            diff, unfetched, gap = claude_review.pr_diff()
        self.assertEqual(gap, ("", ""))
        return diff, unfetched, err.getvalue()

    def test_a_file_without_a_patch_is_diffed_by_git(self):
        diff, unfetched, _ = self._pr_diff(
            [self.PATCHED, {"filename": self.BIG, "status": "modified", "changes": 309}],
            self._git({self.BIG: self._git_diff(self.BIG)}),
        )
        self.assertEqual(unfetched, [])
        self.assertIn("diff --git a/CLAUDE.md b/CLAUDE.md\n@@ -1 +1 @@", diff)
        self.assertIn(f"diff --git a/{self.BIG} b/{self.BIG}\n", diff)
        self.assertIn("+new line 2", diff)

    def test_the_pr_head_is_fetched_as_objects_and_never_checked_out(self):
        self._pr_diff([{"filename": self.BIG}], self._git({self.BIG: self._git_diff(self.BIG)}))
        fetch, diff = self.git_calls
        self.assertIn(f"+refs/pull/{self.PR}/head:{claude_review.PR_HEAD_REF}", fetch)
        # Three dots: from the merge base, as the files API compares.
        self.assertIn(f"HEAD...{claude_review.PR_HEAD_REF}", diff)
        # Git's built-in diff only, whatever a config or attributes file asks for.
        self.assertIn("--no-ext-diff", diff)
        self.assertIn("--no-textconv", diff)
        for call in self.git_calls:
            for verb in ("checkout", "switch", "restore", "reset", "worktree", "merge", "apply"):
                with self.subTest(call=call, verb=verb):
                    self.assertNotIn(verb, call)

    def test_the_fetch_runs_once_however_many_files_need_it(self):
        names = [f"docs/plan/{n}.md" for n in ("11-data-model", "12-architecture", "20-roadmap")]
        _, unfetched, _ = self._pr_diff(
            [{"filename": name} for name in names],
            self._git({name: self._git_diff(name) for name in names}),
        )
        self.assertEqual(unfetched, [])
        self.assertEqual(sum("fetch" in call for call in self.git_calls), 1)

    def test_a_pr_whose_files_all_carry_patches_runs_no_git(self):
        diff, unfetched, _ = self._pr_diff([self.PATCHED], self._git())
        self.assertEqual((unfetched, self.git_calls), ([], []))
        self.assertIn("CLAUDE.md", diff)

    def test_an_excluded_file_without_a_patch_is_neither_fetched_nor_named(self):
        diff, unfetched, _ = self._pr_diff(
            [{"filename": "package-lock.json", "changes": 9000}], self._git()
        )
        self.assertEqual((diff, unfetched, self.git_calls), ("", [], []))

    def test_a_rename_diffs_the_old_path_and_the_new(self):
        new, old = "docs/plan/new.md", "docs/plan/old.md"
        self._pr_diff(
            [{"filename": new, "previous_filename": old, "status": "renamed"}],
            self._git({new: self._git_diff(new)}),
        )
        self.assertEqual(self.git_calls[-1][-3:], ["--", old, new])

    def test_a_failed_fetch_names_the_file_instead_of_dropping_it(self):
        diff, unfetched, err = self._pr_diff(
            [self.PATCHED, {"filename": self.BIG}], self._git(fail=("fetch",))
        )
        self.assertEqual(unfetched, [self.BIG])
        self.assertIn("CLAUDE.md", diff)
        self.assertNotIn(self.BIG, diff)
        self.assertIn(f"pull/{self.PR}/head", err)

    def test_a_pr_number_that_is_not_digits_is_never_fetched(self):
        # The number goes into the refspec. "５７" is digits to isdigit() and
        # not ASCII, so it is refused too.
        for number in ("57;x", "-1", "５７"):
            with self.subTest(number=number):
                self.git_calls = []
                os.environ["PR_NUMBER"] = number
                diff, unfetched, err = self._pr_diff(
                    [self.PATCHED, {"filename": self.BIG}], self._git()
                )
                self.assertEqual((unfetched, self.git_calls), ([self.BIG], []))
                self.assertIn("CLAUDE.md", diff)
                self.assertIn(f"PR_NUMBER={number!r} is not a pull request number", err)
                status, comment, _ = self._main(
                    [self.PATCHED, {"filename": self.BIG}], self._git()
                )
                self.assertEqual(status, claude_review.STATUS_PARTIAL)
                self.assertIn(f"`{self.BIG}`", comment)
                self.assertEqual(self.git_calls, [])

    def test_a_failed_or_empty_git_diff_names_the_file(self):
        for fail, diffs in ((("diff",), {}), ((), {self.BIG: "\n"})):
            with self.subTest(fail=fail, diffs=diffs):
                _, unfetched, _ = self._pr_diff([{"filename": self.BIG}], self._git(diffs, fail))
                self.assertEqual(unfetched, [self.BIG])

    def test_a_fetched_diff_is_read_in_parts_like_any_other(self):
        with mock.patch.object(claude_review, "MAX_REVIEW_CHARS", 1000), self._files_api(
            [self.PATCHED, {"filename": self.BIG}]
        ), self._git({self.BIG: self._git_diff(self.BIG, lines=200)}):
            plan = claude_review.review_plan("base", "head")
        self.assertEqual((plan.files, plan.unfetched), (["CLAUDE.md", self.BIG], []))
        self.assertGreater(len(plan.chunks), 1)
        self.assertTrue("".join(plan.chunks).endswith("+new line 199"))

    def test_the_note_names_an_unfetched_file(self):
        with self._files_api([self.PATCHED, {"filename": self.BIG}]), self._git(
            fail=("fetch",)
        ), contextlib.redirect_stderr(io.StringIO()):
            plan = claude_review.review_plan("base", "head")
        self.assertEqual((plan.files, plan.unfetched), (["CLAUDE.md"], [self.BIG]))
        note = claude_review.left_out_note(plan)
        self.assertIn(
            "Not reviewed at all, because GitHub sent no patch and git could not"
            f" produce one: `{self.BIG}`.",
            note,
        )
        # The size is not the cause, so raising it is not the advice.
        self.assertNotIn("CLAUDE_REVIEW_MAX_CHARS", note)

    def _main(self, files, git):
        """Run main() over the fakes. Returns the status, the comment and the model calls."""
        payload = json.dumps({
            "content": [{"type": "text", "text": "Nothing to flag."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }).encode()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = payload
        response.read.return_value = payload
        os.environ.update({
            "ANTHROPIC_API_KEY": "test-key", "REVIEW_SCOPE": "diff",
            "BASE_SHA": "base", "HEAD_SHA": "head",
        })
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp, self._files_api(files), git, mock.patch.object(
            claude_review._NO_REDIRECT_OPENER, "open", return_value=response
        ) as model, contextlib.redirect_stderr(io.StringIO()):
            os.chdir(tmp)
            try:
                self.assertEqual(claude_review.main(), 0)
                status = Path(claude_review.REVIEW_STATUS_PATH).read_text(encoding="utf-8")
                comment = Path("claude-review.md").read_text(encoding="utf-8")
            finally:
                os.chdir(cwd)
        self.sent = [json.loads(call.args[0].data) for call in model.call_args_list]
        return status.strip(), comment, model.call_count

    def test_a_fetched_file_is_reviewed_and_the_review_is_ok(self):
        status, comment, calls = self._main(
            [self.PATCHED, {"filename": self.BIG}], self._git({self.BIG: self._git_diff(self.BIG)})
        )
        self.assertEqual((status, calls), (claude_review.STATUS_OK, 1))
        self.assertNotIn(claude_review.PARTIAL_BANNER, comment)

    def test_an_unfetched_file_fails_the_check_and_the_comment_names_it(self):
        status, comment, calls = self._main(
            [self.PATCHED, {"filename": self.BIG}], self._git(fail=("fetch",))
        )
        self.assertEqual((status, calls), (claude_review.STATUS_PARTIAL, 1))
        banner = comment.split("## Claude Code Review\n\n", 1)[1].split("\n\n", 1)[0]
        self.assertTrue(banner.startswith(claude_review.PARTIAL_BANNER), banner)
        self.assertIn(f"`{self.BIG}`", banner)
        self.assertIn("Nothing to flag.", comment)
        # The model is told too, so it does not review the PR as if complete.
        self.assertIn(
            "NOT BY THE AUTHOR. Not reviewed at all, because", json.dumps(self.sent[0])
        )

    def test_a_pr_of_only_unfetched_files_fails_without_calling_the_model(self):
        # An empty diff used to mean "Skipped: no reviewable diff", which the
        # gate passes. Every file left out is a gap, not an empty PR.
        status, comment, calls = self._main([{"filename": self.BIG}], self._git(fail=("fetch",)))
        self.assertEqual((status, calls), (claude_review.STATUS_PARTIAL, 0))
        self.assertNotIn("Skipped", comment)
        self.assertIn(f"`{self.BIG}`", comment)
        self.assertFalse(any("--unified=80" in call for call in self.git_calls))

    def test_the_gate_names_every_cause_of_a_partial_review(self):
        workflow = TheCheckoutFollowsTheBaseBranch._workflow()
        self.assertIsNotNone(workflow, "claude-review.yml not found beside this test")
        branch = workflow.split('case "$status" in', 1)[1].split("partial)", 1)[1]
        branch = branch.split(";;", 1)[0]
        self.assertIn("could not get a diff for", branch)
        self.assertIn("3,000-file limit", branch)
        self.assertNotIn("CLAUDE_REVIEW_MAX_CHARS", branch)


class AFileTheFilesAPINeverListedFailsTheCheck(unittest.TestCase):
    """GitHub's files API lists at most 3,000 files of one PR, then empty pages.

    pr_diff() paged until an empty page, so a file past the limit reached
    neither the diff, the budget nor the note, and the check could pass. The
    number it listed is now held against the PR's own `changed_files`. Measured
    on DefinitelyTyped#67085: 100 files on each of pages 1 to 30, none on page
    31, and `changed_files` 0, then 27,399 when read again. GitHub is a fake
    here: no network, no git.
    """

    PR = "67085"
    PATCH = "@@ -1 +1 @@\n-a\n+b"

    def setUp(self):
        # The review job runs this suite with the repo's CLAUDE_REVIEW_MAX_CHARS
        # in its environment, and a set value would override the default budget.
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_REVIEW_MAX_CHARS"}
        env.update({"PR_NUMBER": self.PR, "GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "t"})
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Every file in these fixtures carries a patch, so git never runs.
        git = mock.patch.object(claude_review, "run_git", side_effect=AssertionError("git ran"))
        git.start()
        self.addCleanup(git.stop)
        self.urls = []

    def _files(self, count):
        return [
            {"filename": f"types/p{i}/index.d.ts", "status": "modified", "patch": self.PATCH}
            for i in range(count)
        ]

    def _api(self, files, changed, fail=None):
        """The opener as GitHub answers: 100 files a page up to the limit, then empty pages.

        The PR request answers {"changed_files": changed}, or `changed` itself
        when it is a dict, or raises it when it is an exception. `fail` maps a
        files page number to the exception that page raises, or to the body it
        answers instead of its files.
        """
        listed = files[: claude_review.FILES_API_LIMIT]

        def open_(request, timeout=None):
            self.urls.append(request.full_url)
            page = re.search(r"/files\?.*[?&]page=(\d+)", request.full_url)
            if page is None:
                if isinstance(changed, BaseException):
                    raise changed
                body = changed if isinstance(changed, dict) else {"changed_files": changed}
            elif int(page.group(1)) in (fail or {}):
                body = fail[int(page.group(1))]
                if isinstance(body, BaseException):
                    raise body
            else:
                start = (int(page.group(1)) - 1) * 100
                body = listed[start:start + 100]
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(body).encode()
            return response

        return mock.patch.object(claude_review._GITHUB_OPENER, "open", side_effect=open_)

    def _pr_diff(self, files, changed):
        with self._api(files, changed), contextlib.redirect_stderr(io.StringIO()) as err:
            result = claude_review.pr_diff()
        self.err = err.getvalue()
        return result

    def test_a_pr_past_the_limit_says_how_many_files_were_never_listed(self):
        diff, unfetched, (unlisted, fix) = self._pr_diff(self._files(3412), 3412)
        self.assertEqual(diff.count("diff --git "), 3000)
        self.assertEqual(unfetched, [])
        self.assertIn("lists at most 3,000 files of a PR and this one", unlisted)
        self.assertIn("this one changes 3,412, so 412 files went unlisted and unreviewed", unlisted)
        self.assertEqual(fix, "Split the PR.")
        # The count comes from the PR under review, and paging stops at the
        # first empty page, which is the one past the limit.
        self.assertEqual(self.urls[0], f"https://api.github.com/repos/o/r/pulls/{self.PR}")
        self.assertTrue(self.urls[-1].endswith("&page=31"), self.urls[-1])

    def test_a_count_of_zero_at_the_limit_still_fails(self):
        # DefinitelyTyped#67085 as GitHub first reported it: 3,000 listed, 0 changed.
        _, _, (unlisted, fix) = self._pr_diff(self._files(3000), 0)
        self.assertIn(
            "lists at most 3,000 files of a PR and stopped there, so any file this one"
            " changes beyond those went unlisted and unreviewed.",
            unlisted,
        )
        self.assertEqual(fix, "Split the PR.")

    def test_a_list_at_the_limit_fails_even_when_the_count_matches(self):
        # 3,000 listed is what a PR of 3,000 files and a PR cut at 3,000 both
        # give, and the count that would tell them apart is the one 67085 got
        # wrong. A PR of exactly 3,000 files is told to split; none passes unread.
        _, _, (unlisted, fix) = self._pr_diff(self._files(3000), 3000)
        self.assertIn(
            "lists at most 3,000 files of a PR and stopped there, so any file this one"
            " changes beyond those went unlisted and unreviewed.",
            unlisted,
        )
        self.assertEqual(fix, "Split the PR.")

    def test_a_list_one_short_of_the_limit_that_matches_is_whole(self):
        self.assertEqual(self._pr_diff(self._files(2999), 2999)[2], ("", ""))

    def test_an_excluded_file_counts_as_listed(self):
        # changed_files counts every file, so the listed count must as well, or
        # every PR touching a lockfile would read as a gap.
        files = self._files(1) + [{"filename": "package-lock.json", "patch": self.PATCH}]
        diff, _, gap = self._pr_diff(files, 2)
        self.assertEqual(gap, ("", ""))
        self.assertNotIn("package-lock.json", diff)

    def test_a_count_that_differs_below_the_limit_asks_for_a_re_run(self):
        for changed, effect in (
            (13, "the review may have missed some"),
            (11, "the reviewed diff may not match the PR as it is now"),
        ):
            with self.subTest(changed=changed):
                _, _, (unlisted, fix) = self._pr_diff(self._files(12), changed)
                self.assertIn(
                    f"listed 12 files, but the PR says it changes {changed} files, so {effect}.",
                    unlisted,
                )
                self.assertIn("The PR may have changed", fix)

    def test_no_count_from_github_fails_the_review_and_the_log_says_why(self):
        # A failed PR request crashed the script before it wrote a status, so
        # the check went red with no comment. It is now a gap like any other.
        refused = urllib.error.HTTPError(
            f"https://api.github.com/repos/o/r/pulls/{self.PR}", 403, "rate limited", {}, None
        )
        self.addCleanup(refused.close)
        for changed, logged in (
            (refused, f"could not read pull {self.PR}: HTTP Error 403"),
            (urllib.error.URLError("timed out"), f"could not read pull {self.PR}"),
            (json.JSONDecodeError("Expecting value", "", 0), f"could not read pull {self.PR}"),
            ({}, f"GitHub gave no changed_files for pull {self.PR}"),
            (None, f"GitHub gave no changed_files for pull {self.PR}"),
        ):
            with self.subTest(changed=type(changed).__name__):
                diff, _, (unlisted, fix) = self._pr_diff(self._files(12), changed)
                self.assertEqual(diff.count("diff --git "), 12)
                self.assertIn(
                    "listed 12 files, but GitHub gave no count of the files this PR changes,"
                    " so the review cannot tell whether it read them all.",
                    unlisted,
                )
                self.assertEqual(fix, "The job log says why; re-run.")
                self.assertIn(logged, self.err)

    def test_the_note_carries_the_fix_and_the_model_text_does_not(self):
        with self._api(self._files(12), 13):
            plan = claude_review.review_plan("base", "head")
        note = claude_review.left_out_note(plan)
        self.assertIn("listed 12 files, but the PR says it changes 13 files", note)
        self.assertIn("re-run", note)
        # The model reads the fact whether the diff goes in one call or in parts.
        parts = plan._replace(chunks=plan.chunks * 2)
        for text in (claude_review._left_out_marker(plan), claude_review._parts_context(parts)):
            with self.subTest(text=text[:40]):
                self.assertIn("NOT BY THE AUTHOR. GitHub's files API listed 12 files", text)
                self.assertNotIn("re-run", text)

    def _main(self, files, changed, fail=None):
        """Run main() over the fake. Returns the status, the comment and the model calls."""
        payload = json.dumps({
            "content": [{"type": "text", "text": "Nothing to flag."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }).encode()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = payload
        response.read.return_value = payload
        os.environ.update({
            "ANTHROPIC_API_KEY": "test-key", "REVIEW_SCOPE": "diff",
            "BASE_SHA": "base", "HEAD_SHA": "head",
        })
        cwd = os.getcwd()
        api = self._api(files, changed, fail)
        with tempfile.TemporaryDirectory() as tmp, api, mock.patch.object(
            claude_review._NO_REDIRECT_OPENER, "open", return_value=response
        ) as model, contextlib.redirect_stderr(io.StringIO()):
            os.chdir(tmp)
            try:
                self.assertEqual(claude_review.main(), 0)
                status = Path(claude_review.REVIEW_STATUS_PATH).read_text(encoding="utf-8")
                comment = Path("claude-review.md").read_text(encoding="utf-8")
            finally:
                os.chdir(cwd)
        self.sent = [json.loads(call.args[0].data) for call in model.call_args_list]
        return status.strip(), comment, model.call_count

    def test_a_short_list_fails_the_check_and_the_comment_says_how_many(self):
        status, comment, calls = self._main(self._files(12), 13)
        self.assertEqual((status, calls), (claude_review.STATUS_PARTIAL, 1))
        banner = comment.split("## Claude Code Review\n\n", 1)[1].split("\n\n", 1)[0]
        self.assertTrue(banner.startswith(claude_review.PARTIAL_BANNER), banner)
        self.assertIn("listed 12 files, but the PR says it changes 13 files", banner)
        self.assertIn("Nothing to flag.", comment)
        # The coverage count is of the files GitHub listed, and says so.
        self.assertIn(
            "- Files: 12 of 12 reviewable changed files GitHub listed. It did not list them all.",
            comment,
        )
        self.assertIn("NOT BY THE AUTHOR. GitHub's files API listed 12", json.dumps(self.sent[0]))

    def test_a_list_of_exactly_the_limit_fails_the_check(self):
        status, comment, _ = self._main(self._files(3000), 3000)
        self.assertEqual(status, claude_review.STATUS_PARTIAL)
        self.assertIn("lists at most 3,000 files of a PR and stopped there", comment)
        self.assertIn("Split the PR.", comment)

    def test_a_failed_pr_request_still_posts_a_review_that_fails(self):
        status, comment, calls = self._main(self._files(12), urllib.error.URLError("timed out"))
        self.assertEqual((status, calls), (claude_review.STATUS_PARTIAL, 1))
        self.assertIn("GitHub gave no count of the files this PR changes", comment)
        self.assertIn("The job log says why; re-run.", comment)
        self.assertIn("Nothing to flag.", comment)

    def test_a_failed_page_still_posts_a_review_that_fails(self):
        # An HTTPError on page 2 raised out of pr_diff(), so the job wrote no
        # status and no comment (co-dm#80). It now fails the check as partial
        # and the comment names the code and the page.
        refused = urllib.error.HTTPError(
            f"https://api.github.com/repos/o/r/pulls/{self.PR}/files?page=2", 502, "bad", {}, None
        )
        self.addCleanup(refused.close)
        status, comment, calls = self._main(self._files(150), 150, fail={2: refused})
        self.assertEqual((status, calls), (claude_review.STATUS_PARTIAL, 1))
        banner = comment.split("## Claude Code Review\n\n", 1)[1].split("\n\n", 1)[0]
        self.assertTrue(banner.startswith(claude_review.PARTIAL_BANNER), banner)
        self.assertIn(
            "GitHub's files API failed on page 2 (HTTP 502) after listing 100 files", banner
        )
        self.assertIn("The job log says why; re-run.", banner)
        # Page 1 was still reviewed, and paging stopped at the failure.
        self.assertIn("Nothing to flag.", comment)
        sent = self.sent[0]["messages"][0]["content"][-1]["text"]
        self.assertEqual(sent.count("diff --git "), 100)
        self.assertTrue(self.urls[-1].endswith("&page=2"), self.urls[-1])

    def test_a_failed_first_page_fails_without_calling_the_model(self):
        # Nothing was listed, so there is no diff to send. That is a gap, not an
        # empty PR: no "Skipped", no git fallback, no model call.
        refused = urllib.error.HTTPError(
            f"https://api.github.com/repos/o/r/pulls/{self.PR}/files?page=1", 503, "down", {}, None
        )
        self.addCleanup(refused.close)
        status, comment, calls = self._main(self._files(150), 150, fail={1: refused})
        self.assertEqual((status, calls), (claude_review.STATUS_PARTIAL, 0))
        self.assertNotIn("Skipped", comment)
        self.assertIn("failed on page 1 (HTTP 503) after listing 0 files", comment)
        self.assertTrue(self.urls[-1].endswith("&page=1"), self.urls[-1])

    def test_a_page_that_is_not_a_file_list_still_posts_a_review_that_fails(self):
        # A dict or a list of strings raised AttributeError on file_info.get(),
        # the same crash before the status was written.
        for body in ({"message": "Not Found"}, ["a.py"], None):
            with self.subTest(body=body):
                status, comment, calls = self._main(self._files(150), 150, fail={2: body})
                self.assertEqual((status, calls), (claude_review.STATUS_PARTIAL, 1))
                self.assertIn(
                    "failed on page 2 (the response was not a list of files) after listing"
                    " 100 files",
                    comment,
                )

    def test_a_short_list_of_only_excluded_files_fails_without_calling_the_model(self):
        # An empty diff with nothing left out means "Skipped", which the gate
        # passes, and the local git diff fallback. Neither may run here.
        files = [{"filename": "package-lock.json", "patch": self.PATCH}]
        status, comment, calls = self._main(files, 2)
        self.assertEqual((status, calls), (claude_review.STATUS_PARTIAL, 0))
        self.assertNotIn("Skipped", comment)
        self.assertIn("listed 1 file, but the PR says it changes 2 files", comment)


class AOneShotReviewWritesNoCache(unittest.TestCase):
    """A cache write bills input at 1.25x and pays off only when a later request reads it.

    The single review request marked its diff block cache_control, so every
    review paid for a write that nothing read: 36 posted reviews across capaz
    and the kit show every input token as a cache write and a cache read of 0.
    """

    def test_the_request_carries_no_cache_control(self):
        sent = []

        def open_(request, timeout=None):
            sent.append(json.loads(request.data))
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({
                "content": [{"type": "text", "text": "Nothing to flag."}],
                "stop_reason": "end_turn",
            }).encode()
            return response

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), mock.patch.object(
            claude_review._NO_REDIRECT_OPENER, "open", side_effect=open_
        ):
            os.environ.pop("ANTHROPIC_BASE_URL", None)
            claude_review.call_claude("diff --git a/x.py b/x.py\n+y = 1")
        self.assertEqual(len(sent), 1)
        self.assertNotIn("cache_control", json.dumps(sent[0]))


if __name__ == "__main__":
    unittest.main(verbosity=2)

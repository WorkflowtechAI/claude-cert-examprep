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
        # parses nowhere.
        "call(api_key=abc123def456)",
        "OPENROUTER_API_KEY = abc123def456",
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
        'f("token=abc123def456")',
        "x('api_key=abc123def456')",
        # JS/TS object literal -- the shape from #169.
        'const c = { apiKey: "abc123def456", url: "https://example.test" };',
        'const c = { apiKey: \'abc123def456\' };',
        # JSON.
        '{"api_key": "abc123def456", "user": "bob"}',
        # YAML, quoted and bare.
        '  password: "hunter2"',
        "  api_key: abc123def456",
        # Shell, and shell inside single quotes -- the enclosing-string shape.
        "export OPENROUTER_API_KEY=abc123def456",
        "sh -c 'TOKEN=abc123def456'",
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
        self.assertNotIn("hunter2", claude_review.redact("password: hunter2"))

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
        redacted = {
            "+password:\n+  hunter2": '+password:"<REDACTED>"',
            "-password:\n-  hunter2": '-password:"<REDACTED>"',
            ' password:\n   "hunter2"': ' password:"<REDACTED>"',
            "password:\n\thunter2": 'password:"<REDACTED>"',
            # A value with a colon in it is a value, not a sibling key: the key
            # shape is `word:` followed by a space or the end of the line.
            "password:\n  redis://user:hunter2@host": 'password:"<REDACTED>"',
            "password:\n  db.internal:5432": 'password:"<REDACTED>"',
            # On the key's own line a value ending in `:` is a value.
            "token: abc123:": 'token:"<REDACTED>"',
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
        redacted = {
            'SECRET_KEY = "django-insecure-fake"': 'SECRET_KEY="<REDACTED>"',
            "STRIPE_SECRET_KEY=fake_abc": 'STRIPE_SECRET_KEY="<REDACTED>"',
            "AWS_SECRET_ACCESS_KEY=fake_abc": 'AWS_SECRET_ACCESS_KEY="<REDACTED>"',
            "SECRET_KEY_BASE=fake_abc": 'SECRET_KEY_BASE="<REDACTED>"',
            "MINIO_ACCESS_KEY=fake_abc": 'MINIO_ACCESS_KEY="<REDACTED>"',
            "private_key: fake_abc": 'private_key:"<REDACTED>"',
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
            'password="<REDACTED>"',
            claude_review.redact('password = os.environ.get("PW", "hunter2")'),
        )
        self.assertEqual(
            'token="<REDACTED>"', claude_review.redact('token = os.getenv("T", "sk-live-abc123")')
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
                self.assertEqual('"<REDACTED>"', claude_review.redact(line).split("=", 1)[1])

    def test_a_lookalike_that_is_not_an_env_lookup_is_still_redacted(self):
        # Only the exempted forms are exempt; anything that merely resembles
        # one is an ordinary call and is consumed whole.
        self.assertEqual('token="<REDACTED>"', claude_review.redact('token = myenv.get("X")'))
        self.assertEqual('token="<REDACTED>"', claude_review.redact('token = get_environ("X")'))

    def test_the_subscript_form_is_deliberately_not_exempt(self):
        # `os.environ["X"]` stays on the subscript rule, which predates this
        # exemption and is pinned by RedactionSparesCode. Nothing is gained by
        # exempting it: `token=<REDACTED>` already reads as valid code. What
        # broke was the trailing ` or ""` after a consumed CALL, not the lookup.
        self.assertEqual(
            'token="<REDACTED>"', claude_review.redact('token = os.environ["TOKEN"]')
        )

    def test_a_typed_default_is_deliberately_not_exempt(self):
        # A type annotation means type and default are redacted together, which
        # predates this exemption and is pinned by RedactionSparesTypeAnnotations.
        self.assertEqual(
            'password:"<REDACTED>"', claude_review.redact('password: str = os.getenv("X")')
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
        self.assertEqual('brokerApiKey:"<REDACTED>",', out)

    def test_dotted_call_is_redacted_whole(self):
        out = claude_review.redact('apiKey: settings.resolve("X"),')
        self.assertEqual('apiKey:"<REDACTED>",', out)

    def test_a_nested_call_is_consumed_two_levels_deep(self):
        self.assertEqual(
            'token="<REDACTED>";', claude_review.redact('token = resolveKey(env("X"));')
        )
        self.assertEqual('password="<REDACTED>"', claude_review.redact("password = a(b(c(d)))"))

    def test_a_secret_abutting_a_paren_is_redacted_not_leaked(self):
        # The case that made "leave calls alone" wrong: call-shaped, and a secret.
        self.assertEqual('password="<REDACTED>"', claude_review.redact("password=hunter2(prod)"))
        self.assertEqual('PASSWORD="<REDACTED>"', claude_review.redact("PASSWORD=Summer(2024)!"))
        self.assertEqual('password="<REDACTED>"', claude_review.redact("password=hunter2[prod]"))

    def test_a_subscript_a_suffix_run_and_a_command_substitution_go_the_same_way(self):
        # Consume, never skip: a subscript, a run of suffixes, `$(cmd)` and a
        # parenthesised value are redacted whole, not left dangling and not
        # left to the model.
        cases = {
            'token = os.environ["TOKEN"]': 'token="<REDACTED>"',
            'token = d["a"]["b"]': 'token="<REDACTED>"',
            "token = f(x)[0]": 'token="<REDACTED>"',
            "token = f(x)(y), z": 'token="<REDACTED>", z',
            "TOKEN=$(gcloud auth print-access-token)": 'TOKEN="<REDACTED>"',
            "password=(x)": 'password="<REDACTED>"',
            # `=>` before a call must not fall back to `=` plus a `>` value.
            "'password' => getenv(\"DB_PASSWORD\"),": '\'password\'=>"<REDACTED>",',
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
        self.assertEqual(claude_review.redact("token=${MY_TOKEN}"), 'token="<REDACTED>"')

    def test_a_json_quoted_key_whose_value_is_a_call_is_redacted_whole(self):
        # The JSON form (`"apiKey": ...`) takes the same path; the key's own
        # closing quote is consumed with the separator, as for every JSON value.
        out = claude_review.redact('"brokerApiKey": resolveKey("LITELLM_API_KEY"),')
        self.assertEqual('"brokerApiKey":"<REDACTED>",', out)

    def test_a_secret_followed_by_a_parenthetical_is_still_redacted(self):
        # A space before the paren is not a call; the word is the secret and the
        # parenthetical is prose that stays.
        self.assertEqual(
            'password="<REDACTED>" (rotated weekly)',
            claude_review.redact("password=hunter2 (rotated weekly)"),
        )

    def test_a_literal_default_inside_a_call_goes_with_the_call(self):
        # Redacting the call whole closes the gap that skipping it left open: a
        # literal passed as an argument is inside the redacted span.
        out = claude_review.redact('apiKey = getEnv("API_KEY", "hunter2")')
        self.assertEqual('apiKey="<REDACTED>"', out)
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
        self.assertEqual('apiKey:"<REDACTED>"', claude_review.redact("apiKey: a(b(c(ENV)))"))
        four_deep = claude_review.redact("password = a(b(c(d(e))))")
        self.assertEqual('password="<REDACTED>"))))', four_deep)
        literal_four_deep = claude_review.redact('apiKey: a(b(c(d("X"))))')
        self.assertEqual('apiKey:"<REDACTED>""X"))))', literal_four_deep)

    def test_a_quote_that_closes_an_enclosing_string_is_not_eaten(self):
        # The bare-token branch took any trailing quote, so `x('api_key=abc123')`
        # came out as `x('api_key=<REDACTED>)`: an unterminated literal in the
        # diff the model sees, which it reported as "the test files are
        # syntactically broken" on every PR whose tests carry a fixture. A
        # trailing quote is the value's own only when a leading one opened it.
        self.assertEqual('x(\'api_key="<REDACTED>"\')', claude_review.redact("x('api_key=abc123')"))
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
            claude_review.redact('f("token=abc123")\npassword=hunter2'),
        )
        # AND A NEW SUBJECT STARTS IT OVER TOO. `redact()` runs once per file in
        # the snapshot path, so a cursor left odd by the previous file would
        # single-quote the first bare value of the next one.
        self.assertEqual('f("token=\'<REDACTED>\'")', claude_review.redact('f("token=abc123")'))
        self.assertEqual('token="<REDACTED>"', claude_review.redact("token=abc123"))
        # The key's OWN quote is not an open string. `"brokerApiKey":` has a `"`
        # in front of the key, and group `q` closes it before the value, so the
        # placeholder is double-quoted and the JSON stays JSON.
        self.assertEqual(
            '{"token":"<REDACTED>", "user": bob}',
            claude_review.redact('{"token": abc123, "user": bob}'),
        )
        # The unterminated-quote fallback still takes its own leading quote.
        self.assertEqual('password="<REDACTED>"', claude_review.redact('password="hunter2'))

    def test_the_punctuation_after_a_bare_value_is_code(self):
        # No secret contains `,`, `;` or `)`; the code around a value does.
        cases = {
            "login(password=pw, user=u)": 'login(password="<REDACTED>", user=u)',
            "login(password=get_pw(), user=u)": 'login(password="<REDACTED>", user=u)',
            "connect(host=h, password=pw)": 'connect(host=h, password="<REDACTED>")',
            "$password = hunter2;": '$password="<REDACTED>";',
            "password: hunter2, user: bob": 'password:"<REDACTED>", user: bob',
            # A match arm is redacted, an accepted over-redaction, call or not.
            "token => x,": 'token=>"<REDACTED>",',
            "token => parse(x),": 'token=>"<REDACTED>",',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), want)


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
        self.assertEqual('token="<REDACTED>"', claude_review.redact("token=abc123"))


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
    LEADS = ["", "pre", "f()"]
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
    # main's redactor: seven of eight now reach the model intact.
    #
    # THE EIGHTH REDACTED ON ITS LABEL, NOT ITS VALUE. `redact()` fires on names
    # as well as values, so `"Stripe secret"` was eaten for the word `secret`
    # however the value was written. It is now `"Stripe live key"`. Nothing keys
    # on the label -- VENDOR_SAMPLES is only ever walked as
    # `for label, value in ...` -- so this changes no assertion, and it is the
    # rename that takes the class to eight of eight.
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

    def test_the_boundarys_own_blind_spot_is_pinned_not_assumed(self):
        """What the word boundary costs, asserted so the doc cannot drift.

        Raised in review on #196. Excluding `[A-Za-z0-9_]` means a key glued
        to a leading identifier is no longer seen by this half. That is the
        price of not blanking `TASK` + 32 hex, and it is worth pinning in
        BOTH directions so a future widening of the class shows up here as a
        failure rather than as a silent change of behaviour.
        """
        key = "ghp_" + "16C7e42F292c6912E7710c838347Ae178B4a"
        # Glued to an identifier: this half does not reach it.
        for line in (f"SOMEVAR_{key}", f"SOMEVAR{key}", f"MYVAR=SOMEVAR_{key}"):
            with self.subTest(missed=line):
                self.assertIn(key, claude_review.redact(line))
        # But a hyphen is not an identifier character, so this one IS caught --
        # the deliberate asymmetry, kept biased toward over-redaction.
        self.assertNotIn(key, claude_review.redact(f"some-{key}"))
        # And the OTHER half still reaches a glued key under a known name.
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
        # the same variable, and its contents are unknown to this line.
        for line in (
            "token = other.strip()",
            "token = token_source.strip()",
            "token = source_token.strip()",
        ):
            with self.subTest(line=line):
                self.assertRedacted(line)

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
        # where the value says `client_secret`. Not exempt -- over-redaction,
        # and the safe direction. Pinned so the behaviour is recorded rather
        # than rediscovered as a bug.
        self.assertRedacted('client_secret = client_secret.encode("utf-8")')

    def test_an_ordinary_secret_is_untouched_by_any_of_this(self):
        for line in (
            'token = "AbCdEf0123456789ZzYyXx"',
            "password: hunter2",
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
            'password: str = "hunter2"': 'password:"<REDACTED>"',
            "token: str | None = None": 'token:"<REDACTED>"',
            'password: str = os.getenv("X")': 'password:"<REDACTED>"',
            "password: int = 5)": 'password:"<REDACTED>")',
            "api_key: str | None = None,": 'api_key:"<REDACTED>",',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertEqual(claude_review.redact(line), want)

    def test_only_the_whole_word_is_a_type(self):
        cases = {
            "password: strong_pw": 'password:"<REDACTED>"',
            "password: stringy": 'password:"<REDACTED>"',
            'password: "str"': 'password:"<REDACTED>"',
            # A quoted literal under a secret name stays redacted: the
            # fail-closed side of this rule.
            '{ token: "h", keyName: "k" }': '{ token:"<REDACTED>", keyName: "k" }',
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
    def test_a_parenthesised_literal_is_still_redacted(self):
        """No metacharacter, so it is a value in brackets, not a pattern."""
        self.assertRedacted("password=(hunter2)")

    def test_a_braced_json_value_is_still_redacted(self):
        self.assertRedacted('token={"k": "v"}')

    def test_a_placeholder_with_a_suffix_is_still_redacted(self):
        """The exemption ends the value; anything after it is a value."""
        self.assertRedacted('api_key="(.*)hunter2"')

    def test_two_placeholders_are_not_one_name(self):
        self.assertRedacted("secret={a}{b}")

    def test_a_call_is_still_redacted_whole(self):
        """The call rule owns this shape and keeps it."""
        self.assertRedacted('brokerApiKey: resolveKey("LITELLM_API_KEY"),')

    def test_an_ordinary_secret_is_untouched_by_any_of_this(self):
        for line in [
            "password: hunter2",
            'API_KEY="sk-abcdefghijklmnopqrst"',
            "client_secret => abc123def456",
            "DB_PASSWORD=correct-horse-battery",
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
            "token=(AbC123+/==)",
            "api_key=(a+b+c+d+e+f+g+h)",
            "password=(some.value.here)",
            "secret=(a|b|c)",
            "client_secret=(hunter2*)",
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
            "token=(?:a|b)SECRETAB",
            "api_key=(.*)hunter2x",
            "password=([a-z])abcdefgh",
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
            "token=(?:hunter2)",
            "api_key=(?:AbC123)",
            "password=(?=hunter2)",
            "secret=(?P<x>hunter2)",
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
            "api_key={ABCDEF123456}",
            "password={Hunter2}",
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
            "const token = /hunter2/;",
            "token = /var/lib/secrets",
            "password = /abc123def456/",
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
        """
        cases = {
            "x('token=AbC123def456+')": 'x(\'token="<REDACTED>"\')',
            "password=AbC+dEf/123==": 'password="<REDACTED>"',
            "token=abc.def.ghi": 'token="<REDACTED>"',
            "token=a+b+c": 'token="<REDACTED>"',
            "secret=x&y&z": 'secret="<REDACTED>"',
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertRedactsTo(line, want)

    def test_an_unspaced_workflow_expression_still_ends_the_chain(self):
        # The `${{ }}` guard lives in the chain's literal, and the bare class
        # consults that same literal -- so an expression after the operator
        # neither stops the value early nor gets consumed.
        self.assertRedactsTo(
            'token=pre+"${{ secrets.TOKEN }}"',
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
        would be taken, and that is not English.
        """
        self.assertRedactsTo(
            'The password: is "hunter2" today',
            'The password:"<REDACTED>" "hunter2" today',
        )

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
            "password: hunter2, user: bob": 'password:"<REDACTED>", user: bob',
            "login(password=pw, user=u)": 'login(password="<REDACTED>", user=u)',
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

if __name__ == "__main__":
    unittest.main(verbosity=2)

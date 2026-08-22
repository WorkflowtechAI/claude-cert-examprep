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

import importlib.util
import os
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
            claude_review.redact("api_key = sk_live_abc123def456"),
            "api_key=<REDACTED>",
        )
        self.assertNotIn("hunter2", claude_review.redact("password: hunter2"))

    def test_a_quoted_value_is_redacted_too(self):
        # The value class excluded the opening quote, so `password: "abc123"`
        # never matched and went to the model as written.
        cases = {
            'password: "hunter2"': "password=<REDACTED>",
            "api_key='sk_live_abc123'": "api_key=<REDACTED>",
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


class TheCeilingComesFromTheEnvironment(unittest.TestCase):
    """The workflow always sets CLAUDE_REVIEW_MAX_TOKENS now, from a repository
    variable that is usually unset. So the value the script usually sees is the
    EMPTY STRING, not an absent key, and that has to mean the default."""

    DEFAULT = claude_review.DEFAULT_CLAUDE_REVIEW_MAX_TOKENS

    def test_empty_string_means_the_default(self):
        with mock.patch.dict(os.environ, {"CLAUDE_REVIEW_MAX_TOKENS": ""}):
            self.assertEqual(claude_review.max_tokens_from_env(), self.DEFAULT)

    def test_unset_means_the_default(self):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_REVIEW_MAX_TOKENS"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(claude_review.max_tokens_from_env(), self.DEFAULT)

    def test_a_repo_value_wins(self):
        with mock.patch.dict(os.environ, {"CLAUDE_REVIEW_MAX_TOKENS": " 24000 "}):
            self.assertEqual(claude_review.max_tokens_from_env(), 24000)

    def test_a_typo_costs_the_tuning_not_the_review(self):
        with mock.patch.dict(os.environ, {"CLAUDE_REVIEW_MAX_TOKENS": "lots"}):
            self.assertEqual(claude_review.max_tokens_from_env(), self.DEFAULT)

    def test_zero_and_negatives_are_typos_too(self):
        for bad in ("0", "-5"):
            with self.subTest(value=bad):
                with mock.patch.dict(os.environ, {"CLAUDE_REVIEW_MAX_TOKENS": bad}):
                    self.assertEqual(claude_review.max_tokens_from_env(), self.DEFAULT)


if __name__ == "__main__":
    unittest.main(verbosity=2)

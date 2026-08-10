---
kind: durable
---
<!-- AUTO-SYNCED from the LLM Builder Kit. Do not edit here; edit the kit source and re-run sync-standards.ps1. -->


# Review Standards

The contract every reviewer agent enforces before a change set reaches the
merge policy. Repos vendor this as `docs/standards/review-standards.md`
(synced from the builder kit); the reviewer agents (`code-reviewer`,
`security-reviewer`) apply it to every completed execute-mode task.

## What a review covers

1. **Correctness**: the change does what the task brief says, verified by
   running commands, and existing behavior it touches still works.
2. **Scope**: one concern, inside the task's `targetFiles`. Record adjacent
   fixes and drive-by refactors as findings.
3. **Definition of done**: every criterion in the brief's `definitionOfDone`
   is met and the evidence is named.
4. **Standards fit**: the change follows `docs/standards/coding-rules.md`:
   types at boundaries, dead code removed when in scope, comments explain
   intent, dependencies justified.
5. **Tests**: behavior changes come with test changes; the relevant suite
   passes; the tests still assert the intended behavior.
6. **Security**: the surfaces in the escalation list below, plus the security
   baseline in coding-rules. Any concern of any severity escalates.

## Evidence rules

- A verdict cites its findings as `file:line`.
- "Tests pass" means the reviewer ran them in this working tree, or CI ran them
  on this exact head. A summary's claim is a claim until a command confirms it.
- Review is read-and-run only. The reviewer executes checks (lint, typecheck,
  tests, builds) and leaves every source file untouched. A bug found outside
  the task's scope is recorded in the verdict reasons, never fixed in place.

## Verdict contract

The final line of a reviewer's output is exactly one line of valid JSON behind
the `REVIEW-VERDICT: ` prefix:

```
REVIEW-VERDICT: {"agent":"<code-reviewer|security-reviewer>","verdict":"<autoMerge|needs-operator>","summary":"<one dashboard row>","reasons":["<file:line concrete finding>", "..."]}
```

- `verdict: "autoMerge"`: the change satisfies every autoMerge criterion
  below; `reasons` is empty.
- `verdict: "needs-operator"`: anything else; `reasons` holds concrete,
  actionable findings.
- A missing or malformed verdict line reads as no verdict; the dispatcher
  escalates the task to the operator.

## autoMerge criteria (all required)

- Definition of done met, verified by running
- Scope atomic: one concern, inside targetFiles
- Lint, typecheck, and relevant tests pass (or the repo has none)
- Public contracts preserved unless the task explicitly changes them
- Zero security flags from either reviewer
- No escalation surface touched

## Escalation surfaces (any one forces needs-operator)

The irreversible tier only: secret/credential rotation or exposure, payment or
billing changes, destructive or irreversible data operations (deletion,
destructive migration), authorization/permission changes, force-push over
shared history. This mirrors the merge policy in AGENTS.md and the dispatcher's
escalation gate. A directed production deploy, a normal migration, or ordinary
external-API work is not on this list — those ship. Reaching prod is the
intended outcome, not an escalation.

## Verdict consumption

The dispatcher parses the verdict line and posts it as the task's `review`
status summary; the audit log records the chain. The verdict is advisory
until the operator explicitly enables auto-merge: every reviewed task still
lands as `review` for a human look. AGENTS.md's merge policy auto-merge
criterion ("reviewer verdict is `autoMerge`") refers to this contract.


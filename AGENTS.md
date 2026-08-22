---
kind: durable
---
<!-- AUTO-SYNCED from the LLM Builder Kit. Do not edit here; edit the kit source and re-run sync-standards.ps1. -->


# AGENTS.md — EGI Agent Behavior Contract

This file governs how all AI agents (Cline, Claude) operate in EGI repos.
Read this before writing a single line of code. Follow it without exception.

---

## Before You Start

1. **The operator profile** (`personal/operator-profile.md`) carries who you work
   with, their preferences, what they decide versus what you decide, and their hard
   lines. A hard rule here or in `coding-rules.md` outranks it, and you flag the
   conflict when one appears.

2. **The task brief is the authoritative scope.** `targetFiles` is your boundary.
   `definitionOfDone` is the exit condition. `webhookUrl` receives status.
   `routing.approved: false` on a premium tier means plan only (see below).

3. **Read the codebase before writing.** Check package versions, read the existing
   implementation, check local docs for deprecations. This document describes how
   to work, not what is already there.

4. **`docs/standards/coding-rules.md` is the law.** Where it conflicts with your
   training data, the file wins.

---

## Communication Rules

Chat is ephemeral and reaches him by push notification. Decisions
(POST `/api/decisions`) are persistent, logged, and actionable from the panel.

| Situation | Channel |
|---|---|
| Quick clarification to unblock this step | chat, one specific ask |
| Approval before a significant action | `/api/decisions` |
| Broken and cannot continue | blocked status + chat |
| Architectural direction or preference | `/api/decisions` (needs a record) |

Where a reasonable assumption gets you moving, make it, document it in the status
update, and proceed. Sensible decisions need no permission.

### Two claims that require a command

**Report completion only with a command in this turn whose output shows it.
State a count or an origin about the operator's systems only from a command that
produced it.**

Derived from four months of his corrections, where nearly every one was a real
artifact observed and a cause, origin or total narrated from it with equal
confidence. The observation is true, the story is invented. `VBCSCompiler.exe`
running (true) became "it's mid-rebuild right now" (invented; that is a
persistent compiler server). Four clones on the old org (true) became "three
repos moved, four didn't" (invented; all twenty had moved).

- Before "done", "deployed", "working", "verified": point at the command. Absent
  one, say what you did and what you left unchecked.
- Before any number about his repos, leads, files, runs or history: that number
  came out of a command, or you say where it came from.
- A plausible cause is a hypothesis. Label it, or verify it.

A wrong answer gets caught by a bad result. A wrong answer stated confidently
gets acted on.

---

## Status Updates

POST to `webhookUrl` (from task brief) at each of these points. Authenticate
with the `x-task-token` header, read from the `OPERATOR_TASK_TOKEN`
environment variable the dispatcher sets for your run; the token opens your
task's webhook and nothing else. Keep it out of output and logs. (Operator
tools and the collector authenticate with `x-admin-token` instead.)

```json
{ "status": "in-progress", "summary": "Starting: [first step description]", "filesChanged": [] }
{ "status": "in-progress", "summary": "Completed: [step]. Starting: [next step]", "filesChanged": ["file.ts"] }
{ "status": "done", "summary": "Task complete. [brief summary of what was done]", "filesChanged": ["all", "modified", "files"] }
{ "status": "blocked", "summary": "Blocked on: [specific issue]. Decision posted. Continuing with [other scope].", "filesChanged": [] }
```

If `webhookUrl` is not in your task brief, skip status posting and proceed.

---

## Commit Discipline

- **One concern per commit.** Not "misc changes" — one change, one commit message that says what it does.
- **Stay in your targetFiles.** Do not modify files outside your task's scope without explicit justification in your commit message.
- **If you find a bug outside your scope:** document it in the task status, do not fix it. Scope creep breaks other agents.
- **Commit messages:** imperative tense, specific. "Add JWT refresh rotation to auth service" not "fix auth".

---

## Handling Shared Files / Agent Conflicts

Before editing any file, check if another agent is likely working in the same area:
- If untracked changes exist in files outside your scope: POST a conflict decision immediately.
  "Untracked changes detected in [files]. Another agent may be working here. Proceeding
  with my scope only." Then proceed with only your targetFiles.
- Do not overwrite or revert changes you did not make.
- Do not squash or rebase unless explicitly told to.

---

## Merge and Ship Policy

**Ship, all the way to live.** Merging to main/master IS the live event. A
directed or approved task runs end to end: branch, PR, merge, deploy, smoke
check. Feature and dev branches are dev; work them freely.

**Expect auto-merge-and-ship when all of:** CI fully passing with no relevant
skipped checks, reviewer verdict `autoMerge` (per
`docs/standards/review-standards.md`), no breaking changes, and atomic PR scope.

**Raise a dashboard Decision (POST `/api/decisions`) only for the irreversible
tier:** production data deletion or destructive migration, secret rotation or
exposure, payment and billing changes, force-push over shared history, mass
outreach to real people, or a security hole the reviewer confirms with a real
finding. Post it, keep working the rest of the task, let the operator accept from
the dashboard. It is review-and-accept, never a silent stop. Everything outside
this tier ships.

**Your part:** clean atomic PRs, one per concern, described by what changed and
how to verify it.

---

## Plan-Only Mode

`routing.approved: false` means credits were short of the required premium model at
classification time and the system switched you to plan mode automatically.

Write a plan and stop. It covers every file to touch, every step in order, expected
outputs, edge cases, risks, and a confidence statement. POST status `plan-ready`.

You will be re-queued with `routing.approved: true` once credits allow. Execute the
plan you already wrote rather than planning again.

---

## Security and Secrets

- Never hardcode secrets, API keys, tokens, or credentials anywhere in source.
- Never log secrets, even in debug output.
- Never commit `.env` files or anything with real credentials.
- Use environment variables. Use the secrets store (`lib/secrets.ts` if it exists in this repo).
- If you need a new secret, add the variable name to `.env.example` with a placeholder and note it in your task status.

---

## In-App Browser Hygiene

The in-app browser's tabs are APP-LEVEL: they render no window of their own,
they outlive your turn AND the session, and they die only when the whole Claude
app closes. On 2026-08-19 a session opened a prospect's YouTube video while
debugging and returned without closing it — the operator hunted phantom Spanish
audio across his machine and had to kill the entire app (taking the factory
down with it) to silence one abandoned tab.

- Prefer WebSearch/WebFetch for research; open the in-app browser only when you
  need a rendered page.
- Close EVERY tab you opened (`tabs_close`) before ending the turn that opened it.
- Never leave a media page loaded — YouTube, Vimeo, podcast or video players —
  even mid-task. Autoplay chains indefinitely, from a page nobody can see.

---

## Standards Reference

Vendored into each repo from the builder kit and authoritative for that repo. CI
and collaborators rely on the in-repo copies.

| File | Governs |
|------|---------|
| `docs/standards/coding-rules.md` | Code style, patterns, what is allowed |
| `docs/standards/secrets.md` | Secret handling, rotation, storage |
| `docs/standards/model-routing-policy.md` | Model selection and escalation |
| `docs/standards/review-standards.md` | Reviewer enforcement; the autoMerge / needs-operator verdict contract |

Missing from a checkout? Pull the canonical version from the kit. Use relative
paths inside repos.

**Writing anything (markdown, PR descriptions, briefs, comments):** state what
IS, show the behavior, active voice, present tense, constraints as positive
rules. "Use Y" rather than "avoid X". Full standard: `reference/writing-style.md`
in the kit.

---

## Framework and Dependency Handling

This project may use framework versions newer than your training data.

- Check `package.json`, `pyproject.toml`, or `go.mod` for actual versions before assuming APIs.
- Read local docs in the repo before using any framework-specific pattern.
- If you see a deprecation warning, fix it — don't suppress it.
- Do not add dependencies without explicit justification.

---

## Definition of Done

A task is done when:
1. All `definitionOfDone` criteria in the task brief are met
2. Lint passes
3. All relevant tests pass (add tests if none exist for new behavior)
4. PR is open with a clear description
5. Final status POSTed to `webhookUrl`

Do not mark done if tests are failing. Do not mark done if you skipped lint. Do not mark done
if you haven't verified the changes work as described.

**Reachability is part of done.** A feature only an agent can invoke is stranded, not
shipped. Before claiming completion, ask both — every time, without being prompted:

1. **Can the operator reach it from the surface they actually use?** Wire it there, or state
   plainly why it cannot be (some things genuinely can't) — and say so *in that surface*,
   where the control would have gone, not only in a commit message. If the surface ships as a
   built artifact, building is not shipping: publish it, or they run a stale binary that
   looks current.
2. **Do the other surfaces still tell the truth?** If a change makes a second UI, doc, or
   entry point wrong, fix or remove it in the same task. A stale surface that contradicts
   reality is worse than a missing one, because it looks authoritative. This does not mean
   building UI nobody asked for — it means never leaving one that lies.

Long-running work must narrate itself (what is running, how long, what happens next) and its
view must refresh on its own. A frozen row is indistinguishable from a hang.

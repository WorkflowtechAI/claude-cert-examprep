<!-- AUTO-SYNCED from the LLM Builder Kit. Do not edit here; edit the kit source and re-run sync-standards.ps1. -->

# Secure Workflow and Secret Locations

This file records where secrets should live. It must never contain secret values.

## Single source of truth for provider keys

Provider API keys (`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`) have exactly one
value store on the operator's machine: the gitignored
`operator-admin/.env.local`. Enter and rotate them there — via the dashboard
**Settings** tab, which writes the file, masks the value in the UI, and verifies
it against the provider on save. Never hand-edit keys into tracked files, and
never paste a key value into a chat, issue, log, or screenshot.

Two kinds of consumer read from that one store:

- **Local factory** — the panel, the routing workers, and the interactive
  surface resolve keys live from `.env.local` (process env first, file second),
  so a freshly entered key takes effect without a restart. The local dispatcher
  runs `claude -p` under the operator's Claude login, so it needs no key here.
- **CI** — the per-repo `claude-review` workflow runs on GitHub and cannot read
  the local file, so it needs `ANTHROPIC_API_KEY` as a repo Actions secret.
  GitHub secrets are write-only (no read-back, no copy-across), so they are
  re-issued from the local source by `operator-tools/Sync-Secrets.ps1`, which
  fans the value out via `gh secret set` without it ever touching a command
  line, console, or log. Set once locally → `Sync-Secrets.ps1` → every repo's CI.

### Presence vs. value (the real boundary)

**Presence is queryable; the value is not handed out.** The key `.env.local`
holds is plaintext on the operator's disk — readable by anything running as that
user (a script, `cat`, `process.env`, an agent with shell). A file-based secret
does not defend against code running with your privileges; nothing on the local
box can. So the model is not "guard the file" — it is:

- **Presence** — `operator-tools/Get-KitSecret.ps1 <NAME>` reports masked status
  and an exit code (0 set, 1 unset). No value path. Safe to expose and to let any
  readiness check or agent call.
- **Value** — only ever *used* by a broker that performs one action and returns
  the result, never the key: `Sync-Secrets.ps1` sets a GitHub Actions secret; the
  panel/workers resolve it in-process (`lib/settings.ts` `resolveKey`) and call
  the provider server-side; CI reads it from the repo's Actions secret. Agents get
  brokered actions and scoped tokens, **not** raw keys.

Do not add a "print the value" command, and do not teach one in a repo: that is an
exfiltration signpost, not a boundary. If a key is missing the answer is "set it
in the dashboard Settings tab," never "go find it."

The threat that matters is **untrusted external code, not your own directed
agents**: fork-PR CI, third-party dependencies, connected tools/MCP servers, and
untrusted repo content that could hijack an otherwise-trusted agent. A file store
is fine against agents you trust to do directed work; the defenses that matter
live at the ingress points where someone else's code shows up:

- **CI / fork PRs** — the claude-review workflow runs on `pull_request_target`
  but guards `head.repo.full_name == github.repository`, so a fork PR gets no job
  and no secret; it also checks out the base SHA (not the PR head) and only diffs,
  never executes, PR code. Other CI uses plain `pull_request`: fork PRs get a
  read-only token and no secrets.
- **Self-hosted runners** — a fork PR can otherwise run its code on your hardware.
  Require approval for outside-collaborator / fork-PR workflows, and run the runner
  as a dedicated unprivileged user (see `self-hosted-runner-setup.md`). This is the
  main control for public repos.
- **Fan-out surface** — every repo you push the key to is an attack surface. Keep
  `Sync-Secrets.ps1` scoped to repos carrying the guarded workflow (its default),
  and prefer private repos for the review key.
- **Agent hijack** — a trusted agent can be turned by untrusted input, so the key
  stays server-side, out of any context that processes untrusted content: the
  interactive surface calls the provider server-side and never exposes the key to
  the model's tool-space.

For stolen-disk/backup threats (not local-process ones) you can additionally
encrypt at rest (Windows DPAPI / Credential Manager).

## Local development

- `.env` and `.env.local` stay on the developer machine and are ignored by Git.
- Commit `.env.example` with names, descriptions, and safe placeholders only.
- Use OS/user-level secret stores for long-lived personal credentials when possible.
- Do not paste secret values into LLM chats, issue comments, docs, screenshots, logs, or test output.

## GitHub

Use GitHub Actions secrets and environments for deployment credentials.

Typical names:

- `VPS_HOST`
- `VPS_USER`
- `VPS_SSH_KEY`
- `MODULE_DEPLOY_KEY`
- provider keys such as `ANTHROPIC_API_KEY`
- app secrets such as `JWT_SECRET`, `SESSION_SECRET`, `DATABASE_URL`

Rules:

- environment-specific secrets belong to GitHub Environments;
- deploy keys are scoped to the one repo/module that needs them;
- rotate secrets after suspected exposure;
- never print secrets during workflow debugging.

## SSH

- Personal SSH keys stay under the user profile `.ssh` directory with normal OS permissions.
- Deploy keys are stored as GitHub Actions secrets or on the target server, not in repos.
- Known hosts are pinned or populated during CI with care.
- Do not give LLM agents raw private keys.

## VPS/server

- Runtime env files live on the server with restricted permissions.
- Services load env through systemd, Docker secrets, or a protected env file.
- Backups must not be publicly reachable.
- Logs should not include tokens, auth headers, cookies, or sensitive request bodies.

## Agent/tool access

- Prefer brokered tools that perform one safe action over broad shell access.
- Scope tokens to the exact repo/service/action.
- Use short-lived credentials where available.
- Redact before returning tool output to model context.


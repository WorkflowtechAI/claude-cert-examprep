# AUTO-SYNCED from the LLM Builder Kit. Do not edit here; edit the kit
# source and re-run sync-standards.ps1.

"""Is this commit ready to go to production? One read of the GitHub API, no waiting.

WHY THIS EXISTS. Every deploy in the fleet used to wait for CI INSIDE a job: a
loop on a self-hosted runner, polling every 20s for up to 40 minutes. The org
pool is three runners on one VPS. On 2026-10-06 the standards sweep pushed to
fifteen repos at once, three deploys started, each took a runner to wait for CI,
and the CI they waited for could not get a runner. Nothing moved for 40 minutes
until the waits timed out, and the next two queued deploys did it again. A job
that waits on the pool while holding a slot of the pool deadlocks it, and more
runners only move the threshold on a box already at capacity.

So nothing waits. The deploy workflow is started by `workflow_run` each time a
CI workflow on the default branch COMPLETES, and calls this once:

  ship    every other workflow's newest run on the commit is green, the commit
          is still the branch tip, and it is not already live      -> ship=true
  wait    something is still running; its own completion starts the deploy
          workflow again, so the last one to finish ships it       -> ship=false
  red     a newest run failed, or was cancelled with nothing newer
          coming                                                   -> exit 1

ONE VERDICT PER WORKFLOW, FROM ITS NEWEST RUN. On 2026-09-23 iambraun.com got
two push event sets for one ref update (b3b6029), two seconds apart. CI's
cancel-in-progress killed the first set and the second went green, so older runs
of a workflow are superseded by its newest, and runs of the deploy workflow
itself are never CI.

THE RUN THAT TRIGGERED US IS COMPLETE, whatever the API says. The event fires on
completion, but the runs list can lag it by a few seconds. If the last workflow
to finish read itself as still running, it would decide "wait" and nobody would
be left to start the deploy again. TRIGGER_RUN_ID / TRIGGER_CONCLUSION override
the listed state of that one run.

NOT THE TIP, NOT SHIPPED. A slow CI run on an older commit can finish after a
newer push. Shipping it then would roll production back. The newer commit's own
CI starts its own deploy.

ALREADY LIVE, NOT SHIPPED TWICE. Two workflows can finish in the same second and
both find everything green. The deploy job sets the commit status
`deploy/production`; a success there means this commit is live already.

Environment: REPO, GH_TOKEN, SHA, BRANCH, RUN_ID (this deploy run), and
optionally TRIGGER_RUN_ID, TRIGGER_CONCLUSION, HEAD (a local checkout's HEAD,
compared with SHA), FORCE=true (skip the CI verdict; tip and live checks stay).
Writes ship=true|false and reason=... to $GITHUB_OUTPUT when it is set.
"""
import json
import os
import sys
import urllib.error
import urllib.request

FAILED = ("failure", "timed_out", "startup_failure")
STATUS_CONTEXT = "deploy/production"


# The token rides in a header, and urllib's default redirect handler copies
# headers onto whatever host a Location names. Refuse redirects: a 3xx then
# raises like any other status and is reported below.
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def api(repo, token, path):
    req = urllib.request.Request(
        "https://api.github.com/repos/%s/%s" % (repo, path),
        headers={"Authorization": "Bearer %s" % token,
                 "Accept": "application/vnd.github+json"})
    try:
        with _OPENER.open(req) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # A traceback is what the first deploy printed on a 403, naming neither
        # the permission nor the remedy. Say both.
        if e.code in (401, 403):
            sys.exit("::error::GET %s returned %d. The job's GITHUB_TOKEN cannot read it; "
                     "the workflow needs `actions: read` and `statuses: read` in its "
                     "permissions block, or the repository restricts the token." % (path, e.code))
        sys.exit("::error::GET %s returned %d %s" % (path, e.code, e.reason))


def decide(env, get):
    """Returns (ship, reason). Exits 1 on a red commit."""
    sha, branch = env["SHA"], env["BRANCH"]

    if env.get("HEAD") and env["HEAD"] != sha:
        return False, ("checked out %s, not %s: a newer push moved %s, and that "
                       "push's own CI starts its deploy" % (env["HEAD"][:7], sha[:7], branch))

    tip = get("git/ref/heads/%s" % branch)["object"]["sha"]
    if tip != sha:
        return False, ("%s is no longer the tip of %s (now %s); shipping it would roll "
                       "production back, and the tip's own CI starts its deploy"
                       % (sha[:7], branch, tip[:7]))

    for s in get("commits/%s/status" % sha).get("statuses", []):
        if s.get("context") == STATUS_CONTEXT and s.get("state") == "success":
            return False, "%s is already live (%s is success)" % (sha[:7], STATUS_CONTEXT)

    if env.get("FORCE") == "true":
        return True, "forced: CI verdict skipped by the operator"

    own = get("actions/runs/%s" % env["RUN_ID"])["workflow_id"]
    newest = {}
    for r in get("actions/runs?head_sha=%s&per_page=100" % sha)["workflow_runs"]:
        w = r["workflow_id"]
        # CI is what the PUSH started. A scheduled job (EGI_bot runs a nightly
        # backup and three more crons on master) also carries the tip's SHA,
        # and its failure says nothing about this commit's code.
        if r.get("event", "push") != "push":
            continue
        if w != own and (w not in newest or r["run_number"] > newest[w]["run_number"]):
            newest[w] = r
    trig = env.get("TRIGGER_RUN_ID")
    for w, r in newest.items():
        if trig and str(r["id"]) == trig:
            newest[w] = dict(r, status="completed",
                             conclusion=env.get("TRIGGER_CONCLUSION") or r.get("conclusion"))
    runs = list(newest.values())

    failed = [r for r in runs if r["status"] == "completed" and r["conclusion"] in FAILED]
    if failed:
        for r in failed:
            print("::error::%s concluded %s: %s" % (r["name"], r["conclusion"], r["html_url"]))
        sys.exit("CI is not green on %s, so it is not going to production." % sha[:7])

    pending = [r for r in runs if r["status"] != "completed"]
    if pending:
        return False, ("waiting on %s; the last of them to finish starts this deploy again"
                       % ", ".join(sorted(r["name"] for r in pending)))

    cancelled = [r for r in runs if r["conclusion"] == "cancelled"]
    if cancelled:
        names = ", ".join(sorted(r["name"] for r in cancelled))
        sys.exit("::error::%s was cancelled on %s and nothing newer is running, so CI never "
                 "finished on this commit and it is not going to production. Re-run the "
                 "cancelled workflow; its completion starts this deploy again." % (names, sha[:7]))

    # No checks at all is a fact worth printing, not a silent pass: a repo with
    # no CI is deploying on nothing but a merge button.
    if not runs:
        return True, "no other workflow runs on %s; nothing to wait for" % sha[:7]
    return True, "%d workflow(s) on %s, newest run of each green" % (len(runs), sha[:7])


def main():
    env = dict(os.environ)
    ship, reason = decide(env, lambda path: api(env["REPO"], env["GH_TOKEN"], path))
    print(("ship: " if ship else "not shipping: ") + reason)
    out = env.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            flag = "true" if ship else "false"
            f.write("ship=%s\nreason=%s\n" % (flag, reason.replace("\n", " ")))


if __name__ == "__main__":
    main()

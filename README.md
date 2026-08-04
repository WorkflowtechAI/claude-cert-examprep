# Claude Certification Exam Prep Coach

A study-coach skill for Anthropic's Claude certifications, three lanes under one coach:

| Lane | Credential | Code |
|---|---|---|
| Architect, Foundations | Claude Certified Architect: Foundations | CCAR-F |
| Architect, Professional | Claude Certified Architect: Professional | CCAR-P |
| Developer, Foundations | Claude Certified Developer: Foundations | CCDV-F |

I built this coach while preparing for these exams myself. I hold all three
certifications plus the Claude Partner Badge; you can verify every one of them on
[Credly](https://www.credly.com/users/david-braun.c33bef7f). The coach runs the same
loop I used: profile, baseline diagnostic, a phased plan sized to your real calendar,
domain drills, timed mocks, and a progress check that adjusts the plan when the numbers
move.

## What it does

- Aligns every plan and practice item to the exam blueprints, and treats your official
  Exam Guide PDF as the source of truth the moment you attach it.
- Generates original practice items in the exam's shape (scenario-grounded for CCAR-F,
  multiple-response where the lane uses it). It never reproduces real exam questions.
- Teaches through a reasoning model: six master principles and eight named distractor
  classes, so you learn to recognize the trap, not just the answer.
- Tracks a projected scaled score against the 720 passing line and keeps the gap visible
  in every output.

## Install

For Claude Code, clone into your skills directory:

```bash
git clone https://github.com/GestaltWorks/claude-cert-examprep.git ~/.claude/skills/claude-cert-examprep-coach
```

For a claude.ai Project, paste the contents of `SKILL.md` into the project instructions
and attach your official Exam Guide PDF.

## Use

Start with `/lane` to pick a certification, then `/profile`. The full command set:

```
/lane /profile /diagnostic /prep-plan /weekly-plan /drill [#]
/mock [short|standard|full] /resources /score-check
```

## Honesty notes

- The CCAR-P tables come from the official Exam Guide v1.0. The CCAR-F and CCDV-F tables
  were compiled from public exam-guide summaries in August 2026. Blueprints drift, so the
  skill re-checks its facts against your attached PDF and flags anything that moved.
- The coach generates original practice situations every time. If an item ever resembles
  a real exam question, that is coincidence, and the skill's rules forbid reproducing one.

If you use this and something reads wrong or an exam changes shape, open an issue. I am
glad to keep it current.

## License

MIT. See [LICENSE](LICENSE).

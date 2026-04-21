# CLAUDE.md

## Purpose

Claude Code entry point for this repository.
Use this file to orient work quickly and then defer to the canonical instructions.

## Precedence

- Canonical instructions live in `AGENTS.md`.
- If this file conflicts with `AGENTS.md`, follow `AGENTS.md`.
- Before any design or implementation work, read `AGENTS.md` first.

## Key References

- Repository docs: `README.md`, `<domain>/README.md`, `<domain>/docs/DESIGN.md`

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

### Precedence: superpowers vs gstack

Superpowers skills handle process and methodology (TDD, verification, code review
discipline, git worktrees, plan writing). Gstack skills handle domain-specific
workflows (QA testing, deploy pipelines, design consultation, investigation).

When both could apply, superpowers governs HOW you work, gstack governs WHAT workflow
to run. They compose, not compete. For example: `/investigate` (gstack) finds the root
cause using `superpowers:systematic-debugging` methodology.

Specific conflict resolutions:

- Brainstorming a new feature → `superpowers:brainstorming` first (intent/requirements),
  then `/office-hours` if the user wants a full design doc
- Debugging → `/investigate` (gstack), which uses systematic-debugging principles
- Shipping / PR creation → `/ship` (gstack), which handles the full workflow
- Code review → `superpowers:requesting-code-review` for process,
  `/review` (gstack) for automated diff analysis. Both can run.
- Implementation planning → `superpowers:writing-plans` for plan structure,
  `/plan-eng-review` (gstack) for architecture review of an existing plan
- Before claiming work is done → `superpowers:verification-before-completion` always applies
- TDD, git worktrees, subagent-driven-dev → superpowers only (no gstack equivalent)

### Gstack routing rules

- Product ideas, "is this worth building", full design doc → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Automated diff review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review of existing plan → invoke plan-eng-review
- Save progress, checkpoint, resume → invoke checkpoint
- Code quality, health check → invoke health

## Coding conventions

- **Time format:** All deterministic time output (narrative strings, fact summaries, log messages, timestamps) must use 24-hour format (e.g. `13:00`, not `1pm`).

## Scope

- v0 is CLI-only (`python -m agents.orchestrator --<agent> …`).
  Webpage selection and frontend player are deferred to v1.

## Note

This file intentionally stays short to avoid drift.
Use it as an entry point only.
Keep operational rules, workflow gating, and domain-specific requirements in `AGENTS.md`.

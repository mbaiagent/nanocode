# Maintenance Playbook (English summary)

The full, authoritative playbook is in Chinese: [agent-playbook.md](agent-playbook.md). Quick version:

- **May do alone:** review PRs and squash-merge clean ones; open and evaluate issues; open fix PRs.
- **Ask the owner first:** force-pushing or rewriting history, deleting anything (ask first before deleting anything, except deleting a PR's head branch after merging it), changing repo settings or secrets, merging PRs with architecture risk or carrying the `需要人工决策` (needs human decision) PR label or marked "do not merge / WIP", enabling security-gated features in production.
- **Never bypass CI.** Run both test suites locally (`PYTHONPATH=. python3 -m unittest discover -s tests`; `PYTHONPATH=server:server/tests:. python3 -m unittest discover -s server/tests`) and wait for all 8 PR CI jobs.
- **Public repo:** placeholders only; never print secrets; commit with a noreply email; docs change in the same PR as code.
- **Automations:** weekday 09:00 PT read-only code patrol (max 5 issues); new-issue triage every 2h on weekdays (max 3, comment `🤖 评估：`, fix via PR with `Fixes #N`, never merge your own fix); PR review on open (squash-merge pinned to the reviewed head SHA; delete the branch only if no open PR uses it as base; re-review after pushes); Monday managed-Redis keepalive check.
- **gh quirk:** `gh pr edit` / `gh issue view` fail with a Projects (classic) error, so use `gh api`.
- **Reports:** concise Chinese, with links and merge SHAs, times in PT, silent when nothing changed.

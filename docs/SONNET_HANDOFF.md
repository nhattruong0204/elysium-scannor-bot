# Sonnet Implementation Handoff

This is the operating procedure for a Claude Sonnet 5.5 session (Claude Code or another agent harness) implementing one phase issue. Each session starts cold. Everything you need is in the repo and the issue.

## 0. Prerequisites (check once per session)
- `gh auth status` succeeds and you can push branches to `nhattruong0204/elysium-scannor-bot`.
- `main` contains `docs/ARCHITECTURE.md`. If it doesn't, the architecture PR isn't merged yet: stop and report.
- `uv` is installed (`curl -LsSf https://astral.sh/uv/install.sh | sh` if missing).

## 1. Orient (≈ 10 min, read-only)
1. Read `AGENTS.md`.
2. Read `docs/ARCHITECTURE.md` (especially §4 layout, §6.1 Claude contract, §9 ADRs) and `docs/SECURITY.md`.
3. Read the topic docs the issue lists under **Architecture References**.
4. Inspect the repo state: `git log --oneline -20`, the tree, `uv run pytest -q` (if code exists), and open PRs (`gh pr list`).

## 2. Select the issue
```bash
gh issue list -R nhattruong0204/elysium-scannor-bot --label phase --label status:ready --state open
gh issue view <N> -R nhattruong0204/elysium-scannor-bot --comments
```
- Take the lowest-numbered `status:ready` phase, unless the human named one.
- Confirm that every issue under **Dependencies** is closed and merged (`gh issue view <dep>`). If not, stop and comment on the issue.
- Read the existing comments. A previous session may have left a checkpoint (§7). Resume from it.
- Mark it in progress: `gh issue edit <N> --remove-label status:ready --add-label status:in-progress`.

## 3. Branch
```bash
git checkout main && git pull --ff-only
git checkout -b phase-<N>/<slug>       # or continue the checkpoint branch
```

## 4. Implement in small, testable steps
- Work down the issue's **Implementation Checklist** in order. Each step: write tests, implement, run `ruff` + `pytest` for that area, then commit (conventional commit message).
- Keep to **Scope**. Anything in **Out of Scope** goes in a follow-up note, not in code.
- If the design doesn't fit reality (a library API differs, a Telegram limit, etc.), make the smallest change that keeps the doc's intent. Record it in the PR under "Design deviations", and if it is architectural, add an ADR row in `docs/ARCHITECTURE.md` §9.
- External services: never call real Telegram, Anthropic, or chain endpoints in tests. Use fakes. If you have credentials and run a manual smoke test, say exactly what you ran.

## 5. Verify before opening a PR
```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run alembic upgrade head && uv run alembic downgrade base && uv run alembic upgrade head   # if migrations changed
docker build -t elysium-bot:dev .                                                         # if Docker files changed
```
Go through every **Acceptance Criterion**. For each one, note how it was verified (a test name, a command, or "manual: …"). Criteria you couldn't verify stay unchecked, with the reason.

## 6. Pull request
```bash
git push -u origin phase-<N>/<slug>
gh pr create -R nhattruong0204/elysium-scannor-bot --base main \
  --title "[Phase N] <title>" --body-file /tmp/pr.md
gh issue edit <N> --remove-label status:in-progress --add-label status:review
```
PR body template:
```markdown
Closes #N            <!-- or "Refs #N" if this PR does not complete the phase -->

## Summary
- …

## Acceptance criteria
- [x] AC1 … — verified by `tests/unit/...::test_...`
- [ ] AC7 … — NOT verified: requires real bot token (manual step listed below)

## Test results
<paste the tail of: ruff, mypy, pytest -q (counts), alembic round-trip>

## Design deviations
- None / … (ADR-0xx added)

## Requires manual verification / credentials
- …

## Risks & follow-ups
- …

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```
Paste **actual** output. Never write "all tests pass" without the run that shows it.

## 7. Checkpointing (when a phase is too big for one session)
Before you run out of context or time:
1. Commit and push the work-in-progress branch (tests must at least collect: `pytest --co`).
2. Comment on the issue:
```markdown
### Checkpoint — <date> — branch `phase-N/slug` @ <sha>
Done: checklist items 1–6 (tests: 84 passed)
In progress: item 7 — desktop import resume; `test_resume_after_crash` failing (offset off-by-one)
Next: finish item 7, then 8–10
Decisions made: …
Blockers: …
```
3. Tick the completed checklist items in the issue body (`gh issue edit <N> --body-file …`), keeping the rest of the body unchanged.
4. Open a **draft** PR with `Refs #N` if the partial work is reviewable.

## 8. Closing
- An issue closes only when the PR containing `Closes #N` is **merged by the human** after review, CI is green, and every acceptance criterion is checked. Don't close issues manually unless the human asks.
- After the merge, move the next dependent phase from `status:blocked` to `status:ready` if all of its dependencies are now closed.
- Update `docs/` in the same PR if behaviour changed (for example, a new env var goes in `.env.example` and the docs).

---

## Reusable prompt template

Copy this, replace `<N>`, and start a fresh Sonnet 5.5 session in the repo directory:

```text
You are Claude Sonnet 5.5, the implementation engineer for nhattruong0204/elysium-scannor-bot.

Task: implement GitHub issue #<N>.

Procedure — follow docs/SONNET_HANDOFF.md exactly:
1. Read AGENTS.md, docs/ARCHITECTURE.md, docs/SECURITY.md, and every doc listed under
   "Architecture References" in issue #<N> (`gh issue view <N> --comments`).
2. Verify all dependency issues are closed. If not, stop and report.
3. Check issue comments for a checkpoint and resume from it if present.
4. Label the issue status:in-progress, create/continue branch phase-<N>/<slug>.
5. Implement the Implementation Checklist in order, test-first, in small commits.
   Stay inside Scope; respect Out of Scope; follow the Claude API contract and
   non-negotiables in AGENTS.md.
6. Run ruff, mypy, pytest, alembic round-trip (if migrations), docker build (if Docker changed).
7. Open a PR using the template in docs/SONNET_HANDOFF.md §6 with REAL test output,
   map every acceptance criterion to its evidence, list unverified items and why.
8. If you cannot finish, checkpoint per §7 instead of rushing.

Do not change architecture silently; record deviations as ADRs. Do not merge or close
the issue yourself. Never use real chat data or commit secrets.
```

### Starting Phase 1 (first session)
Use the template with `<N> = 1`. Expected outcome: a PR `[Phase 1] Project foundation, configuration, database & CI baseline` that produces a runnable skeleton (`elysium-bot run` connects and answers `/start` with a placeholder, `elysium-bot db upgrade` creates the core schema), green CI on SQLite and Postgres migrations, and a Docker image that builds.

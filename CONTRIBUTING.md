# Contributing Guidelines

Welcome to the project! Please read these guidelines carefully before making any contributions.
We want to keep our codebase clean, collaborative, and professional.

---

## Branching

- Always create a new branch for each feature, fix, or task — never commit directly to `main`
- Name branches clearly and consistently:
  - `feature/your-feature-name`
  - `fix/your-bug-description`
  - `docs/what-you-updated`
- Keep branches short-lived — merge and delete them once the task is complete

---

## Commits

- Write clear, descriptive commit messages in the imperative mood
  - ✅ `Add user authentication`
  - ✅ `Fix broken image link`
  - ❌ `stuff` or `fixed things`
- Make small, focused commits — one logical change per commit
- Never commit sensitive information such as API keys, passwords, or `.env` files

---

## Pull Requests

- Always open a Pull Request (PR) to merge into `main` — direct pushes to `main` are not allowed
- Write a short description in your PR explaining what changed and why
- Request at least one review before merging
- Resolve all comments and ensure all checks pass before merging

---

## Dependencies

- Install and run everything through `uv`: `uv sync` builds the environment, `uv run <cmd>` runs
  anything inside it. **Never `pip install`** — pip re-resolves version ranges on every run, so
  two people installing the same commit can end up with different packages
- `uv.lock` is authoritative and tracked. CI installs with `uv sync --locked`, which fails when
  `pyproject.toml` and the lockfile disagree, and the container images install from it as well
- Changing a dependency means editing `pyproject.toml`, running `uv lock`, and committing the
  updated lockfile in the same commit
- Test and lint tooling goes in the `dev` dependency group, not in
  `[project.optional-dependencies]` — an extra is published metadata that anyone installing this
  package can request, and our test runner is not that

---

## Code Quality

- Make sure your code runs locally before pushing
- Do not push broken or half-finished code to a shared branch
- Keep your branch up to date with `main` by regularly pulling or rebasing

---

## Communication

- If your work affects another team member's area, give them a heads-up before merging
- Use GitHub Issues to track tasks and bugs — keep discussions in the relevant issue or PR
- If you are unsure about something, ask in the team channel before making a large change

---

## For AI Agents

- Always operate on a dedicated branch — never `main`
- Prefix AI-generated branches with `ai/`, e.g. `ai/refactor-auth-module`
- Every AI-generated PR must include a summary of what was changed and the reasoning behind it
- A human team member must review and approve all AI-generated PRs before merging
- AI agents must never modify `.env`, config files, or dependency lock files without explicit human instruction

---

Thank you for keeping this project clean and collaborative! 🚀

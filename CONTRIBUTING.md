# Contributing Guidelines

Welcome to the project! Please read these guidelines carefully before making any contributions.
We want to keep our codebase clean, collaborative, and professional.

---

## Branching

- Always create a new branch for each feature, fix, or task — never commit directly to `main`
- Name branches clearly and consistently:
  - `feature/#ISSUE_NR-your-feature-name`
  - `bugfix/#ISSUE_NR-your-bug-description`
  - `docs/#ISSUE_NR-what-you-updated`
  - `refactor/#ISSUE_NR-what-you-refactor`
  - `experimental/#ISSUE_NR-not-necessarily-merged-into-main`
- Keep branches short-lived — merge and delete them once the task is complete
- For minor work which is not covered by an issue `#ISSUE_NR` should be set to `NOREF`

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

- Install and run everything through `uv`: `uv sync --all-packages --all-extras` builds the
  environment, `uv run <cmd>` runs anything inside it. **Never `pip install`** — pip re-resolves
  version ranges on every run, so two people installing the same commit can end up with
  different packages
- This is a workspace, so **a bare `uv sync` is an error**: the root `pyproject.toml` has no
  `[project]` table and there is nothing for it to install. Pass `--all-packages`, or
  `--package themis-<member>` to install one member's closure alone. There is a single `.venv`
  at the root either way — `--package` *replaces* its contents rather than adding to them
- `uv.lock` is authoritative and tracked. CI installs with `uv sync --locked`, which fails when
  `pyproject.toml` and the lockfile disagree, and the container images install from it as well
- Changing a dependency means editing the **owning member's** `pyproject.toml`, running
  `uv lock` at the root, and committing the updated lockfile in the same commit
- Test and lint tooling goes in the `dev` dependency group, not in
  `[project.optional-dependencies]` — an extra is published metadata that anyone installing this
  package can request, and our test runner is not that

### IDE setup (PyCharm)

PyCharm may report `Unresolved reference 'themis_shared'` on every cross-member import — for
example `from themis_shared.config import get_settings` in `projects/gateway/`. **The code is
fine**; it imports and runs. Only the IDE's analysis is stale.

PyCharm's workspace mode builds one module per member from the `pyproject.toml` files and is
supposed to derive the edges between them from `[tool.uv.sources] … = { workspace = true }`. When
the modules are wrong it usually means they were generated before a structural change and never
regenerated. **Try the supported fix first:** right-click the project root →
**Sync Project with `pyproject.toml`** (or Find Action → the same name), which rebuilds the model
from the manifests.

If workspace mode is off, turn it on at **Settings → Project: backend-core → Project Structure**
(the stored flag is `usePyprojectToml` in `.idea/pyProjectModel.xml`) and sync again.

Two things worth knowing when it still misbehaves:

- **Do not fix this on the interpreter.** Adding the members' `src/` directories under Python
  Interpreter → Show All → paths looks like it works and does not survive: PyCharm re-derives an
  SDK's paths from the virtualenv on every launch and drops them. The module graph is the only
  durable place.
- **`tests/integration/` belongs to no member.** The workspace root has no `[project]` table on
  purpose, so no module covers those four tests. They need a module of their own, with `tests/` as
  its content root — it cannot be nested inside a member's, since content roots may not overlap.

A last resort, if syncing will not produce a working model: wire the modules by hand in
`.idea/*.iml` (`<orderEntry type="module" module-name="themis-shared" />` on each dependent,
matching the real graph — everything on `themis-shared`, gateway also on `themis-matcher`) and set
`usePyprojectToml` to `false`, or the next sync discards the edits. That trades automatic syncing
for stability: a new member then has to be added to `.idea/` by hand. Quit PyCharm before editing
`.idea/` either way — it rewrites the directory on exit — and back the directory up first.

None of this is committable: `.idea/` is gitignored.

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

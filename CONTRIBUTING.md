# Contributing to v2get

Thanks for taking the time to contribute. This guide covers everything you
need to get a change merged.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By
participating you agree to uphold it.

## Reporting bugs

Open an issue with:

- what you expected and what actually happened,
- how to reproduce it,
- your deployment mode (Docker or local) and v2get version (shown in the
  dashboard sidebar),
- relevant log lines from the **Logs** page or `docker compose logs`.

**Redact before you paste.** Logs and config dumps can contain proxy links,
channel names, your GitHub repository and — in stack traces — parts of your
`.env`. Never paste your `TELEGRAM_SESSION` or `GITHUB_TOKEN` into an issue.

## Suggesting features

Open an issue describing the problem you are trying to solve rather than only
the solution you have in mind. Explain the use case; that usually surfaces a
simpler design.

## Development setup

```bash
git clone https://github.com/OWNER/v2get.git
cd v2get

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env       # credentials are optional for development
export DATA_DIR=./data

uvicorn app.main:app --reload --port 8080
```

The dashboard is then on <http://localhost:8080>. Telegram and GitHub
credentials are **not** required to develop: without them the app boots, serves
the dashboard and exercises every code path except live collection and
publishing.

## Running the checks

```bash
ruff check .        # lint
ruff format --check .   # formatting
pytest              # tests
```

All three run in CI on every pull request and must pass before merge. Tests use
a temporary `DATA_DIR`, so they never touch your real database.

## Writing tests

Any behavioural change needs a test. The suite lives in `tests/` and is
organised by module. Aim for tests that state a *rule*, not an implementation
detail — `test_lookalike_domain_is_not_blocked` is more useful than
`test_blacklist_returns_false`.

Tests must not require network access, a Telegram session, or a GitHub token.

## Coding style

The existing code has a consistent voice; please match it.

- **Docstrings explain *why*, not *what*.** Every module opens with a docstring
  covering its role and its non-obvious design decisions. Follow that pattern.
- **Type hints everywhere**, with `from __future__ import annotations` at the
  top of each module.
- **Comment the surprising, not the obvious.** Look at the notes explaining WAL
  mode, the newest-first Telethon iteration or the forced-TLS vless-grpc case —
  each documents a decision a future reader would otherwise undo by accident.
- **Broad `except Exception` is acceptable at external boundaries only** —
  Telegram, GitHub, TCP, the relayed bot. Every such site must log the failure
  and degrade gracefully. Annotate it with `# noqa: BLE001` and a reason. Do
  not swallow exceptions in pure internal logic.
- Line length 100. `ruff format` settles all other formatting questions.

## Architectural boundaries

Two rules protect the design:

1. **`app/npvt/` must stay isolated.** It may *import from* `app/core/`, but
   nothing in `app/core/` may import from `app/npvt/`, and npvt must never
   modify core behaviour. It keeps its own settings table, its own ORM
   declarative base and its own worker precisely so a failure there can never
   affect collection. The single deliberate coupling is acquiring the
   collector's `_run_lock` so pool mutations never overlap.

2. **Two-tier configuration.** Deployment concerns (secrets, paths) belong in
   `app/config.py`, read once from the environment. Anything a user might
   reasonably want to tune belongs in `settings_manager.py`, stored in the
   database and editable from the dashboard without a restart. When adding a
   knob, ask which tier it belongs to — new environment variables should be
   rare.

## Database changes

`create_all()` never alters an existing table, so any new column on a shipped
model must also be registered in `_COLUMN_MIGRATIONS` in `app/database.py`.
Migrations are additive and idempotent; they run on every startup. Never write
a destructive migration — users' collected archives are not reproducible.

## Pull requests

1. Branch from `main`: `git checkout -b feat/short-description`.
2. Keep the change focused. Unrelated refactors belong in their own PR.
3. Write [Conventional Commits](https://www.conventionalcommits.org/):
   `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `perf:`, `ci:`.
   Add `!` for a breaking change: `feat!: rename the pool API`.
4. Run lint and tests locally.
5. Update `README.md` if you changed behaviour, and add a `CHANGELOG.md` entry
   under *Unreleased*.
6. Open the PR and describe what changed and why.

## Security issues

Do not open a public issue. Follow [SECURITY.md](SECURITY.md).

## Legal note

v2get collects publicly posted proxy configurations from Telegram channels.
Contributors are responsible for ensuring their use complies with local law and
with Telegram's Terms of Service. Please do not contribute features whose
primary purpose is to defeat anti-abuse controls — for example, CAPTCHA
solving. The existing npvt module deliberately *detects and backs off* from
CAPTCHAs rather than solving them, and that stance will not change.

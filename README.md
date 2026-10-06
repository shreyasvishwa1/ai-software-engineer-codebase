# AI Software Engineer — Codebase Intelligence Platform

A self-hosted web application for understanding software repositories. You import a repository
(a ZIP upload or a public GitHub archive), then browse, search and analyze its code in the
browser. Most analyses are deterministic static analyses of Python source; an optional AI
feature explains a single file through Anthropic's API.

Analyzed code is never executed, imported or compiled to bytecode: every analysis reads the
stored files and, for Python, builds syntax trees with `ast` and `tokenize` only.

## Contents

- [Architecture](#architecture)
- [Features](#features)
- [Security model](#security-model)
- [API overview](#api-overview)
- [Setup](#setup)
- [Testing](#testing)
- [Development workflow](#development-workflow)
- [Known limitations](#known-limitations)

## Architecture

```
Browser (React SPA, http://127.0.0.1:5173)
   │  /api/*  (Vite dev proxy)
   ▼
FastAPI app (uvicorn, http://127.0.0.1:8000) ──► PostgreSQL 16 (users, projects, repositories,
   │                                              files with SHA-256, jobs, code metrics)
   ├──► Redis 7 ──► arq worker (ingestion and code-metrics jobs, scheduled maintenance)
   │      └── per-user rate limits (fail open when Redis is unavailable)
   └──► DATA_DIR/repositories/<repository_id>/   (extracted repository files)
```

**Backend** (`backend/`): Python 3.12, FastAPI, SQLAlchemy 2 (async, asyncpg), Alembic
migrations, Pydantic settings, arq for background jobs, argon2 password hashing, PyJWT access
tokens. Code lives in `app/api` (routes), `app/services` (one module per capability),
`app/schemas` (response models), `app/models` (ORM), `app/workers` (the arq worker) and
`app/ai` (the Anthropic provider).

**Frontend** (`frontend/`): React 18, TypeScript, Vite 5, Tailwind CSS, TanStack Query and
React Router. Pages: login, registration, projects, a project, and a repository. API calls go
through one axios client (`/api/v1`).

**Storage**: an ingestion extracts the archive into a staging directory and swaps it into
`DATA_DIR/repositories/<repository_id>/`. The database records every file's path, language,
size and SHA-256; each later read is verified against that snapshot.

## Features

### Accounts, projects and repositories
- Registration and login (email and password); JWT bearer access tokens, no refresh tokens.
- Projects: create, list, rename or describe, delete.
- Repositories inside a project: create (ZIP or GitHub source), rename, delete. The source is
  fixed when the repository is created.

### Ingestion
- **ZIP upload**, limited by `MAX_UPLOAD_SIZE_MB`, entry count, total and per-file extracted size,
  and compression ratio (zip-bomb guard).
- **Public GitHub archive**: only `https://github.com/<owner>/<repository>` URLs, an optional
  branch, downloaded without git or credentials. Redirects are followed only to GitHub hosts,
  every resolved address must be public, and the download is size-limited.
- Ingestion runs in the arq worker. The repository page shows job progress and recent jobs, and
  a pending job can be cancelled. Scheduled maintenance fails stuck jobs and removes leftover
  staging files.

### Browsing
- File tree with a path filter: a case-insensitive, literal substring match, done entirely in
  the browser.
- Language statistics (files and bytes per language).
- File viewer: read-only, rendered as plain text, preview limit 1 MiB, line highlighting.
  Analysis results navigate to their lines.

### Search
- **Code search**: literal, case-insensitive text search across the repository, stopping at
  5,000 files, 64 MiB read or 200 matches.
- **Symbol search**: exact, case-sensitive names of Python classes, functions and methods.

### Python code intelligence
These run on demand for the open Python file:
- **Outline**: classes, functions and methods.
- **Imports**, resolved to repository files where possible, and **importers** (files that
  import this one).
- **Go to definition** and **name occurrences** for the name under the selection, and **find
  references** for a `def` or `class`.
- **Call hierarchy**, **type hierarchy** and **method overrides**.

### Python diagnostics
These also run on demand for the open Python file. Every one reports only what the syntax
proves; uncertain cases are counted separately or not reported at all.
- **Unused imports**: import bindings whose name nothing in the file reads.
- **Unbound names**: names read with no statically known binding. This is not a runtime
  `NameError` detector.
- **Unused variables**: assignments to function locals that nothing in the file reads.
- **Redefined names**: `def`, `async def` and `class` statements replaced by a later one of the
  same name before anything reads them.
- **Unreachable code**: statements after a `return`, `raise`, `break` or `continue`, after an
  `if` whose every branch leaves, or after a `while True:` loop without a `break`.

### Repository analyses
- **Code metrics**: per-file line and blank-line counts, computed by a background job and stored
  per snapshot.
- **Dependencies**: declared dependencies from `package.json`, `pyproject.toml` (the PEP 621
  `[project]` table) and `requirements*.txt`. Nothing is resolved, installed or fetched.
- **Secret scan**: a fixed set of rules for likely committed credentials. Findings carry the
  path, line and rule, never the matched text.

### AI file explanation (optional)
- Disabled unless `ANTHROPIC_API_KEY` is set (otherwise the endpoint answers 503).
- Explaining a file sends its verified content (at most 64 KiB) to Anthropic's API, so review
  Anthropic's data-retention and privacy terms before enabling it. Requests always go to
  `https://api.anthropic.com`; the model is set by `LLM_MODEL` (default `claude-opus-5-5`).
  Limited to 20 explanations per user per hour.

## Security model

- **Authentication**: argon2 password hashing, and HS256/384/512-signed JWT access tokens
  (`SECRET_KEY`, at least 32 characters; the placeholder value is rejected in production).
  Login is rate-limited per IP address and per email, registration per IP address.
- **Authorization**: every project and repository endpoint checks ownership first. Another
  user's project or repository answers 404 ("Project not found", "Repository not found"),
  exactly like a missing one.
- **Rate limits**: search, per-file analyses, secret scans and explanations have per-user
  limits in Redis, checked after ownership: 20 to 120 requests per 10 minutes depending on cost,
  and per hour for secret scans and explanations. The limiter fails open if Redis is
  unavailable.
- **Verified reads**: every file read checks containment in the repository directory, refuses
  links and junctions, bounds the size, and compares the SHA-256 recorded at ingestion. Any
  mismatch answers 409 "Repository changed during analysis; run it again".
- **No code execution**: analyses never run, import or compile analyzed code, and make no
  network calls on its behalf. Python is parsed with `ast.parse` and `tokenize` only.
- **Untrusted content**: the frontend renders repository content and every server-provided
  string as plain text, never as HTML. External URLs are shown as text, not links.
- **Data minimization**: analysis responses carry positions, names and counts, never file
  contents or snippets (apart from the file viewer and search previews), checksums or storage
  paths. Repository content is not logged.
- **Client storage**: the access token is kept in `localStorage`. No other server data is
  stored in the browser.

## API overview

All endpoints are under `/api/v1`. Interactive documentation runs at
`http://127.0.0.1:8000/docs`. Repository endpoints share the prefix
`/projects/{project_id}/repositories/{repository_id}`, written `…` below. Per-file endpoints take
`?path=<file path>`. Go to definition and name occurrences also take `line` and `column`; find
references and the three hierarchy endpoints also take `line`.

| Area | Endpoints |
| --- | --- |
| Health | `GET /health`, `GET /api/health`, `GET /api/v1/health` |
| Auth | `POST /auth/register`, `POST /auth/login`, `GET /auth/me` |
| Projects | `GET /projects`, `POST /projects`, `GET`/`PATCH`/`DELETE /projects/{project_id}` |
| Repositories | `GET`/`POST /projects/{project_id}/repositories`, `GET`/`PATCH`/`DELETE …` |
| Ingestion and jobs | `POST …/ingest`, `GET …/jobs`, `GET …/jobs/{job_id}`, `POST …/jobs/{job_id}/cancel` |
| Files | `GET …/tree`, `GET …/languages`, `GET …/files/content` |
| Search | `POST …/search` (code), `POST …/symbols/search` |
| Code intelligence | `GET …/files/outline`, `…/files/imports`, `…/files/importers`, `…/files/definition`, `…/files/references`, `…/files/occurrences`, `…/files/call-hierarchy`, `…/files/type-hierarchy`, `…/files/method-overrides` |
| Diagnostics | `GET …/files/unused-imports`, `…/files/unbound-names`, `…/files/unused-variables`, `…/files/replaced-definitions`, `…/files/unreachable-code` |
| Repository analyses | `GET`/`POST …/analysis/code-metrics`, `GET …/analysis/dependencies`, `GET …/analysis/secrets` |
| AI | `POST …/files/explain` |

## Setup

These steps run everything locally. `docker-compose.yml` provides PostgreSQL and Redis for
development only; it is not a production deployment.

**Requirements**: Python 3.12, Node.js 18 or newer, and Docker (or local PostgreSQL 16 and
Redis 7).

1. **Configuration**: copy `.env.example` to `backend/.env`, which the backend reads, and to a
   repository-root `.env`, which Docker Compose reads for `POSTGRES_*`. Choose your own local
   password and `SECRET_KEY`. `DATABASE_URL` must use `postgresql+asyncpg://`. Never commit
   either file.
2. **Database and queue**:
   ```bash
   docker compose up -d postgres redis
   ```
3. **Backend** (from `backend/`):
   ```bash
   python -m venv .venv
   .venv/Scripts/pip install -r requirements-dev.txt -c constraints.txt
   .venv/Scripts/alembic upgrade head
   .venv/Scripts/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
   ```
   On macOS or Linux, use `.venv/bin/` in place of `.venv/Scripts/`. `constraints.txt` pins
   every backend package to the verified versions.
4. **Worker** (from `backend/`, in another terminal; ingestion and code metrics need it):
   ```bash
   .venv/Scripts/arq app.workers.worker.WorkerSettings
   ```
5. **Frontend** (from `frontend/`):
   ```bash
   npm install
   npm run dev
   ```
   Open http://127.0.0.1:5173. The dev server proxies `/api` to the backend.

## Testing

**Frontend** (from `frontend/`):
```bash
npm test
npm run typecheck
npm run build
```
Vitest with Testing Library runs in jsdom; the API is mocked at the axios adapter, so no
backend is needed.

**Backend** (from `backend/`):
```bash
.venv/Scripts/python -m pytest
.venv/Scripts/ruff check .
.venv/Scripts/mypy
```
Analysis tests run offline. Database and API tests need these variables, and are skipped
without them:
- `TEST_DATABASE_URL`: an already-migrated PostgreSQL database whose name ends in `_test`.
  Every test runs inside a transaction that is rolled back.
- `TEST_MIGRATION_DATABASE_URL`: a separate, disposable database for migration tests.
- `TEST_REDIS_URL`: a loopback Redis with a database index other than 0. The rate-limit tests
  flush it.

Symlink tests are skipped on Windows unless the process may create symlinks.

## Development workflow

Work proceeds one capability per commit. Each change goes through a written proposal, then:
- implementation with focused tests (oracle tests compare analyses against CPython where
  possible);
- mutation testing of the new code, where every mutant must be caught;
- the full frontend and backend regression suites, typecheck, build and lint;
- an integrity check that no unrelated file changed.

## Known limitations

- **Python only**: code intelligence and diagnostics cover Python only, one file at a time,
  with Python 3.12 syntax. Files above 256 KiB are not parsed, files above 1 MiB are not read,
  and result lists are capped (typically 500 entries) with exact counts.
- **Static analysis only**: dynamic features such as `globals()`, `eval`, decorators,
  metaclasses and `sys.modules` limit what can be proven. The diagnostics stay silent or count
  such cases rather than guess. Unreachable-code detection does not follow `try`, `with` or
  `match` statements, or calls that never return.
- **Accounts**: there are no refresh tokens, password reset or account deletion, and the
  access token lives in `localStorage`.
- **Repositories**: the source cannot be changed after creation; re-ingest instead. GitHub
  import supports public repositories only.
- **Navigation**: selected files and lines are not reflected in the URL, so they cannot be
  shared as links.
- **Tooling**: `npm run lint` fails because ESLint is not installed. Nineteen older backend
  files do not match `ruff format`. The production bundle exceeds Vite's 500 kB chunk warning.
- **Deployment**: this is a single-node development setup; there is no production deployment
  guide.

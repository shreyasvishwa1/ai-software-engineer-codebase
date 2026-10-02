# AI Software Engineer — Codebase Intelligence Platform

Production-quality AI-powered developer platform to understand, analyze and explain software repositories.

## Phase 1 — Foundation (Current)

- FastAPI backend with /health and /api/health
- React + Vite + Tailwind frontend
- Health check connectivity
- SQLite (dev) -> PostgreSQL (prod) ready
- CORS configured
- Structured logging & global error handler

## Quick Start

### Backend
```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt -c constraints.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```
`constraints.txt` pins every backend package to the verified versions; regenerate and review it deliberately whenever dependency versions are intentionally changed.
- Health: http://127.0.0.1:8000/health
- Docs: http://127.0.0.1:8000/docs

#### AI file explanations (optional)
Set `ANTHROPIC_API_KEY` in `backend/.env` to enable `POST .../files/explain`; without a key the feature is disabled (503). When an explanation is requested, the verified content of that file (up to 64 KiB) is sent to Anthropic's API, so review Anthropic's applicable data-retention and privacy terms before enabling it. Requests always go to `https://api.anthropic.com` (`ANTHROPIC_BASE_URL` is ignored); operator-defined `ANTHROPIC_CUSTOM_HEADERS`, if set, are added by the SDK and never logged or returned.

### Frontend
```powershell
cd frontend
npm install
npm run dev
```
- App: http://localhost:5173

## Project Structure
See spec — backend/app + frontend/src modular layout.

## Next Phases
2: Project Creation  3: GitHub Import  4: ZIP Upload ... 23: Documentation

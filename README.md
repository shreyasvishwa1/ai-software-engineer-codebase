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
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```
- Health: http://127.0.0.1:8000/health
- Docs: http://127.0.0.1:8000/docs

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

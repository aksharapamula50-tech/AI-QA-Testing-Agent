# AI QA Testing Agent

A working local-first QA workspace built with React + Vite, FastAPI and SQLite. Upload a specification, inspect extracted requirement statements and likely clarification gaps, generate four-category test cases, manage QA tasks, record execution evidence, and export a filtered CSV.

## Features

- Midnight blue workspace with electric violet accents, responsive layouts and dashboard/report charts.
- Account login with a hashed password, signed HttpOnly session cookie, logout, and persisted dark/light themes.
- Multiple SQLite projects, a seeded Checkout & Payments demo, requirement history, test-case history, execution records, and QA tasks.
- TXT, text-based PDF and DOCX uploads (8 MB limit). Scanned PDFs without extractable text are reported as such; OCR is not included.
- Rule-based requirement statement and missing-condition analysis. It identifies prompts for clarification; it does not claim formal specification verification.
- Positive, negative, boundary and validation case suggestions for up to 12 extracted requirement statements (48 generated suggestions maximum per document).
- Automatic clarification tasks generated from detected gaps, plus manually managed QA tasks with status tracking.
- Test-case search, category filtering, deletion and CSV export; task status management.
- Execution is intentionally constrained to local deterministic utility adapters (word count, explicitly supplied email format and safe arithmetic). Supported assertions report their actual outcome. Natural-language cases without adapters remain **Not Run**; no external system is touched.
- Optional OpenAI-compatible chat-completions review provider. Without a key, review uses local heuristics and clearly identifies that mode. AI advice is not an execution result.
- Pydantic request validation, upload constraints, parameterized SQL, origin-configurable CORS, useful HTTP errors and API tests.

## Requirements

- Python 3.10 or newer
- Node.js 20.19+ or 22.12+ (current Vite releases require a modern Node runtime)

## Run on Windows PowerShell

Open two terminals in the project directory.

### 1. Start the backend

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
# First run only: create an account using hidden password prompts.
python -m app.create_user
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

API docs: http://127.0.0.1:8000/docs. The default SQLite file is created at `backend/qa_agent.db` on startup. Override it with `QA_DB_PATH`.

### 2. Start the frontend

```powershell
cd frontend
npm install
npm run dev
```

Open the local URL printed by Vite (normally http://localhost:5173). The development server proxies `/api` to `http://127.0.0.1:8000`, so it also works when opening Vite through this machine's LAN address (for example, `http://192.168.1.3:5173`). Keep the API URL relative in development. `QA_API_PROXY_TARGET` can override the proxy target if needed. FastAPI also allows `http://localhost:5173`, `http://127.0.0.1:5173`, and `http://192.168.1.3:5173` for direct browser API access; add more comma-separated origins with `CORS_ORIGINS`.

## Optional AI review provider

Copy `backend/.env.example` to `backend/.env` for reference, then configure the environment in the backend shell before launch:

```powershell
$env:AI_API_KEY = 'your-provider-key'
$env:AI_BASE_URL = 'https://api.openai.com/v1'
$env:AI_MODEL = 'gpt-4o-mini'
```

The API reads environment variables directly; it does not load `.env` automatically. Keep credentials out of source control. Any OpenAI-compatible endpoint should implement `/chat/completions`. AI provider errors are surfaced as HTTP 502 rather than silently replaced with local output.

## Workspace login

The API requires an authenticated session for project, report, requirement, task, and test-case routes. The login page uses a signed, eight-hour HttpOnly, SameSite=Lax cookie; passwords are salted PBKDF2-HMAC-SHA256 hashes in SQLite. There is no default account or public signup.

Create the first account after installing backend requirements:

```powershell
cd backend
python -m app.create_user
```

Enter an email and a password of at least 12 characters when prompted. Password input is hidden and is not put in shell history. The signing secret is generated locally and persisted to `backend/.auth_secret`; it is ignored by Git. For deployments, set `QA_AUTH_SECRET` to a securely generated value of at least 32 characters and set `QA_COOKIE_SECURE=1` when serving over HTTPS. All accounts in this local demo share the same project workspace; per-user project isolation, password recovery, and deployment-grade rate limiting are not implemented.

To verify authentication through the UI, create an account, open the Vite URL, sign in, refresh to confirm the session persists, and use **Log out** to return to the login page. The API tests cover valid and invalid credentials, protected routes, HttpOnly cookie attributes, and logout.

## Run checks

Backend API tests:

```powershell
cd backend
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Frontend production compilation:

```powershell
cd frontend
npm install
npm test
npm run build
```

Frontend API helper tests:

```powershell
cd frontend
npm test
```

## Security and limitations

- This is a local development demonstration. Authentication protects API routes, but accounts share the same project workspace and production-grade rate limiting, password recovery, and deployment hardening are not included. Do not expose it to untrusted networks as-is.
- Uploads are parsed in memory and only extracted text is persisted. Uploads are size-limited and allowlisted; uploaded content is not executed.
- The safe runner never interprets arbitrary steps as Python, shell, SQL, browser actions, or network calls. Generated natural-language cases therefore usually remain Not Run until a specific trusted adapter is implemented.
- AI review transmits requirement text and case details to the configured AI provider. Use an approved provider and avoid confidential input unless authorized.
- Requirement checks and generated cases are useful starting points, not proof of completeness or replacements for human review.
- PDF OCR, external application/browser automation, authentication, concurrent team collaboration, and deployment configuration are not included.

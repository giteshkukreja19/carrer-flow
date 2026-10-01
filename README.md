# Placement Pilot

Placement Pilot is a personal, read-only placement operations dashboard for Haveloc. It tracks jobs, applications, interview rounds, attendance reminders, Gmail placement alerts, triage, scheduler settings, and an answer profile.

## Safety defaults

- Haveloc scanning is paused. The application does not submit applications, answer Haveloc questions, or mark attendance.
- Gmail access uses the read-only scope. OAuth, sender configuration, and the Settings polling switch are all required before polling can run.
- Gemini and WhatsApp are inactive until configured. WhatsApp additionally requires an explicit `WHATSAPP_ENABLED=true` setting.
- Email notifications, auto-apply, and attendance automation remain disabled by server-side safety gates.
- Email contents are untrusted data and cannot change settings or trigger Haveloc actions.

## Local development

Requirements: Python 3.11 or newer, Node.js with npm, and PostgreSQL if you want persistent records. The API can start without PostgreSQL, but dashboard data will not persist.

### Backend

```sh
cd backend
cp .env.example .env
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements-dev.txt
```

Set `DATABASE_URL` in the local `backend/.env` to your PostgreSQL database if persistence is desired. The file is ignored by Git. Start the API:

```sh
uvicorn server:app --reload --host 127.0.0.1 --port 8001
```

Health: <http://127.0.0.1:8001/api/health>

### Frontend

```sh
cd frontend
cp .env.example .env
npm ci
npm run dev
```

Open <http://localhost:5173>. Vite proxies `/api` to `http://127.0.0.1:8001` for local development. `VITE_API_BASE_URL` can remain blank locally.

### Checks

```sh
cd backend
source venv/bin/activate
PYTHONPYCACHEPREFIX=/tmp/placement-pilot-pycache python -m compileall -q .
python -m pytest -q
cd ../frontend
npm run build
```

## Environment variables

Set secrets only in local ignored `.env` files or the deployment platform's secret variable store. `.env.example` files contain placeholders and safe local defaults.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection string. Railway should provide/reference its PostgreSQL service URL; no localhost URL is baked into the backend. |
| `APP_URL` | Frontend origin used after Gmail OAuth completes. |
| `GMAIL_REDIRECT_URI` | Optional exact OAuth callback URL. Leave blank locally to use `APP_URL/api/gmail/oauth/callback`; set to the backend callback URL for separate production services. |
| `CORS_ORIGINS` | Comma-separated, explicit browser origins allowed to call the API. Defaults to local Vite origins; `*` is rejected. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google OAuth client values. Gmail remains disabled without valid OAuth and encryption setup. |
| `GMAIL_TOKEN_ENCRYPTION_KEY` | Fernet key used to encrypt Gmail tokens in PostgreSQL. |
| `AI_PROVIDER`, `GEMINI_API_KEY`, `GEMINI_MODEL` | AI provider selection and Gemini settings. Classification is skipped until configured. |
| `WHATSAPP_ENABLED`, `WHATSAPP_PROVIDER`, `WHATSAPP_API_VERSION`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_RECIPIENT_NUMBER` | WhatsApp Cloud provider settings. Leave blank and disabled until intentionally configured. |
| `HAVELOC_BASE_URL`, `HAVELOC_USERNAME`, `HAVELOC_PASSWORD`, `HAVELOC_SCAN_INTERVAL_MINUTES` | Haveloc configuration placeholders. These do not enable the paused scanner. |
| `NOTIFICATIONS_ENABLED`, `EMAIL_NOTIFICATIONS_ENABLED`, `EMAIL_RECIPIENT` | Global/in-app and email notification settings. Email notifications default off. |
| `VITE_API_BASE_URL` | Public backend origin baked into the frontend build. Leave blank for same-origin `/api`; set it to the backend origin when Railway frontend and backend use separate domains. This is not a secret. |

Never place secrets in `VITE_*` variables; those values are readable by every browser user.

## Database initialization

On backend startup, `backend/lib/db.py` connects using `DATABASE_URL` and applies the schema. The SQL uses `CREATE TABLE IF NOT EXISTS`, additive `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, and conflict-safe seed inserts. It does not drop or reset tables. This initializes a fresh PostgreSQL database and applies the current additive schema on later starts. Keep backups before any future schema changes that remove or transform data.

`GET /api/health` does not require Gmail, Gemini, WhatsApp, or Haveloc credentials. It reports database readiness separately from application health.

## Railway deployment preparation

No Railway project, service, domain, or deployment has been created. Railway's isolated-monorepo setup uses a service root directory for each app; use `/backend` for the API and `/frontend` for the UI. Each directory includes a Dockerfile, so Railway can build the service from that root. See [Railway monorepo deployment](https://docs.railway.com/deployments/monorepo) and [Dockerfile builds](https://docs.railway.com/builds/dockerfiles).

### Backend service

- Root directory: `/backend`
- The Docker start command binds `0.0.0.0` and uses Railway's runtime `PORT` (fallback `8000` is for local container runs).
- Health check path: `/api/health`
- Set `DATABASE_URL` from the Railway PostgreSQL service reference.
- Set `APP_URL` to the frontend's deployed origin, `GMAIL_REDIRECT_URI` to the backend's `/api/gmail/oauth/callback` URL, and `CORS_ORIGINS` to the exact frontend origin(s).
- Leave Gmail, Gemini, WhatsApp, and Haveloc credentials unset. Keep the Gmail polling switch off unless you later configure Gmail deliberately.

### Frontend service

- Root directory: `/frontend`
- The Dockerfile runs `npm ci`, builds the Vite app, and serves the SPA with Caddy on Railway's runtime `PORT`.
- Set `VITE_API_BASE_URL` to the backend's deployed origin (scheme and host, without `/api`). Vite embeds this public URL during the build, so changing it requires a new build/deployment.
- Caddy routes client-side navigation to `index.html`. API calls go directly to the configured backend origin and are limited by backend `CORS_ORIGINS`.

The backend remains the long-running service for the existing gated Gmail polling and WhatsApp/reminder worker. Do not replace it with Railway Cron. Haveloc scanning stays paused. For setup screens and the current provider variables, see the checked-in `.env.example` files and configure values only in Railway's variable store when you choose to deploy.

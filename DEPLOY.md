# Deploying to Railway

The production image (`Dockerfile`) runs everything the app needs in one container:
- the built frontend, served by Caddy;
- the FastAPI backend;
- the arq worker.

Caddy passes `/api` to the backend. The backend and the worker share the repository storage on
a persistent volume. PostgreSQL and Redis are Railway services.

## 1. Create the services

In a new Railway project:

1. **Add the app:** choose *New → GitHub Repo* and select this repository. Railway finds
   `railway.json` and builds the `Dockerfile`.
2. **Add the database:** *New → Database → PostgreSQL*.
3. **Add the queue:** *New → Database → Redis*.

## 2. Add a volume

Open the app service, then *Settings → Volumes → Add volume*, with mount path **`/data`**.
Ingested repositories are stored there.

## 3. Set the app's variables

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (reference to the PostgreSQL service) |
| `REDIS_URL` | `${{Redis.REDIS_URL}}` (reference to the Redis service) |
| `SECRET_KEY` | A long random value, for example the output of `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `ANTHROPIC_API_KEY` | Optional; set it only to enable AI file explanations |
| `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `APP_BASE_URL` | Optional; set all five to enable password reset by email. For Gmail: `smtp.gmail.com`, your address, a Google app password, your address again, and the public URL from step 4 (for example `https://<name>.up.railway.app`) |

Note that Railway's trial plans may block outgoing SMTP connections; if reset emails never
arrive, check the deployment logs for "Password reset email could not be sent".

The image already sets the following:
- `ENVIRONMENT=production`, which disables the API docs;
- `DEBUG=false`;
- `DATA_DIR=/data`.

The start script converts Railway's `postgresql://` URL to the `postgresql+asyncpg://` form the
app requires.

## 4. Get the public URL

Open the app service, then *Settings → Networking → Generate Domain*. Railway gives a URL like
`https://<name>.up.railway.app`.

Each deploy runs the database migrations automatically. Railway checks
`/api/v1/health` before routing traffic to a new deploy.

## Test the image locally

```bash
docker build -t ai-software-engineer .
docker run -p 8080:8080 -e DATABASE_URL=... -e REDIS_URL=... -e SECRET_KEY=... -v ai_se_data:/data ai-software-engineer
```

Then open http://localhost:8080.

# Production image: the built frontend, the FastAPI backend, the arq worker and Caddy in one
# container. Backend and worker share the repository storage at DATA_DIR (a persistent volume).
# PostgreSQL and Redis are external services (for example Railway's). See DEPLOY.md.

FROM node:20-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM caddy:2-alpine AS caddy

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production \
    DEBUG=false \
    DATA_DIR=/data
WORKDIR /app
COPY backend/requirements.txt backend/constraints.txt backend/
RUN pip install --no-cache-dir -r backend/requirements.txt -c backend/constraints.txt
COPY backend/ backend/
COPY --from=frontend /build/dist frontend/dist
COPY --from=caddy /usr/bin/caddy /usr/local/bin/caddy
COPY deploy/ deploy/
RUN mkdir -p /data
EXPOSE 8080
CMD ["bash", "/app/deploy/start.sh"]

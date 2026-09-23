FROM node:24.15.0-bookworm-slim AS web
WORKDIR /build
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY frontend ./frontend
RUN npm run web:build

FROM python:3.11.14-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY scripts ./scripts
COPY tests ./tests
COPY --from=web /build/frontend/dist ./frontend/dist
RUN useradd --create-home --uid 10001 app && mkdir -p /data /models && chown app:app /data /models
USER app
CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]

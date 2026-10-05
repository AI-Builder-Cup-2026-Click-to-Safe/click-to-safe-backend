FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080
WORKDIR /app
# ffmpeg draws the boxes onto a copy of the clip. The slim variant is enough:
# the work is a drawbox filter and an H.264 re-encode, nothing exotic.
RUN apt-get update \
 && apt-get install --no-install-recommends --yes ffmpeg \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.9.28
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app/ ./app/
RUN useradd --uid 10001 --create-home agent
USER 10001
EXPOSE 8080
CMD ["sh", "-c", "exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]

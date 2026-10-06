# A small, locked-down image for a public demo link.
#   docker build -t refund-desk .
#   docker run -p 8000:8000 --read-only --tmpfs /tmp -v rd-var:/app/var refund-desk
# Real model:  add  -e DEMO_MODE=0 -e ANTHROPIC_API_KEY=...
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VAR_DIR=/app/var \
    DEMO_MODE=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY refund_desk ./refund_desk
RUN pip install -e .
COPY data ./data
COPY prompts ./prompts
COPY evals ./evals
COPY experiments.json ./

# Least privilege: a user with no rights outside /app/var.
RUN useradd --create-home --uid 10001 app && mkdir -p /app/var && chown app:app /app/var
USER app
VOLUME ["/app/var"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["refund-desk", "serve", "--host", "0.0.0.0", "--port", "8000"]

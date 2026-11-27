FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd -u 10003 -m -s /bin/bash evaluator \
    && pip install --no-cache-dir httpx==0.28.1

COPY . ./eval/

RUN chown -R 10003:10003 /app

USER evaluator

ENTRYPOINT ["python"]

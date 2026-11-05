FROM python:3.14-slim@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN useradd \
    --create-home \
    --user-group \
    --uid 10001 \
    --shell /usr/sbin/nologin \
    mcp \
    && mkdir -p /app/state \
    && chown mcp:mcp /app/state

COPY requirements-hashed.txt ./

RUN pip install \
    --no-cache-dir \
    --disable-pip-version-check \
    --require-hashes --only-binary=:all: -r requirements-hashed.txt

COPY --chown=mcp:mcp server.py formatting.py ./
COPY --chown=mcp:mcp shadow ./shadow

USER mcp

EXPOSE 8000

CMD ["python", "server.py"]

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Reference engine in its own interpreter environment, readable only by root, so the code
# under test (which runs as `nobody`) cannot import it.
RUN python -m venv /opt/ref \
 && /opt/ref/bin/pip install duckdb==2.0.1 \
 && chmod -R go-rwx /opt/ref

COPY . /tests/
RUN chmod -R go-rwx /tests && mkdir -p /app

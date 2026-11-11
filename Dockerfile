FROM docker:29-cli AS docker_cli

FROM python:3.11-slim-trixie AS api
RUN apt-get update && apt-get install -y --no-install-recommends --only-upgrade zlib1g \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' zlib1g)" ge '1:1.3.dfsg+really1.3.1-1+deb13u1' \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LANGSMITH_TRACING=false
WORKDIR /opt/rag
COPY requirements.txt requirements-step8.txt ./
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install --timeout 15 --retries 2 --only-binary=:all: -r requirements.txt -r requirements-step8.txt
COPY app/*.py app/
COPY app/tools/*.py app/tools/
COPY evaluation/__init__.py evaluation/bird_column_notes.py evaluation/
COPY sandbox/__init__.py sandbox/
COPY sandbox/step7/__init__.py sandbox/step7/protocol.py sandbox/step7/
RUN mkdir -p /state /run/rag && chown 65532:65532 /state
USER 65532:65532
CMD ["uvicorn", "app.product_api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM api AS ui
USER root
COPY requirements-step7.txt ./
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install --timeout 15 --retries 2 --only-binary=:all: -r requirements-step7.txt
ENV HOME=/tmp
USER 65532:65532
CMD ["python", "-m", "streamlit", "run", "app/product_ui.py", "--server.address=0.0.0.0", "--server.headless=true", "--browser.gatherUsageStats=false"]

FROM api AS broker
USER root
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
CMD ["uvicorn", "app.sandbox_broker:app", "--uds", "/run/rag/sandbox.sock", "--no-access-log"]

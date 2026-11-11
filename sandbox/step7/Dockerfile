FROM python:3.11-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends --only-upgrade zlib1g \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' zlib1g)" ge '1:1.3.dfsg+really1.3.1-1+deb13u1' \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /worker
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install --timeout 15 --retries 2 --only-binary=:all: -r requirements.txt
COPY protocol.py worker.py tasks.py audit.py panel.py tabular.py ./
USER 65532:65532
ENTRYPOINT ["python", "/worker/worker.py"]

FROM python:3.14-slim-bookworm@sha256:516b51cb9db2ef061e465c803e50f64361b82cefd91e525b889b967b83e83527 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/evidence-wiki-venv

WORKDIR /build
COPY . .

RUN /opt/evidence-wiki-venv/bin/python -m pip install --no-cache-dir . \
    && /opt/evidence-wiki-venv/bin/evidence-wiki --version \
    && find /opt/evidence-wiki-venv -type d -name __pycache__ -prune -exec rm -rf '{}' + \
    && find /opt/evidence-wiki-venv -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

FROM python:3.14-slim-bookworm@sha256:516b51cb9db2ef061e465c803e50f64361b82cefd91e525b889b967b83e83527

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/workspace \
    PATH=/opt/evidence-wiki-venv/bin:$PATH

LABEL org.opencontainers.image.title="EvidenceWiki" \
      org.opencontainers.image.description="Evidence-gated research workspaces with auditable sources" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o 10001 -g 10001 /workspace

COPY --from=builder /opt/evidence-wiki-venv /opt/evidence-wiki-venv

USER 10001:10001
WORKDIR /workspace

ENTRYPOINT ["/opt/evidence-wiki-venv/bin/evidence-wiki"]
CMD ["--help"]

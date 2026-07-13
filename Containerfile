FROM python:3.12-slim-bookworm@sha256:8a7e7cc04fd3e2bd787f7f24e22d5d119aa590d429b50c95dfe12b3abe52f48b AS builder

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

FROM python:3.12-slim-bookworm@sha256:8a7e7cc04fd3e2bd787f7f24e22d5d119aa590d429b50c95dfe12b3abe52f48b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/workspace \
    PATH=/opt/evidence-wiki-venv/bin:$PATH

LABEL org.opencontainers.image.title="EvidenceWiki" \
      org.opencontainers.image.description="Evidence-gated research workspaces with auditable sources" \
      org.opencontainers.image.licenses="MIT"

RUN install -d -o 10001 -g 10001 /workspace

COPY --from=builder /opt/evidence-wiki-venv /opt/evidence-wiki-venv

USER 10001:10001
WORKDIR /workspace

ENTRYPOINT ["/opt/evidence-wiki-venv/bin/evidence-wiki"]
CMD ["--help"]

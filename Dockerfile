# syntax=docker/dockerfile:1.7
# Portfolia – ein einzelnes Image (Web + Scheduler + SQLite), Multi-Stage für schlanke Laufzeit.
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
# Native Bibliotheken werden bewusst NICHT mit strip verkleinert: binutils 2.40 (bookworm) beschädigt die von
# auditwheel/patchelf angepassten Bibliotheken der Wheels (z. B. OpenBLAS in numpy: „ELF load command
# address/offset not page-aligned“) – die App startet dann nicht.
WORKDIR /build
COPY requirements.txt .
# Optional: CA-Zertifikat eines TLS-inspizierenden Proxys als Build-Secret (docker build --secret id=pip_ca,src=ca.crt)
RUN --mount=type=secret,id=pip_ca,required=false \
    if [ -s /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi \
 && pip install --prefix=/install -r requirements.txt \
 && find /install -type d -name "__pycache__" -prune -exec rm -rf {} + \
 && rm -rf /install/lib/python3*/site-packages/pandas/tests \
           /install/lib/python3*/site-packages/numpy/*/tests \
           /install/lib/python3*/site-packages/numpy/tests \
           /install/lib/python3*/site-packages/numpy/f2py \
           /install/lib/python3*/site-packages/numpy/_pyinstaller \
           /install/lib/python3*/site-packages/lxml/includes \
           /install/lib/python3*/site-packages/*/tests \
 && find /install -name "*.pyi" -delete \
 && find /install -name "*.pxd" -delete \
 && find /install -name "*.c" -path "*site-packages*" -delete \
 && find /install -name "*.h" -path "*site-packages*" -delete \
 && rm -f /install/lib/python3*/site-packages/PIL/_avif*.so /install/lib/python3*/site-packages/pillow.libs/libavif* \
          /install/lib/python3*/site-packages/PIL/_imagingtk*.so
# Kein vorkompilierter Bytecode im Image (spart ~45 MB): Python legt ihn beim ersten Start unter
# /data/cache/pyc ab (PYTHONPYCACHEPREFIX) – inklusive Standardbibliothek, danach startet die App schneller.

FROM python:${PYTHON_VERSION}-slim-bookworm
LABEL org.opencontainers.image.title="Portfolia" \
      org.opencontainers.image.description="Self-hosted Portfolio-Dashboard (Aktien & Krypto), nur lesend" \
      org.opencontainers.image.source="https://github.com/pneumann1980/portfolia" \
      org.opencontainers.image.licenses="MIT"
ENV PYTHONUNBUFFERED=1 \
    PYTHONPYCACHEPREFIX=/data/cache/pyc \
    PYTHONHASHSEED=0 \
    TZ=Europe/Berlin \
    PORT=8080 \
    PUID=99 \
    PGID=100 \
    DATA_DIR=/data \
    IMPORT_DIR=/import \
    HOME=/data \
    XDG_CACHE_HOME=/data/cache \
    MALLOC_ARENA_MAX=2
COPY --from=builder /install /usr/local
WORKDIR /opt/portfolia
COPY app ./app
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY examples/sources.yaml ./examples/sources.yaml
COPY examples/beispiel-import.zip ./examples/beispiel-import.zip
COPY docker/entrypoint.sh docker/healthcheck.py /usr/local/bin/
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/healthcheck.py \
 && mkdir -p /data /import
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=6s --start-period=60s --retries=3 CMD ["python", "-B", "/usr/local/bin/healthcheck.py"]
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "app"]

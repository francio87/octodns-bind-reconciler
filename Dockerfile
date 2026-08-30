FROM python:3.13-slim-bookworm@sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca

COPY requirements.txt /tmp/requirements.txt

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates=20250419~deb12u1 \
        git=1:2.39.5-0+deb12u3 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt

COPY octodns_reconciler.py /app/octodns_reconciler.py
COPY git-askpass.sh /usr/local/bin/git-askpass

RUN chmod 0755 /usr/local/bin/git-askpass \
    && mkdir -p /data \
    && chown 65532:65532 /data

ENV GIT_ASKPASS=/usr/local/bin/git-askpass \
    GIT_TERMINAL_PROMPT=0 \
    PYTHONUNBUFFERED=1

USER 65532:65532
WORKDIR /app
VOLUME ["/data"]

ENTRYPOINT ["python", "/app/octodns_reconciler.py"]

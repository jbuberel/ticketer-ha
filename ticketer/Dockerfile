# https://developers.home-assistant.io/docs/apps/configuration#app-dockerfile
# Debian slim rather than the Alpine HA base: later deps (onnxruntime for fast-alpr) need glibc.
FROM python:3.13-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Tailscale static binaries, checksum-pinned per architecture
ARG TARGETARCH
ARG TAILSCALE_VERSION=1.102.4
ARG TAILSCALE_SHA256_AMD64=50748df1045e60b5b695f19f4c56b0da36c019948b440fb456b6584a50f0d8b9
ARG TAILSCALE_SHA256_ARM64=9dd1e6a592a014bbaea0103167ffe299adeda4ba14e078ce9c2895364f6c4c3f
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) sha="${TAILSCALE_SHA256_AMD64}" ;; \
        arm64) sha="${TAILSCALE_SHA256_ARM64}" ;; \
        *) echo "Unsupported TARGETARCH: '${TARGETARCH}' (build with BuildKit)" >&2; exit 1 ;; \
    esac; \
    pkg="tailscale_${TAILSCALE_VERSION}_${TARGETARCH}"; \
    python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
        "https://pkgs.tailscale.com/stable/${pkg}.tgz" /tmp/tailscale.tgz; \
    echo "${sha}  /tmp/tailscale.tgz" | sha256sum -c -; \
    tar -xzf /tmp/tailscale.tgz -C /tmp; \
    install -m 0755 "/tmp/${pkg}/tailscale" "/tmp/${pkg}/tailscaled" /usr/local/bin/; \
    rm -rf /tmp/tailscale.tgz "/tmp/${pkg}"

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the local plate reader's models (~16 MB) into the image; keep names in sync with app/plates.py
RUN python3 -c "from fast_alpr import ALPR; ALPR(detector_model='yolo-v9-t-384-license-plate-end2end', ocr_model='cct-s-v2-global-model')"

# Browser libraries for the phone UI (Ionic, lit-html), checksum-pinned; see vendor.py
COPY vendor.py ./
RUN python3 vendor.py app/static/vendor

COPY config.yaml run.sh ./
COPY app ./app
RUN chmod a+x run.sh

LABEL \
    org.opencontainers.image.title="Home Assistant App: Ticketer" \
    org.opencontainers.image.source="https://github.com/jbuberel/ticketer-ha"

CMD ["/srv/run.sh"]

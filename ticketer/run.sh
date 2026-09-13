#!/usr/bin/env bash
# Start tailscaled (userspace), join the tailnet, serve HTTPS -> the API, then run the API.
#
# Settings come from Home Assistant's /data/options.json; environment variables override them
# for local runs. TS_DISABLE=1 skips Tailscale and binds the API to 0.0.0.0.
set -euo pipefail

OPTIONS_FILE="${OPTIONS_FILE:-/data/options.json}"
DATA_DIR="${DATA_DIR:-/data}"
API_PORT="${API_PORT:-8099}"
TS_SOCKET="/tmp/tailscaled.sock"

log() { echo "[ticketer] $*" >&2; }

# opt <key> <default>: value from options.json, or the default when missing/empty
opt() {
  python3 -c '
import json, sys
try:
    v = json.load(open(sys.argv[1])).get(sys.argv[2])
except FileNotFoundError:
    v = None
print(sys.argv[3] if v in (None, "") else v)' "$OPTIONS_FILE" "$1" "$2"
}

export TICKETER_VERSION
TICKETER_VERSION="$(sed -n 's/^version: *"\{0,1\}\([^"]*\)"\{0,1\} *$/\1/p' /srv/config.yaml)"
log "version ${TICKETER_VERSION:-unknown}"

ts() { tailscale --socket="$TS_SOCKET" "$@"; }

backend_state() {
  ts status --json 2>/dev/null \
    | python3 -c 'import json, sys; print(json.load(sys.stdin).get("BackendState", ""))' 2>/dev/null \
    || true
}

if [[ "${TS_DISABLE:-0}" == "1" ]]; then
  log "TS_DISABLE=1: skipping Tailscale"
  API_HOST="${API_HOST:-0.0.0.0}"
else
  API_HOST="${API_HOST:-127.0.0.1}"  # only tailscaled's HTTPS proxy can reach the API
  TS_AUTHKEY="${TS_AUTHKEY:-$(opt tailscale_auth_key "")}"
  TS_HOSTNAME="${TS_HOSTNAME:-$(opt tailscale_hostname ticketer)}"
  TS_TAGS="${TS_TAGS:-$(opt tailscale_tags "")}"

  mkdir -p "$DATA_DIR/tailscale"
  tailscaled --tun=userspace-networking --statedir="$DATA_DIR/tailscale" --socket="$TS_SOCKET" &

  state=""
  for _ in $(seq 60); do
    state="$(backend_state)"
    [[ -n "$state" && "$state" != "NoState" ]] && break
    sleep 0.5
  done
  log "tailscale state: ${state:-unknown}"

  up_args=(--hostname="$TS_HOSTNAME" --advertise-tags="$TS_TAGS")
  if [[ "$state" == "NeedsLogin" ]]; then
    if [[ -z "$TS_AUTHKEY" ]]; then
      log "Tailscale is not logged in. Set the 'tailscale_auth_key' option and restart (see Documentation)."
      exit 1
    fi
    ts up "${up_args[@]}" --auth-key="$TS_AUTHKEY"
  else
    ts up "${up_args[@]}"  # already logged in; the auth key is not needed again
  fi

  ts serve reset
  # `serve` waits for a browser click if HTTPS certificates are off in the tailnet; don't hang on it.
  if timeout 60 tailscale --socket="$TS_SOCKET" serve --bg --https=443 "http://127.0.0.1:${API_PORT}"; then
    dns_name="$(ts status --json | python3 -c 'import json, sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
    log "serving https://${dns_name}/"
  else
    log "tailscale serve failed: enable HTTPS Certificates on the Tailscale admin console DNS page, then restart."
  fi
fi

cd /srv
exec uvicorn app.main:app --host "$API_HOST" --port "$API_PORT" \
  --proxy-headers --forwarded-allow-ips 127.0.0.1

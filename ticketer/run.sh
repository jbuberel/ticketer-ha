#!/usr/bin/env bash
# Start tailscaled (userspace), join the tailnet, serve HTTPS -> the API, then run the API.
#
# Settings come from Home Assistant's /data/options.json; environment variables override them
# for local runs. TS_DISABLE=1 skips Tailscale and binds the API to 0.0.0.0.
set -euo pipefail

OPTIONS_FILE="${OPTIONS_FILE:-/data/options.json}"
export DATA_DIR="${DATA_DIR:-/data}"  # the API keeps its database and photos here
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

# Extraction settings. The key goes only into the API process's environment; it is never logged.
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-$(opt anthropic_api_key "")}"
export TICKETER_EXTRACTOR_MODEL="${TICKETER_EXTRACTOR_MODEL:-$(opt extractor_model claude-sonnet-5)}"
if [[ -z "$ANTHROPIC_API_KEY" ]]; then
  log "anthropic_api_key is not set: photos will be stored but not extracted"
fi

# 311 submission. Dry run stays on unless the option says otherwise, so an unconfigured install
# can never file a real request with the city.
# options.json holds a JSON boolean, which `opt` prints as True/False; the API compares lowercase.
TICKETER_SUBMIT_DRY_RUN="${TICKETER_SUBMIT_DRY_RUN:-$(opt submit_dry_run true)}"
export TICKETER_SUBMIT_DRY_RUN="$(echo "$TICKETER_SUBMIT_DRY_RUN" | tr '[:upper:]' '[:lower:]')"
TICKETER_ATTACH_PHOTO="${TICKETER_ATTACH_PHOTO:-$(opt attach_photo true)}"
export TICKETER_ATTACH_PHOTO="$(echo "$TICKETER_ATTACH_PHOTO" | tr '[:upper:]' '[:lower:]')"
export TICKETER_REPORTER_FIRST_NAME="${TICKETER_REPORTER_FIRST_NAME:-$(opt reporter_first_name "")}"
export TICKETER_REPORTER_LAST_NAME="${TICKETER_REPORTER_LAST_NAME:-$(opt reporter_last_name "")}"
export TICKETER_REPORTER_EMAIL="${TICKETER_REPORTER_EMAIL:-$(opt reporter_email "")}"
export TICKETER_REPORTER_PHONE="${TICKETER_REPORTER_PHONE:-$(opt reporter_phone "")}"

if [[ "$TICKETER_SUBMIT_DRY_RUN" == "true" ]]; then
  log "311 submission is in dry run: requests are built and shown, never filed"
else
  log "311 submission is LIVE: approved drafts are filed with the city"
fi

# Retention. Photos, plates and drafts are deleted on whichever of these two clocks falls later.
export TICKETER_RETAIN_UNSUBMITTED_HOURS="${TICKETER_RETAIN_UNSUBMITTED_HOURS:-$(opt retain_unsubmitted_hours 8)}"
export TICKETER_RETAIN_SUBMITTED_HOURS="${TICKETER_RETAIN_SUBMITTED_HOURS:-$(opt retain_submitted_hours 24)}"
log "retention: ${TICKETER_RETAIN_UNSUBMITTED_HOURS} h unsubmitted, ${TICKETER_RETAIN_SUBMITTED_HOURS} h after filing"

cd /srv
exec uvicorn app.main:app --host "$API_HOST" --port "$API_PORT" \
  --proxy-headers --forwarded-allow-ips 127.0.0.1

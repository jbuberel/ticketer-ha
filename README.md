# Ticketer — Home Assistant app repository

A Home Assistant app for a phone-based parking-report helper. Photos taken on a phone are
turned into draft reports that a person reviews before anything is submitted.

**Status:** hello-world build. It only proves the phone → Tailscale HTTPS → Home Assistant
path: identity, GPS, and a photo upload that is counted and discarded.

## Install

In Home Assistant: **Settings → Apps → App store → ⋮ → Repositories**, add
`https://github.com/jbuberel/ticketer-ha`, then install **Ticketer**. See
[ticketer/DOCS.md](ticketer/DOCS.md) for Tailscale setup.

## Layout

- `repository.yaml`: HA repository metadata
- `ticketer/`: the app (`config.yaml`, `Dockerfile`, `run.sh`, FastAPI code in `app/`)
- `.github/workflows/`: builds amd64 + aarch64 images and pushes them to
  `ghcr.io/jbuberel/ticketer` on every push to `main` that touches the app. Bump `version` in
  `ticketer/config.yaml` so Home Assistant offers the update.

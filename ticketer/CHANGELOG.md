# Changelog

## 0.2.0

- Capture sessions: **Begin Capture** → **Snap photo** (repeat) → **Stop Capture & Process**.
- Each photo records the phone's GPS fix (accuracy, heading, speed, fix time) when it's taken.
- Photos are kept on the phone first (IndexedDB) and upload in the background with retries. A
  session survives the page reloading or the app being closed.
- The server stores batches in SQLite and photos under the app's data folder. Retried uploads
  are safe.
- Photos can be removed during a session, and a whole session can be discarded.
- Home screen lists recent batches; a batch view shows its photos and locations.
- Processing (plate/vehicle extraction) is not built yet: finished batches stay `queued`.

## 0.1.0

- Hello-world build: Tailscale (userspace) inside the container, HTTPS via Tailscale Serve,
  page that checks HTTPS, Tailscale identity, GPS, and a photo upload (counted, not stored).

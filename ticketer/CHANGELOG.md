# Changelog

## 0.4.1

- **Delete batch**, at the bottom of a batch, permanently removes the batch, its photos, plate
  close-ups and drafts. It works in any state after capture ends, including while extraction is
  still running. Only the person who captured the batch can delete it.

## 0.4.0

- **Review drafts.** When a batch finishes extracting, each photo gets **Report**,
  **Don't report** and **Edit**.
  - **Edit** changes plate, state, color, make, model or address. The extracted values are kept;
    changed fields show an "edited" tag.
  - **Plate check:** a plate is trusted only when Claude says `high` and the local reader
    matches. Any other plate has to be corrected, or ticked "matches the photo", before
    **Report**.
  - **Report** also needs plate, color, make, model and address.
  - **Warnings on each draft:** plate to check, same plate twice in a batch, low-confidence
    vehicle, weak GPS or an estimated address, and missing fields.
  - **Stale screens:** every change bumps the draft's version, and a change made from an older
    screen is refused. A decision always covers the exact values on screen.
- The home screen shows how many drafts in each batch still need a decision.
- Only the person who captured a batch can review it or re-run its extraction.
- **Re-run extraction** keeps edits but clears Report / Don't report choices.
- Nothing is sent to 311 yet.

## 0.3.2

- Fix: after **Re-run extraction**, the phone could keep showing the old plate close-up for up to
  5 minutes. The server had the new one, but the browser reused its cached copy. The close-up
  URL now changes whenever the close-up is rewritten.

## 0.3.1

- Fix: the local plate reader's close-up could show the wrong thing (e.g. a tire) and say "no
  plate found" while the real plate was detected and read correctly.
  - Detections with no readable text are now ignored.
  - The rest are ranked by detection confidence instead of box size.
  - When a photo shows several plates, the reading that matches Claude's plate is used.
- **Re-run extraction** button on finished batches: replaces all results for the batch with a
  fresh run, e.g. to pick up this fix.

## 0.3.0

- **Extraction.** Queued batches, including ones captured before this version, are processed
  in the background, one photo at a time:
  - **Vehicle details:** Claude (default `claude-sonnet-5`) reads plate, state, color, make and
    model, each with a confidence.
  - **Plate cross-check:** a local plate reader (fast-alpr) reads the plate too and saves a
    close-up; the batch view shows whether the two readings match.
  - **Address:** the GPS fix is reverse-geocoded to the nearest address (a building within 40 m,
    otherwise a point along the block).
- Temporary API errors retry with backoff. Failed photos can be re-run with **Retry failed**.
- Batch view shows each draft and refreshes while extraction runs. The header shows the batch's
  API cost.
- New options: **Anthropic API key** and **Extraction model**.

## 0.2.1

- Fix: overlapping uploads of the same photo could fail with "not a readable image" and block
  **Stop Capture & Process**. This happens when the app is open twice, or a retry overlaps a
  slow first attempt. The uploads now run one at a time and the extras are treated as already
  stored.
- Server requests time out (20 s, uploads 2 min) and show an error instead of a stuck
  "Loading…". The batch view has a **Try again** button.

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

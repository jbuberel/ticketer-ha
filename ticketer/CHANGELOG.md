# Changelog

## 0.6.4

- **311 submissions failed with a certificate error.** The city's portal sends only its own
  certificate and leaves out the one that signs it, so nothing could verify the connection and
  every request stopped before it was sent. The missing certificate now ships with the app.
  Requests that failed this way can simply be sent again; nothing was filed.
- **The app could keep running old code after an update.** The home screen showed the new
  version while the page itself was still the cached previous one, which is why the Send button
  looked missing in 0.6.3. The app's files are now re-checked on each load.
- After updating, if anything still looks like the old version, close the app and open it again.

## 0.6.3

- **The Send button was easy to miss.** It sat above the photos, so after deciding the last
  draft you were at the bottom of the batch with nothing in sight to press. It now sits directly
  below the photos, where you finish reviewing.

## 0.6.2

- **Requests now carry their photo.** The portal uploads a photo through its own endpoint, separate
  from the rest of the request; that step has now been captured from a real submission and
  implemented, so `attach_photo` is on in a fresh install and a request carries its photo.
- A photo becomes part of the public case record, the same as the plate and address. Turn
  `attach_photo` off to file text-only requests.
- If the portal ever changes how it takes uploads, a send now stops and says so rather than
  filing a request with the photo silently missing.

## 0.6.1

- **The 311 request now matches what the portal itself sends**, checked against a real
  submission captured from the website rather than worked out from its code.
  - Vehicle colour, make, model and plate are sent as the form's own separate answers. They were
    previously written into a description field the portal does not use at all.
  - The location block now carries the same map details, to the same precision, as the website.
  - A request filed with your contact details no longer also marks itself anonymous.
- **Re-run any dry run you looked at before updating**: the request it showed you was not the
  shape 311 expects.
- **Attaching the photo doesn't work yet** and is off in a fresh install (`attach_photo`). The
  portal uploads photos through a separate endpoint that still needs to be captured; with the
  option on, a real send stops before anything is filed and tells you so. Text-only requests
  carry the plate, vehicle, address and concern, which is everything the form requires.

## 0.6.0

- **Send requests to Sacramento 311.** A reviewed batch gets a Submit panel listing what would
  go, and each draft shows what happened to it and its case number.
- **Dry run is on by default and has to be turned off deliberately** (`submit_dry_run` in the
  app's Configuration tab). A dry run does every lookup, builds the exact request, and stops:
  no photo is uploaded and no case is created. Open **What would be sent** on a draft to read
  the text an officer would see, or **Show the raw request** for the full payload.
- **New options:** `submit_dry_run`, and `reporter_first_name` / `reporter_last_name` /
  `reporter_email` / `reporter_phone`. Leave all four names empty to file anonymously; 311 then
  sends no confirmation email.
- The photo is uploaded with each request.
- **What stops a mistake:**
  - Only drafts you marked **Report** can be sent, and only by the person who captured them.
  - Approval is pinned to the draft version you were looking at. Edit a draft and the send is
    refused until you look again.
  - Requests go one at a time with a pause between them.
  - **Nothing is ever retried automatically.** If 311 doesn't confirm, the draft is left
    **unconfirmed** and says so: a case may exist, so check the city's open data before sending
    it again. Sending an already-sent draft is refused.
  - A batch with requests at 311 can no longer be deleted; it is the record of what was sent.
- Addresses are checked against the city's own map before sending. One it can't place, or that
  is outside Sacramento, is reported instead of being sent as-is.
- Times in the request text are written in Sacramento local time.

## 0.5.0

- **The address is worked out as each photo is taken**, not hours later during extraction. It
  appears on the photo in the list within a second or two of the snap, while you're still in
  front of the house.
  - The address stands as looked up. Tap it only when it's wrong: the picker offers the
    neighbouring house numbers on the same side of the street, plus a box for anything else.
  - You can still correct a photo's address later in the session, including one already
    uploaded.
  - If the lookup fails — no signal, or nothing found near the fix — the photo uploads without
    one and extraction geocodes the GPS fix as before.
- A draft says where its address came from: **looked up while capturing**, **chosen on the
  street** or **typed on the street**.
- An address you settled on the street is no longer flagged as an estimate or as weak GPS; you
  were standing there.
- Addresses can still be changed on the draft during review, as before.

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

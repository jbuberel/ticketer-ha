# Ticketer

Take GPS-tagged photos of vehicles on a walk. They're grouped into a batch and stored on this
Home Assistant server. Each photo is then turned into a draft: plate, state, color, make, model
and the nearest street address. You review each draft: fix anything that's wrong, then choose
**Report** or **Don't report**. The ones you marked Report are then filed with Sacramento 311.

**Dry run is on when you install it.** Nothing reaches the city until you turn `submit_dry_run`
off yourself, and even then only drafts you approved are sent.

The app joins your Tailscale network (tailnet) as its own device and serves the phone app at
`https://ticketer.<your-tailnet>.ts.net`. It is **not** exposed on your LAN or the internet.

## Before you install: Tailscale admin console

1. **DNS page:** MagicDNS on, and **HTTPS Certificates** enabled.
2. **Access controls:** the policy file has
   `"tagOwners": { "tag:ticketer": ["autogroup:admin"] }`.
3. **Settings → Keys → Generate auth key:**
   - Reusable: off
   - Ephemeral: off
   - Pre-approved: on
   - Tags: `tag:ticketer`
   - Expiration: 1 day

   Copy the key; it's shown once.

## Install and start

1. Home Assistant → **Settings → Apps → App store → ⋮ → Repositories**, add
   `https://github.com/jbuberel/ticketer-ha`.
2. Install **Ticketer**.
3. **Configuration** tab:
   - Paste the Tailscale key into **Tailscale auth key**.
   - Set **Anthropic API key**; create one at console.anthropic.com → API keys. Without it,
     photos are stored but not extracted.
   - Save.
4. Start the app and open the **Log** tab. You should see
   `[ticketer] serving https://ticketer.<tailnet>.ts.net/`.
5. On your phone (with Tailscale connected), open that URL and add it to your home screen.

The key is only needed the first time; the app keeps its Tailscale identity in its data folder.
You can clear the option afterwards. Uninstalling the app deletes that folder: remove the old
`ticketer` device in the Tailscale admin console and use a fresh key when reinstalling.

## Using it

The app looks like an iPhone app on an iPhone and like an Android app on Android. Pull down on
the home screen or a batch to refresh it.

1. **Begin capture**, at the bottom of the home screen. Allow location access. The bar at the
   top shows GPS accuracy and how old the last fix is.
2. **Snap photo**, at the bottom, for each vehicle, then keep walking.
   - Each photo is saved on the phone right away, with the latest GPS fix, and uploads in the
     background.
   - If there's no recent fix, the photo waits a few seconds for one.
   - The **address** is looked up there and then and appears on the photo in the list. It's
     used as-is, so you can keep walking. Tap it if it's wrong: it offers the neighbouring
     house numbers on the same side of the street, and **Something else…** to type one.
     Correcting it works for photos already uploaded, until you finish the session.
   - No signal, or nothing found nearby? The photo goes up without an address and one is worked
     out from its GPS fix during extraction, as before.
3. **Finish**, top right, when you're done. It waits for the remaining uploads, then queues the
   batch for extraction.
4. The batch fills in as each photo is processed, usually 5–10 s per photo. Each draft shows:
   - **Plate** and state, with Claude's confidence.
   - **Local plate reader:** a second, on-device plate reading, and whether it matches.
   - A **close-up** of the plate.
   - **Vehicle:** color, make and model, with a confidence.
   - **Address:** what was settled on the street — *chosen* or *typed on the street* if you
     touched it, *looked up while capturing* if you let it stand. For a photo that had no
     address, it's worked out from the GPS fix here instead, and shown with how far away it
     matched: a "building" match is a specific address, "along the block" is an estimate.

   Treat a plate as trustworthy only when it's `plate high` **and** the local reader matches.
   Check anything else against the close-up.
   - If some photos fail, tap **Retry failed**.
   - **Re-run extraction**, at the bottom of a finished batch, replaces all of its results with
     a fresh run. It makes new API calls. Your edits are kept; Report / Don't report choices are
     cleared.
5. **Review** each draft once the batch is finished. The home screen shows how many are left.
   - The yellow box lists what to check: the plate, a plate that appears twice, a low-confidence
     vehicle, weak GPS or an estimated address, and missing fields.
   - **Edit**, beside the plate, to fix any field. The form shows the photo and the plate
     close-up; when Claude and the local reader disagree, tap either reading to use it.
   - **Report** marks the draft to be sent. It needs plate, color, make, model and address. A
     plate that isn't trusted (see above) has to be corrected, or you tick **The plate above
     matches the photo**.
   - **Don't report** for anything that shouldn't be reported, such as a guest with a pass.
     Tap a chosen option again to undo it.
   - Only the person who captured a batch can review it.
6. **Submit.** Once every draft is decided, the panel at the bottom of the batch says how many
   are ready and what will happen.
   - With **dry run** on (the default), the button builds each request and stops. Open **What
     would be sent** on a draft to read the text an officer would see, and **Show the raw
     request** for the whole payload. Nothing reaches the city.
   - With dry run off, the button says how many go to 311 and who they're filed as. It asks you
     to confirm, listing every plate and address. **This dispatches a parking officer and can't
     be undone.**
   - The photo goes with the request (`attach_photo`, on by default), along with the plate,
     vehicle, address and concern. Turn it off to file text-only requests.
   - Requests go one at a time with a short pause. Each draft then shows **sent to 311** with
     its case number.
   - **unconfirmed** means 311 never answered. The request may still have been filed, so the app
     will not send it again. Look the address up in the city's 311 open data before retrying.
   - **not sent** means it definitely wasn't filed; fix what the message says and submit again.
   - A batch with requests at 311 can't be deleted any more: it's your record of what was sent.

To take out a bad shot, tap **✕** on it; **Discard**, top left, throws the whole session away.
To get rid of a finished batch, for example test photos, open it and tap **Delete batch** at the
bottom. This permanently removes its photos, close-ups and drafts from the server. A batch with
requests already at 311 can't be deleted by hand — it stays as the record of what was sent, and
retention removes it a day later.
If the app is closed or the page reloads mid-session, reopening it resumes the session.

## Your data

- Stored in the app's data folder: `ticketer.db` (batches, locations, drafts) and `photos/`
  (photos and plate close-ups).
- Photos are excluded from Home Assistant backups. The database, including extracted plates, is
  included.
- **Sent elsewhere:**
  - Each photo, downscaled and with its metadata removed, goes to the **Anthropic API** for
    extraction.
  - Each photo's GPS position goes to the **ArcGIS geocoder** that the City of Sacramento 311
    address map uses — while you're capturing, and again during extraction for any photo that
    has no address yet.
  - The local plate reader runs on this server.
  - When you submit, the request goes to **Sacramento 311**: the plate, vehicle colour, make
    and model, the address, the photo if `attach_photo` is on, and your contact details if you
    set them. The city's own map details for that address travel with it, as they do from the
    website — including who owns the parcel. Everything in a submitted request becomes a city
    record, and stays with the city on their own terms whatever this app deletes.

### How long it is kept

The app deletes batches on its own. A photo of a car parked where it shouldn't be is worth
something for a few hours; after that it is a picture of a stranger's car with their plate next
to it, and there is nothing left to do with it.

Two clocks, whichever falls later:

| | Default | Option |
|---|---|---|
| After the last photo of a session arrived | 8 hours | `retain_unsubmitted_hours` |
| After a request was really filed with 311 | 24 hours | `retain_submitted_hours` |

Eight hours because these vehicles are parked for six to eight at most: a report you haven't
sent by then can no longer be sent usefully, because the car has gone. Twenty-four because the
city auto-closes a request it hasn't acted on within a day.

When a batch goes, its photos, plate close-ups, drafts, locations and submission payloads go
with it. A session left open on a phone expires the same way. A request still being sent holds
its batch back until it finishes.

Each batch shows how long it has left, and a batch in its last hour with something still
undecided or unsent says so at the top. **If you want a report filed, send it before then.**

What survives is one line per request that really reached 311: the case number, whether it was
confirmed, and when. That is enough to look the case up on the city's site or in their open
data. It holds no plate, no address, no photo and none of the request payload. These appear
under **Filed cases** on the home screen.

Both windows can be changed in Configuration, between 1 hour and 7 days. Retention can't be
turned off.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Log: `Tailscale is not logged in` | Set the auth key option and restart |
| Log: `tailscale serve failed` | Enable HTTPS Certificates on the Tailscale DNS page, restart |
| Log: `requested tags ... are invalid or not permitted` | Add the `tagOwners` entry; make sure the key has `tag:ticketer` |
| Device shows up as `ticketer-1` | An old `ticketer` device exists; delete it in the admin console and restart |
| App: `Not signed in: No Tailscale identity` | Open the `https://ticketer.<tailnet>.ts.net` address, with Tailscale connected |
| Submit says `dry run` and you want to send | Turn off `submit_dry_run` in the Configuration tab and restart the app |
| `The city's map can't place ...` | Edit the draft's address. It has to be a real address inside Sacramento |
| A draft is stuck on `unconfirmed` | Check the city's 311 open data for a case at that address, then decide whether to send it again |
| Phone: "Can't connect to the site", yet Tailscale is connected and `tailscale ping ticketer` works | The phone can't look up the `ts.net` name (the app log shows no requests). To confirm, open `https://<ticketer's Tailscale IP>/`: "can't provide a secure connection" means the connection works and only the lookup fails. Set Android Private DNS to Automatic/Off, set Chrome secure DNS to your current provider or off, check **Use Tailscale DNS** is on in the Tailscale app. If it still fails, restart the phone |
| GPS bar: `Location permission denied` | Allow location for the site (iPhone: Settings → Privacy & Security → Location Services → Safari Websites) |

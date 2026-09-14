# Ticketer

Take GPS-tagged photos of vehicles on a walk. They're grouped into a batch and stored on this
Home Assistant server, ready for processing. Plate and vehicle extraction come in a later
version.

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
3. **Configuration** tab: paste the key into **Tailscale auth key** and save.
4. Start the app and open the **Log** tab. You should see
   `[ticketer] serving https://ticketer.<tailnet>.ts.net/`.
5. On your phone (with Tailscale connected), open that URL and add it to your home screen.

The key is only needed the first time; the app keeps its Tailscale identity in its data folder.
You can clear the option afterwards. Uninstalling the app deletes that folder: remove the old
`ticketer` device in the Tailscale admin console and use a fresh key when reinstalling.

## Using it

1. **Begin Capture.** Allow location access. The bar at the top shows GPS accuracy and how old
   the last fix is.
2. **Snap photo** for each vehicle, then keep walking.
   - Each photo is saved on the phone right away, with the latest GPS fix, and uploads in the
     background.
   - If there's no recent fix, the photo waits a few seconds for one.
3. **Stop Capture & Process** when you're done. It waits for the remaining uploads, then
   queues the batch.

To take out a bad shot, tap **✕** on it; **Discard session** throws the whole session away.
If the app is closed or the page reloads mid-session, reopening it resumes the session.

## Your data

- Stored in the app's data folder: `ticketer.db` (batches, times, locations) and `photos/`.
- Photos are excluded from Home Assistant backups. The database is included.
- Nothing is sent anywhere else in this version, and nothing is deleted automatically yet.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Log: `Tailscale is not logged in` | Set the auth key option and restart |
| Log: `tailscale serve failed` | Enable HTTPS Certificates on the Tailscale DNS page, restart |
| Log: `requested tags ... are invalid or not permitted` | Add the `tagOwners` entry; make sure the key has `tag:ticketer` |
| Device shows up as `ticketer-1` | An old `ticketer` device exists; delete it in the admin console and restart |
| App: `Not signed in: No Tailscale identity` | Open the `https://ticketer.<tailnet>.ts.net` address, with Tailscale connected |
| GPS bar: `Location permission denied` | Allow location for the site (iPhone: Settings → Privacy & Security → Location Services → Safari Websites) |

# Ticketer

Hello-world build. The app joins your Tailscale network (tailnet) as its own device and serves
a page at `https://ticketer.<your-tailnet>.ts.net`. It is **not** exposed on your LAN or the
internet. The page checks that HTTPS, Tailscale identity, GPS, and photo upload all work from
your phone. Uploaded photos are counted and discarded.

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
5. On your phone (with Tailscale connected), open that URL.

The key is only needed the first time; the app keeps its Tailscale identity in its data folder.
You can clear the option afterwards. Uninstalling the app deletes that folder: remove the old
`ticketer` device in the Tailscale admin console and use a fresh key when reinstalling.

## What to check on the phone

- **Connection:** HTTPS = yes, and "Signed in as" shows your Tailscale account.
- **Location:** a fix within a few seconds, accuracy ideally under ~25 m.
- **Photo:** the native camera opens, and the upload reports OK with the photo's dimensions.
- Repeat after **Add to Home Screen** ("Opened as: home-screen app"). On iPhone, note whether
  the location permission prompt comes back each time the app is opened.

## Troubleshooting

| Log message | Fix |
|---|---|
| `Tailscale is not logged in` | Set the auth key option and restart |
| `tailscale serve failed` | Enable HTTPS Certificates on the Tailscale DNS page, restart |
| `requested tags ... are invalid or not permitted` | Add the `tagOwners` entry; make sure the key has `tag:ticketer` |
| Device shows up as `ticketer-1` | An old `ticketer` device exists; delete it in the admin console and restart |

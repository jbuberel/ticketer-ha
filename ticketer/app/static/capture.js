// The capture session: GPS-tagged photos taken on the street, each with its address settled on
// the spot, kept on the phone (store.js) until they have uploaded (uploads.js).

import { html, nothing, render } from "/vendor/lit-html-3.3.3/lit-html.js";
import { classMap } from "/vendor/lit-html-3.3.3/directives/class-map.js";
import { repeat } from "/vendor/lit-html-3.3.3/directives/repeat.js";
import { actionSheetController, alertController } from "/vendor/ionic-9.0.4/index.esm.js";
import { api } from "./api.js";
import { back, replace } from "./nav.js";
import * as store from "./store.js";
import { confirmed, fmtTime, notice, plural, showError, sleep, tag } from "./ui.js";
import { drainUploads, startUploads, stopUploads } from "./uploads.js";

const FRESH_FIX_MS = 15000; // a fix older than this is re-requested when a photo is taken
const WEAK_GPS_M = 25;
const page = () => document.querySelector("page-capture");

// ---- GPS ----

let fix = null; // latest { lat, lon, accuracy, heading, speed, at }
let fixError = null;
let watchId = null;

function toFix(position) {
  const c = position.coords;
  return {
    lat: c.latitude,
    lon: c.longitude,
    accuracy: c.accuracy,
    heading: Number.isFinite(c.heading) ? c.heading : null,
    speed: Number.isFinite(c.speed) ? c.speed : null,
    at: position.timestamp,
  };
}

function startGps() {
  if (!("geolocation" in navigator)) {
    fixError = "This browser can't provide a location";
    return;
  }
  stopGps();
  watchId = navigator.geolocation.watchPosition(
    (position) => { fix = toFix(position); fixError = null; page()?.draw(); },
    (error) => {
      fixError = error.code === error.PERMISSION_DENIED
        ? "Location permission denied: photos will have no location"
        : `Location unavailable: ${error.message}`;
      page()?.draw();
    },
    { enableHighAccuracy: true, maximumAge: 0, timeout: 30000 },
  );
}

function stopGps() {
  if (watchId != null) navigator.geolocation.clearWatch(watchId);
  watchId = null;
}

// iOS can stop the watch while the app is in the background.
export function resumeGps() {
  if (watchId != null) startGps();
}

const isFresh = (f) => f != null && Date.now() - f.at <= FRESH_FIX_MS;

// The watch can go quiet while the camera is open; ask once more before settling for a stale fix.
function freshFix() {
  if (isFresh(fix) || !("geolocation" in navigator)) return Promise.resolve(fix);
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (position) => { fix = toFix(position); resolve(fix); },
      () => resolve(fix),
      { enableHighAccuracy: true, maximumAge: 5000, timeout: 10000 },
    );
  });
}

// The address for a fix, looked up now rather than during extraction, so it can be checked
// against the house actually being stood in front of.
async function lookupAddress(at) {
  try {
    const found = await api("GET", `/api/geocode?lat=${at.lat}&lon=${at.lon}`);
    return found.address ? found : null;
  } catch {
    return null; // extraction geocodes the fix again later, so this only costs the check
  }
}

function gpsBanner() {
  if (!fix) {
    return html`<div class="gps ${fixError ? "bad" : ""}">
      <ion-icon name="locate-outline" aria-hidden="true"></ion-icon>${fixError ?? "Waiting for GPS…"}</div>`;
  }
  const age = Math.max(0, Math.round((Date.now() - fix.at) / 1000));
  return html`<div class="gps ${fix.accuracy > WEAK_GPS_M || age > 15 ? "warn" : "ok"}">
    <ion-icon name="locate" aria-hidden="true"></ion-icon>GPS ±${Math.round(fix.accuracy)} m · ${age <= 1 ? "now" : `${age} s ago`}</div>`;
}

// ---- Thumbnails ----

const thumbUrls = new Map(); // capture id -> object URL

function thumbUrl(capture) {
  let url = thumbUrls.get(capture.id);
  if (!url) {
    url = URL.createObjectURL(new Blob([capture.thumb.data], { type: capture.thumb.type }));
    thumbUrls.set(capture.id, url);
  }
  return url;
}

function dropThumb(id) {
  const url = thumbUrls.get(id);
  if (url) URL.revokeObjectURL(url);
  thumbUrls.delete(id);
}

async function makeThumb(file) {
  try {
    const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
    const scale = 160 / Math.max(bitmap.width, bitmap.height);
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close();
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.7));
    return blob ? { data: await blob.arrayBuffer(), type: "image/jpeg" } : null;
  } catch {
    return null; // no thumbnail; the photo itself is still kept
  }
}

// ---- Session ----

// Called by the uploader whenever a photo changes state.
export function onUploadChange() {
  page()?.refresh();
}

async function endSession(session) {
  stopUploads();
  stopGps();
  await store.clearSession(session.batchId);
  for (const id of [...thumbUrls.keys()]) dropThumb(id);
}

const STATE_LABEL = {
  locating: "getting location",
  geocoding: "finding address",
  queued: "waiting to upload",
  uploading: "uploading",
  uploaded: "uploaded",
  failed: "upload failed",
};
const STATE_KIND = { locating: "warn", geocoding: "warn", queued: "info", uploading: "warn", uploaded: "ok", failed: "bad" };
const LOOKING_UP = ["locating", "geocoding"];

// The page is drawn once per visit and then only updated in place, so the file input is never
// replaced while the camera is open: a replaced input drops the photo it was about to deliver.
class CapturePage extends HTMLElement {
  connectedCallback() {
    this.session = null;
    this.captures = [];
    this.busy = null; // status text while finishing or discarding
    this.ticket = 0;
    this.draw();
    this.tick = setInterval(() => this.draw(), 1000); // the GPS fix's age counts up
    this.start();
  }

  disconnectedCallback() {
    clearInterval(this.tick);
  }

  async start() {
    this.session = await store.getSession();
    if (!this.session) return replace("/"); // ended in another tab
    if (watchId == null) startGps();
    await this.refresh();
  }

  // Re-reads the photos from the phone's storage. A later refresh overtakes an earlier one that
  // is still reading, so an older list never lands on top of a newer one.
  async refresh() {
    if (!this.session) return;
    const ticket = ++this.ticket;
    const captures = await store.capturesFor(this.session.batchId);
    if (ticket !== this.ticket) return;
    this.captures = captures.sort((a, b) => b.capturedAt.localeCompare(a.capturedAt));
    this.draw();
  }

  draw() {
    render(this.view(), this);
  }

  setBusy(message) {
    this.busy = message;
    this.draw();
  }

  view() {
    const { session, captures, busy } = this;
    const uploaded = captures.filter((c) => c.state === "uploaded").length;
    return html`
      <ion-header>
        <ion-toolbar>
          <ion-buttons slot="start">
            <ion-button color="danger" ?disabled=${busy != null} @click=${() => this.discard()}>Discard</ion-button>
          </ion-buttons>
          <ion-title>Capturing</ion-title>
          <ion-buttons slot="end">
            <ion-button strong ?disabled=${busy != null} @click=${() => this.finish()}>Finish</ion-button>
          </ion-buttons>
          ${busy ? html`<ion-progress-bar type="indeterminate"></ion-progress-bar>` : nothing}
        </ion-toolbar>
      </ion-header>
      <ion-content>
        <div class="capture-status">
          ${gpsBanner()}
          <p class="muted small">${session ? `Started ${fmtTime(session.startedAt)} · ` : ""}${captures.length
            ? `${plural(captures.length, "photo")} · ${uploaded} uploaded`
            : "No photos yet. Snap each vehicle as you walk."}</p>
          ${busy ? notice("", busy) : nothing}
        </div>
        ${captures.length ? html`<ion-list inset class="shots">
          ${repeat(captures, (capture) => capture.id, (capture) => this.shot(capture))}
        </ion-list>` : nothing}
        <input id="snap" type="file" accept="image/*" capture="environment" @change=${(event) => this.onSnap(event.target)}>
      </ion-content>
      <ion-footer>
        <ion-toolbar>
          <!-- A label for the file input rather than a button that clicks it: tapping a label is
               the one way of opening the camera that every phone treats as the user's own tap. -->
          <label for="snap" class=${classMap({ "snap-label": true, disabled: busy != null })}>
            <ion-button class="footer-action" expand="block" tabindex="-1" ?disabled=${busy != null}>
              <ion-icon slot="start" name="camera"></ion-icon>Snap photo
            </ion-button>
          </label>
        </ion-toolbar>
      </ion-footer>`;
  }

  shot(capture) {
    let where = "no location";
    if (capture.state === "locating") where = "getting location…";
    else if (capture.fix) {
      const age = Math.round((Date.parse(capture.capturedAt) - capture.fix.at) / 1000);
      where = `±${Math.round(capture.fix.accuracy)} m${age > 15 ? ` · fix ${age} s old` : ""}`;
    }
    const weak = (!capture.fix || capture.fix.accuracy > WEAK_GPS_M) && capture.state !== "locating";
    return html`
      <ion-item>
        <ion-thumbnail slot="start">${capture.thumb ? html`<img src=${thumbUrl(capture)} alt="">` : nothing}</ion-thumbnail>
        <ion-label class="ion-text-wrap">
          <div class="shot-head">${fmtTime(capture.capturedAt)} ${tag(STATE_LABEL[capture.state], STATE_KIND[capture.state])}</div>
          ${this.shotAddress(capture)}
          <p class=${weak ? "warn-text" : ""}>${where}</p>
          ${capture.error ? html`<p class="error-text">${capture.error}</p>` : nothing}
        </ion-label>
        <ion-button slot="end" fill="clear" color="medium" aria-label="Remove photo"
          ?disabled=${this.busy != null || capture.state === "uploading" || LOOKING_UP.includes(capture.state)}
          @click=${() => this.removeShot(capture)}>
          <ion-icon slot="icon-only" name="close-circle-outline"></ion-icon>
        </ion-button>
      </ion-item>`;
  }

  // The looked-up address stands as-is; tapping it offers the neighbours for the times it's a
  // door or two off. Locked while the photo is on the wire, so an edit can't race its own upload.
  shotAddress(capture) {
    if (LOOKING_UP.includes(capture.state)) return html`<p>finding address…</p>`;
    const known = capture.address?.address;
    return html`
      <button class=${classMap({ address: true, none: !known })} ?disabled=${this.busy != null || capture.state === "uploading"}
        @click=${() => this.pickAddress(capture)}>
        ${known ?? "No address — tap to set"}<ion-icon name="create-outline" aria-hidden="true"></ion-icon>
      </button>`;
  }

  // Neighbours first, one tap each; anything else is typed.
  async pickAddress(capture) {
    const chosen = capture.address?.address;
    const candidates = capture.candidates ?? [];
    let typed = !candidates.length;
    if (!typed) {
      const sheet = await actionSheetController.create({
        header: `Photo at ${fmtTime(capture.capturedAt)}: which address?`,
        buttons: [
          ...candidates.map((option) => ({
            text: option, data: { address: option }, cssClass: option === chosen ? "chosen" : undefined,
          })),
          { text: "Something else…", data: { other: true } },
          { text: "Cancel", role: "cancel" },
        ],
      });
      await sheet.present();
      const { data, role } = await sheet.onDidDismiss();
      if (role === "cancel" || role === "backdrop" || !data) return;
      if (data.address) return this.setShotAddress(capture, data.address, "picked");
      typed = true;
    }
    const alert = await alertController.create({
      header: "Address",
      message: candidates.length ? undefined : "No addresses were found near this photo.",
      inputs: [{ name: "address", value: chosen ?? "", placeholder: "e.g. 1200 Example St",
                 attributes: { autocapitalize: "words", autocomplete: "off" } }],
      buttons: [{ text: "Cancel", role: "cancel" }, { text: "Use this", role: "confirm" }],
    });
    await alert.present();
    const { data, role } = await alert.onDidDismiss();
    if (role === "confirm") await this.setShotAddress(capture, data?.values?.address, "typed");
  }

  // A photo still on the phone carries the new address up with it; one already uploaded is
  // corrected on the server.
  async setShotAddress(capture, value, source) {
    const address = (value ?? "").trim();
    if (!address) return showError("Pick an address from the list, or type one in.");
    if (address === capture.address?.address) return;
    // full and match described the geocoder's own match, which this no longer is.
    const settled = { address, full: null, source, match: null };
    const outcome = await store.setAddressBeforeUpload(capture.id, settled);
    if (outcome === "uploading") {
      return showError("That photo is uploading right now. Set its address again in a moment.");
    }
    if (outcome === "uploaded") {
      try {
        await api("PATCH", `/api/batches/${this.session.batchId}/captures/${capture.id}`,
          { json: { address, address_source: source } });
      } catch (error) {
        return showError(`Couldn't save the address: ${error.message}`);
      }
      await store.updateCapture(capture.id, { address: settled });
    }
    await this.refresh();
  }

  async removeShot(capture) {
    if (!await confirmed({ header: "Remove this photo?", confirm: "Remove", destructive: true })) return;
    const outcome = await store.removeIfNotSent(capture.id);
    if (outcome === "uploading") {
      return showError("That photo is uploading right now. Remove it again in a moment.");
    }
    if (outcome !== "removed") { // uploaded, or may have been: delete it on the server first
      try {
        await api("DELETE", `/api/batches/${this.session.batchId}/captures/${capture.id}`);
      } catch (error) {
        return showError(`Couldn't remove the photo: ${error.message}`);
      }
      await store.removeCapture(capture.id);
    }
    dropThumb(capture.id);
    await this.refresh();
  }

  async onSnap(input) {
    const file = input.files[0];
    if (!file) return;
    const { session } = this;
    try {
      const capturedAt = new Date().toISOString();
      const haveFix = isFresh(fix);
      const [data, thumb] = await Promise.all([file.arrayBuffer(), makeThumb(file)]);
      const capture = {
        id: crypto.randomUUID(),
        batchId: session.batchId,
        capturedAt,
        fix: haveFix ? fix : null,
        address: null,
        candidates: [],
        state: haveFix ? "geocoding" : "locating",
        error: null,
        thumb,
      };
      await store.addCapture(capture, { data, type: file.type || "image/jpeg" });
      await this.refresh();

      let located = capture.fix;
      if (!haveFix) {
        located = await freshFix();
        await store.updateCapture(capture.id, { fix: located, state: located ? "geocoding" : "queued" });
        await this.refresh();
      }
      // Looking the address up now, while the phone is still in front of the house, is the whole
      // point: a batch reviewed hours later can't say which one it was.
      if (located) {
        const found = await lookupAddress(located);
        await store.updateCapture(capture.id, {
          address: found && { address: found.address, full: found.address_full, source: "geocoded", match: found.address_match },
          candidates: found?.candidates ?? [],
          state: "queued",
        });
        await this.refresh();
      }
      startUploads(onUploadChange);
    } catch (error) {
      showError(`Couldn't save that photo: ${error.message}`);
    } finally {
      input.value = "";
    }
  }

  async finish() {
    const { session } = this;
    const captures = await store.capturesFor(session.batchId);
    if (captures.length === 0) return this.discard("No photos in this session. Discard it?");

    this.setBusy("Uploading photos…");
    try {
      for (let attempt = 0; ; attempt++) {
        const remaining = await drainUploads();
        if (remaining.length === 0) break;
        const rejected = remaining.filter((c) => c.state === "failed" && !c.retryable);
        if (rejected.length) {
          throw new Error(`${plural(rejected.length, "photo")} rejected (${rejected[0].error}). Remove and try again.`);
        }
        if (attempt >= 20) {
          throw new Error(`${plural(remaining.length, "photo")} still not uploaded. Check the connection and try again.`);
        }
        this.setBusy(`Uploading… ${captures.length - remaining.length} of ${captures.length} done`);
        await sleep(1500);
      }
      this.setBusy("Starting processing…");
      await api("POST", `/api/batches/${session.batchId}/process`);
      await endSession(session);
      replace(`/batch/${session.batchId}`);
    } catch (error) {
      this.setBusy(null);
      showError(error.message);
    }
  }

  async discard(question = "Discard this session and delete its photos?") {
    const { session } = this;
    if (!await confirmed({ header: question, confirm: "Discard", destructive: true })) return;
    this.setBusy("Discarding…");
    stopUploads();
    try {
      await api("DELETE", `/api/batches/${session.batchId}`);
    } catch (error) {
      const current = await store.getSession();
      // Offline and never reached the server: nothing to delete there.
      if (error.status || current?.batchCreated) {
        this.setBusy(null);
        startUploads(onUploadChange);
        return showError(`Couldn't discard on the server: ${error.message}`);
      }
    }
    await endSession(session);
    back("/");
  }
}

customElements.define("page-capture", CapturePage);

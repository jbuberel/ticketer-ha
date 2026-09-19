// Ticketer phone UI: home (recent batches), capture session, batch view.

import { api } from "./api.js";
import * as store from "./store.js";
import { drainUploads, startUploads, stopUploads } from "./uploads.js";

const FRESH_FIX_MS = 15000; // a fix older than this is re-requested when a photo is taken
const view = document.getElementById("view");
const toast = document.getElementById("toast");
const thumbUrls = new Map(); // capture id -> object URL

const whoami = api("GET", "/api/whoami").catch((error) => ({ error: error.message }));
let fix = null; // latest { lat, lon, accuracy, heading, speed, at }
let fixError = null;
let watchId = null;
let busy = null; // status text while stopping or discarding a session
let picking = null; // { id, candidates, typed } while the address picker is open on a shot

// ---- DOM helpers ----

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  el.append(...children.flat().filter((c) => c != null && c !== false));
  return el;
}

const chip = (text, kind) => h("span", { class: `chip ${kind}` }, text);
const notice = (kind, text) => h("p", { class: `notice ${kind}` }, text);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const fmtTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const fmtDateTime = (iso) => new Date(iso).toLocaleString([], {
  month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
});
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

let toastTimer = null;
function showError(message) {
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 8000);
}

function navigate(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

// ---- GPS ----

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
    (position) => { fix = toFix(position); fixError = null; renderGps(); },
    (error) => {
      fixError = error.code === error.PERMISSION_DENIED
        ? "Location permission denied: photos will have no location"
        : `Location unavailable: ${error.message}`;
      renderGps();
    },
    { enableHighAccuracy: true, maximumAge: 0, timeout: 30000 },
  );
}

function stopGps() {
  if (watchId != null) navigator.geolocation.clearWatch(watchId);
  watchId = null;
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
async function lookupAddress(fix) {
  try {
    const found = await api("GET", `/api/geocode?lat=${fix.lat}&lon=${fix.lon}`);
    return found.address ? found : null;
  } catch {
    return null; // extraction geocodes the fix again later, so this only costs the check
  }
}

function renderGps() {
  const el = document.getElementById("gps");
  if (!el) return;
  if (!fix) {
    el.className = fixError ? "gps bad" : "gps";
    el.textContent = fixError ?? "Waiting for GPS…";
    return;
  }
  const age = Math.max(0, Math.round((Date.now() - fix.at) / 1000));
  el.className = `gps ${fix.accuracy > 25 || age > 15 ? "warn" : "ok"}`;
  el.textContent = `GPS ±${Math.round(fix.accuracy)} m · ${age <= 1 ? "now" : `${age} s ago`}`;
}
setInterval(renderGps, 1000);

// ---- Routing ----

async function route() {
  const session = await store.getSession();
  const hash = location.hash || "#/";
  if (hash.startsWith("#/batch/")) return renderBatch(decodeURIComponent(hash.slice("#/batch/".length)));
  if (session) return renderCapture(session); // an unfinished session always comes first
  return renderHome();
}

// ---- Home ----

async function renderHome() {
  stopGps();
  view.dataset.view = "home";
  const user = h("p", { class: "muted" });
  const list = h("div", { class: "batches" }, h("p", { class: "muted" }, "Loading…"));
  view.replaceChildren(
    h("header", {}, h("h1", {}, "Ticketer"), user),
    h("button", { class: "button primary big", onclick: beginCapture }, "Begin Capture"),
    h("section", {}, h("h2", {}, "Recent batches"), list),
  );

  const me = await whoami;
  if (me.error) user.replaceWith(notice("error", `Not signed in: ${me.error}`));
  else user.textContent = `${me.user_name || me.user_login} · v${me.version}`;

  try {
    const { batches } = await api("GET", "/api/batches");
    list.replaceChildren(...(batches.length
      ? batches.map((b) => h("a", { class: "batch", href: `#/batch/${b.id}` },
        h("span", {}, h("strong", {}, fmtDateTime(b.created_at)), h("span", { class: "muted" }, ` · ${b.created_by_name || b.created_by}`)),
        h("span", {}, `${plural(b.capture_count, "photo")} `, batchChip(b))))
      : [h("p", { class: "muted" }, "No batches yet.")]));
  } catch (error) {
    list.replaceChildren(notice("error", `Couldn't load batches: ${error.message}`));
  }
}

async function beginCapture() {
  navigator.storage?.persist?.().catch(() => {});
  await store.setSession({ batchId: crypto.randomUUID(), startedAt: new Date().toISOString(), batchCreated: false });
  startUploads(onUploadChange); // creates the batch on the server in the background
  navigate("#/");
}

// ---- Capture session ----

// The layout is built once per visit; updates only touch the parts below the Snap button,
// so the file input is never replaced while the camera is open.
async function renderCapture(session) {
  view.dataset.view = "capture";
  picking = null;
  if (watchId == null) startGps();
  view.replaceChildren(
    h("header", {}, h("h1", {}, "Capturing"), h("p", { class: "muted" }, `Started ${fmtTime(session.startedAt)}`)),
    h("div", { id: "gps", class: "gps" }),
    h("label", { id: "snap-label", class: "button primary big", for: "snap" }, "Snap photo"),
    h("input", { id: "snap", type: "file", accept: "image/*", capture: "environment", onchange: (e) => onSnap(session, e.target) }),
    h("p", { id: "count", class: "muted" }),
    h("ul", { id: "shots", class: "shots" }),
    h("footer", { id: "actions", class: "actions" }),
  );
  renderGps();
  await updateCapture(session);
}

// Background updates (an upload finishing) leave an open picker alone: redrawing the list would
// take the keyboard focus out of its text field. Opening and closing it ask for the redraw.
async function updateCapture(session, { redrawShots = !picking } = {}) {
  if (view.dataset.view !== "capture") return;
  const captures = (await store.capturesFor(session.batchId))
    .sort((a, b) => b.capturedAt.localeCompare(a.capturedAt));
  const uploaded = captures.filter((c) => c.state === "uploaded").length;

  document.getElementById("snap-label").classList.toggle("disabled", busy != null);
  document.getElementById("count").textContent = captures.length
    ? `${plural(captures.length, "photo")} · ${uploaded} uploaded`
    : "No photos yet. Snap each vehicle as you walk.";
  if (redrawShots) document.getElementById("shots").replaceChildren(...captures.map((c) => shotItem(session, c)));
  document.getElementById("actions").replaceChildren(
    busy ? h("p", { class: "muted" }, busy) : "",
    h("button", { class: "button primary", disabled: busy != null, onclick: () => stopAndProcess(session) }, "Stop Capture & Process"),
    h("button", { class: "button subtle", disabled: busy != null, onclick: () => discardSession(session) }, "Discard session"),
  );
}

const STATE_LABEL = {
  locating: "getting location",
  geocoding: "finding address",
  queued: "waiting to upload",
  uploading: "uploading",
  uploaded: "uploaded",
  failed: "upload failed",
};

const LOOKING_UP = ["locating", "geocoding"];

function shotItem(session, capture) {
  if (picking?.id === capture.id) return addressPicker(session, capture);
  let where = "no location";
  if (capture.state === "locating") where = "getting location…";
  else if (capture.fix) {
    const age = Math.round((Date.parse(capture.capturedAt) - capture.fix.at) / 1000);
    where = `±${Math.round(capture.fix.accuracy)} m${age > 15 ? ` · fix ${age} s old` : ""}`;
  }
  const weak = !capture.fix || capture.fix.accuracy > 25;
  return h("li", { class: "shot", id: `shot-${capture.id}` },
    capture.thumb ? h("img", { src: thumbUrl(capture), alt: "" }) : h("div", { class: "thumb" }),
    h("div", { class: "meta" },
      h("div", {}, fmtTime(capture.capturedAt), " ", chip(STATE_LABEL[capture.state], capture.state)),
      shotAddress(session, capture),
      h("div", { class: `small ${weak && capture.state !== "locating" ? "warn-text" : "muted"}` }, where),
      capture.error ? h("div", { class: "small error-text" }, capture.error) : null),
    h("button", {
      class: "icon",
      "aria-label": "Remove photo",
      disabled: busy != null || capture.state === "uploading" || LOOKING_UP.includes(capture.state),
      onclick: () => removeShot(session, capture),
    }, "✕"));
}

// The looked-up address stands as-is; tapping it opens the picker for the times it's a door or
// two off. Locked while the photo is on the wire, so an edit can't race its own upload.
function shotAddress(session, capture) {
  if (LOOKING_UP.includes(capture.state)) return h("div", { class: "small muted" }, "finding address…");
  const known = capture.address?.address;
  return h("button", {
    class: `address ${known ? "" : "none"}`,
    disabled: busy != null || capture.state === "uploading",
    onclick: () => openPicker(session, capture),
  }, known ?? "No address — tap to set", h("span", { class: "pencil", "aria-hidden": "true" }, "✎"));
}

function openPicker(session, capture) {
  picking = { id: capture.id, candidates: capture.candidates ?? [], typed: capture.address?.address ?? "" };
  updateCapture(session, { redrawShots: true }).then(() => {
    document.getElementById(`shot-${capture.id}`)?.scrollIntoView({ block: "nearest" });
  });
}

function closePicker(session) {
  picking = null;
  return updateCapture(session, { redrawShots: true });
}

function addressPicker(session, capture) {
  const chosen = capture.address?.address;
  const typedInput = h("input", {
    value: picking.typed, autocomplete: "off", placeholder: "e.g. 1200 Example St",
    "aria-label": "A different address",
    oninput: (event) => { picking.typed = event.target.value; },
  });
  return h("li", { class: "shot picking", id: `shot-${capture.id}` },
    h("div", { class: "picker" },
      h("div", { class: "small muted" }, `Photo at ${fmtTime(capture.capturedAt)} — which address?`),
      picking.candidates.length
        ? h("div", { class: "options" }, picking.candidates.map((option) => h("button", {
          class: `option ${option === chosen ? "on" : ""}`,
          "aria-pressed": String(option === chosen),
          onclick: () => setShotAddress(session, capture, option, "picked"),
        }, option)))
        : h("p", { class: "small muted" }, "No addresses were found near this photo."),
      h("label", { class: "field" }, "Something else", typedInput),
      h("div", { class: "form-actions" },
        h("button", { class: "button primary", onclick: () => setShotAddress(session, capture, typedInput.value, "typed") }, "Use this"),
        h("button", { class: "button subtle", onclick: () => closePicker(session) }, "Cancel"))));
}

// An uploaded photo is corrected on the server; one still waiting carries the new address up with it.
async function setShotAddress(session, capture, value, source) {
  const address = (value ?? "").trim();
  if (!address) return showError("Pick an address from the list, or type one in.");
  if (address === capture.address?.address) return closePicker(session);
  if (capture.state === "uploaded") {
    try {
      await api("PATCH", `/api/batches/${session.batchId}/captures/${capture.id}`,
        { json: { address, address_source: source } });
    } catch (error) {
      return showError(`Couldn't save the address: ${error.message}`);
    }
  }
  // full and match described the geocoder's own match, which this no longer is.
  await store.updateCapture(capture.id, { address: { address, full: null, source, match: null } });
  await closePicker(session);
}

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

async function onSnap(session, input) {
  const file = input.files[0];
  if (!file) return;
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
    await updateCapture(session);

    let located = capture.fix;
    if (!haveFix) {
      located = await freshFix();
      await store.updateCapture(capture.id, { fix: located, state: located ? "geocoding" : "queued" });
      await updateCapture(session);
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
      await updateCapture(session);
    }
    startUploads(onUploadChange);
  } catch (error) {
    showError(`Couldn't save that photo: ${error.message}`);
  } finally {
    input.value = "";
  }
}

async function onUploadChange() {
  const session = await store.getSession();
  if (session) await updateCapture(session);
}

async function removeShot(session, capture) {
  if (!confirm("Remove this photo?")) return;
  if (capture.state === "uploaded" || capture.state === "failed") {
    try {
      await api("DELETE", `/api/batches/${session.batchId}/captures/${capture.id}`);
    } catch (error) {
      return showError(`Couldn't remove the photo: ${error.message}`);
    }
  }
  await store.removeCapture(capture.id);
  dropThumb(capture.id);
  await updateCapture(session);
}

async function setBusy(message, session) {
  busy = message;
  await updateCapture(session);
}

async function stopAndProcess(session) {
  const captures = await store.capturesFor(session.batchId);
  if (captures.length === 0) return discardSession(session, "No photos in this session. Discard it?");

  await setBusy("Uploading photos…", session);
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
      await setBusy(`Uploading… ${captures.length - remaining.length} of ${captures.length} done`, session);
      await sleep(1500);
    }
    await setBusy("Starting processing…", session);
    await api("POST", `/api/batches/${session.batchId}/process`);
    await endSession(session);
    navigate(`#/batch/${session.batchId}`);
  } catch (error) {
    await setBusy(null, session);
    showError(error.message);
  }
}

async function discardSession(session, question = "Discard this session and delete its photos?") {
  if (!confirm(question)) return;
  await setBusy("Discarding…", session);
  stopUploads();
  try {
    await api("DELETE", `/api/batches/${session.batchId}`);
  } catch (error) {
    const current = await store.getSession();
    // Offline and never reached the server: nothing to delete there.
    if (error.status || current?.batchCreated) {
      await setBusy(null, session);
      return showError(`Couldn't discard on the server: ${error.message}`);
    }
  }
  await endSession(session);
  navigate("#/");
}

async function endSession(session) {
  stopUploads();
  stopGps();
  busy = null;
  await store.clearSession(session.batchId);
  for (const id of [...thumbUrls.keys()]) dropThumb(id);
}

// ---- Batch view ----

const BATCH_REFRESH_MS = 3000;
const CONFIDENCE_KIND = { high: "ok", medium: "warn", low: "bad" };
const WEAK_GPS_M = 25;
const FIELDS = [["plate_text", "Plate"], ["plate_state", "State"], ["color", "Color"], ["make", "Make"], ["model", "Model"], ["address", "Address"]];
const REQUIRED_TO_REPORT = [["plate_text", "plate"], ["color", "color"], ["make", "make"], ["model", "model"], ["address", "address"]];
const missingFields = (values) => REQUIRED_TO_REPORT.filter(([f]) => !values[f]).map(([, label]) => label);
const edited = (edits, ...fields) => fields.some((f) => f in edits);
let batchTimer = null;
let shown = null; // { batch, me } on screen
let editing = null; // { id, values, plateChecked, reportAfter } for the draft whose edit form is open
let saving = null; // capture id with a review change in flight
let submitting = false; // a submit request is in flight

// Re-renders in place every few seconds while extraction is running.
async function renderBatch(id) {
  stopGps();
  clearTimeout(batchTimer);
  if (view.dataset.view !== "batch" || view.dataset.batchId !== id) {
    view.dataset.view = "batch";
    view.dataset.batchId = id;
    shown = null;
    editing = null;
    view.replaceChildren(
      h("a", { class: "back", href: "#/" }, "‹ Back"),
      h("div", { id: "batch-body" }, h("p", { class: "muted" }, "Loading…")),
    );
  }
  const body = document.getElementById("batch-body");
  try {
    const [batch, me] = await Promise.all([api("GET", `/api/batches/${encodeURIComponent(id)}`), whoami]);
    if (view.dataset.view !== "batch" || view.dataset.batchId !== id) return; // navigated away
    shown = { batch, me };
    drawBatch();
    const working = batch.captures.some((c) => ["queued", "sending"].includes(c.submission?.status));
    if (batch.status === "queued" || batch.status === "processing" || working) {
      batchTimer = setTimeout(() => {
        if (location.hash === `#/batch/${id}`) renderBatch(id);
      }, BATCH_REFRESH_MS);
    }
  } catch (error) {
    body.replaceChildren(
      notice("error", `Couldn't load this batch: ${error.message}`),
      h("button", { class: "button subtle", onclick: () => renderBatch(id) }, "Try again"),
    );
  }
}

function batchChip(batch) {
  if (batch.status !== "ready") return chip(batch.status, batch.status);
  return batch.review.undecided ? chip(`${batch.review.undecided} to review`, "warn") : chip("reviewed", "ok");
}

function drawBatch() {
  const body = document.getElementById("batch-body");
  if (!body || !shown || view.dataset.batchId !== shown.batch.id) return;
  body.replaceChildren(...batchContent(shown.batch, shown.me).filter(Boolean));
}

function batchContent(batch, me) {
  const drafts = batch.drafts;
  const canReview = batch.status === "ready" && me.user_login === batch.created_by;
  const status = {
    capturing: notice("info", "This session is still open on the phone that started it."),
    queued: me.extraction_enabled === false
      ? notice("error", "Extraction is off. In Home Assistant, set the Anthropic API key in the Ticketer app's Configuration tab and restart the app.")
      : notice("info", "Waiting for extraction to start…"),
    processing: notice("info", `Extracting… ${drafts.done + drafts.error} of ${batch.capture_count} done`),
    ready: reviewSummary(batch, canReview),
  }[batch.status];
  const cost = batch.cost_usd ? ` · $${batch.cost_usd.toFixed(3)}` : "";
  return [
    h("header", {},
      h("h1", {}, fmtDateTime(batch.created_at)),
      h("p", { class: "muted" }, `${plural(batch.capture_count, "photo")} · ${batch.created_by_name || batch.created_by}${cost}`)),
    status,
    drafts.error ? h("div", { class: "notice error" },
      `${plural(drafts.error, "photo")} couldn't be extracted.`,
      h("button", { class: "button subtle inline", onclick: () => retryExtraction(batch.id) }, "Retry failed")) : null,
    batch.status === "ready" && canReview ? submitPanel(batch, me) : null,
    h("ul", { class: "photos" }, batch.captures.map((capture, index) => draftCard(capture, index, { batch, canReview }))),
    batch.status === "ready" && drafts.done && canReview
      ? h("button", { class: "button subtle", onclick: () => rerunBatch(batch) }, "Re-run extraction")
      : null,
    // An open capture session is discarded from the phone that started it instead.
    batch.status !== "capturing" && me.user_login === batch.created_by
      ? h("button", { class: "button danger", onclick: () => deleteBatch(batch) }, "Delete batch")
      : null,
  ];
}

// Drafts marked Report that 311 hasn't been told about yet. A draft whose submission came back
// `unknown` is deliberately not here: the owner has to check the city's open data first.
const SENDABLE = (capture) => capture.draft?.review.decision === "report"
  && !["queued", "sending", "sent", "unknown"].includes(capture.submission?.status ?? "");

function submitPanel(batch, me) {
  const sendable = batch.captures.filter(SENDABLE);
  const sent = batch.captures.filter((c) => c.submission?.status === "sent");
  const dryRun = me.submit_dry_run !== false;
  if (!sendable.length) {
    return sent.length
      ? notice("ok", `${plural(sent.length, "request")} sent to 311.`)
      : null;
  }
  return h("div", { class: `notice ${dryRun ? "" : "warn"}` },
    h("div", {}, h("strong", {}, `${plural(sendable.length, "request")} ready to send`),
      sent.length ? ` · ${sent.length} already sent` : ""),
    h("div", { class: "small" }, dryRun
      ? "Dry run is on: this builds the exact request and shows it to you without sending anything to 311."
      : `These go to Sacramento 311 as ${me.reporter === "anonymous" ? "an anonymous report" : me.reporter}, with the photo attached. A parking officer is dispatched.`),
    h("button", { class: `button ${dryRun ? "subtle" : "primary"}`, disabled: submitting,
      onclick: () => submitDrafts(batch, sendable, dryRun) },
      submitting ? "Sending…" : dryRun ? `Dry run ${plural(sendable.length, "request")}` : `Send ${plural(sendable.length, "request")} to 311`));
}

async function submitDrafts(batch, sendable, dryRun) {
  const what = sendable.map((c, i) => `${i + 1}. ${c.draft.review.values.plate_text} — ${c.draft.review.values.address}`).join("\n");
  const question = dryRun
    ? `Build ${plural(sendable.length, "request")} without sending?\n\n${what}\n\nNothing is sent to 311.`
    : `Send ${plural(sendable.length, "request")} to Sacramento 311?\n\n${what}\n\nThis files real requests and dispatches a parking officer. It can't be undone.`;
  if (!confirm(question)) return;
  submitting = true;
  drawBatch();
  try {
    await api("POST", `/api/batches/${encodeURIComponent(batch.id)}/submit`, {
      json: { dry_run: dryRun, drafts: sendable.map((c) => ({ capture_id: c.id, version: c.draft.review.version })) },
    });
  } catch (error) {
    showError(`Couldn't submit: ${error.message}`);
  } finally {
    submitting = false;
  }
  renderBatch(batch.id);
}

async function deleteBatch(batch) {
  const decided = batch.review.report + batch.review.skip;
  const reviewed = decided ? ` and your review of ${plural(decided, "draft")}` : "";
  if (!confirm(`Delete this batch? Its ${plural(batch.capture_count, "photo")}, plate close-ups and drafts${reviewed} are removed from the server. This can't be undone.`)) return;
  try {
    await api("DELETE", `/api/batches/${encodeURIComponent(batch.id)}`);
  } catch (error) {
    return showError(`Couldn't delete the batch: ${error.message}`);
  }
  clearTimeout(batchTimer);
  shown = null;
  editing = null;
  navigate("#/");
}

function reviewSummary(batch, canReview) {
  const decisions = batch.captures.map((c) => c.draft?.review.decision);
  const report = decisions.filter((d) => d === "report").length;
  const skip = decisions.filter((d) => d === "skip").length;
  const undecided = decisions.length - report - skip;
  const counts = `${report} to report · ${skip} not reporting`;
  if (!canReview) {
    return notice("info", `${counts} · ${undecided} undecided. Only ${batch.created_by_name || batch.created_by} can review this batch.`);
  }
  return h("div", { class: `notice ${undecided ? "" : "ok"}` },
    h("div", {}, h("strong", {}, undecided ? `${plural(undecided, "draft")} to review` : "All drafts reviewed"), ` · ${counts}`),
    undecided ? h("div", { class: "small" }, "Choose Report or Don't report for each photo. Edit anything that's wrong first.") : null);
}

function draftCard(capture, index, ctx) {
  const review = capture.draft?.review;
  return h("li", { class: `draft ${review?.decision ?? ""}`, id: `draft-${capture.id}` },
    h("img", {
      src: capture.photo_url, loading: "lazy", width: capture.width, height: capture.height,
      alt: `Photo ${index + 1}, taken at ${fmtTime(capture.captured_at)}`,
    }),
    h("div", { class: "draft-body" },
      editing?.id === capture.id && ctx.canReview && review
        ? editForm(capture)
        : draftDetails(capture, index, ctx)));
}

function draftDetails(capture, index, ctx) {
  const draft = capture.draft;
  const when = h("div", { class: "small muted" },
    `Photo ${index + 1} · ${fmtTime(capture.captured_at)}`,
    capture.lat != null ? ` · GPS ±${Math.round(capture.accuracy_m)} m` : " · no GPS");
  if (!draft) return [h("p", { class: "muted" }, "Waiting for extraction…"), when];
  if (draft.status === "pending") {
    return [h("p", { class: "muted" }, draft.error ? `Will retry: ${draft.error}` : "Extracting…"), when];
  }

  const { values, edits } = draft.review;
  const rows = [];
  if (draft.status === "error") rows.push(notice("error", `Extraction failed: ${draft.error}`));
  const flags = draftFlags(capture, ctx.batch);
  if (flags.length && ctx.batch.status === "ready") rows.push(h("ul", { class: "flags" }, flags.map((f) => h("li", {}, f))));

  let plateChip = null;
  if (edited(edits, "plate_text", "plate_state")) plateChip = chip("edited", "edited");
  else if (draft.review.plate_checked) plateChip = chip("plate checked", "ok");
  else if (draft.status === "done") plateChip = chip(`plate ${draft.plate_confidence}`, CONFIDENCE_KIND[draft.plate_confidence]);
  rows.push(h("div", { class: "plate-row" },
    h("span", { class: `plate ${draft.review.plate_needs_check && values.plate_text ? "unverified" : ""}` }, values.plate_text ?? "no plate"),
    values.plate_state ? h("span", { class: "muted" }, values.plate_state) : null,
    plateChip));
  rows.push(localPlateRow(draft));
  if (capture.plate_crop_url) rows.push(h("img", { class: "plate-crop", src: capture.plate_crop_url, alt: "Plate close-up" }));

  const vehicle = [values.color, values.make, values.model].filter(Boolean).join(" ") || "Vehicle not identified";
  let vehicleChip = null;
  if (edited(edits, "color", "make", "model")) vehicleChip = chip("edited", "edited");
  else if (draft.status === "done") vehicleChip = chip(`vehicle ${draft.make_model_confidence}`, CONFIDENCE_KIND[draft.make_model_confidence]);
  rows.push(h("div", {}, `${vehicle} `, vehicleChip));
  rows.push(addressRow(capture, draft));
  if (capture.submission) rows.push(submissionRow(capture.submission));
  if (draft.notes) rows.push(h("div", { class: "small muted" }, draft.notes));
  rows.push(when);
  if (ctx.canReview) rows.push(reviewControls(capture));
  return rows;
}

// Reasons to look closely before deciding. Edited fields are the reviewer's own, so they aren't flagged.
function draftFlags(capture, batch) {
  const draft = capture.draft;
  const { values, edits } = draft.review;
  const flags = [];
  if (draft.review.plate_needs_check && values.plate_text) {
    let why = "the local plate reader couldn't confirm it";
    if (draft.plate_confidence && draft.plate_confidence !== "high") why = `Claude's confidence is ${draft.plate_confidence}`;
    else if (draft.plates_agree === false) why = "the two readings differ";
    flags.push(`Check the plate: ${why}`);
  }
  const twin = values.plate_text
    ? batch.captures.findIndex((c) => c.id !== capture.id && c.draft?.review.values.plate_text === values.plate_text)
    : -1;
  if (twin >= 0) flags.push(`Same plate as photo ${twin + 1}`);
  if (!edited(edits, "color", "make", "model") && draft.make_model_confidence === "low") flags.push("Check the vehicle: low confidence");
  const settledOnTheStreet = capture.address_source === "picked" || capture.address_source === "typed";
  if (!("address" in edits) && values.address && !settledOnTheStreet) {
    if (capture.accuracy_m > WEAK_GPS_M) flags.push(`GPS was only ±${Math.round(capture.accuracy_m)} m: check the address`);
    else if (draft.address_match === "StreetAddress") flags.push("Address is an estimate along the block");
  }
  const missing = missingFields(values);
  if (missing.length) flags.push(`Missing ${missing.join(", ")}`);
  return flags;
}

function localPlateRow(draft) {
  if (draft.alpr_error) return h("div", { class: "small warn-text" }, `Local plate reader failed: ${draft.alpr_error}`);
  if (!draft.alpr_text) return h("div", { class: "small muted" }, "Local plate reader: no plate found");
  const verdict = draft.plates_agree == null ? "" : draft.plates_agree ? " ✓ matches" : " ✗ differs";
  return h("div", { class: `small ${draft.plates_agree === false ? "warn-text" : "muted"}` },
    `Local plate reader: ${draft.alpr_text}${verdict}`);
}

const SUBMISSION_LABEL = {
  queued: "waiting to send",
  sending: "sending to 311…",
  prepared: "dry run — nothing sent",
  sent: "sent to 311",
  failed: "not sent",
  unknown: "unconfirmed",
};
const SUBMISSION_KIND = { sent: "ok", failed: "bad", unknown: "bad", prepared: "", queued: "warn", sending: "warn" };

function submissionRow(submission) {
  const rows = [h("div", {},
    chip(SUBMISSION_LABEL[submission.status] ?? submission.status, SUBMISSION_KIND[submission.status] ?? ""),
    submission.case_number ? h("strong", {}, ` ${submission.case_number}`) : null,
    submission.photo_attached ? h("span", { class: "small muted" }, " · photo attached") : null)];
  if (submission.description) {
    // On a dry run this is the whole point: the words an officer would read.
    rows.push(h("details", { class: "sent-detail" },
      h("summary", {}, submission.dry_run ? "What would be sent" : "What was sent"),
      h("p", { class: "small" }, submission.description),
      submission.warnings.length
        ? h("ul", { class: "flags" }, submission.warnings.map((w) => h("li", {}, w)))
        : null,
      h("button", { class: "button subtle", onclick: (e) => showPayload(e.target, submission) }, "Show the raw request")));
  }
  if (submission.status === "unknown") {
    rows.push(notice("error", `311 never confirmed this one: ${submission.error}. It may still have been`
      + " filed. Check the city's open data before sending it again."));
  } else if (submission.status === "failed") {
    rows.push(h("div", { class: "small error-text" }, submission.error));
  }
  if (submission.stale && submission.status === "sent") {
    rows.push(h("div", { class: "small warn-text" }, "This draft was edited after it was sent; 311 has the older version."));
  }
  return h("div", { class: "submission" }, rows);
}

async function showPayload(button, submission) {
  button.disabled = true;
  try {
    const full = await api("GET", `/api/batches/${encodeURIComponent(shown.batch.id)}/submissions/${submission.id}`);
    button.replaceWith(h("pre", { class: "payload" }, JSON.stringify(full.case_record, null, 1)));
  } catch (error) {
    button.disabled = false;
    showError(`Couldn't load the request: ${error.message}`);
  }
}

const ADDRESS_SOURCE = {
  geocoded: "looked up while capturing",
  picked: "chosen on the street",
  typed: "typed on the street",
};

function addressRow(capture, draft) {
  const { values, edits } = draft.review;
  if (!values.address) {
    if ("address" in edits) return h("div", { class: "small warn-text" }, "No address");
    if (draft.geocode_error) return h("div", { class: "small warn-text" }, `Address lookup failed: ${draft.geocode_error}`);
    if (capture.lat == null) return h("div", { class: "small warn-text" }, "No GPS fix, so no address");
    return h("div", { class: "small warn-text" }, "No address found near the GPS fix");
  }
  let note;
  if ("address" in edits) note = chip("edited", "edited");
  else if (capture.address_source) note = h("span", { class: "small muted" }, `(${ADDRESS_SOURCE[capture.address_source]})`);
  else {
    const kind = draft.address_match === "PointAddress" ? "building" : "along the block";
    const how = draft.address_distance_m == null ? kind : `${kind}, ${Math.round(draft.address_distance_m)} m from GPS`;
    note = h("span", { class: "small muted" }, `(${how})`);
  }
  return h("div", {}, `📍 ${values.address} `, note);
}

// ---- Review ----

function reviewControls(capture) {
  const { decision } = capture.draft.review;
  const disabled = saving != null;
  const option = (value, label) => h("button", {
    class: `seg ${decision === value ? `on ${value}` : ""}`,
    "aria-pressed": String(decision === value),
    disabled,
    onclick: () => decide(capture, decision === value ? null : value), // tapping the chosen one undoes it
  }, label);
  return h("div", { class: "review" },
    h("div", { class: "segmented", role: "group", "aria-label": "Decision" },
      option("report", "Report"), option("skip", "Don't report")),
    h("button", { class: "button subtle", disabled, onclick: () => openEditor(capture) }, "Edit"));
}

function decide(capture, decision) {
  const { values, plate_needs_check } = capture.draft.review;
  // Reporting needs every field and a trusted or checked plate: open the form to get there.
  if (decision === "report" && (plate_needs_check || missingFields(values).length)) {
    return openEditor(capture, { reportAfter: true });
  }
  return saveReview(capture, { decision });
}

function openEditor(capture, { reportAfter = false } = {}) {
  editing = { id: capture.id, values: { ...capture.draft.review.values }, plateChecked: false, reportAfter };
  drawBatch();
  document.getElementById(`draft-${capture.id}`)?.scrollIntoView({ block: "start" });
}

function closeEditor() {
  const id = editing?.id;
  editing = null;
  drawBatch();
  if (id) document.getElementById(`draft-${id}`)?.scrollIntoView({ block: "nearest" });
}

function editForm(capture) {
  const draft = capture.draft;
  const form = editing;
  const input = (field, props = {}) => h("input", {
    name: field, value: form.values[field] ?? "", autocomplete: "off",
    oninput: (event) => { form.values[field] = event.target.value; },
    ...props,
  });
  const field = (name, label, props) => h("label", { class: "field" }, label, input(name, props));
  const plateInput = input("plate_text", { class: "plate-input", autocapitalize: "characters", spellcheck: "false", maxlength: 10 });
  const readings = [["Claude", draft.plate_text], ["Local reader", draft.alpr_text]].filter(([, text]) => text);
  const offerReadings = new Set(readings.map(([, text]) => text)).size > 1;
  const missing = missingFields(form.values);

  return [
    h("h3", {}, form.reportAfter ? "Check before reporting" : "Edit draft"),
    form.reportAfter && missing.length ? notice("warn", `To report, fill in: ${missing.join(", ")}`) : null,
    capture.plate_crop_url ? h("img", { class: "plate-crop", src: capture.plate_crop_url, alt: "Plate close-up" }) : null,
    h("label", { class: "field" }, "Plate", plateInput),
    offerReadings ? h("div", { class: "readings" }, "Use:", readings.map(([who, text]) => h("button", {
      type: "button", class: "reading", "aria-label": `Use ${who} reading ${text}`,
      onclick: () => { plateInput.value = text; form.values.plate_text = text; },
    }, `${text} (${who})`))) : null,
    draft.review.plate_needs_check ? h("label", { class: "check" },
      h("input", { type: "checkbox", checked: form.plateChecked, onchange: (event) => { form.plateChecked = event.target.checked; } }),
      "The plate above matches the photo") : null,
    h("div", { class: "field-row" },
      field("plate_state", "State", { maxlength: 2, autocapitalize: "characters", spellcheck: "false" }),
      field("color", "Color")),
    h("div", { class: "field-row" }, field("make", "Make"), field("model", "Model")),
    field("address", "Address"),
    h("div", { class: "form-actions" },
      h("button", { class: "button primary", disabled: saving != null, onclick: () => saveEdits(capture) },
        saving === capture.id ? "Saving…" : form.reportAfter ? "Save & report" : "Save"),
      h("button", { class: "button subtle", disabled: saving != null, onclick: closeEditor }, "Cancel")),
  ];
}

async function saveEdits(capture) {
  const form = editing;
  const current = capture.draft.review.values;
  const changes = {};
  for (const [field] of FIELDS) {
    const value = (form.values[field] ?? "").trim();
    if (value !== (current[field] ?? "")) changes[field] = value || null;
  }
  if (form.plateChecked) changes.plate_checked = true;
  if (form.reportAfter) {
    const plateTyped = "plate_text" in changes;
    if (capture.draft.review.plate_needs_check && !plateTyped && !form.plateChecked) {
      return showError("Compare the plate with the photo: tick the box if it's right, or correct it.");
    }
    changes.decision = "report";
  }
  if (Object.keys(changes).length === 0) return closeEditor();
  if (await saveReview(capture, changes)) closeEditor();
}

async function saveReview(capture, changes) {
  const { batch } = shown;
  saving = capture.id;
  drawBatch();
  try {
    const updated = await api(
      "PATCH",
      `/api/batches/${encodeURIComponent(batch.id)}/captures/${encodeURIComponent(capture.id)}/draft`,
      { json: { version: capture.draft.review.version, ...changes } },
    );
    batch.captures = batch.captures.map((c) => (c.id === updated.id ? updated : c));
    return true;
  } catch (error) {
    if (error.status === 409) {
      editing = null;
      showError(`${error.message}. Showing the latest version.`);
      renderBatch(batch.id);
    } else {
      showError(`Couldn't save: ${error.message}`);
    }
    return false;
  } finally {
    saving = null;
    drawBatch();
  }
}

async function retryExtraction(id, rerunAll = false) {
  try {
    await api("POST", `/api/batches/${encodeURIComponent(id)}/retry${rerunAll ? "?rerun_all=true" : ""}`);
  } catch (error) {
    return showError(`Couldn't retry: ${error.message}`);
  }
  window.scrollTo(0, 0);
  renderBatch(id);
}

function rerunBatch(batch) {
  const estimate = batch.cost_usd ? ` (about $${batch.cost_usd.toFixed(2)} again)` : "";
  const reviewed = batch.review.report + batch.review.skip;
  const note = reviewed ? " Your edits are kept, but Report / Don't report choices are cleared." : "";
  if (confirm(`Re-run extraction for all ${plural(batch.capture_count, "photo")}? This replaces the current results and makes new API calls${estimate}.${note}`)) {
    retryExtraction(batch.id, true);
  }
}

// ---- Start ----

window.addEventListener("hashchange", route);
window.addEventListener("online", () => startUploads());
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (watchId != null) startGps(); // iOS can stop the watch while the app is in the background
  startUploads();
});

(async () => {
  const session = await store.getSession();
  if (session) {
    await store.recoverInterrupted(session.batchId);
    startUploads(onUploadChange);
  }
  route();
})();

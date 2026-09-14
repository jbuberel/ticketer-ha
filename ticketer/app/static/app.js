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
        h("span", {}, `${plural(b.capture_count, "photo")} `, chip(b.status, b.status))))
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

async function updateCapture(session) {
  if (view.dataset.view !== "capture") return;
  const captures = (await store.capturesFor(session.batchId))
    .sort((a, b) => b.capturedAt.localeCompare(a.capturedAt));
  const uploaded = captures.filter((c) => c.state === "uploaded").length;

  document.getElementById("snap-label").classList.toggle("disabled", busy != null);
  document.getElementById("count").textContent = captures.length
    ? `${plural(captures.length, "photo")} · ${uploaded} uploaded`
    : "No photos yet. Snap each vehicle as you walk.";
  document.getElementById("shots").replaceChildren(...captures.map((c) => shotItem(session, c)));
  document.getElementById("actions").replaceChildren(
    busy ? h("p", { class: "muted" }, busy) : "",
    h("button", { class: "button primary", disabled: busy != null, onclick: () => stopAndProcess(session) }, "Stop Capture & Process"),
    h("button", { class: "button subtle", disabled: busy != null, onclick: () => discardSession(session) }, "Discard session"),
  );
}

const STATE_LABEL = {
  locating: "getting location",
  queued: "waiting to upload",
  uploading: "uploading",
  uploaded: "uploaded",
  failed: "upload failed",
};

function shotItem(session, capture) {
  let where = "no location";
  if (capture.state === "locating") where = "getting location…";
  else if (capture.fix) {
    const age = Math.round((Date.parse(capture.capturedAt) - capture.fix.at) / 1000);
    where = `±${Math.round(capture.fix.accuracy)} m${age > 15 ? ` · fix ${age} s old` : ""}`;
  }
  const weak = !capture.fix || capture.fix.accuracy > 25;
  return h("li", { class: "shot" },
    capture.thumb ? h("img", { src: thumbUrl(capture), alt: "" }) : h("div", { class: "thumb" }),
    h("div", { class: "meta" },
      h("div", {}, fmtTime(capture.capturedAt), " ", chip(STATE_LABEL[capture.state], capture.state)),
      h("div", { class: `small ${weak && capture.state !== "locating" ? "warn-text" : "muted"}` }, where),
      capture.error ? h("div", { class: "small error-text" }, capture.error) : null),
    h("button", {
      class: "icon",
      "aria-label": "Remove photo",
      disabled: busy != null || capture.state === "uploading" || capture.state === "locating",
      onclick: () => removeShot(session, capture),
    }, "✕"));
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
      state: haveFix ? "queued" : "locating",
      error: null,
      thumb,
    };
    await store.addCapture(capture, { data, type: file.type || "image/jpeg" });
    await updateCapture(session);
    if (!haveFix) {
      const located = await freshFix();
      await store.updateCapture(capture.id, { fix: located, state: "queued" });
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

async function renderBatch(id) {
  stopGps();
  view.dataset.view = "batch";
  const body = h("div", {}, h("p", { class: "muted" }, "Loading…"));
  view.replaceChildren(h("a", { class: "back", href: "#/" }, "‹ Back"), body);
  try {
    const batch = await api("GET", `/api/batches/${encodeURIComponent(id)}`);
    const status = {
      queued: notice("info", "Queued for processing. Plate and vehicle extraction isn't built yet."),
      capturing: notice("info", "This session is still open on the phone that started it."),
    }[batch.status];
    body.replaceChildren(
      h("header", {},
        h("h1", {}, fmtDateTime(batch.created_at)),
        h("p", { class: "muted" }, `${plural(batch.capture_count, "photo")} · ${batch.created_by_name || batch.created_by}`)),
      status ?? null,
      h("ul", { class: "photos" }, batch.captures.map((c) => h("li", {},
        h("img", { src: c.photo_url, loading: "lazy", alt: `Photo taken at ${fmtTime(c.captured_at)}` }),
        h("div", { class: "small" },
          `${fmtTime(c.captured_at)} · `,
          c.lat != null ? `${c.lat.toFixed(5)}, ${c.lon.toFixed(5)} ±${Math.round(c.accuracy_m)} m` : "no location")))),
    );
  } catch (error) {
    body.replaceChildren(
      notice("error", `Couldn't load this batch: ${error.message}`),
      h("button", { class: "button subtle", onclick: () => renderBatch(id) }, "Try again"),
    );
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

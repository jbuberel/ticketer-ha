// Background uploader for the active session: one photo at a time, oldest first,
// retrying network and server errors every few seconds.

import { api } from "./api.js";
import * as store from "./store.js";

const RETRY_MS = 5000;
const UPLOAD_TIMEOUT_MS = 120000; // a few MB over a weak cellular signal

let running = null;
let again = false;
let retryTimer = null;
let listener = () => {};

const isRetryable = (error) => !error.status || error.status >= 500 || error.status === 408 || error.status === 429;

// Start (or re-run) an upload pass. Resolves when the pass ends.
export function startUploads(onChange) {
  if (onChange) listener = onChange;
  if (running) {
    again = true;
    return running;
  }
  clearTimeout(retryTimer);
  running = (async () => {
    let needsRetry = false;
    try {
      do {
        again = false;
        needsRetry = await uploadPass();
      } while (again);
    } catch (error) {
      needsRetry = isRetryable(error); // e.g. offline while creating the batch
    } finally {
      running = null;
    }
    if (needsRetry) retryTimer = setTimeout(() => startUploads(), RETRY_MS);
  })();
  return running;
}

export function stopUploads() {
  clearTimeout(retryTimer);
}

// Wait until no pass is running, then report what is still not on the server.
export async function drainUploads() {
  await startUploads();
  while (running) await running;
  const session = await store.getSession();
  const captures = session ? await store.capturesFor(session.batchId) : [];
  return captures.filter((c) => c.state !== "uploaded");
}

// Returns true when something failed in a way worth retrying.
async function uploadPass() {
  const session = await store.getSession();
  if (!session) return false;

  if (!session.batchCreated) {
    await api("POST", "/api/batches", { json: { id: session.batchId } });
    await store.updateSession({ batchCreated: true });
  }

  const pending = (await store.capturesFor(session.batchId))
    .filter((c) => c.state === "queued" || (c.state === "failed" && c.retryable))
    .sort((a, b) => a.capturedAt.localeCompare(b.capturedAt));

  let needsRetry = false;
  for (const capture of pending) {
    const photo = await store.getPhoto(capture.id);
    if (!photo) continue;
    await store.updateCapture(capture.id, { state: "uploading", error: null });
    listener();
    try {
      await api("PUT", `/api/batches/${session.batchId}/captures/${capture.id}`, {
        form: captureForm(capture, photo),
        timeoutMs: UPLOAD_TIMEOUT_MS,
      });
      await store.updateCapture(capture.id, { state: "uploaded" });
    } catch (error) {
      const retryable = isRetryable(error);
      needsRetry ||= retryable;
      await store.updateCapture(capture.id, { state: "failed", error: error.message, retryable });
    }
    listener();
  }
  return needsRetry;
}

function captureForm(capture, photo) {
  const form = new FormData();
  form.append("photo", new Blob([photo.data], { type: photo.type }), `${capture.id}.jpg`);
  form.append("captured_at", capture.capturedAt);
  const fix = capture.fix;
  if (fix) {
    form.append("lat", fix.lat);
    form.append("lon", fix.lon);
    form.append("accuracy_m", fix.accuracy);
    if (fix.heading != null) form.append("heading", fix.heading);
    if (fix.speed != null) form.append("speed_mps", fix.speed);
    form.append("fix_at", new Date(fix.at).toISOString());
  }
  const address = capture.address;
  if (address?.address) {
    form.append("address", address.address);
    form.append("address_source", address.source);
    // Only meaningful while the address is still the one the geocoder returned.
    if (address.full) form.append("address_full", address.full);
    if (address.match) form.append("address_match", address.match);
  }
  return form;
}

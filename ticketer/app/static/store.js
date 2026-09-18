// IndexedDB storage for the active capture session, so photos survive a page reload
// (iOS can reload the page while the camera is open) and upload when the network allows.
//
// Stores:
//   kv        "session" -> { batchId, startedAt, batchCreated }
//   captures  { id, batchId, capturedAt, fix, address, candidates, state, error, retryable, thumb }
//             state: locating | geocoding | queued | uploading | uploaded | failed
//             address: { address, full, source, match } settled on the street, or null
//             candidates: nearby addresses to choose from, for the picker
//   photos    capture id -> { data: ArrayBuffer, type }  (kept apart so listing captures stays cheap)

const DB_NAME = "ticketer";
const DB_VERSION = 1;
let dbPromise = null;

function db() {
  dbPromise ??= new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const d = request.result;
      d.createObjectStore("kv");
      d.createObjectStore("captures", { keyPath: "id" }).createIndex("batchId", "batchId");
      d.createObjectStore("photos");
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  return dbPromise;
}

// Runs fn(transaction, setResult) and resolves with the result once the transaction commits.
async function tx(storeNames, mode, fn) {
  const d = await db();
  return new Promise((resolve, reject) => {
    const t = d.transaction(storeNames, mode);
    let result;
    t.oncomplete = () => resolve(result);
    t.onerror = () => reject(t.error);
    t.onabort = () => reject(t.error ?? new Error("Storage transaction aborted"));
    fn(t, (value) => { result = value; });
  });
}

export const getSession = () => tx("kv", "readonly", (t, done) => {
  const r = t.objectStore("kv").get("session");
  r.onsuccess = () => done(r.result ?? null);
});

export const setSession = (session) => tx("kv", "readwrite", (t) => {
  t.objectStore("kv").put(session, "session");
});

export const updateSession = (patch) => tx("kv", "readwrite", (t) => {
  const kv = t.objectStore("kv");
  const r = kv.get("session");
  r.onsuccess = () => { if (r.result) kv.put({ ...r.result, ...patch }, "session"); };
});

export const capturesFor = (batchId) => tx("captures", "readonly", (t, done) => {
  const r = t.objectStore("captures").index("batchId").getAll(batchId);
  r.onsuccess = () => done(r.result);
});

export const getPhoto = (id) => tx("photos", "readonly", (t, done) => {
  const r = t.objectStore("photos").get(id);
  r.onsuccess = () => done(r.result ?? null);
});

export const addCapture = (capture, photo) => tx(["captures", "photos"], "readwrite", (t) => {
  t.objectStore("photos").put(photo, capture.id);
  t.objectStore("captures").put(capture);
});

// Merge a patch into a capture; a capture removed in the meantime stays removed.
export const updateCapture = (id, patch) => tx("captures", "readwrite", (t) => {
  const captures = t.objectStore("captures");
  const r = captures.get(id);
  r.onsuccess = () => { if (r.result) captures.put({ ...r.result, ...patch }); };
});

export const removeCapture = (id) => tx(["captures", "photos"], "readwrite", (t) => {
  t.objectStore("captures").delete(id);
  t.objectStore("photos").delete(id);
});

// After a reload, nothing is mid-upload or waiting on a GPS or address callback any more. A photo
// caught mid-lookup uploads without an address; extraction falls back to geocoding its GPS fix.
const INTERRUPTED = ["uploading", "locating", "geocoding"];

export const recoverInterrupted = (batchId) => tx("captures", "readwrite", (t) => {
  const captures = t.objectStore("captures");
  const r = captures.index("batchId").getAll(batchId);
  r.onsuccess = () => {
    for (const c of r.result) {
      if (INTERRUPTED.includes(c.state)) captures.put({ ...c, state: "queued" });
    }
  };
});

export const clearSession = (batchId) => tx(["kv", "captures", "photos"], "readwrite", (t) => {
  const captures = t.objectStore("captures");
  const photos = t.objectStore("photos");
  const r = captures.index("batchId").getAllKeys(batchId);
  r.onsuccess = () => {
    for (const id of r.result) {
      captures.delete(id);
      photos.delete(id);
    }
  };
  t.objectStore("kv").delete("session");
});

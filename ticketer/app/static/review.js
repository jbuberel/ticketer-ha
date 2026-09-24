// Ticketer review screen on Ionic: one batch's drafts, reviewed and sent to 311. A prototype of
// the batch view in app.js -- the same API and the same rules, presented with Ionic components.
//
// lit-html draws it, so a redraw updates the page in place instead of rebuilding it. Rebuilding
// would re-create every Ionic component, and a new one stays invisible until it has booted: the
// page would blink on every refresh while extraction runs.

import { html, nothing, render } from "/vendor/lit-html-3.3.3/lit-html.js";
import { classMap } from "/vendor/lit-html-3.3.3/directives/class-map.js";
import { keyed } from "/vendor/lit-html-3.3.3/directives/keyed.js";
import { live } from "/vendor/lit-html-3.3.3/directives/live.js";
import { alertController, getMode, modalController, toastController } from "/vendor/ionic-9.0.4/index.esm.js";
import { api } from "./api.js";

const page = document.getElementById("page");
const whoami = api("GET", "/api/whoami").catch((error) => ({ error: error.message }));

const BATCH_REFRESH_MS = 3000;
const EXPIRY_WARN_MS = 3600000; // the last hour, when a draft is worth deciding now or not at all
const CONFIDENCE_KIND = { high: "ok", medium: "warn", low: "bad" };
const WEAK_GPS_M = 25;
const FIELDS = ["plate_text", "plate_state", "color", "make", "model", "address"];
const REQUIRED_TO_REPORT = [["plate_text", "plate"], ["color", "color"], ["make", "make"], ["model", "model"], ["address", "address"]];
const missingFields = (values) => REQUIRED_TO_REPORT.filter(([f]) => !values[f]).map(([, label]) => label);
const edited = (edits, ...fields) => fields.some((f) => f in edits);

let batchId = null;
let shown = null; // { batch, me } on screen
let failure = null; // why the last load failed
let timer = null;
let saving = null; // capture id with a review change in flight
let submitting = false; // a submit request is in flight
let asking = false; // a confirmation is open; a second tap mustn't open another
let editor = null; // { id, values, plateChecked, reportAfter, root, modal } while the edit sheet is open
const payloads = new Map(); // submission id -> the raw request as text, or null while it loads

// ---- Formatting ----

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const fmtTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const fmtDateTime = (iso) => new Date(iso).toLocaleString([], {
  month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
});

// How long a batch has before retention deletes it, rounded down. Null once it is due.
function fmtLeft(iso) {
  const minutes = iso ? Math.floor((new Date(iso) - Date.now()) / 60000) : 0;
  if (minutes <= 0) return null;
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours} h` : `${Math.floor(hours / 24)} d`;
}

const tag = (text, kind = "") => html`<span class="tag ${kind}">${text}</span>`;
const notice = (kind, ...content) => html`<div class="notice ${kind}">${content}</div>`;

const expiryTag = (iso) => {
  if (!iso) return nothing;
  const left = fmtLeft(iso);
  return tag(left ? `${left} left` : "deleting now", new Date(iso) - Date.now() < EXPIRY_WARN_MS ? "warn" : "");
};

// ---- Dialogs ----

async function showError(message) {
  const toast = await toastController.create({
    message, color: "danger", duration: 8000, position: "bottom", buttons: [{ text: "OK", role: "cancel" }],
  });
  await toast.present();
}

// Resolves true when the confirming button was pressed; Cancel, the backdrop and a second
// confirmation opened on top of the first all count as no.
async function confirmed({ header, message, confirm, destructive = false, cssClass }) {
  if (asking) return false;
  asking = true;
  try {
    const alert = await alertController.create({
      header, message, cssClass,
      buttons: [{ text: "Cancel", role: "cancel" }, { text: confirm, role: destructive ? "destructive" : "confirm" }],
    });
    await alert.present();
    const { role } = await alert.onDidDismiss();
    return role === "confirm" || role === "destructive";
  } finally {
    asking = false;
  }
}

// ---- Loading ----

function route() {
  const match = location.hash.match(/^#\/batch\/(.+)$/);
  batchId = match ? decodeURIComponent(match[1]) : null;
  clearTimeout(timer);
  shown = null;
  failure = null;
  editor?.modal?.dismiss();
  draw();
  page.querySelector("ion-content")?.scrollToTop(0); // the same page, drawn for another batch
  if (batchId) load();
}

// Re-loads every few seconds while extraction or a submission is running.
async function load() {
  clearTimeout(timer);
  const id = batchId;
  try {
    const [batch, me] = await Promise.all([api("GET", `/api/batches/${encodeURIComponent(id)}`), whoami]);
    if (id !== batchId) return; // navigated away
    shown = { batch, me };
    failure = null;
    draw();
    const working = batch.captures.some((c) => ["queued", "sending"].includes(c.submission?.status));
    if (batch.status === "queued" || batch.status === "processing" || working) {
      timer = setTimeout(() => { if (id === batchId) load(); }, BATCH_REFRESH_MS);
    }
  } catch (error) {
    if (id !== batchId) return;
    failure = error.message;
    draw();
  }
}

function draw() {
  render(pageView(), page);
  if (editor) render(editorView(), editor.root);
}

// ---- Page ----

function pageView() {
  const batch = shown?.batch;
  const title = batch ? fmtDateTime(batch.created_at) : "Batch";
  document.title = batch ? `Ticketer · ${title}` : "Ticketer";
  return html`
    <ion-header translucent>
      <ion-toolbar>
        <ion-buttons slot="start">${backButton()}</ion-buttons>
        <ion-title>${title}</ion-title>
        ${batchId ? html`<ion-buttons slot="end">
          <ion-button href="/#/batch/${encodeURIComponent(batchId)}">Classic view</ion-button>
        </ion-buttons>` : nothing}
        ${progressBar(batch)}
      </ion-toolbar>
    </ion-header>
    <ion-content fullscreen>
      <ion-refresher slot="fixed" @ionRefresh=${(event) => load().then(() => event.target.complete())}>
        <ion-refresher-content></ion-refresher-content>
      </ion-refresher>
      <ion-header collapse="condense">
        <ion-toolbar><ion-title size="large">${title}</ion-title></ion-toolbar>
      </ion-header>
      ${content()}
    </ion-content>`;
}

// iOS says where Back goes; Android shows an arrow.
const backButton = () => (getMode() === "ios"
  ? html`<ion-button href="/"><ion-icon slot="start" name="chevron-back"></ion-icon>Batches</ion-button>`
  : html`<ion-button href="/" aria-label="Back to batches"><ion-icon slot="icon-only" name="arrow-back"></ion-icon></ion-button>`);

function progressBar(batch) {
  if (batch?.status === "queued") return html`<ion-progress-bar type="indeterminate"></ion-progress-bar>`;
  if (batch?.status !== "processing") return nothing;
  const done = batch.drafts.done + batch.drafts.error;
  return html`<ion-progress-bar .value=${batch.capture_count ? done / batch.capture_count : 0}></ion-progress-bar>`;
}

function content() {
  if (!batchId) {
    return html`<div class="empty">
      <p class="muted">No batch chosen. Open one from the list of batches.</p>
      <ion-button href="/">Go to batches</ion-button>
    </div>`;
  }
  const problem = failure ? notice("error", `Couldn't load this batch: ${failure}`,
    html`<div><ion-button fill="outline" size="small" @click=${load}>Try again</ion-button></div>`) : nothing;
  if (!shown) return failure ? problem : html`<div class="loading"><ion-spinner></ion-spinner></div>`;

  const { batch, me } = shown;
  const drafts = batch.drafts;
  const canReview = batch.status === "ready" && me.user_login === batch.created_by;
  const status = {
    capturing: notice("", "This session is still open on the phone that started it."),
    queued: me.extraction_enabled === false
      ? notice("error", "Extraction is off. In Home Assistant, set the Anthropic API key in the Ticketer app's Configuration tab and restart the app.")
      : notice("", "Waiting for extraction to start…"),
    processing: notice("", `Extracting… ${drafts.done + drafts.error} of ${batch.capture_count} done`),
    ready: reviewSummary(batch, canReview),
  }[batch.status] ?? nothing;
  const cost = batch.cost_usd ? ` · $${batch.cost_usd.toFixed(3)}` : "";

  return html`
    <div class="meta muted small">
      <span>${plural(batch.capture_count, "photo")} · ${batch.created_by_name || batch.created_by}${cost}</span>
      ${expiryTag(batch.expires_at)}
    </div>
    ${problem}
    ${status}
    ${expiringSoon(batch) ? notice("warn", `These photos and drafts are deleted ${
      fmtLeft(batch.expires_at) ? `in ${fmtLeft(batch.expires_at)}` : "any moment now"
    }. Send anything you mean to report before then.`) : nothing}
    ${drafts.error ? notice("error", `${plural(drafts.error, "photo")} couldn't be extracted.`,
      html`<div><ion-button fill="outline" size="small" color="danger" @click=${() => retryExtraction(batch.id)}>Retry failed</ion-button></div>`) : nothing}
    ${batch.captures.map((capture, index) => draftCard(capture, index, { batch, canReview }))}
    ${batch.status === "ready" && canReview ? submitPanel(batch, me) : nothing}
    <div class="end-actions">
      ${batch.status === "ready" && drafts.done && canReview
        ? html`<ion-button fill="clear" @click=${() => rerunBatch(batch)}>
            <ion-icon slot="start" name="refresh"></ion-icon>Re-run extraction</ion-button>`
        : nothing}
      ${batch.status !== "capturing" && me.user_login === batch.created_by
        ? html`<ion-button fill="outline" color="danger" @click=${() => deleteBatch(batch)}>
            <ion-icon slot="start" name="trash-outline"></ion-icon>Delete batch</ion-button>`
        : nothing}
    </div>`;
}

// An hour or less left, and there is still something undecided or unsent to lose.
function expiringSoon(batch) {
  if (!batch.expires_at || new Date(batch.expires_at) - Date.now() > EXPIRY_WARN_MS) return false;
  return batch.review.undecided > 0 || batch.captures.some(SENDABLE);
}

// What became of this batch's requests, in the order worth hearing it.
const SUBMISSION_SUMMARY = [
  ["sent", (n) => `${plural(n, "request")} sent to 311`],
  ["sending", (n) => `${n} sending`],
  ["queued", (n) => `${n} waiting to send`],
  ["unknown", (n) => `${n} unconfirmed`],
  ["failed", (n) => `${n} not sent`],
  ["prepared", (n) => `${plural(n, "dry run")}, nothing sent`],
];

function reviewSummary(batch, canReview) {
  const decisions = batch.captures.map((c) => c.draft?.review.decision);
  const report = decisions.filter((d) => d === "report").length;
  const skip = decisions.filter((d) => d === "skip").length;
  const undecided = decisions.length - report - skip;
  const counts = `${report} to report · ${skip} not reporting`;
  if (!canReview) {
    return notice("", `${counts} · ${undecided} undecided. Only ${batch.created_by_name || batch.created_by} can review this batch.`);
  }

  // Once anything has been sent, what happened to it is the news, not the review tally.
  const statuses = batch.captures.map((c) => c.submission?.status).filter(Boolean);
  const done = SUBMISSION_SUMMARY
    .map(([status, phrase]) => [statuses.filter((s) => s === status).length, phrase])
    .filter(([n]) => n)
    .map(([n, phrase]) => phrase(n));
  const cases = batch.captures.map((c) => c.submission?.case_number).filter(Boolean);
  // Reported drafts 311 has not been told about at all: a failed one is already counted above.
  const unsent = batch.captures.filter((c) => c.draft?.review.decision === "report" && !c.submission).length;
  const headline = [
    ...(undecided ? [`${plural(undecided, "draft")} to review`] : []),
    ...done,
    ...(done.length && unsent ? [`${unsent} still to send`] : []),
  ];
  const bad = statuses.some((s) => s === "failed" || s === "unknown");
  const settled = !undecided && !unsent && (!done.length || statuses.every((s) => s === "sent"));
  return notice(bad ? "warn" : settled ? "ok" : "",
    html`<div><strong>${headline.length ? headline.join(" · ") : "All drafts reviewed"}</strong>${done.length ? nothing : ` · ${counts}`}</div>`,
    cases.length ? html`<div class="small">${cases.length > 1 ? "Case numbers" : "Case number"}: ${cases.join(", ")}</div>` : nothing,
    undecided ? html`<div class="small">Choose Report or Don't report for each photo. Edit anything that's wrong first.</div>` : nothing);
}

// ---- Drafts ----

function draftCard(capture, index, ctx) {
  const decision = capture.draft?.review.decision;
  return html`
    <ion-card class=${classMap({ draft: true, report: decision === "report", skip: decision === "skip" })} id="draft-${capture.id}">
      <div class="photo">
        <img src=${capture.photo_url} loading="lazy" width=${capture.width} height=${capture.height}
             alt="Photo ${index + 1}, taken at ${fmtTime(capture.captured_at)}">
        ${decision ? html`<ion-badge color=${decision === "report" ? "primary" : "medium"}>${
          decision === "report" ? "Report" : "Not reporting"}</ion-badge>` : nothing}
      </div>
      ${draftDetails(capture, index, ctx)}
    </ion-card>`;
}

function draftDetails(capture, index, ctx) {
  const draft = capture.draft;
  const when = `Photo ${index + 1} · ${fmtTime(capture.captured_at)}${
    capture.lat != null ? ` · GPS ±${Math.round(capture.accuracy_m)} m` : " · no GPS"}`;
  if (!draft || draft.status === "pending") {
    const waiting = !draft ? "Waiting for extraction…" : draft.error ? `Will retry: ${draft.error}` : "Extracting…";
    return html`
      <ion-card-header><ion-card-subtitle>${when}</ion-card-subtitle></ion-card-header>
      <ion-card-content class="muted">${waiting}</ion-card-content>`;
  }

  const { values, edits } = draft.review;
  const flags = ctx.batch.status === "ready" ? draftFlags(capture, ctx.batch) : [];
  let plateTag = nothing;
  if (edited(edits, "plate_text", "plate_state")) plateTag = tag("edited", "edited");
  else if (draft.review.plate_checked) plateTag = tag("plate checked", "ok");
  else if (draft.status === "done") plateTag = tag(`plate ${draft.plate_confidence}`, CONFIDENCE_KIND[draft.plate_confidence]);
  let vehicleTag = nothing;
  if (edited(edits, "color", "make", "model")) vehicleTag = tag("edited", "edited");
  else if (draft.status === "done") vehicleTag = tag(`vehicle ${draft.make_model_confidence}`, CONFIDENCE_KIND[draft.make_model_confidence]);
  const vehicle = [values.color, values.make, values.model].filter(Boolean).join(" ") || "Vehicle not identified";

  return html`
    <ion-card-header>
      <ion-card-subtitle>${when}</ion-card-subtitle>
      <div class="plate-row">
        <div class="plate-line">
          <span class="plate ${draft.review.plate_needs_check && values.plate_text ? "unverified" : ""}">${values.plate_text ?? "no plate"}</span>
          ${values.plate_state ? html`<span class="plate-state">${values.plate_state}</span>` : nothing}
          ${plateTag}
        </div>
        ${ctx.canReview ? html`<ion-button class="edit" fill="clear" size="small" ?disabled=${saving != null}
          @click=${() => openEditor(capture)}><ion-icon slot="start" name="create-outline"></ion-icon>Edit</ion-button>` : nothing}
      </div>
    </ion-card-header>
    ${draft.status === "error" ? notice("error", `Extraction failed: ${draft.error}`) : nothing}
    ${flags.length ? html`<div class="flags">${flags.map((f) => html`
      <div class="flag"><ion-icon name="alert-circle" aria-hidden="true"></ion-icon><span>${f}</span></div>`)}</div>` : nothing}
    ${capture.plate_crop_url ? html`<img class="plate-crop" src=${capture.plate_crop_url} alt="Plate close-up">` : nothing}
    <ion-list lines="none" class="facts">
      ${fact("car-outline", html`${vehicle} ${vehicleTag}`)}
      ${addressFact(capture, draft)}
      ${localPlateFact(draft)}
      ${draft.notes ? fact("chatbox-ellipses-outline", html`<p>${draft.notes}</p>`) : nothing}
    </ion-list>
    ${capture.submission ? submissionBlock(capture.submission) : nothing}
    ${ctx.canReview ? reviewControls(capture) : nothing}`;
}

const fact = (icon, label) => html`
  <ion-item>
    <ion-icon slot="start" name=${icon} aria-hidden="true"></ion-icon>
    <ion-label class="ion-text-wrap">${label}</ion-label>
  </ion-item>`;

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

function localPlateFact(draft) {
  let text = "Local plate reader: no plate found";
  let kind = "";
  if (draft.alpr_error) [text, kind] = [`Local plate reader failed: ${draft.alpr_error}`, "warn-text"];
  else if (draft.alpr_text) {
    const verdict = draft.plates_agree == null ? "" : draft.plates_agree ? " ✓ matches" : " ✗ differs";
    text = `Local plate reader: ${draft.alpr_text}${verdict}`;
    if (draft.plates_agree === false) kind = "warn-text";
  }
  return fact("scan-outline", html`<p class=${kind}>${text}</p>`);
}

const ADDRESS_SOURCE = {
  geocoded: "looked up while capturing",
  picked: "chosen on the street",
  typed: "typed on the street",
};

function addressFact(capture, draft) {
  const { values, edits } = draft.review;
  if (!values.address) {
    let why = "No address found near the GPS fix";
    if ("address" in edits) why = "No address";
    else if (draft.geocode_error) why = `Address lookup failed: ${draft.geocode_error}`;
    else if (capture.lat == null) why = "No GPS fix, so no address";
    return fact("location-outline", html`<span class="warn-text">${why}</span>`);
  }
  let note;
  if ("address" in edits) note = tag("edited", "edited");
  else if (capture.address_source) note = html`<p>${ADDRESS_SOURCE[capture.address_source]}</p>`;
  else {
    const kind = draft.address_match === "PointAddress" ? "building" : "along the block";
    note = html`<p>${draft.address_distance_m == null ? kind : `${kind}, ${Math.round(draft.address_distance_m)} m from GPS`}</p>`;
  }
  return fact("location-outline", html`${values.address} ${note}`);
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

function submissionBlock(submission) {
  return html`
    <div class="submission">
      <div class="submission-status">
        ${tag(SUBMISSION_LABEL[submission.status] ?? submission.status, SUBMISSION_KIND[submission.status] ?? "")}
        ${submission.case_number ? html`<strong>${submission.case_number}</strong>` : nothing}
        ${submission.photo_attached ? html`<span class="small muted">photo attached</span>` : nothing}
      </div>
      ${submission.description ? sentDetail(submission) : nothing}
      ${submission.status === "unknown"
        ? notice("error", `311 never confirmed this one: ${submission.error}. It may still have been`
          + " filed. Check the city's open data before sending it again.")
        : submission.status === "failed" ? html`<div class="small error-text">${submission.error}</div>` : nothing}
      ${submission.stale && submission.status === "sent"
        ? html`<div class="small warn-text">This draft was edited after it was sent; 311 has the older version.</div>`
        : nothing}
    </div>`;
}

// On a dry run this is the whole point: the words an officer would read.
function sentDetail(submission) {
  const payload = payloads.get(submission.id);
  return html`
    <ion-accordion-group>
      <ion-accordion value="sent">
        <ion-item slot="header" lines="none">
          <ion-label>${submission.dry_run ? "What would be sent" : "What was sent"}</ion-label>
        </ion-item>
        <div slot="content" class="sent-detail">
          <div>${submission.description}</div>
          ${submission.warnings.length
            ? html`<div class="flags">${submission.warnings.map((w) => html`<div>${w}</div>`)}</div>`
            : nothing}
          ${payload != null
            ? html`<pre class="payload">${payload}</pre>`
            : html`<ion-button fill="outline" size="small" ?disabled=${payloads.has(submission.id)}
                @click=${() => showPayload(submission)}>Show the raw request</ion-button>`}
        </div>
      </ion-accordion>
    </ion-accordion-group>`;
}

async function showPayload(submission) {
  payloads.set(submission.id, null);
  draw();
  try {
    const full = await api("GET", `/api/batches/${encodeURIComponent(shown.batch.id)}/submissions/${submission.id}`);
    payloads.set(submission.id, JSON.stringify(full.case_record, null, 1));
  } catch (error) {
    payloads.delete(submission.id);
    showError(`Couldn't load the request: ${error.message}`);
  }
  draw();
}

// ---- Review ----

// Keyed by the decision, so the buttons are made afresh when it changes: an Ionic button copies
// its aria attributes inward once, when it boots, and would go on announcing the old state.
function reviewControls(capture) {
  const { decision } = capture.draft.review;
  const disabled = saving != null;
  const option = (value, label, color) => html`
    <ion-button fill=${decision === value ? "solid" : "outline"} color=${color} ?disabled=${disabled}
      aria-pressed=${String(decision === value)}
      @click=${() => decide(capture, decision === value ? null : value)}>
      ${decision === value ? html`<ion-icon slot="start" name="checkmark"></ion-icon>` : nothing}${label}
    </ion-button>`;
  return keyed(decision ?? "undecided", html`
    <div class="review" role="group" aria-label="Decision">
      ${option("report", "Report", "primary")}
      ${option("skip", "Don't report", "medium")}
    </div>`);
}

function decide(capture, decision) {
  if (saving) return;
  const { values, plate_needs_check } = capture.draft.review;
  // Reporting needs every field and a trusted or checked plate: open the form to get there.
  if (decision === "report" && (plate_needs_check || missingFields(values).length)) {
    return openEditor(capture, { reportAfter: true });
  }
  return saveReview(capture, { decision });
}

async function saveReview(capture, changes) {
  const { batch } = shown;
  saving = capture.id;
  draw();
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
      editor?.modal?.dismiss();
      showError(`${error.message}. Showing the latest version.`);
      load();
    } else {
      showError(`Couldn't save: ${error.message}`);
    }
    return false;
  } finally {
    saving = null;
    draw();
  }
}

// ---- Edit sheet ----

async function openEditor(capture, { reportAfter = false } = {}) {
  if (editor || saving) return;
  const root = document.createElement("div");
  editor = { id: capture.id, values: { ...capture.draft.review.values }, plateChecked: false, reportAfter, root, modal: null };
  render(editorView(), root);
  // The sheet is presented over the page, so on iOS the page shrinks back behind it like a card.
  const modal = await modalController.create({ component: root, presentingElement: page });
  editor.modal = modal;
  modal.onDidDismiss().then(() => {
    if (editor?.modal === modal) editor = null;
    draw();
  });
  await modal.present();
}

const editingCapture = () => shown?.batch.captures.find((c) => c.id === editor?.id);

function editorView() {
  const capture = editingCapture();
  if (!capture?.draft) return nothing;
  const draft = capture.draft;
  const form = editor;
  const readings = [["Claude", draft.plate_text], ["local reader", draft.alpr_text]].filter(([, text]) => text);
  const offerReadings = new Set(readings.map(([, text]) => text)).size > 1;
  const missing = missingFields(form.values);
  const busy = saving != null;
  const saveLabel = saving === capture.id ? "Saving…" : form.reportAfter ? "Save & report" : "Save";
  const set = (field) => (event) => {
    form.values[field] = event.target.value ?? "";
    if (form.reportAfter) render(editorView(), form.root); // keep "To report, fill in" current
  };
  const field = (name, label, { maxlength, caps = "words", plate = false } = {}) => html`
    <ion-item>
      <ion-input label=${label} label-placement="stacked" class=${classMap({ "plate-input": plate })}
        .value=${live(form.values[name] ?? "")}
        .maxlength=${maxlength} .autocapitalize=${caps} autocomplete="off" @ionInput=${set(name)}></ion-input>
    </ion-item>`;

  return html`
    <ion-header>
      <ion-toolbar>
        <ion-buttons slot="start">
          <ion-button ?disabled=${busy} @click=${() => form.modal?.dismiss()}>Cancel</ion-button>
        </ion-buttons>
        <ion-title>${form.reportAfter ? "Check & report" : "Edit draft"}</ion-title>
        <ion-buttons slot="end">
          <ion-button strong ?disabled=${busy} @click=${saveEdits}>${form.reportAfter ? "Report" : "Save"}</ion-button>
        </ion-buttons>
      </ion-toolbar>
    </ion-header>
    <ion-content>
      <img class="edit-photo" src=${capture.photo_url} alt="The photo being edited">
      ${capture.plate_crop_url ? html`<img class="edit-crop" src=${capture.plate_crop_url} alt="Plate close-up">` : nothing}
      ${form.reportAfter && missing.length ? notice("warn", `To report, fill in: ${missing.join(", ")}`) : nothing}
      <ion-list inset>
        ${field("plate_text", "Plate", { maxlength: 10, caps: "characters", plate: true })}
        ${draft.review.plate_needs_check ? html`
          <ion-item>
            <ion-checkbox label-placement="end" justify="start" .checked=${live(form.plateChecked)}
              @ionChange=${(event) => { form.plateChecked = event.detail.checked; }}>
              The plate above matches the photo
            </ion-checkbox>
          </ion-item>` : nothing}
        ${field("plate_state", "State", { maxlength: 2, caps: "characters" })}
      </ion-list>
      ${offerReadings ? html`<div class="readings muted">Use:
        ${readings.map(([who, text]) => html`<ion-chip aria-label="Use ${who} reading ${text}"
          @click=${() => { form.values.plate_text = text; render(editorView(), form.root); }}>
          <span class="mono">${text}</span>&nbsp;<span class="muted">${who}</span></ion-chip>`)}
      </div>` : nothing}
      <ion-list inset>
        ${field("color", "Color")}
        ${field("make", "Make")}
        ${field("model", "Model")}
        ${field("address", "Address")}
      </ion-list>
      <div class="edit-actions">
        <ion-button expand="block" ?disabled=${busy} @click=${saveEdits}>${saveLabel}</ion-button>
      </div>
    </ion-content>`;
}

async function saveEdits() {
  const form = editor;
  const capture = editingCapture();
  if (!form || !capture || saving) return;
  const current = capture.draft.review.values;
  const changes = {};
  for (const field of FIELDS) {
    const value = String(form.values[field] ?? "").trim();
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
  if (Object.keys(changes).length === 0) return form.modal?.dismiss();
  if (await saveReview(capture, changes)) form.modal?.dismiss();
}

// ---- Sending ----

// Drafts marked Report that 311 hasn't been told about yet. A draft whose submission came back
// `unknown` is deliberately not here: the owner has to check the city's open data first.
const SENDABLE = (capture) => capture.draft?.review.decision === "report"
  && !["queued", "sending", "sent", "unknown"].includes(capture.submission?.status ?? "");

// Below the photos, not above them: you reach this having just decided the last draft.
function submitPanel(batch, me) {
  const sendable = batch.captures.filter(SENDABLE);
  const sent = batch.captures.filter((c) => c.submission?.status === "sent");
  const dryRun = me.submit_dry_run !== false;
  if (!sendable.length) {
    return sent.length ? notice("ok", `${plural(sent.length, "request")} sent to 311.`) : nothing;
  }
  return html`
    <ion-card class="send">
      <ion-card-content>
        <div><strong>${plural(sendable.length, "request")} ready to send</strong>${sent.length ? ` · ${sent.length} already sent` : ""}</div>
        <div class="small">${dryRun
          ? html`Dry run is on: this builds the exact request and shows it to you without sending anything to 311.
              <strong>To file for real</strong>: in Home Assistant, Settings → Add-ons → Ticketer → Configuration,
              turn off <code>submit_dry_run</code>, save, then restart the app.`
          : `These go to Sacramento 311 as ${me.reporter === "anonymous" ? "an anonymous report" : me.reporter}, with the photo attached. A parking officer is dispatched.`}</div>
        <ion-button expand="block" fill=${dryRun ? "outline" : "solid"} ?disabled=${submitting}
          @click=${() => submitDrafts(batch, sendable, dryRun)}>
          <ion-icon slot="start" name="paper-plane"></ion-icon>
          ${submitting ? "Sending…" : dryRun ? `Dry run ${plural(sendable.length, "request")}` : `Send ${plural(sendable.length, "request")} to 311`}
        </ion-button>
      </ion-card-content>
    </ion-card>`;
}

async function submitDrafts(batch, sendable, dryRun) {
  if (submitting) return;
  const what = sendable.map((c, i) => `${i + 1}. ${c.draft.review.values.plate_text} — ${c.draft.review.values.address}`).join("\n");
  const go = await confirmed(dryRun
    ? { header: `Build ${plural(sendable.length, "request")}?`, confirm: "Build",
        message: `${what}\n\nNothing is sent to 311.`, cssClass: "send-confirm" }
    : { header: `Send ${plural(sendable.length, "request")} to Sacramento 311?`, confirm: "Send", destructive: true,
        message: `${what}\n\nThis files real requests and dispatches a parking officer. It can't be undone.`,
        cssClass: "send-confirm" });
  if (!go) return;
  submitting = true;
  draw();
  try {
    await api("POST", `/api/batches/${encodeURIComponent(batch.id)}/submit`, {
      json: { dry_run: dryRun, drafts: sendable.map((c) => ({ capture_id: c.id, version: c.draft.review.version })) },
    });
  } catch (error) {
    showError(`Couldn't submit: ${error.message}`);
  } finally {
    submitting = false;
  }
  load();
}

// ---- Batch actions ----

async function deleteBatch(batch) {
  const decided = batch.review.report + batch.review.skip;
  const reviewed = decided ? ` and your review of ${plural(decided, "draft")}` : "";
  if (!await confirmed({
    header: "Delete this batch?", confirm: "Delete", destructive: true,
    message: `Its ${plural(batch.capture_count, "photo")}, plate close-ups and drafts${reviewed} are removed from the server. This can't be undone.`,
  })) return;
  try {
    await api("DELETE", `/api/batches/${encodeURIComponent(batch.id)}`);
  } catch (error) {
    return showError(`Couldn't delete the batch: ${error.message}`);
  }
  clearTimeout(timer);
  location.href = "/";
}

async function retryExtraction(id, rerunAll = false) {
  try {
    await api("POST", `/api/batches/${encodeURIComponent(id)}/retry${rerunAll ? "?rerun_all=true" : ""}`);
  } catch (error) {
    return showError(`Couldn't retry: ${error.message}`);
  }
  page.querySelector("ion-content")?.scrollToTop(300);
  load();
}

async function rerunBatch(batch) {
  const estimate = batch.cost_usd ? ` (about $${batch.cost_usd.toFixed(2)} again)` : "";
  const reviewed = batch.review.report + batch.review.skip;
  const note = reviewed ? " Your edits are kept, but Report / Don't report choices are cleared." : "";
  if (await confirmed({
    header: "Re-run extraction?", confirm: "Re-run",
    message: `All ${plural(batch.capture_count, "photo")} are extracted again, replacing the current results, with new API calls${estimate}.${note}`,
  })) retryExtraction(batch.id, true);
}

// ---- Start ----

window.addEventListener("hashchange", route);
route();

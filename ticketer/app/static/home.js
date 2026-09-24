// The home screen: recent batches, the cases filed from deleted ones, and Begin capture.

import { html, nothing, render } from "/vendor/lit-html-3.3.3/lit-html.js";
import { api, whoami } from "./api.js";
import { onUploadChange } from "./capture.js";
import { go } from "./nav.js";
import * as store from "./store.js";
import { expiryTag, fmtDateTime, notice, plural, tag } from "./ui.js";
import { startUploads } from "./uploads.js";

const STATUS_KIND = { capturing: "warn", queued: "info", processing: "warn", ready: "ok" };

function batchTag(batch) {
  if (batch.status !== "ready") return tag(batch.status, STATUS_KIND[batch.status]);
  return batch.review.undecided ? tag(`${batch.review.undecided} to review`, "warn") : tag("reviewed", "ok");
}

async function beginCapture() {
  navigator.storage?.persist?.().catch(() => {});
  await store.setSession({ batchId: crypto.randomUUID(), startedAt: new Date().toISOString(), batchCreated: false });
  startUploads(onUploadChange); // creates the batch on the server in the background
  go("/capture");
}

class HomePage extends HTMLElement {
  connectedCallback() {
    this.me = null;
    this.batches = null; // null while loading
    this.batchesError = null;
    this.cases = [];
    this.draw();
    this.load();
  }

  async load() {
    const [me, batches, cases] = await Promise.all([
      whoami(),
      api("GET", "/api/batches").then(({ batches }) => ({ batches }), (error) => ({ error: error.message })),
      // The ledger is a footnote on this screen; a failure here shouldn't bury the batch list.
      api("GET", "/api/cases").then(({ cases }) => cases, () => []),
    ]);
    this.me = me;
    this.batches = batches.batches ?? this.batches;
    this.batchesError = batches.error ?? null;
    this.cases = cases;
    this.draw();
  }

  draw() {
    render(this.view(), this);
  }

  view() {
    const { me } = this;
    return html`
      <ion-header translucent>
        <ion-toolbar><ion-title>Ticketer</ion-title></ion-toolbar>
      </ion-header>
      <ion-content fullscreen>
        <ion-refresher slot="fixed" @ionRefresh=${(event) => this.load().then(() => event.target.complete())}>
          <ion-refresher-content></ion-refresher-content>
        </ion-refresher>
        <ion-header collapse="condense">
          <ion-toolbar><ion-title size="large">Ticketer</ion-title></ion-toolbar>
        </ion-header>
        ${me?.error
          ? notice("error", `Not signed in: ${me.error}`)
          : html`<div class="meta muted small">${me ? `${me.user_name || me.user_login} · v${me.version}` : ""}</div>`}
        <h2 class="section-title">Recent batches</h2>
        ${this.batchList()}
        ${this.caseList()}
      </ion-content>
      <ion-footer>
        <ion-toolbar>
          <ion-button class="footer-action" expand="block" @click=${beginCapture}>
            <ion-icon slot="start" name="camera"></ion-icon>Begin capture
          </ion-button>
        </ion-toolbar>
      </ion-footer>`;
  }

  batchList() {
    if (this.batchesError && !this.batches) return notice("error", `Couldn't load batches: ${this.batchesError}`);
    if (!this.batches) return html`<div class="loading"><ion-spinner></ion-spinner></div>`;
    return html`
      ${this.batchesError ? notice("error", `Couldn't refresh: ${this.batchesError}`) : nothing}
      <ion-list inset>
        ${this.batches.length ? this.batches.map((batch) => html`
          <ion-item button detail @click=${() => go(`/batch/${encodeURIComponent(batch.id)}`)}>
            <ion-label>
              <h2>${fmtDateTime(batch.created_at)}</h2>
              <p>${plural(batch.capture_count, "photo")} · ${batch.created_by_name || batch.created_by}</p>
            </ion-label>
            <div slot="end" class="item-tags">${batchTag(batch)}${expiryTag(batch.expires_at)}</div>
          </ion-item>`)
        : html`<ion-item><ion-label class="muted">No batches yet.</ion-label></ion-item>`}
      </ion-list>`;
  }

  // Requests that were really filed and whose batch retention has since deleted. Only the case
  // number is left, which is what the city's public status page and open data are looked up by.
  caseList() {
    if (!this.cases.length) return nothing;
    const hours = this.me?.retention;
    return html`
      <h2 class="section-title">Filed cases</h2>
      <ion-list inset>
        ${this.cases.map((c) => html`
          <ion-item>
            <ion-label>
              <h2 class="mono">${c.case_number || "no case number"}</h2>
              <p>${fmtDateTime(c.filed_at)}</p>
            </ion-label>
            <div slot="end">${c.status === "unknown" ? tag("unconfirmed", "warn") : tag("filed", "ok")}</div>
          </ion-item>`)}
      </ion-list>
      <p class="list-note muted small">${hours
        ? `Batches are deleted ${hours.unsubmitted_hours} h after the last photo, or ${hours.submitted_hours} h after being filed. These case numbers are all that is kept.`
        : "These case numbers are all that is kept of deleted batches."}</p>`;
  }
}

customElements.define("page-home", HomePage);

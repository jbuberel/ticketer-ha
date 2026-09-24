// What the screens share: formatting, status tags and notices, and the dialogs.

import { html, nothing } from "/vendor/lit-html-3.3.3/lit-html.js";
import { alertController, toastController } from "/vendor/ionic-9.0.4/index.esm.js";

export const EXPIRY_WARN_MS = 3600000; // the last hour, when a draft is worth deciding now or not at all

export const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
export const fmtTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
export const fmtDateTime = (iso) => new Date(iso).toLocaleString([], {
  month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
});

// How long a batch has before retention deletes it, photos and all. Rounded down, so "2 h" never
// means two hours and fifty minutes: a batch goes no later than this says. Null once it is due.
export function fmtLeft(iso) {
  const minutes = iso ? Math.floor((new Date(iso) - Date.now()) / 60000) : 0;
  if (minutes <= 0) return null;
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours} h` : `${Math.floor(hours / 24)} d`;
}

// kind: ok, warn, bad or info; none for a neutral grey.
export const tag = (text, kind = "") => html`<span class="tag ${kind}">${text}</span>`;
export const notice = (kind, ...content) => html`<div class="notice ${kind}">${content}</div>`;

export const expiryTag = (iso) => {
  if (!iso) return nothing;
  const left = fmtLeft(iso);
  return tag(left ? `${left} left` : "deleting now", new Date(iso) - Date.now() < EXPIRY_WARN_MS ? "warn" : "");
};

export async function showError(message) {
  const toast = await toastController.create({
    message, color: "danger", duration: 8000, position: "bottom", buttons: [{ text: "OK", role: "cancel" }],
  });
  await toast.present();
}

let asking = false; // a confirmation is open; a second tap mustn't open another on top of it

// Resolves true when the confirming button was pressed; Cancel, the backdrop and a second
// confirmation opened on top of the first all count as no.
export async function confirmed({ header, message, confirm, destructive = false, cssClass }) {
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

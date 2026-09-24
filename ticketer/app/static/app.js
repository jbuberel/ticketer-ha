// Ticketer phone UI. Each screen is a page element of its own -- home.js, capture.js, batch.js --
// and ion-router swaps them as the address changes: #/, #/capture, #/batch/<id>.

import "./batch.js";
import { onUploadChange, resumeGps } from "./capture.js";
import "./home.js";
import * as store from "./store.js";
import { startUploads } from "./uploads.js";

const hasSession = async () => (await store.getSession()) != null;

// Where a guard sends the screen, the address follows. The router moves the address itself for a
// push, but not after Back: Back from the capture screen would leave #/ showing capture.
function redirect(from, to) {
  if ((location.hash || "#/") === `#${from}`) history.replaceState(history.state, "", `#${to}`);
  return { redirect: to };
}

// The routes are made here rather than written into index.html, so that the pages are defined
// and the guards set before the router reads the address for the first time.
async function startRouter() {
  await Promise.all(["ion-router", "ion-route"].map((tag) => customElements.whenDefined(tag)));
  const route = (url, component, beforeEnter) => Object.assign(document.createElement("ion-route"), { url, component, beforeEnter });
  const router = document.createElement("ion-router");
  // No catch-all redirect: ion-router applies redirects before matching routes, so one from "*"
  // sends every address, the good ones included, to where it points.
  router.append(
    // An unfinished capture session always comes first, from a cold start or a reload
    // mid-session (iOS can reload the page while the camera is open).
    route("/", "page-home", async () => (await hasSession()) ? redirect("/", "/capture") : true),
    route("/capture", "page-capture", async () => (await hasSession()) || redirect("/capture", "/")),
    route("/batch/:batchId", "page-batch"),
  );
  document.querySelector("ion-app").prepend(router);
}

window.addEventListener("online", () => startUploads());
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  resumeGps();
  startUploads();
});

(async () => {
  const session = await store.getSession();
  if (session) {
    await store.recoverInterrupted(session.batchId);
    startUploads(onUploadChange);
  }
  await startRouter();
})();

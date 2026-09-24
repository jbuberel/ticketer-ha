// Moving between screens. ion-router keeps the address and the phone's own Back gesture in step
// with the screen shown; this adds the one thing it doesn't know: whether Back can stay in the app.

const router = () => document.querySelector("ion-router");

// In-app entries behind this one. Back returns to one of them when there is one. When the app
// was opened on this screen -- a reload mid-review, say -- Back goes home instead of out of it.
let behind = 0;
window.addEventListener("popstate", () => { behind = Math.max(0, behind - 1); });

export function go(path) {
  behind++;
  return router().push(path, "forward");
}

// In place of this screen, so Back skips it: a finished capture session isn't somewhere to return to.
export const replace = (path) => router().push(path, "root");

export function back(fallback = "/") {
  if (behind > 0) history.back();
  else router().push(fallback, "back");
}

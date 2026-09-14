// Small fetch wrapper: JSON or FormData in, JSON out, errors carry the HTTP status.
// Every call has a timeout, so a dead connection surfaces as an error instead of hanging.

const DEFAULT_TIMEOUT_MS = 20000;

export async function api(method, path, { json, form, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const init = { method, headers: {}, signal: controller.signal };
  if (json !== undefined) {
    init.headers["content-type"] = "application/json";
    init.body = JSON.stringify(json);
  } else if (form) {
    init.body = form;
  }

  try {
    let response;
    try {
      response = await fetch(path, init);
    } catch (error) {
      // No status on these errors, so callers treat them as retryable.
      throw new Error(controller.signal.aborted
        ? `No response from the server after ${Math.round(timeoutMs / 1000)} s`
        : `Can't reach the server (${error.message})`);
    }

    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const body = await response.json();
        if (body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      } catch { /* not JSON */ }
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    return response.status === 204 ? null : await response.json();
  } finally {
    clearTimeout(timer);
  }
}

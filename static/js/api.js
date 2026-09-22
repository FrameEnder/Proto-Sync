// Thin fetch wrapper + server-sent events.
export async function api(path, { method = "GET", body, raw = false } = {}) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try { const j = await res.json(); detail = j.detail || detail; } catch { /* not json */ }
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status;
    throw err;
  }
  if (raw) return res.text();
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res.text();
}

export const get = (p) => api(p);
export const post = (p, body = {}) => api(p, { method: "POST", body });
export const put = (p, body) => api(p, { method: "PUT", body });
export const del = (p) => api(p, { method: "DELETE" });

// Reconnecting EventSource. handlers: { eventName: fn(data) }
export function connectEvents(handlers, onState) {
  let es;
  let retry = 1000;
  const open = () => {
    es = new EventSource("/api/events");
    es.onopen = () => { retry = 1000; onState?.(true); };
    es.onerror = () => {
      onState?.(false);
      es.close();
      setTimeout(open, retry);
      retry = Math.min(retry * 2, 15000);
    };
    for (const [name, fn] of Object.entries(handlers)) {
      es.addEventListener(name, (ev) => {
        try { fn(JSON.parse(ev.data)); } catch (e) { console.error(name, e); }
      });
    }
  };
  open();
}

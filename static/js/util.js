// Small DOM + formatting helpers shared by every view.
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Size units: "si" = 1 kB is 1000 bytes (FreeFileSync, drive labels);
// "iec" = 1 KiB is 1024 bytes. Set from the server setting at boot.
let SIZE_UNITS = "si";
export function setSizeUnits(u) { SIZE_UNITS = u === "iec" ? "iec" : "si"; }
export function bytes(n, digits = 1) {
  n = Number(n || 0);
  const [base, u] = SIZE_UNITS === "iec"
    ? [1024, ["KiB", "MiB", "GiB", "TiB", "PiB"]]
    : [1000, ["kB", "MB", "GB", "TB", "PB"]];
  if (Math.abs(n) < base) return `${n} B`;
  let i = -1;
  do { n /= base; i++; } while (Math.abs(n) >= base && i < u.length - 1);
  return `${n.toFixed(n >= 100 ? 0 : digits)} ${u[i]}`;
}

export const num = (n) => Number(n || 0).toLocaleString();

export function dtime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const pad = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function rel(ts) {
  if (!ts) return "never";
  const s = Math.round(ts - Date.now() / 1000);
  const a = Math.abs(s);
  if (a < 5) return "just now";
  const f = a < 60 ? `${a}s` : a < 3600 ? `${Math.round(a / 60)} min` : a < 86400 * 2 ? `${Math.round(a / 3600)} h` : `${Math.round(a / 86400)} days`;
  return s >= 0 ? `in ${f}` : `${f} ago`;
}

export function dur(sec) {
  if (sec == null) return "—";
  if (sec < 1) return "<1s";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

export const icon = (name, cls = "") => `<svg class="ico ${cls}"><use href="#i-${name}"/></svg>`;

export function toast(text, level = "info", ms = 4500) {
  const box = $("#toasts");
  const t = document.createElement("div");
  t.className = `toast ${level}`;
  t.textContent = text;
  box.appendChild(t);
  setTimeout(() => t.remove(), level === "error" ? ms * 2 : ms);
}

// ------------------------------------------------------------------ menu --
let menuClose = null;
export function openMenu(x, y, items) {
  const m = $("#menu");
  m.innerHTML = "";
  for (const it of items) {
    if (it === "-") { m.appendChild(document.createElement("hr")); continue; }
    if (it.label && !it.run) {
      const l = document.createElement("div");
      l.className = "menu-label";
      l.textContent = it.label;
      m.appendChild(l);
      continue;
    }
    const b = document.createElement("button");
    b.innerHTML = `${it.icon ? icon(it.icon, it.icon) : '<span style="width:18px"></span>'}<span>${esc(it.text)}</span>${it.kbd ? `<span class="kbd">${esc(it.kbd)}</span>` : ""}`;
    b.disabled = !!it.disabled;
    b.onclick = () => { closeMenu(); it.run(); };
    m.appendChild(b);
  }
  m.hidden = false;
  const r = m.getBoundingClientRect();
  m.style.left = `${Math.min(x, innerWidth - r.width - 8)}px`;
  m.style.top = `${Math.min(y, innerHeight - r.height - 8)}px`;
  setTimeout(() => {
    menuClose = (e) => { if (!m.contains(e.target)) closeMenu(); };
    document.addEventListener("mousedown", menuClose);
  });
}
export function closeMenu() {
  $("#menu").hidden = true;
  if (menuClose) document.removeEventListener("mousedown", menuClose);
  menuClose = null;
}

// ----------------------------------------------------------------- modal --
export function openModal(html, { narrow = false, onClose } = {}) {
  const d = $("#modal");
  d.className = `modal${narrow ? " narrow" : ""}`;
  d.innerHTML = html;
  d.onclose = () => { d.innerHTML = ""; onClose?.(); };
  if (!d.open) d.showModal();
  $$("[data-close]", d).forEach((b) => (b.onclick = () => d.close()));
  return d;
}
export const closeModal = () => $("#modal").open && $("#modal").close();

export function confirmBox(title, text, { ok = "Continue", danger = false } = {}) {
  return new Promise((resolve) => {
    let answered = false;
    const d = openModal(`
      <div class="modal-inner">
        <div class="modal-head"><h2>${esc(title)}</h2></div>
        <div class="modal-body"><p style="margin:0">${text}</p></div>
        <div class="modal-foot">
          <button class="btn ghost" data-close>Cancel</button>
          <button class="btn ${danger ? "danger solid" : "primary"}" id="cbOk">${esc(ok)}</button>
        </div>
      </div>`, { narrow: true, onClose: () => !answered && resolve(false) });
    $("#cbOk", d).onclick = () => { answered = true; resolve(true); d.close(); };
  });
}

// Deep get/set by dotted path — used by the job editor's data binding.
export function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
}
export function setPath(obj, path, value) {
  const keys = path.split(".");
  let o = obj;
  for (const k of keys.slice(0, -1)) o = o[k] ??= {};
  o[keys.at(-1)] = value;
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export const ACTION_LABEL = {
  copy_lr: "Copy to right", copy_rl: "Copy to left", delete_left: "Delete on left",
  delete_right: "Delete on right", move_left: "Move on left", move_right: "Move on right", none: "Do nothing",
};
export const CATEGORY_LABEL = {
  left_only: "Left only", right_only: "Right only", left_newer: "Left newer", right_newer: "Right newer",
  different: "Different", conflict: "Conflict", equal: "Identical",
};
export const VARIANT_LABEL = { mirror: "Mirror →", update: "Update →", two_way: "Two way ⇄", custom: "Custom" };

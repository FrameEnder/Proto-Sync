// Virtualized, server-paged comparison grid (handles hundreds of thousands of rows).
import { get } from "./api.js";
import { $, esc, bytes, dtime, icon, CATEGORY_LABEL, ACTION_LABEL } from "./util.js";

const ROW = 28;
const PAGE = 250;
const OVERSCAN = 12;

export class Grid {
  constructor({ onContext, onActionClick, onSelection, onKeyAction }) {
    this.body = $("#gridBody");
    this.rowsEl = $("#gridRows");
    this.spacer = $("#gridSpacer");
    this.root = $("#grid");
    this.onContext = onContext;
    this.onActionClick = onActionClick;
    this.onSelection = onSelection;
    this.onKeyAction = onKeyAction;
    this.sid = null;
    this.filters = { cats: [], acts: [], q: "", equal: false };
    this.ids = [];
    this.pages = new Map();
    this.inflight = new Map();
    this.sel = new Set();
    this.cursor = -1;
    this.anchor = -1;
    this.gen = 0;
    this.body.addEventListener("scroll", () => this.render(), { passive: true });
    new ResizeObserver(() => this.render()).observe(this.body);
    this.rowsEl.addEventListener("mousedown", (e) => this.onMouse(e));
    this.rowsEl.addEventListener("contextmenu", (e) => this.onCtx(e));
    this.root.addEventListener("keydown", (e) => this.onKey(e));
  }

  query() {
    const f = this.filters;
    return `cats=${encodeURIComponent(f.cats.join(","))}&acts=${encodeURIComponent(f.acts.join(","))}` +
      `&q=${encodeURIComponent(f.q)}&equal=${f.equal ? "true" : "false"}`;
  }

  clear() {
    this.sid = null;
    this.ids = [];
    this.pages.clear();
    this.sel.clear();
    this.cursor = -1;
    this.spacer.style.height = "0px";
    this.rowsEl.innerHTML = "";
  }

  async load(sid, { keepSelection = false } = {}) {
    const gen = ++this.gen;
    if (sid !== this.sid) { this.sel.clear(); this.cursor = -1; this.body.scrollTop = 0; }
    this.sid = sid;
    const ids = await get(`/api/sessions/${sid}/ids?${this.query()}`);
    if (gen !== this.gen) return;
    this.ids = ids;
    this.pages.clear();
    this.inflight.clear();
    if (!keepSelection) this.sel.clear();
    else { const live = new Set(ids); for (const id of [...this.sel]) if (!live.has(id)) this.sel.delete(id); }
    this.spacer.style.height = `${ids.length * ROW}px`;
    this.render();
    this.onSelection?.(this.sel.size);
  }

  async fetchPage(p) {
    if (this.pages.has(p) || this.inflight.has(p)) return;
    const gen = this.gen;
    const pr = get(`/api/sessions/${this.sid}/rows?offset=${p * PAGE}&limit=${PAGE}&${this.query()}`)
      .then((r) => { if (gen === this.gen) { this.pages.set(p, r.rows); this.render(); } })
      .catch(() => {})
      .finally(() => this.inflight.delete(p));
    this.inflight.set(p, pr);
  }

  rowAt(i) {
    const p = Math.floor(i / PAGE);
    return this.pages.get(p)?.[i - p * PAGE];
  }

  render() {
    if (!this.sid) return;
    const top = this.body.scrollTop;
    const h = this.body.clientHeight;
    const start = Math.max(0, Math.floor(top / ROW) - OVERSCAN);
    const end = Math.min(this.ids.length, Math.ceil((top + h) / ROW) + OVERSCAN);
    for (let p = Math.floor(start / PAGE); p <= Math.floor(Math.max(start, end - 1) / PAGE); p++) this.fetchPage(p);
    let html = "";
    for (let i = start; i < end; i++) html += this.rowHtml(i, this.rowAt(i));
    this.rowsEl.style.transform = `translateY(${start * ROW}px)`;
    this.rowsEl.innerHTML = html;
  }

  sideCell(side, r) {
    const e = r[side === "left" ? "l" : "r"];
    if (!e) return `<div class="cell side ${side} empty"></div>`;
    const slash = r.rel.lastIndexOf("/");
    const dir = slash >= 0 ? r.rel.slice(0, slash + 1) : "";
    const base = slash >= 0 ? r.rel.slice(slash + 1) : r.rel;
    const ico = r.kind === "d" ? icon("folder", "folder") : r.kind === "l" ? icon("link") : icon("file");
    const gone = (side === "left" && r.action === "delete_left") || (side === "right" && r.action === "delete_right");
    const title = `${r.rel}${r.note ? "\n" + r.note : ""}`;
    return `<div class="cell side ${side}${gone ? " gone" : ""}" title="${esc(title)}">
      <span class="name">${ico}${dir ? `<span class="dir"><bdi>${esc(dir)}</bdi></span>` : ""}<span class="base">${esc(base)}</span></span>
      <span class="size">${r.kind === "d" ? "" : bytes(e[0])}</span>
      <span class="time">${dtime(e[1])}</span></div>`;
  }

  rowHtml(i, r) {
    const id = this.ids[i];
    if (!r) return `<div class="grow" data-i="${i}"><div class="cell side left"></div><div class="cell mid"></div><div class="cell side right"></div></div>`;
    const cls = ["grow"];
    if (this.sel.has(id)) cls.push("sel");
    if (i === this.cursor) cls.push("cursor");
    if (r.locked) cls.push("locked");
    const over = r.action !== r.default ? " overridden" : "";
    const tip = `${CATEGORY_LABEL[r.category]} → ${ACTION_LABEL[r.action]}${r.note ? "\n" + r.note : ""}${over ? "\n(changed by you)" : ""}`;
    return `<div class="${cls.join(" ")}" data-i="${i}" data-id="${id}">
      ${this.sideCell("left", r)}
      <div class="cell mid"><span class="cat-dot cat-${r.category}" title="${esc(CATEGORY_LABEL[r.category])}"></span>
        <button class="act-btn ${r.action}${over}" data-act="${i}" title="${esc(tip)}" ${r.locked ? "disabled" : ""}>${icon(r.action)}</button></div>
      ${this.sideCell("right", r)}</div>`;
  }

  // ------------------------------------------------------------- selection -
  setCursor(i, { extend = false, toggle = false } = {}) {
    if (!this.ids.length) return;
    i = Math.max(0, Math.min(this.ids.length - 1, i));
    if (extend && this.anchor >= 0) {
      this.sel.clear();
      const [a, b] = [Math.min(this.anchor, i), Math.max(this.anchor, i)];
      for (let k = a; k <= b; k++) this.sel.add(this.ids[k]);
    } else if (toggle) {
      const id = this.ids[i];
      this.sel.has(id) ? this.sel.delete(id) : this.sel.add(id);
      this.anchor = i;
    } else {
      this.sel.clear();
      this.sel.add(this.ids[i]);
      this.anchor = i;
    }
    this.cursor = i;
    const top = i * ROW, st = this.body.scrollTop, h = this.body.clientHeight;
    if (top < st) this.body.scrollTop = top;
    else if (top + ROW > st + h) this.body.scrollTop = top + ROW - h;
    this.render();
    this.onSelection?.(this.sel.size);
  }

  selectAll() {
    this.sel = new Set(this.ids);
    this.render();
    this.onSelection?.(this.sel.size);
  }

  selectedIds() { return [...this.sel]; }

  selectedRows() {
    const out = [];
    for (let i = 0; i < this.ids.length && out.length < 5000; i++) {
      if (this.sel.has(this.ids[i])) { const r = this.rowAt(i); if (r) out.push(r); }
    }
    return out;
  }

  onMouse(e) {
    const rowEl = e.target.closest(".grow");
    if (!rowEl) return;
    const i = Number(rowEl.dataset.i);
    const act = e.target.closest("[data-act]");
    this.root.focus({ preventScroll: true });
    if (act && e.button === 0) {
      if (!this.sel.has(this.ids[i])) this.setCursor(i);
      const r = this.rowAt(i);
      const rect = act.getBoundingClientRect();
      if (r) this.onActionClick?.(r, rect.left, rect.bottom + 4);
      e.preventDefault();
      return;
    }
    if (e.button === 2 && this.sel.has(this.ids[i])) return;   // keep multi-selection for context menu
    this.setCursor(i, { extend: e.shiftKey, toggle: e.ctrlKey || e.metaKey });
  }

  onCtx(e) {
    const rowEl = e.target.closest(".grow");
    if (!rowEl) return;
    e.preventDefault();
    const i = Number(rowEl.dataset.i);
    if (!this.sel.has(this.ids[i])) this.setCursor(i);
    this.onContext?.(e.clientX, e.clientY);
  }

  onKey(e) {
    if (!this.ids.length) return;
    const page = Math.max(1, Math.floor(this.body.clientHeight / ROW) - 1);
    const k = e.key;
    if (e.altKey) {
      const map = { ArrowRight: "copy_lr", ArrowLeft: "copy_rl", "0": "none", d: "default", D: "default" };
      if (map[k]) { e.preventDefault(); this.onKeyAction?.(map[k]); }
      return;
    }
    const nav = { ArrowDown: 1, ArrowUp: -1, PageDown: page, PageUp: -page };
    if (k in nav) { e.preventDefault(); this.setCursor((this.cursor < 0 ? 0 : this.cursor) + nav[k], { extend: e.shiftKey }); }
    else if (k === "Home") { e.preventDefault(); this.setCursor(0, { extend: e.shiftKey }); }
    else if (k === "End") { e.preventDefault(); this.setCursor(this.ids.length - 1, { extend: e.shiftKey }); }
    else if ((e.ctrlKey || e.metaKey) && k.toLowerCase() === "a") { e.preventDefault(); this.selectAll(); }
    else if (k === "Escape") { this.sel.clear(); this.render(); this.onSelection?.(0); }
    else if (k === "ContextMenu" || (e.shiftKey && k === "F10")) {
      e.preventDefault();
      const r = this.rowsEl.querySelector(".grow.cursor")?.getBoundingClientRect();
      this.onContext?.(r ? r.left + 200 : 200, r ? r.bottom : 200);
    }
  }
}

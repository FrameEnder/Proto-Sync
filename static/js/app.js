// Proto-Sync main controller.
import { get, post, put, del, connectEvents } from "./api.js";
import {
  $, $$, esc, bytes, num, dtime, rel, dur, icon, toast, openMenu, closeMenu, openModal, closeModal, confirmBox,
  debounce, ACTION_LABEL, CATEGORY_LABEL, VARIANT_LABEL,
} from "./util.js";
import { Grid } from "./grid.js";
import { openEditor, pickFolder, describeTrigger, TRIGGER_TYPES, DEFAULT_EXCLUDES } from "./editor.js";

const CATS = ["left_only", "right_only", "left_newer", "right_newer", "different", "conflict"];
const ACTS = ["copy_lr", "copy_rl", "delete_left", "delete_right", "move_left", "move_right", "none"];
const LS = { job: "protosync.job" };

const S = {
  info: null,
  jobs: [],
  job: null,
  summary: null,
  comparing: {},            // job_id -> latest compare progress
  runs: new Map(),          // run_id -> { meta, progress, log[], done, showLog }
  drives: {},               // "i:side" -> fs/check result
  hideCats: new Set(),
  hideActs: new Set(),
  view: "compare",
  connected: false,
  panelOpen: true,
  history: { offset: 0, rows: [], job: "" },
};

// ================================================================== grid ===
const grid = new Grid({
  onContext: (x, y) => rowMenu(x, y),
  onActionClick: (r, x, y) => actionMenu(r, x, y),
  onSelection: () => renderStatus(),
  onKeyAction: (a) => applyAction(grid.selectedIds(), a),
});

function syncGridFilters() {
  const cats = S.hideCats.size ? [...CATS.filter((c) => !S.hideCats.has(c)), "equal"] : [];
  const acts = S.hideActs.size ? ACTS.filter((a) => !S.hideActs.has(a)) : [];
  grid.filters = { cats, acts, q: $("#search").value.trim(), equal: $("#showEqual").checked };
}

async function reloadGrid(opts = {}) {
  if (!S.summary) { grid.clear(); renderEmpty(); renderStatus(); return; }
  syncGridFilters();
  try {
    await grid.load(S.summary.id, opts);
  } catch (e) {
    if (e.status === 404) { S.summary = null; grid.clear(); toast("That comparison expired — compare again", "info"); }
    else toast(e.message, "error");
  }
  renderEmpty();
  renderStatus();
}

// ================================================================== jobs ===
const jobById = (id) => S.jobs.find((j) => j.id === id);

async function loadJobs() {
  S.jobs = await get("/api/jobs");
  if (S.job) S.job = jobById(S.job.id) || null;
  renderJobHeader();
}

function replaceJob(card) {
  const i = S.jobs.findIndex((j) => j.id === card.id);
  if (i >= 0) S.jobs[i] = card; else S.jobs.push(card);
  if (S.job?.id === card.id) S.job = card;
}

async function selectJob(id) {
  S.job = jobById(id) || S.jobs[0] || null;
  S.summary = null;
  S.drives = {};
  S.hideCats.clear();
  S.hideActs.clear();
  grid.clear();
  if (S.job) localStorage.setItem(LS.job, S.job.id);
  renderCompareView();
  if (!S.job) return;
  try {
    const sum = await get(`/api/jobs/${S.job.id}/session`);
    if (sum && S.job?.id === sum.job_id) { S.summary = sum; renderChips(); await reloadGrid(); }
  } catch { /* none cached */ }
  refreshDrives();
}

async function saveJob(mutate, { recompare = false } = {}) {
  const j = structuredClone(S.job);
  mutate(j);
  try {
    const card = await put(`/api/jobs/${j.id}`, j);
    replaceJob(card);
    S.summary = null;
    grid.clear();
    renderCompareView();
    refreshDrives();
    if (recompare) startCompare();
    return true;
  } catch (e) {
    toast(e.message, "error");
    renderPairs();
    return false;
  }
}

async function editJob(tab = "general", jobId = S.job?.id) {
  let full = null;
  if (jobId) {
    try { full = await get(`/api/jobs/${jobId}`); } catch (e) { return toast(e.message, "error"); }
  }
  openEditor(full, tab, {
    jobs: S.jobs,
    onSaved: async (card) => {
      await loadJobs();
      replaceJob(card);
      if (!jobId || jobId === S.job?.id) await selectJob(card.id);
      if (S.view === "schedule") renderSchedule();
    },
  });
}

function jobMenu(x, y) {
  const j = S.job;
  const items = [{ label: "Jobs" }];
  for (const job of S.jobs) {
    items.push({
      icon: job.busy ? "sync" : job.id === j?.id ? "check" : "drive",
      text: `${job.name}${job.busy ? "  · running" : ""}`,
      run: () => selectJob(job.id),
    });
  }
  items.push("-", { icon: "plus", text: "New job", run: () => editJob("general", null) });
  if (j) {
    items.push(
      { icon: "gear", text: "Edit job…", run: () => editJob("general") },
      { icon: "copy_lr", text: "Duplicate", run: async () => {
        try { const c = await post(`/api/jobs/${j.id}/duplicate`); await loadJobs(); selectJob(c.id); toast("Duplicated — its triggers start disabled", "info"); }
        catch (e) { toast(e.message, "error"); }
      } },
      "-",
      { icon: "play", text: "Run now (compare + sync)", run: () => runJob(false) },
      { icon: "compare", text: "Dry run now", run: () => runJob(true) },
      "-",
      { icon: "up", text: "Export as JSON", run: () => { location.href = `/api/jobs/${j.id}/export`; } },
      { icon: "plus", text: "Import job…", run: importJob },
    );
    if (j.sync.variant === "two_way") {
      items.push({ icon: "history", text: "Reset two-way history…", run: async () => {
        const ok = await confirmBox("Reset two-way history?",
          "The next comparison treats every difference as new: nothing is deleted until a fresh baseline exists, and files that exist on only one side are copied across.",
          { ok: "Reset" });
        if (!ok) return;
        try { await post(`/api/jobs/${j.id}/reset-baseline`); toast("Two-way history cleared", "success"); }
        catch (e) { toast(e.message, "error"); }
      } });
    }
    items.push("-", { icon: "x", text: "Delete job…", run: async () => {
      const ok = await confirmBox(`Delete “${esc(j.name)}”?`,
        "The job, its schedule and its two-way history are removed. Run history and files on disk are kept.",
        { ok: "Delete job", danger: true });
      if (!ok) return;
      try { await del(`/api/jobs/${j.id}`); await loadJobs(); selectJob(S.jobs[0]?.id); toast("Job deleted", "info"); }
      catch (e) { toast(e.message, "error"); }
    } });
  } else {
    items.push({ icon: "plus", text: "Import job…", run: importJob });
  }
  openMenu(x, y, items);
}

function importJob() {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".json,application/json";
  input.onchange = async () => {
    const f = input.files?.[0];
    if (!f) return;
    try {
      const data = JSON.parse(await f.text());
      if (data.schedule?.triggers) data.schedule.triggers.forEach((t) => (t.enabled = false));
      const card = await post("/api/jobs/import", data);
      await loadJobs();
      selectJob(card.id);
      toast(`Imported “${card.name}” — triggers start disabled`, "success");
    } catch (e) { toast(`Import failed: ${e.message}`, "error"); }
  };
  input.click();
}

// ============================================================ compare view ==
function renderCompareView() {
  renderJobHeader();
  renderPairs();
  renderVariant();
  renderFilterState();
  renderChips();
  renderCompareBtn();
  renderEmpty();
  renderStatus();
}

function renderJobHeader() {
  $("#jobName").textContent = S.job ? S.job.name : S.jobs.length ? "Choose a job" : "No job";
  $("#jobVariant").textContent = S.job ? VARIANT_LABEL[S.job.sync.variant] : "";
  document.title = S.job ? `${S.job.name} · Proto-Sync` : "Proto-Sync";
}

function renderVariant() {
  const v = S.job?.sync.variant;
  $$("#variantSwitch button").forEach((b) => {
    b.classList.toggle("on", b.dataset.variant === v);
    b.disabled = !S.job;
  });
}

function renderFilterState() {
  const el = $("#filterState");
  if (!S.job) { el.textContent = ""; return; }
  const f = S.job.filter;
  const extra = [];
  if (f.include.length !== 1 || f.include[0] !== "*") extra.push(`${f.include.length} include`);
  const custom = f.exclude.filter((x) => !DEFAULT_EXCLUDES.includes(x)).length;
  if (custom) extra.push(`${custom} exclude`);
  if (f.min_size != null || f.max_size != null) extra.push("size");
  if (f.max_age_days != null || f.newer_than) extra.push("age");
  el.textContent = extra.length ? `Filter: ${extra.join(", ")}` : "";
  el.title = `Exclude:\n${f.exclude.join("\n")}`;
}

function pairDirIcon() {
  const v = S.job?.sync.variant;
  return v === "two_way" ? "swap" : v === "custom" ? "sync" : "copy_lr";
}

function renderPairs() {
  const box = $("#pairs");
  if (!S.job) { box.innerHTML = ""; return; }
  box.innerHTML = S.job.pairs.map((p, i) => `
    <div class="pair${p.enabled ? "" : " disabled"}" title="${p.enabled ? "" : "This pair is disabled"}">
      ${pathbox(i, "left", p.left)}
      <div class="pair-mid">
        <span class="pair-dir" title="${esc(VARIANT_LABEL[S.job.sync.variant])}">${icon(pairDirIcon(), "lg")}</span>
        <button class="btn" data-swap="${i}" title="Swap left and right">${icon("swap", "sm")}</button>
      </div>
      ${pathbox(i, "right", p.right)}
    </div>`).join("");
  for (const key of Object.keys(S.drives)) paintMeta(key);
}

function pathbox(i, side, path) {
  return `<div class="pathbox ${side}">
    <button class="btn ghost icon-only" data-pick="${i}:${side}" title="Browse">${icon("folder", "sm")}</button>
    <input value="${esc(path)}" data-path="${i}:${side}" spellcheck="false" placeholder="${side === "left" ? "Left folder (source)" : "Right folder (target)"}">
    <div class="meta" data-meta="${i}:${side}"><span class="drive-state"><span class="dot"></span></span></div>
  </div>`;
}

function paintMeta(key) {
  const el = $(`[data-meta="${key}"]`);
  const c = S.drives[key];
  if (!el || !S.job) return;
  const sentinel = S.job.safety.sentinel_file || ".mounted";
  if (!c) { el.innerHTML = `<span class="drive-state"><span class="dot"></span>checking</span>`; return; }
  let state = "ok", label = "ready", tip = "";
  if (!c.allowed) { state = "bad"; label = "not allowed"; tip = `Outside the allowed roots: ${S.info?.roots.join(", ")}`; }
  else if (!c.exists) { state = "bad"; label = "missing"; tip = "Folder does not exist — is the drive mounted?"; }
  else if (S.job.safety.require_sentinel && sentinel && !c.sentinel) { state = "bad"; label = `no ${sentinel}`; tip = `Click to create ${sentinel} and mark this drive as verified`; }
  else if (c.system_disk) { state = ""; label = "system disk"; tip = "This folder is on the root file system — make sure the drive is really mounted"; }
  const u = c.usage;
  const used = u && u.total ? Math.round(((u.total - u.free) / u.total) * 100) : 0;
  el.innerHTML = `
    <span class="drive-state ${state}" title="${esc(tip)}" ${label.startsWith("no ") ? `data-mksent="${key}" style="cursor:pointer"` : ""}><span class="dot"></span>${esc(label)}</span>
    ${c.fstype ? `<span class="fs">${esc(c.fstype)}</span>` : ""}
    ${u ? `<span class="freebar" title="${used}% used"><i style="width:${used}%"></i></span><span class="free">${bytes(u.free)} free</span>` : ""}`;
}

async function refreshDrives() {
  if (!S.job) return;
  const jobId = S.job.id;
  const sentinel = S.job.safety.sentinel_file || ".mounted";
  await Promise.all(S.job.pairs.flatMap((p, i) => ["left", "right"].map(async (side) => {
    const key = `${i}:${side}`;
    if (!p[side]) { S.drives[key] = null; return; }
    try {
      const c = await get(`/api/fs/check?path=${encodeURIComponent(p[side])}&sentinel=${encodeURIComponent(sentinel)}`);
      if (S.job?.id === jobId) { S.drives[key] = c; paintMeta(key); }
    } catch { /* transient */ }
  })));
}

function renderCompareBtn() {
  const b = $("#btnCompare");
  const busy = S.job && S.comparing[S.job.id];
  b.innerHTML = busy ? `${icon("stop")}<span>Cancel</span>` : `${icon("compare")}<span>Compare</span>`;
  b.disabled = !S.job;
  $("#btnSync").disabled = !S.job || !!busy;
}

function renderChips() {
  const s = S.summary;
  $("#catChips").innerHTML = CATS.map((c) => {
    const n = s?.categories[c] || 0;
    return `<button class="chip${S.hideCats.has(c) ? "" : " on"}" data-cat="${c}" ${n ? "" : "disabled"} title="${esc(CATEGORY_LABEL[c])}">
      <span class="swatch cat-${c}"></span>${esc(CATEGORY_LABEL[c])}<span class="n">${num(n)}</span></button>`;
  }).join("");
  $("#actChips").innerHTML = ACTS.filter((a) => a !== "none" && (!a.startsWith("move_") || s?.actions[a]))
    .concat(["none"]).map((a) => {
      const n = s?.actions[a] || 0;
      return `<button class="chip${S.hideActs.has(a) ? "" : " on"}" data-act-chip="${a}" ${n ? "" : "disabled"} title="${esc(ACTION_LABEL[a])}">
        <span class="act-btn ${a}" style="width:auto;height:auto;border:0">${icon(a)}</span><span class="n">${num(n)}</span></button>`;
    }).join("");
}

function renderEmpty() {
  const el = $("#gridEmpty");
  const j = S.job;
  if (!j) {
    el.innerHTML = `<div class="empty-card"><h2>No job yet</h2>
      <p>A job is a pair of folders plus rules for keeping them in sync. Start with one — you can add schedules, filters and more later.</p>
      <button class="btn primary" data-act-empty="new">${icon("plus")} Create a job</button></div>`;
    return;
  }
  const cmp = S.comparing[j.id];
  if (cmp) {
    const pct = cmp.pairs ? Math.round(((cmp.pair - 1) / cmp.pairs) * 100) : 0;
    el.innerHTML = `<div class="empty-card">
      <h2>${esc(cmp.phase || "Comparing")}…</h2>
      <p class="mono dim" style="font-size:12.5px">${cmp.pairs > 1 ? `Pair ${cmp.pair} of ${cmp.pairs} · ` : ""}${cmp.items != null ? `${num(cmp.items)} items` : ""}</p>
      <div class="scan-meter"><i></i></div>
      <p class="mono faint" style="font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:rtl"><bdi>${esc(cmp.path || "")}</bdi></p>
      ${pct ? `<p class="faint" style="font-size:12px">${pct}% of pairs done</p>` : ""}
    </div>`;
    return;
  }
  if (!S.summary) {
    const last = j.last_run;
    el.innerHTML = `<div class="empty-card">
      <h2>Compare to preview</h2>
      <p>Proto-Sync scans both sides and shows exactly what will be copied, updated, moved and deleted — nothing changes until you press Synchronize.</p>
      <button class="btn primary" data-act-empty="compare">${icon("compare")} Compare <span class="mono" style="opacity:.6;font-size:11px">F5</span></button>
      ${last ? `<p class="dim" style="margin-top:16px;font-size:12.5px">Last run ${rel(last.finished || last.started)}:
        <span class="status ${last.status}">${esc(last.status)}</span>
        ${last.stats?.files_copied != null ? ` · ${num(last.stats.files_copied)} copied, ${num(last.stats.files_deleted)} deleted` : ""}</p>` : ""}
      ${j.next_run ? `<p class="faint" style="font-size:12.5px">Next scheduled run ${rel(j.next_run)}</p>` : ""}
    </div>`;
    return;
  }
  if (!grid.ids.length) {
    const actionable = Object.entries(S.summary.actions).some(([a, n]) => a !== "none" && n);
    if (!actionable && !S.summary.error_count) {
      el.innerHTML = `<div class="empty-card"><h2 style="color:var(--ok)">${icon("check", "lg")} Everything is in sync</h2>
        <p>${num(S.summary.total)} items compared in ${dur(S.summary.duration)}. Tick “Show identical” to browse them.</p></div>`;
    } else {
      el.innerHTML = `<div class="empty-card"><h2>Nothing matches this view</h2>
        <p>Some categories or actions are hidden, or the search excludes everything.</p>
        <button class="btn" data-act-empty="reset">Show everything</button></div>`;
    }
    return;
  }
  el.innerHTML = "";
}

function renderStatus() {
  const bar = $("#statusbar");
  const s = S.summary;
  const offline = S.connected ? "" : `<span style="color:var(--del)">● offline — reconnecting</span>`;
  if (!s) {
    bar.innerHTML = `<div>${S.job ? "No comparison" : ""}</div><div class="mid"></div><div class="right">${offline}</div>`;
    return;
  }
  const a = s.actions;
  const n = (k) => a[k] || 0;
  const stat = (cls, ic, count, extra = "", title = "") =>
    `<span class="stat ${cls}${count ? "" : " zero"}" title="${esc(title)}">${icon(ic)}${num(count)}${extra}</span>`;
  const sel = grid.sel.size;
  bar.innerHTML = `
    <div>${num(grid.ids.length)} shown of ${num(s.total)}${sel ? ` · <b style="color:var(--text)">${num(sel)} selected</b>` : ""}</div>
    <div class="mid">
      ${stat("copy_lr", "copy_lr", n("copy_lr"), n("copy_lr") ? ` · ${bytes(s.bytes_lr)}` : "", "Copy to right")}
      ${stat("copy_rl", "copy_rl", n("copy_rl"), n("copy_rl") ? ` · ${bytes(s.bytes_rl)}` : "", "Copy to left")}
      ${stat("del", "delete_left", n("delete_left"), "", "Delete on left")}
      ${stat("del", "delete_right", n("delete_right"), "", "Delete on right")}
      ${stat("move", "move_right", n("move_left") + n("move_right"), "", "Move / rename")}
    </div>
    <div class="right">
      ${s.error_count ? `<button class="linkish" id="showErrors">${num(s.error_count)} scan warning${s.error_count > 1 ? "s" : ""}</button> · ` : ""}
      Compared ${rel(s.created)} in ${dur(s.duration)} ${offline}
    </div>`;
}

// ------------------------------------------------------------- compare ----
async function startCompare() {
  const j = S.job;
  if (!j) return;
  if (S.comparing[j.id]) {
    try { await del(`/api/jobs/${j.id}/compare`); } catch (e) { toast(e.message, "error"); }
    return;
  }
  // Set local state BEFORE the request: a fast compare can finish and deliver
  // compare_done over SSE before this POST resolves. Doing it afterwards would
  // wipe the freshly loaded result and leave the view stuck on "Starting".
  const token = { phase: "Starting" };
  S.comparing[j.id] = token;
  S.summary = null;
  grid.clear();
  renderChips();
  renderCompareBtn();
  renderEmpty();
  renderStatus();
  refreshDrives();
  try {
    await post(`/api/jobs/${j.id}/compare`);
  } catch (e) {
    if (S.comparing[j.id] === token) delete S.comparing[j.id];
    renderCompareBtn();
    renderEmpty();
    toast(e.message, "error");
  }
}

// ------------------------------------------------------------- actions ----
async function applyAction(ids, action, all = false) {
  if (!S.summary) return;
  $("#grid").focus({ preventScroll: true });   // menus steal focus; keep Alt+ shortcuts working
  if (!all && !ids.length) return;
  const body = all
    ? { action, all_filtered: true, filters: { cats: grid.filters.cats.join(","), acts: grid.filters.acts.join(","), q: grid.filters.q, equal: grid.filters.equal } }
    : { ids, action };
  try {
    const r = await post(`/api/sessions/${S.summary.id}/actions`, body);
    S.summary = r.summary;
    renderChips();
    await reloadGrid({ keepSelection: true });
    if (!r.changed) toast("No rows changed — that action isn't possible for the selection", "info");
  } catch (e) {
    if (e.status === 404) { S.summary = null; grid.clear(); renderCompareView(); }
    toast(e.message, "error");
  }
}

function actionMenu(r, x, y) {
  const ids = grid.selectedIds();
  const opts = ACTS.filter((a) => r.valid.includes(a));
  openMenu(x, y, [
    { label: ids.length > 1 ? `${num(ids.length)} selected` : r.rel.split("/").pop() },
    ...opts.map((a) => ({ icon: a, text: `${ACTION_LABEL[a]}${a === r.default ? "  (default)" : ""}`, run: () => applyAction(ids.length ? ids : [r.id], a) })),
    "-",
    { icon: "history", text: "Restore default", kbd: "Alt+D", run: () => applyAction(ids.length ? ids : [r.id], "default"), disabled: r.action === r.default },
  ]);
}

function rowMenu(x, y) {
  const ids = grid.selectedIds();
  if (!ids.length) return;
  const rows = grid.selectedRows();
  const can = (a) => !rows.length || rows.some((r) => r.valid.includes(a));
  const shown = grid.ids.length;
  openMenu(x, y, [
    { label: `${num(ids.length)} selected` },
    { icon: "copy_lr", text: ACTION_LABEL.copy_lr, kbd: "Alt+→", run: () => applyAction(ids, "copy_lr"), disabled: !can("copy_lr") },
    { icon: "copy_rl", text: ACTION_LABEL.copy_rl, kbd: "Alt+←", run: () => applyAction(ids, "copy_rl"), disabled: !can("copy_rl") },
    { icon: "delete_left", text: ACTION_LABEL.delete_left, run: () => applyAction(ids, "delete_left"), disabled: !can("delete_left") },
    { icon: "delete_right", text: ACTION_LABEL.delete_right, run: () => applyAction(ids, "delete_right"), disabled: !can("delete_right") },
    { icon: "none", text: ACTION_LABEL.none, kbd: "Alt+0", run: () => applyAction(ids, "none") },
    { icon: "history", text: "Restore default", kbd: "Alt+D", run: () => applyAction(ids, "default") },
    "-",
    { icon: "filter", text: "Exclude via filter…", run: () => excludeRows(rows, ids) },
    { icon: "folder", text: "Show only this folder", run: () => {
      const r = rows[0];
      if (!r) return;
      const folder = r.kind === "d" ? r.rel : r.rel.split("/").slice(0, -1).join("/");
      $("#search").value = folder;
      reloadGrid();
    } },
    { icon: "file", text: "Copy relative path", run: () => copyText(rows.map((r) => r.rel).join("\n")) },
    "-",
    { label: `All ${num(shown)} shown rows` },
    { icon: "copy_lr", text: "Copy all to right", run: () => bulk("copy_lr", shown) },
    { icon: "none", text: "Do nothing for all", run: () => bulk("none", shown) },
    { icon: "history", text: "Restore defaults for all", run: () => bulk("default", shown) },
  ]);
}

async function bulk(action, count) {
  if (count > 500) {
    const ok = await confirmBox("Change every shown row?", `${num(count)} rows will be set to <b>${esc(action === "default" ? "their default" : ACTION_LABEL[action])}</b>.`);
    if (!ok) return;
  }
  applyAction([], action, true);
}

async function excludeRows(rows, ids) {
  const pats = rows.map((r) => `/${r.rel}${r.kind === "d" ? "/" : ""}`);
  const ok = await confirmBox("Exclude via filter",
    `These patterns are added to the job's exclude list and the items are left alone from now on:<div class="codeblock" style="margin-top:10px;max-height:200px;overflow:auto">${pats.slice(0, 50).map(esc).join("\n")}${pats.length > 50 ? `\n… and ${pats.length - 50} more` : ""}</div>`,
    { ok: "Exclude" });
  if (!ok) return;
  try {
    const r = await post(`/api/sessions/${S.summary.id}/exclude`, { ids });
    S.summary = r.summary;
    await loadJobs();
    renderFilterState();
    renderChips();
    await reloadGrid();
    toast(`Excluded ${r.added.length} item${r.added.length > 1 ? "s" : ""}`, "success");
  } catch (e) { toast(e.message, "error"); }
}

async function copyText(t) {
  try { await navigator.clipboard.writeText(t); toast("Copied", "success", 1500); }
  catch { toast("Clipboard not available (needs HTTPS or localhost)", "error"); }
}

// ================================================================== sync ===
async function synchronize() {
  const j = S.job;
  if (!j) return;
  if (j.busy) { toast(`${j.name} is already running`, "info"); S.panelOpen = true; renderRunPanel(); return; }
  if (!S.summary) {
    const ok = await confirmBox("Synchronize without a preview?",
      `Nothing has been compared yet. Proto-Sync will compare and synchronize in one go (<b>${esc(VARIANT_LABEL[j.sync.variant])}</b>), still behind every safety guard. Press Compare first if you want to review the changes.`,
      { ok: "Compare & sync" });
    if (ok) runJob(false);
    return;
  }
  openSyncConfirm();
}

function syncEstimate() {
  const s = S.summary, a = s.actions;
  const rf = s.pairs.reduce((t, p) => t + (p.right_files || 0), 0);
  const lf = s.pairs.reduce((t, p) => t + (p.left_files || 0), 0);
  const pr = rf ? ((a.delete_right || 0) * 100) / rf : 0;
  const pl = lf ? ((a.delete_left || 0) * 100) / lf : 0;
  const lim = S.job.safety.max_delete_percent;
  const cnt = S.job.safety.max_delete_count;
  const over = (lim && (pr > lim || pl > lim)) || (cnt && ((a.delete_right || 0) > cnt || (a.delete_left || 0) > cnt));
  return { pr, pl, over };
}

function openSyncConfirm() {
  const j = S.job, s = S.summary, a = s.actions;
  const n = (k) => a[k] || 0;
  const total = ACTS.filter((x) => x !== "none").reduce((t, k) => t + n(k), 0);
  if (!total) return toast("Nothing to do — both sides already match", "success");
  const est = syncEstimate();
  const delMode = j.sync.deletion === "permanent"
    ? `<div class="note bad">${icon("alert", "sm")} Deleted and overwritten files are removed <b>permanently</b>.</div>`
    : j.sync.deletion === "versioning"
      ? `<div class="note">Deleted and overwritten files are kept in <span class="mono">${esc(j.sync.versioning_path)}</span>.</div>`
      : `<div class="note">Deleted and overwritten files go to <span class="mono">.protosync-trash</span> on the same drive${j.sync.recycle_retention_days ? ` for ${j.sync.recycle_retention_days} days` : ""}.</div>`;
  const cell = (cls, ic, count, label, extra = "") => count ? `
    <div class="confirm-cell ${cls}"><div class="big">${num(count)}</div><div class="lbl">${icon(ic, "sm")}${label}</div>${extra ? `<div class="dim mono" style="font-size:12px;margin-top:3px">${extra}</div>` : ""}</div>` : "";
  const d = openModal(`
    <div class="modal-inner">
      <div class="modal-head"><h2>Start synchronization</h2><button class="btn ghost icon-only" data-close>${icon("x")}</button></div>
      <div class="modal-body">
        <p style="margin:0 0 12px"><b>${esc(j.name)}</b> · <span style="color:var(--amber)">${esc(VARIANT_LABEL[j.sync.variant])}</span>
          <span class="dim"> · compared ${rel(s.created)}</span></p>
        <div class="confirm-grid">
          ${cell("copy_lr", "copy_lr", n("copy_lr"), "copy to right", bytes(s.bytes_lr))}
          ${cell("copy_rl", "copy_rl", n("copy_rl"), "copy to left", bytes(s.bytes_rl))}
          ${cell("move", "move_right", n("move_left") + n("move_right"), "move / rename")}
          ${cell("del", "delete_right", n("delete_right"), "delete on right", est.pr ? `${est.pr.toFixed(1)}% of right` : "")}
          ${cell("del", "delete_left", n("delete_left"), "delete on left", est.pl ? `${est.pl.toFixed(1)}% of left` : "")}
        </div>
        ${n("delete_left") + n("delete_right") ? delMode : ""}
        ${est.over ? `<div class="note bad">${icon("alert", "sm")} This exceeds the job's deletion limit
          (${j.safety.max_delete_percent ? `${j.safety.max_delete_percent}%` : ""}${j.safety.max_delete_count ? ` / ${j.safety.max_delete_count} files` : ""}) and will be blocked unless you override it.
          <label class="check" style="margin-top:8px"><input type="checkbox" id="scForce"> I reviewed the preview — override the limit for this run</label></div>` : ""}
        ${s.error_count ? `<div class="note">${num(s.error_count)} items couldn't be read during the scan and will be skipped.</div>` : ""}
        ${(Date.now() / 1000 - s.created) > 3600 ? `<div class="note">This comparison is over an hour old. Files changed since then are re-checked before being touched, but you may want to compare again.</div>` : ""}
        <label class="check" style="margin-top:10px"><input type="checkbox" id="scDry"> Dry run — log what would happen, change nothing</label>
      </div>
      <div class="modal-foot">
        <button class="btn ghost" data-close>Cancel</button>
        <button class="btn primary" id="scGo">${icon("sync")} Synchronize</button>
      </div>
    </div>`, { narrow: true });
  const go = $("#scGo", d);
  const dry = $("#scDry", d);
  const force = $("#scForce", d);
  const upd = () => {
    go.innerHTML = dry.checked ? `${icon("search")} Dry run` : `${icon("sync")} Synchronize`;
    go.className = `btn ${force?.checked && !dry.checked ? "danger solid" : "primary"}`;
  };
  dry.onchange = upd;
  if (force) force.onchange = upd;
  go.onclick = async () => {
    const body = { dry_run: dry.checked, force: !!force?.checked };
    // Close first: a fast run can finish (and a blocked run can open the override
    // prompt in this same dialog) before the POST below resolves.
    closeModal();
    S.panelOpen = true;
    try {
      await post(`/api/sessions/${s.id}/sync`, body);
    } catch (e) {
      toast(e.message, "error");
    }
  };
}

async function runJob(dry, force = false, jobId = S.job?.id) {
  if (!jobId) return;
  try {
    const r = await post(`/api/jobs/${jobId}/run`, { dry_run: dry, force });
    if (r.run_id == null) toast("Queued", "info");
    S.panelOpen = true;
  } catch (e) { toast(e.message, "error"); }
}

async function offerOverride(run) {
  const ok = await confirmBox("Safety limit reached",
    `${esc(run.error)}<br><br>If you reviewed the preview and these deletions are expected, you can run once more with the limit overridden.`,
    { ok: "Override and sync", danger: true });
  if (!ok) return;
  if (S.summary && S.summary.job_id === run.job_id) {
    try { await post(`/api/sessions/${S.summary.id}/sync`, { dry_run: run.dry_run, force: true }); return; }
    catch (e) { if (e.status !== 404) return toast(e.message, "error"); }
  }
  runJob(run.dry_run, true, run.job_id);
}

// ============================================================= run panel ===
function runEntry(id, meta = {}) {
  let r = S.runs.get(id);
  if (!r) { r = { meta, progress: {}, log: [], done: null, showLog: false }; S.runs.set(id, r); }
  Object.assign(r.meta, meta);
  return r;
}

let rafPending = 0;
function scheduleRender() {
  if (rafPending) return;
  rafPending = requestAnimationFrame(() => { rafPending = 0; renderRunPanel(); renderActivity(); });
}

function renderActivity() {
  const running = [...S.runs.values()].filter((r) => !r.done).length;
  const comparing = Object.keys(S.comparing).length;
  const btn = $("#activity");
  btn.hidden = !running && !comparing && !S.runs.size;
  const bits = [];
  if (running) bits.push(`${running} running`);
  if (comparing) bits.push(`${comparing} comparing`);
  if (!bits.length && S.runs.size) bits.push(`${S.runs.size} finished`);
  $("#activityText").textContent = bits.join(" · ");
  $(".pulse", btn).style.display = running || comparing ? "" : "none";
}

function logLineHtml(l) {
  const cls = /ERROR:|FATAL/.test(l) ? "e" : /WARN:/.test(l) ? "w" : "";
  return `<div${cls ? ` class="${cls}"` : ""}>${esc(l)}</div>`;
}

function renderRunPanel() {
  const panel = $("#runpanel");
  if (!S.runs.size || !S.panelOpen) { panel.hidden = true; return; }
  const scrolls = {};
  $$(".run-log", panel).forEach((el) => {
    const id = el.closest("[data-run]").dataset.run;
    scrolls[id] = el.scrollHeight - el.scrollTop - el.clientHeight < 20 ? -1 : el.scrollTop;
  });
  panel.hidden = false;
  panel.innerHTML = [...S.runs.entries()].sort((a, b) => b[0] - a[0]).map(([id, r]) => {
    const p = r.progress || {};
    const m = r.meta;
    const name = m.job_name || p.job_name || "Run";
    const dry = m.dry_run || p.dry_run;
    if (r.done) {
      const st = r.done.stats || {};
      return `<div class="run" data-run="${id}">
        <div class="run-top"><div style="min-width:0"><div class="run-title">${esc(name)}${dry ? " · dry run" : ""}</div>
          <div class="run-phase">${r.done.error ? `<span style="color:var(--del)">${esc(r.done.error)}</span>` : `Finished in ${dur(st.duration)}`}</div></div>
          <span class="status ${r.done.status}">${esc(r.done.status)}</span></div>
        <div class="run-stats" style="margin-top:8px">
          ${dry ? `<span>would copy ${num(st.would_copy)} · ${bytes(st.would_copy_bytes)}</span><span>would delete ${num(st.would_delete)}</span>
            <span>would move ${num(st.would_move)}</span>` : `<span>${num(st.files_copied)} copied · ${bytes(st.bytes_copied)}</span>
            <span>${num(st.files_deleted)} deleted</span><span>${num(st.moved)} moved</span>`}${st.errors ? `<span style="color:var(--del)">${num(st.errors)} errors</span>` : ""}
          ${st.verify_mismatches ? `<span style="color:var(--warn)">${num(st.verify_mismatches)} repaired</span>` : ""}
        </div>
        <div class="run-actions">
          <button class="btn sm" data-runlog="${id}">${icon("file", "sm")} Full log</button>
          ${r.done.status === "blocked" && /override/i.test(r.done.error || "") ? `<button class="btn sm danger" data-override="${id}">Override…</button>` : ""}
          <span style="flex:1"></span>
          <button class="btn sm ghost" data-dismiss="${id}">Dismiss</button>
        </div>
      </div>`;
    }
    const pct = Math.max(0, Math.min(100, p.percent || 0));
    return `<div class="run" data-run="${id}">
      <div class="run-top"><div style="min-width:0"><div class="run-title">${esc(name)}${dry ? " · dry run" : ""}</div>
        <div class="run-phase">${esc(p.phase || "Starting")}${p.paused ? " — paused" : ""}</div></div>
        <div class="run-pct">${pct.toFixed(pct < 10 ? 1 : 0)}%</div></div>
      <div class="bar${p.paused ? " paused" : ""}"><i style="width:${pct}%"></i></div>
      <div class="run-stats">
        ${p.bytes_total ? `<span>${bytes(p.bytes_done)} / ${bytes(p.bytes_total)}</span>` : ""}
        ${p.items_total ? `<span>${num(p.items_done)} / ${num(p.items_total)} items</span>` : ""}
        ${p.rate ? `<span>${bytes(p.rate)}/s</span>` : ""}
        ${p.eta != null ? `<span>ETA ${dur(p.eta)}</span>` : ""}
        ${p.started ? `<span>${dur(Date.now() / 1000 - p.started)} elapsed</span>` : ""}
      </div>
      ${p.current ? `<div class="run-current"><bdi>${esc(p.current)}</bdi></div>` : ""}
      <div class="run-actions">
        ${p.paused ? `<button class="btn sm" data-resume="${id}">${icon("play", "sm")} Resume</button>`
          : `<button class="btn sm" data-pause="${id}">${icon("pause", "sm")} Pause</button>`}
        <button class="btn sm danger" data-cancel="${id}">${icon("stop", "sm")} Stop</button>
        <span style="flex:1"></span>
        <button class="btn sm ghost" data-togglelog="${id}">${r.showLog ? "Hide log" : "Show log"}</button>
      </div>
      ${r.showLog ? `<div class="run-log">${r.log.map(logLineHtml).join("")}</div>` : ""}
    </div>`;
  }).join("");
  $$(".run-log", panel).forEach((el) => {
    const id = el.closest("[data-run]").dataset.run;
    const sc = scrolls[id];
    el.scrollTop = sc === undefined || sc === -1 ? el.scrollHeight : sc;
  });
}

$("#runpanel").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const d = b.dataset;
  const idOf = (k) => Number(d[k]);
  try {
    if (d.pause) await post(`/api/runs/${idOf("pause")}/pause`);
    else if (d.resume) await post(`/api/runs/${idOf("resume")}/resume`);
    else if (d.cancel) {
      if (await confirmBox("Stop this run?", "The current transfer is interrupted. Files already copied stay; partial files are kept for resuming.", { ok: "Stop", danger: true }))
        await post(`/api/runs/${idOf("cancel")}/cancel`);
    } else if (d.togglelog) { const r = S.runs.get(idOf("togglelog")); r.showLog = !r.showLog; renderRunPanel(); }
    else if (d.dismiss) { S.runs.delete(idOf("dismiss")); scheduleRender(); }
    else if (d.runlog) openLog(idOf("runlog"));
    else if (d.override) { const r = S.runs.get(idOf("override")); S.runs.delete(idOf("override")); scheduleRender(); offerOverride(r.done); }
  } catch (err) { toast(err.message, "error"); }
});

// ================================================================ log view ==
async function openLog(runId) {
  let run, text;
  try {
    [run, text] = await Promise.all([get(`/api/runs/${runId}`), get(`/api/runs/${runId}/log`)]);
  } catch (e) { return toast(e.message, "error"); }
  const st = run.stats || {};
  const lines = String(text).split("\n");
  const html = lines.map((l) => {
    const cls = /ERROR:|FATAL/.test(l) ? "e" : /WARN:/.test(l) ? "w"
      : /^(={5,}|\s(CREATED|UPDATED|MOVED|DELETED)|\[.*\] ---|\[.*\] ===)/.test(l) ? "h" : "";
    return cls ? `<span class="${cls}">${esc(l)}</span>` : esc(l);
  }).join("\n");
  openModal(`
    <div class="modal-inner">
      <div class="modal-head"><h2>Run #${run.id} — ${esc(run.job_name)}</h2><button class="btn ghost icon-only" data-close>${icon("x")}</button></div>
      <div class="modal-body">
        <dl class="kv" style="padding-top:0">
          <dt>Status</dt><dd><span class="status ${run.status}">${esc(run.status)}${run.dry_run ? " · dry run" : ""}</span></dd>
          <dt>Trigger</dt><dd>${esc(run.trigger)}</dd>
          <dt>Started</dt><dd>${dtime(run.started)} (${rel(run.started)})</dd>
          <dt>Duration</dt><dd>${run.finished ? dur(run.finished - run.started) : "running"}</dd>
          ${run.dry_run && st.would_copy != null ? `<dt>Would change</dt><dd>${num(st.would_copy)} copies (${bytes(st.would_copy_bytes)}), ${num(st.would_delete)} deletions, ${num(st.would_move)} moves</dd>` : ""}
          <dt>Result</dt><dd>${num(st.files_copied)} copied (${bytes(st.bytes_copied)}), ${num(st.files_deleted)} deleted, ${num(st.moved)} moved,
            ${num(st.dirs_created)} folders created, ${num(st.errors)} errors, ${num(st.warnings)} warnings</dd>
        </dl>
        ${run.error ? `<div class="note bad">${esc(run.error)}</div>` : ""}
        <div class="logview">${html}</div>
      </div>
      <div class="modal-foot">
        <input class="input" id="logFind" placeholder="Find in log" style="margin-right:auto;width:220px">
        <a class="btn" href="/api/runs/${run.id}/log?download=true" download>${icon("up", "sm")} Download</a>
        <button class="btn ghost" data-close>Close</button>
      </div>
    </div>`);
  const view = $(".logview");
  view.scrollTop = view.scrollHeight;
  $("#logFind").onkeydown = (e) => {
    if (e.key !== "Enter") return;
    const q = e.target.value.trim();
    if (q && window.find) window.find(q, false, e.shiftKey, true);
  };
}

// ================================================================ schedule ==
async function renderSchedule() {
  const box = $("#scheduleList");
  let data;
  try { [data] = await Promise.all([get("/api/schedule"), loadJobs()]); }
  catch (e) { box.innerHTML = `<div class="note bad">${esc(e.message)}</div>`; return; }
  if (!data.length) { box.innerHTML = `<div class="dim">No jobs yet.</div>`; return; }

  const upcoming = [];
  for (const row of data) {
    const job = jobById(row.job_id);
    if (!job || !row.enabled) continue;
    for (const ti of row.triggers) {
      const t = job.schedule.triggers.find((x) => x.id === ti.id);
      if (t && t.enabled && ti.next) upcoming.push({ when: ti.next, job, t });
    }
  }
  upcoming.sort((a, b) => a.when - b.when);

  box.innerHTML = `
    ${upcoming.length ? `<div class="settings-block"><dl class="kv">
      ${upcoming.slice(0, 8).map((u) => `<dt class="mono">${dtime(u.when)} <span class="faint">(${rel(u.when)})</span></dt>
        <dd style="font-family:var(--ui)">${esc(u.job.name)} <span class="dim">— ${esc(describeTrigger(u.t, S.jobs))}</span></dd>`).join("")}
    </dl></div>` : ""}
    ${data.map((row) => {
      const job = jobById(row.job_id);
      if (!job) return "";
      const info = Object.fromEntries(row.triggers.map((t) => [t.id, t]));
      const last = job.last_run;
      return `<div class="sched">
        <div style="min-width:0">
          <h3>${esc(job.name)} ${row.enabled ? "" : `<span class="status cancelled">paused</span>`}
            ${job.busy ? `<span class="status running">running</span>` : ""}</h3>
          <div class="paths">${job.pairs.filter((p) => p.enabled).map((p) =>
            `${esc(p.left)} <span style="color:var(--amber)">${esc(VARIANT_LABEL[job.sync.variant])}</span> ${esc(p.right)}`).join("<br>")}</div>
          <div class="trig-pills">${job.schedule.triggers.map((t) => `
            <span class="trig-pill${t.enabled && row.enabled ? "" : " off"}" title="${esc(TRIGGER_TYPES[t.type]?.label || t.type)}">
              ${icon(TRIGGER_TYPES[t.type]?.icon || "clock")}${esc(describeTrigger(t, S.jobs))}
              ${info[t.id]?.watching ? `<span style="color:var(--ok)">●</span>` : ""}</span>`).join("")
            || `<span class="dim" style="font-size:13px">Manual only</span>`}</div>
          ${job.schedule.window_start && job.schedule.window_end ? `<div class="dim" style="font-size:12px;margin-top:8px">Allowed ${esc(job.schedule.window_start)}–${esc(job.schedule.window_end)}</div>` : ""}
          ${row.skip ? `<div class="faint" style="font-size:12px;margin-top:6px">Skipped ${rel(row.skip.when)}: ${esc(row.skip.why)}</div>` : ""}
        </div>
        <div class="sched-side">
          <div class="next-run">${row.next_run && row.enabled ? dtime(row.next_run) : "—"}
            <small>${row.next_run && row.enabled ? `next run ${rel(row.next_run)}` : "no timed trigger"}</small></div>
          ${last ? `<div><span class="status ${last.status}">${esc(last.status)}</span> <span class="dim" style="font-size:12px">${rel(last.finished || last.started)}</span></div>` : ""}
          <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end">
            <button class="btn sm" data-srun="${job.id}" ${job.busy ? "disabled" : ""}>${icon("play", "sm")} Run now</button>
            <button class="btn sm" data-sedit="${job.id}">${icon("gear", "sm")} Triggers</button>
            <button class="btn sm ghost" data-stoggle="${job.id}">${row.enabled ? "Pause" : "Resume"}</button>
          </div>
        </div>
      </div>`;
    }).join("")}`;
}

$("#scheduleList").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.srun) runJob(false, false, b.dataset.srun);
  else if (b.dataset.sedit) editJob("schedule", b.dataset.sedit);
  else if (b.dataset.stoggle) {
    const job = structuredClone(jobById(b.dataset.stoggle));
    job.schedule.enabled = !job.schedule.enabled;
    try { replaceJob(await put(`/api/jobs/${job.id}`, job)); renderSchedule(); toast(job.schedule.enabled ? "Automation resumed" : "Automation paused", "info"); }
    catch (err) { toast(err.message, "error"); }
  }
});

// ================================================================= history ==
async function renderHistory(append = false) {
  const h = S.history;
  const sel = $("#historyJob");
  const cur = sel.value;
  sel.innerHTML = `<option value="">All jobs</option>${S.jobs.map((j) => `<option value="${j.id}">${esc(j.name)}</option>`).join("")}`;
  sel.value = cur;
  h.job = sel.value;
  if (!append) { h.offset = 0; h.rows = []; }
  const PAGE = 60;
  let rows;
  try { rows = await get(`/api/runs?limit=${PAGE}&offset=${h.offset}${h.job ? `&job_id=${h.job}` : ""}`); }
  catch (e) { return toast(e.message, "error"); }
  h.rows.push(...rows);
  h.offset += rows.length;
  $("#historyMore").hidden = rows.length < PAGE;
  $("#runsTable").innerHTML = `
    <thead><tr><th>#</th><th>Job</th><th>Trigger</th><th>Status</th><th>Started</th><th>Duration</th>
      <th class="num">Copied</th><th class="num">Data</th><th class="num">Deleted</th><th class="num">Moved</th><th>Problems</th></tr></thead>
    <tbody>${h.rows.map((r) => {
      const st = r.stats || {};
      return `<tr data-run="${r.id}">
        <td class="num">${r.id}</td><td>${esc(r.job_name)}</td><td class="dim">${esc(r.trigger)}</td>
        <td><span class="status ${r.status}">${esc(r.status)}${r.dry_run ? " · dry" : ""}</span></td>
        <td title="${dtime(r.started)}">${rel(r.started)}</td>
        <td class="num">${r.finished ? dur(r.finished - r.started) : "…"}</td>
        ${r.dry_run && st.would_copy != null ? `<td class="num dim">${num(st.would_copy)}</td><td class="num dim">${bytes(st.would_copy_bytes)}</td>
          <td class="num dim">${num(st.would_delete)}</td><td class="num dim">${num(st.would_move)}</td>`
          : `<td class="num">${num(st.files_copied)}</td><td class="num">${bytes(st.bytes_copied)}</td>
          <td class="num">${num(st.files_deleted)}</td><td class="num">${num(st.moved)}</td>`}
        <td class="err" title="${esc(r.error)}">${r.error ? esc(r.error) : st.errors ? `${num(st.errors)} errors` : ""}</td>
      </tr>`;
    }).join("") || `<tr><td colspan="11" class="dim" style="text-align:center;padding:30px">No runs yet</td></tr>`}</tbody>`;
}
$("#runsTable").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-run]");
  if (tr) openLog(Number(tr.dataset.run));
});
$("#historyJob").onchange = () => renderHistory();
$("#historyMore").onclick = () => renderHistory(true);

// ================================================================ settings ==
async function renderSettings() {
  try { S.info = await get("/api/info"); } catch (e) { return toast(e.message, "error"); }
  const i = S.info;
  const origin = location.origin;
  $("#settingsBody").innerHTML = `
    <div class="settings-block"><dl class="kv">
      <dt>Version</dt><dd>Proto-Sync ${esc(i.version)}</dd>
      <dt>Transfer engine</dt><dd>${esc(i.rsync || "rsync NOT FOUND")}</dd>
      <dt>Allowed roots</dt><dd>${i.roots.map(esc).join("<br>")}</dd>
      <dt>Parallel runs</dt><dd>${i.max_runs} at a time (PROTOSYNC_MAX_RUNS)</dd>
      <dt>Time zone</dt><dd>${esc(i.timezone)} (set TZ)</dd>
      <dt>Authentication</dt><dd>${i.auth ? "HTTP basic auth enabled" : `<span style="color:var(--warn)">off</span> — set PROTOSYNC_USER / PROTOSYNC_PASSWORD, or keep it behind Tailscale`}</dd>
    </dl></div>

    <div class="settings-block">
      <div class="field"><label>Keep run history<small>Runs and their log files older than this are removed nightly.</small></label>
        <div class="control"><input type="number" class="input" id="setRet" min="1" value="${i.log_retention_days}" style="width:110px"><span class="unit">days</span>
          <button class="btn sm" id="setRetSave">Save</button></div></div>
      <div class="field"><label>Jobs<small>Export from the job menu; imported jobs start with triggers disabled.</small></label>
        <div class="control"><button class="btn sm" id="setImport">${icon("plus", "sm")} Import job…</button></div></div>
    </div>

    <div class="settings-block">
      <div class="field"><label>HTTP API<small>Everything in the UI is available over REST. Webhook URLs (per job, in the editor's Schedule tab) need no login.</small></label>
        <div class="control"><div class="codeblock" style="width:100%"># health
curl ${esc(origin)}/api/health

# run a job (id from the list)
curl ${esc(origin)}/api/jobs
curl -X POST ${esc(origin)}/api/jobs/&lt;id&gt;/run -H 'Content-Type: application/json' -d '{"dry_run": false}'

# follow live events
curl -N ${esc(origin)}/api/events</div></div></div>
    </div>

    <div class="settings-block"><dl class="kv">
      <dt>F5</dt><dd>Compare</dd>
      <dt>F9</dt><dd>Synchronize</dd>
      <dt>/</dt><dd>Search the comparison</dd>
      <dt>↑ ↓ PgUp PgDn Home End</dt><dd>Move in the grid (Shift extends the selection)</dd>
      <dt>Ctrl+A · Esc</dt><dd>Select all / clear selection</dd>
      <dt>Alt+→ · Alt+← · Alt+0 · Alt+D</dt><dd>Copy right · copy left · do nothing · default</dd>
      <dt>Shift+F10</dt><dd>Context menu</dd>
    </dl></div>`;
  $("#setRetSave").onclick = async () => {
    try { S.info = await put("/api/settings", { log_retention_days: Number($("#setRet").value) }); toast("Saved", "success"); }
    catch (e) { toast(e.message, "error"); }
  };
  $("#setImport").onclick = importJob;
}

// ================================================================== views ===
function setView(v) {
  if (!["compare", "schedule", "history", "settings"].includes(v)) v = "compare";
  S.view = v;
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === v));
  $$(".view").forEach((el) => (el.hidden = el.id !== `view-${v}`));
  if (location.hash !== `#${v}`) history.replaceState(null, "", `#${v}`);
  if (v === "schedule") renderSchedule();
  else if (v === "history") renderHistory();
  else if (v === "settings") renderSettings();
  else { grid.render(); refreshDrives(); }
}

// ================================================================= events ===
function wireStatic() {
  $$(".tab").forEach((t) => (t.onclick = () => setView(t.dataset.view)));
  $("#jobCurrent").onclick = (e) => { const r = e.currentTarget.getBoundingClientRect(); jobMenu(r.left, r.bottom + 6); };
  $("#btnCompare").onclick = startCompare;
  $("#btnSync").onclick = synchronize;
  $("#activity").onclick = () => { S.panelOpen = !S.panelOpen; renderRunPanel(); };
  $$("[data-open-editor]").forEach((b) => (b.onclick = () => S.job ? editJob(b.dataset.openEditor) : editJob("general", null)));

  $("#variantSwitch").onclick = async (e) => {
    const b = e.target.closest("[data-variant]");
    if (!b || !S.job) return;
    const v = b.dataset.variant;
    if (v === S.job.sync.variant) { if (v === "custom") editJob("sync"); return; }
    if (v === "two_way") {
      const ok = await confirmBox("Switch to two-way?",
        "Two-way sync remembers the state after each run to tell deletions from new files. The first run only copies in both directions — nothing is deleted until that history exists.",
        { ok: "Switch" });
      if (!ok) return;
    }
    const had = !!S.summary;
    const saved = await saveJob((j) => { j.sync.variant = v; }, { recompare: had });
    if (saved && v === "custom") editJob("sync");
  };

  $("#pairs").addEventListener("change", (e) => {
    const inp = e.target.closest("[data-path]");
    if (!inp) return;
    const [i, side] = inp.dataset.path.split(":");
    const val = inp.value.trim();
    if (val === S.job.pairs[Number(i)][side]) return;
    saveJob((j) => { j.pairs[Number(i)][side] = val; });
  });
  $("#pairs").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches("[data-path]")) e.target.blur(); });
  $("#pairs").addEventListener("click", async (e) => {
    const pick = e.target.closest("[data-pick]");
    if (pick) {
      const [i, side] = pick.dataset.pick.split(":");
      const got = await pickFolder(S.job.pairs[Number(i)][side], side === "left" ? "Left folder" : "Right folder");
      if (got) saveJob((j) => { j.pairs[Number(i)][side] = got; });
      return;
    }
    const sw = e.target.closest("[data-swap]");
    if (sw) {
      const i = Number(sw.dataset.swap);
      const ok = await confirmBox("Swap left and right?",
        S.job.sync.variant === "two_way" ? "This also clears the two-way history for this job." :
          `The right folder becomes the source. With <b>${esc(VARIANT_LABEL[S.job.sync.variant])}</b> that changes which side gets overwritten.`,
        { ok: "Swap" });
      if (ok) saveJob((j) => { const p = j.pairs[i]; [p.left, p.right] = [p.right, p.left]; });
      return;
    }
    const mk = e.target.closest("[data-mksent]");
    if (mk) {
      const c = S.drives[mk.dataset.mksent];
      const name = S.job.safety.sentinel_file || ".mounted";
      const ok = await confirmBox(`Create ${esc(name)}?`,
        `Only do this if <span class="mono">${esc(c.path)}</span> is really the mounted drive (${esc(c.fstype)}, ${bytes(c.usage?.total)}). The sentinel is how Proto-Sync tells a mounted drive from an empty mountpoint.`,
        { ok: "Create sentinel" });
      if (!ok) return;
      try { await post("/api/fs/sentinel", { path: c.path, name }); toast(`Created ${name}`, "success"); refreshDrives(); }
      catch (err) { toast(err.message, "error"); }
    }
  });

  $("#catChips").onclick = (e) => {
    const c = e.target.closest("[data-cat]");
    if (!c) return;
    const k = c.dataset.cat;
    S.hideCats.has(k) ? S.hideCats.delete(k) : S.hideCats.add(k);
    renderChips(); reloadGrid();
  };
  $("#actChips").onclick = (e) => {
    const c = e.target.closest("[data-act-chip]");
    if (!c) return;
    const k = c.dataset.actChip;
    S.hideActs.has(k) ? S.hideActs.delete(k) : S.hideActs.add(k);
    renderChips(); reloadGrid();
  };
  $("#showEqual").onchange = () => reloadGrid();
  $("#search").addEventListener("input", debounce(() => reloadGrid(), 220));

  $("#gridEmpty").addEventListener("click", (e) => {
    const b = e.target.closest("[data-act-empty]");
    if (!b) return;
    const a = b.dataset.actEmpty;
    if (a === "compare") startCompare();
    else if (a === "new") editJob("general", null);
    else if (a === "reset") { S.hideCats.clear(); S.hideActs.clear(); $("#search").value = ""; renderChips(); reloadGrid(); }
  });

  $("#statusbar").addEventListener("click", (e) => {
    if (!e.target.closest("#showErrors") || !S.summary) return;
    openModal(`<div class="modal-inner">
      <div class="modal-head"><h2>Scan warnings</h2><button class="btn ghost icon-only" data-close>${icon("x")}</button></div>
      <div class="modal-body"><p class="dim" style="margin-top:0">These items couldn't be read and are skipped. Usually permissions, broken links or a flaky drive.</p>
        <div class="logview">${S.summary.errors.map(esc).join("\n")}${S.summary.error_count > S.summary.errors.length ? `\n… ${S.summary.error_count - S.summary.errors.length} more in the run log` : ""}</div></div>
      <div class="modal-foot"><button class="btn" data-close>Close</button></div></div>`);
  });

  document.addEventListener("keydown", (e) => {
    if ($("#modal").open || document.querySelector("dialog[open]")) return;
    if (e.key === "F5") { e.preventDefault(); if (S.view === "compare") startCompare(); else setView("compare"); }
    else if (e.key === "F9") { e.preventDefault(); if (S.view === "compare") synchronize(); }
    else if (e.key === "/" && !e.target.closest("input, textarea, select") && S.view === "compare") { e.preventDefault(); $("#search").focus(); }
    else if (e.key === "Escape") closeMenu();
  });
  window.addEventListener("hashchange", () => setView(location.hash.slice(1)));

  setInterval(() => {
    if (document.visibilityState === "visible" && S.view === "compare") refreshDrives();
    if (S.summary) renderStatus();   // keeps "compared N min ago" fresh
  }, 30000);
}

const refreshJobsSoon = debounce(async () => {
  try { await loadJobs(); } catch { return; }
  if (S.view === "schedule") renderSchedule();
  if (S.view === "compare" && !S.summary) renderEmpty();
}, 400);

function wireEvents() {
  connectEvents({
    hello: (snap) => {
      for (const r of snap.runs) {
        const e = runEntry(r.run_id, { job_id: r.job_id, job_name: r.job_name, dry_run: r.dry_run, trigger: r.trigger });
        e.progress = r.progress || {};
        e.log = r.recent || [];
        e.done = null;
      }
      S.comparing = {};
      for (const c of snap.compares) S.comparing[c.job_id] = c.progress || { phase: "Comparing" };
      renderCompareBtn();
      renderEmpty();
      scheduleRender();
    },
    run_started: (d) => {
      runEntry(d.run_id, { job_id: d.job_id, dry_run: d.dry_run, trigger: d.trigger, job_name: jobById(d.job_id)?.name });
      S.panelOpen = S.panelOpen || d.trigger === "manual";
      scheduleRender();
      refreshJobsSoon();
    },
    run_progress: (p) => {
      if (p.run_id == null) return;
      const e = runEntry(p.run_id, { job_id: p.job_id, job_name: p.job_name });
      if (e.done) return;
      e.progress = p;
      scheduleRender();
    },
    run_log: (d) => {
      const e = S.runs.get(d.run_id);
      if (!e) return;
      e.log.push(d.line);
      if (e.log.length > 400) e.log.splice(0, e.log.length - 400);
      if (e.showLog) scheduleRender();
    },
    run_done: (run) => {
      const e = runEntry(run.id, { job_id: run.job_id, job_name: run.job_name, dry_run: run.dry_run });
      e.done = run;
      scheduleRender();
      const st = run.stats || {};
      const msg = {
        success: `${run.job_name}: ${run.dry_run ? `dry run — would copy ${num(st.would_copy)}, delete ${num(st.would_delete)}, move ${num(st.would_move)}` : `synced — ${num(st.files_copied)} copied, ${num(st.files_deleted)} deleted`}`,
        warning: `${run.job_name}: finished with ${num(st.errors)} error(s)`,
        blocked: `${run.job_name}: blocked — ${run.error}`,
        failed: `${run.job_name}: failed — ${run.error}`,
        cancelled: `${run.job_name}: stopped`,
      }[run.status] || `${run.job_name}: ${run.status}`;
      toast(msg, run.status === "success" ? "success" : run.status === "cancelled" || run.status === "warning" ? "info" : "error", 7000);
      if (run.status === "success" && !run.dry_run) {
        setTimeout(() => { if (S.runs.get(run.id)?.done) { S.runs.delete(run.id); scheduleRender(); } }, 20000);
      }
      if (run.job_id === S.job?.id && run.status === "success" && !run.dry_run) {
        S.summary = null;
        grid.clear();
        renderChips();
        renderStatus();
        refreshDrives();
      }
      if (run.status === "blocked" && run.trigger === "manual" && /override/i.test(run.error || "")) {
        S.runs.delete(run.id);
        scheduleRender();
        offerOverride(run);
      }
      refreshJobsSoon();
      if (S.view === "history") renderHistory();
    },
    compare_progress: (p) => {
      S.comparing[p.job_id] = p;
      if (p.job_id === S.job?.id) renderEmpty();
      scheduleRender();
    },
    compare_done: async (d) => {
      delete S.comparing[d.job_id];
      scheduleRender();
      if (d.job_id !== S.job?.id) { if (d.error) toast(d.error, "error"); return; }
      renderCompareBtn();
      if (d.error) { toast(`Compare failed: ${d.error}`, "error"); renderEmpty(); return; }
      if (d.summary.cancelled) { toast("Comparison cancelled", "info"); renderEmpty(); return; }
      S.summary = d.summary;
      renderChips();
      await reloadGrid();
      $("#grid").focus({ preventScroll: true });
      if (d.summary.error_count) toast(`${num(d.summary.error_count)} items couldn't be read — see the status bar`, "info");
    },
    toast: (d) => toast(d.text, d.level || "info"),
    schedule_changed: () => refreshJobsSoon(),
  }, (up) => {
    const was = S.connected;
    S.connected = up;
    renderStatus();
    if (up && !was) refreshJobsSoon();
  });
}

// =================================================================== boot ===
async function boot() {
  wireStatic();
  try {
    [S.info] = await Promise.all([get("/api/info"), loadJobs()]);
  } catch (e) {
    toast(`Can't reach Proto-Sync: ${e.message}`, "error");
  }
  wireEvents();
  const want = localStorage.getItem(LS.job);
  await selectJob(jobById(want) ? want : S.jobs[0]?.id);
  setView(location.hash.slice(1) || "compare");
}

boot();

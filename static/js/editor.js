// Job editor (side-tab modal), folder picker and trigger helpers.
import { get, post, put } from "./api.js";
import {
  $, $$, esc, bytes, dtime, rel, icon, toast, openModal, closeModal, getPath, setPath, debounce,
  ACTION_LABEL, CATEGORY_LABEL,
} from "./util.js";

export const DEFAULT_EXCLUDES = [
  ".mounted", ".Trash-*", "$RECYCLE.BIN/", "System Volume Information/", "lost+found/", "*.partial", ".DS_Store",
];
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const CUSTOM_CATS = ["left_only", "right_only", "left_newer", "right_newer", "different", "conflict"];
const CUSTOM_ACTS = ["copy_lr", "copy_rl", "delete_left", "delete_right", "none"];

export const TRIGGER_TYPES = {
  daily: { label: "Daily at a time", icon: "clock" },
  interval: { label: "Every N minutes", icon: "clock" },
  cron: { label: "Cron expression", icon: "clock" },
  once: { label: "Once at a date", icon: "clock" },
  startup: { label: "When Proto-Sync starts", icon: "play" },
  realtime: { label: "Real-time (files change)", icon: "bolt" },
  mount: { label: "When the drive connects", icon: "drive" },
  after_job: { label: "After another job", icon: "sync" },
};

// ------------------------------------------------------------ describe -----
export function describeTrigger(t, jobs = []) {
  const mode = t.mode === "dry_run" ? " · dry run" : "";
  switch (t.type) {
    case "daily": {
      const d = [...new Set(t.days || [])].sort();
      const days = d.length === 7 ? "every day" : d.length === 5 && d.every((x) => x < 5) ? "weekdays"
        : d.length === 2 && d[0] === 5 && d[1] === 6 ? "weekends" : d.map((x) => DAY_NAMES[x]).join(", ");
      return `${t.time || "03:00"} ${days}${mode}`;
    }
    case "interval": {
      const m = t.every_minutes || 0;
      return `every ${m % 1440 === 0 ? `${m / 1440} d` : m % 60 === 0 ? `${m / 60} h` : `${m} min`}${mode}`;
    }
    case "cron": return `cron ${t.cron}${mode}`;
    case "once": return `once ${t.at ? t.at.replace("T", " ").slice(0, 16) : "(no date)"}${mode}`;
    case "startup": return `on startup${mode}`;
    case "realtime": return `on change, ${t.idle_seconds}s idle${t.poll ? " (polling)" : ""}${mode}`;
    case "mount": return `when drives connect${mode}`;
    case "after_job": {
      const j = jobs.find((x) => x.id === t.after_job_id);
      return `after ${j ? j.name : "(pick a job)"} ${t.after_on === "any" ? "finishes" : `on ${t.after_on}`}${mode}`;
    }
    default: return t.type;
  }
}

// -------------------------------------------------------- folder picker ----
// Uses its own <dialog> so it can stack on top of the job editor.
export function pickFolder(initial = "", title = "Choose a folder") {
  return new Promise((resolve) => {
    const d = document.createElement("dialog");
    d.className = "modal narrow";
    document.body.appendChild(d);
    let current = "";
    let result = null;
    d.onclose = () => { d.remove(); resolve(result); };

    const draw = async (path) => {
      let data;
      try {
        data = await get(`/api/fs/browse?path=${encodeURIComponent(path)}`);
      } catch (e) {
        toast(e.message, "error");
        if (path) return draw("");
        return;
      }
      let vols = [];
      if (!data.path) { try { vols = await get("/api/fs/volumes"); } catch { /* optional */ } }
      current = data.path;
      const crumbs = [];
      if (data.path) {
        let acc = "";
        for (const part of data.path.split("/").filter(Boolean)) {
          acc += `/${part}`;
          crumbs.push(`<button data-go="${esc(acc)}">${esc(part)}</button><span class="faint">/</span>`);
        }
      }
      const usage = data.usage ? `${bytes(data.usage.free)} free of ${bytes(data.usage.total)} · ${esc(data.fstype || "")}` : "";
      d.innerHTML = `
        <div class="modal-inner">
          <div class="modal-head"><h2>${esc(title)}</h2><button class="btn ghost icon-only" data-x>${icon("x")}</button></div>
          <div class="modal-body">
            ${vols.length ? `<div class="vols">${vols.map((v) => `
              <button class="vol" data-go="${esc(v.mountpoint)}">
                <b>${icon("drive", "sm")} ${esc(v.mountpoint)}</b>
                <span>${esc(v.fstype)} · ${bytes(v.free)} free of ${bytes(v.total)}</span>
              </button>`).join("")}</div>` : ""}
            <div class="picker-crumbs">
              <button data-go="">${icon("drive", "sm")} roots</button><span class="faint">/</span>${crumbs.join("")}
            </div>
            <div class="picker-list">
              ${data.parent ? `<button data-go="${esc(data.parent)}">${icon("up")}<span>..</span></button>` : ""}
              ${data.dirs.map((x) => `<button data-go="${esc(x.path)}">${icon("folder")}<span>${esc(x.name)}</span></button>`).join("")
                || `<div class="dim" style="padding:12px">No sub-folders</div>`}
            </div>
            <div class="dim" style="margin-top:8px;font-size:12px">${usage}</div>
            ${data.path ? `<div class="copyline" style="margin-top:12px">
              <input class="input" id="pkNew" placeholder="New folder name"><button class="btn sm" id="pkMk">${icon("plus", "sm")} Create</button></div>` : ""}
          </div>
          <div class="modal-foot">
            <span class="mono dim" style="margin-right:auto;align-self:center;font-size:12px">${esc(data.path || "—")}</span>
            <button class="btn ghost" data-x>Cancel</button>
            <button class="btn primary" id="pkOk" ${data.path ? "" : "disabled"}>Use this folder</button>
          </div>
        </div>`;
      $$("[data-go]", d).forEach((b) => (b.onclick = () => draw(b.dataset.go)));
      $$("[data-x]", d).forEach((b) => (b.onclick = () => d.close()));
      $("#pkOk", d).onclick = () => { result = current; d.close(); };
      const mk = $("#pkMk", d);
      if (mk) mk.onclick = async () => {
        const name = $("#pkNew", d).value.trim();
        if (!name || name.includes("/")) return toast("Enter a plain folder name", "error");
        try {
          const r = await post("/api/fs/mkdir", { path: `${current}/${name}` });
          draw(r.path);
        } catch (e) { toast(e.message, "error"); }
      };
    };
    d.showModal();
    draw(initial || "");
  });
}

// ------------------------------------------------------- control helpers ---
const field = (label, hint, control) =>
  `<div class="field"><label>${label}${hint ? `<small>${hint}</small>` : ""}</label><div class="control">${control}</div></div>`;
const val = (s, p) => esc(getPath(s, p) ?? "");
const text = (s, p, attrs = "") => `<input type="text" data-bind="${p}" value="${val(s, p)}" ${attrs}>`;
const num = (s, p, attrs = "", type = "num") => `<input type="number" data-bind="${p}" data-type="${type}" value="${val(s, p)}" ${attrs}>`;
const check = (s, p, label, rr = false) =>
  `<label class="check"><input type="checkbox" data-bind="${p}" ${getPath(s, p) ? "checked" : ""} ${rr ? "data-rerender" : ""}> ${label}</label>`;
const select = (s, p, opts, rr = false) =>
  `<select data-bind="${p}" ${rr ? "data-rerender" : ""}>${opts.map(([v, t]) =>
    `<option value="${esc(v)}" ${String(getPath(s, p)) === String(v) ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>`;
const seg = (s, p, opts, rr = false) => `<div class="seg">${opts.map(([v, t, d]) => `
  <label><input type="radio" name="${p}" data-bind="${p}" data-type="radio" value="${v}" ${getPath(s, p) === v ? "checked" : ""}
    ${rr ? "data-rerender" : ""}><span><b>${t}</b>${d ? `<small>${d}</small>` : ""}</span></label>`).join("")}</div>`;
const lines = (s, p, rows = 7) =>
  `<textarea class="mono" rows="${rows}" data-bind="${p}" data-type="lines" spellcheck="false">${esc((getPath(s, p) || []).join("\n"))}</textarea>`;
const mb = (s, p) => {
  const v = getPath(s, p);
  return `<input type="number" min="0" step="any" data-bind="${p}" data-type="mb" value="${v == null ? "" : +(v / 1048576).toFixed(3)}" placeholder="off"><span class="unit">MB</span>`;
};

// ------------------------------------------------------------------ panes --
const PANES = {
  general: { label: "General", render: paneGeneral },
  compare: { label: "Compare", render: paneCompare },
  filter: { label: "Filter", render: paneFilter },
  sync: { label: "Synchronize", render: paneSync },
  safety: { label: "Safety", render: paneSafety },
  schedule: { label: "Schedule", render: paneSchedule },
  notify: { label: "Notify & hooks", render: paneNotify },
};

function paneGeneral(s) {
  return `<h3>General</h3>
    <p class="hint">Name the job and choose the folder pairs it keeps in sync. Each pair is compared and synchronized on its own; left is the source for one-way variants.</p>
    ${field("Name", "", text(s, "name"))}
    ${field("Description", "", text(s, "description", 'placeholder="optional"'))}
    <div class="field" style="grid-template-columns:1fr"><label>Folder pairs</label></div>
    <div class="card-list">
      ${s.pairs.map((p, i) => `
        <div class="subcard" data-pair="${i}">
          <div class="subcard-head">
            ${check(s, `pairs.${i}.enabled`, `<b>Pair ${i + 1}</b>`)}
            <span class="grow1"></span>
            <button class="btn ghost sm" data-swap-pair="${i}" title="Swap sides">${icon("swap", "sm")} Swap</button>
            ${s.pairs.length > 1 ? `<button class="btn ghost sm danger" data-del-pair="${i}">${icon("x", "sm")} Remove</button>` : ""}
          </div>
          ${["left", "right"].map((side) => field(side === "left" ? "Left (source)" : "Right (target)", "",
            `<div class="copyline"><input type="text" class="mono" data-bind="pairs.${i}.${side}" value="${val(s, `pairs.${i}.${side}`)}"
              placeholder="/mnt/…" data-check-side="${i}:${side}">
              <button class="btn sm" data-browse="${i}:${side}">${icon("folder", "sm")} Browse</button></div>
             <div class="drive-line dim" style="font-size:12px;width:100%" data-drive="${i}:${side}"></div>`)).join("")}
        </div>`).join("")}
    </div>
    <button class="btn sm" id="addPair" style="margin-top:10px">${icon("plus", "sm")} Add folder pair</button>`;
}

function paneCompare(s) {
  return `<h3>Comparison</h3>
    <p class="hint">How Proto-Sync decides whether two files are the same.</p>
    ${field("Compare by", "", seg(s, "compare.variant", [
      ["time_size", "File time and size", "Fast. Same size and modification time within the tolerance = identical."],
      ["content", "File content", "Reads both files byte-for-byte when sizes match. Slow, catches silent corruption."],
      ["size", "File size", "Only size matters. For destinations that don't keep times."],
    ]))}
    ${field("Time tolerance", "Differences up to this many seconds count as equal (FAT/exFAT/NTFS round times).",
      `${num(s, "compare.time_tolerance", 'min="0" step="0.5"')}<span class="unit">seconds</span>`)}
    ${field("Ignore time shift", "Whole-hour offsets treated as equal — e.g. 1 for daylight-saving drift on FAT drives.",
      `<input type="text" data-bind="compare.ignore_time_shift" data-type="ints" value="${esc((s.compare.ignore_time_shift || []).join(", "))}" placeholder="e.g. 1" style="width:160px">`)}
    ${field("Symbolic links", "", select(s, "compare.symlinks", [["direct", "Copy the link itself"], ["follow", "Follow to the target"], ["exclude", "Exclude links"]]))}
    ${field("Detect moved files", "Renames and moves are applied as a move instead of copy + delete. Uses size, time and a sample hash.",
      check(s, "compare.detect_moves", "Enabled"))}`;
}

function paneFilter(s) {
  return `<h3>Filter</h3>
    <p class="hint">FreeFileSync-style patterns, one per line. <span class="mono">*</span> matches within a name, <span class="mono">**</span> or no slash matches anywhere,
      a leading <span class="mono">/</span> anchors to the pair root, a trailing <span class="mono">/</span> matches folders only. Excluded items are never copied or deleted on either side.</p>
    ${field("Include", "Only items matching one of these are considered.", lines(s, "filter.include", 4))}
    ${field("Exclude", `<button class="linkish" id="resetExcl" type="button">Reset to media-backup defaults</button>`, lines(s, "filter.exclude", 9))}
    ${field("Minimum size", "Smaller files are ignored.", mb(s, "filter.min_size"))}
    ${field("Maximum size", "Larger files are ignored.", mb(s, "filter.max_size"))}
    ${field("Changed within", "Only files modified in the last N days.",
      `${num(s, "filter.max_age_days", 'min="0" step="any" placeholder="off"', "numnull")}<span class="unit">days</span>`)}
    ${field("Changed after", "Only files modified after this date.",
      `<input type="date" data-bind="filter.newer_than" data-type="textnull" value="${val(s, "filter.newer_than")}">`)}`;
}

function paneSync(s) {
  const v = s.sync.variant;
  const del = s.sync.deletion;
  return `<h3>Synchronization</h3>
    <p class="hint">What happens to each category of difference, and how deleted or overwritten files are handled.</p>
    ${field("Variant", "", seg(s, "sync.variant", [
      ["mirror", "Mirror →", "Right becomes an exact copy of left. Extra files on the right are removed."],
      ["update", "Update →", "Copy new and newer files to the right. Never delete."],
      ["two_way", "Two way ⇄", "Changes and deletions on either side are propagated, using the last sync as reference."],
      ["custom", "Custom", "Pick the action for every category yourself."],
    ], true))}
    ${v === "custom" ? field("Custom rules", "Category → action.", `<div class="custom-table">${CUSTOM_CATS.map((c) => `
        <span class="cat"><span class="cat-dot cat-${c}"></span>${CATEGORY_LABEL[c]}</span>
        ${select(s, `sync.custom.${c}`, CUSTOM_ACTS.map((a) => [a, ACTION_LABEL[a]]))}`).join("")}</div>`) : ""}
    ${v === "two_way" ? field("Conflicts", "When both sides changed since the last sync.", select(s, "sync.conflict", [
      ["skip", "Leave them, show for review"], ["newer", "Newer file wins"], ["left", "Left always wins"], ["right", "Right always wins"]])) : ""}
    ${field("Deleted & overwritten files", "", seg(s, "sync.deletion", [
      ["recycle", "Recycle bin", "Moved to a hidden .protosync-trash folder on the same drive, purged after the retention period."],
      ["versioning", "Versioning", "Kept in a separate folder with timestamps — a rolling file history."],
      ["permanent", "Delete permanently", "Gone immediately, like the original script."],
    ], true))}
    ${del === "recycle" ? field("Keep recycled files", "", `${num(s, "sync.recycle_retention_days", 'min="0"')}<span class="unit">days (0 = forever)</span>`) : ""}
    ${del === "versioning" ? `
      ${field("Versioning folder", "Must be outside both sides. Each pair gets its own sub-folder.",
        `<div class="copyline"><input type="text" class="mono" data-bind="sync.versioning_path" value="${val(s, "sync.versioning_path")}" placeholder="/mnt/versions">
         <button class="btn sm" data-browse-vers>${icon("folder", "sm")} Browse</button></div>`)}
      ${field("Naming", "", select(s, "sync.versioning_style", [
        ["timestamp_folder", "Time-stamped folder per run"], ["timestamp_file", "Time-stamp appended to each file"], ["replace", "Replace (keep latest only)"]]))}
      ${field("Keep versions", "Remove versions older than N days, but always keep at least the minimum.",
        `${num(s, "sync.versioning_max_age_days", 'min="0"')}<span class="unit">days max (0 = forever)</span>
         ${num(s, "sync.versioning_keep_min", 'min="0"')}<span class="unit">min</span>
         ${num(s, "sync.versioning_keep_max", 'min="0"')}<span class="unit">max (0 = unlimited)</span>`)}` : ""}
    ${field("Deletion timing", "After is safest: nothing is removed until all copies succeeded (rsync --delete-after).",
      select(s, "sync.delete_timing", [["after", "After copying"], ["before", "Before copying (frees space first)"]]))}
    ${field("Permissions & owners", "Auto skips them on NTFS/exFAT/FAT/CIFS, which avoids endless false changes.",
      select(s, "sync.preserve_permissions", [["auto", "Auto (by file system)"], ["yes", "Always preserve"], ["no", "Never preserve"]]))}
    ${field("Extended attributes", "", check(s, "sync.preserve_xattrs", "Copy xattrs"))}
    ${field("Empty folders", "", check(s, "sync.prune_empty_dirs", "Remove folders that become empty"))}
    ${field("Verify copies", "Re-read data with checksums after copying. Full re-checks every file on both sides (very slow on large drives).",
      seg(s, "sync.verify", [["off", "Off", "Size + time check only"], ["copied", "Copied files", "Checksum everything just written"], ["full", "Full", "Checksum the whole tree"]]))}
    ${field("Bandwidth limit", "", `${num(s, "sync.bandwidth_limit_kbps", 'min="0"')}<span class="unit">KiB/s (0 = unlimited)</span>`)}
    ${field("Resume interrupted copies", "Keep partial transfers in .protosync-partial and continue next run.", check(s, "sync.resume_partial", "Enabled"))}`;
}

function paneSafety(s) {
  return `<h3>Safety</h3>
    <p class="hint">Guards that refuse to run rather than damage a backup. A blocked run changes nothing and says why.</p>
    ${field("Sentinel file", "A file that proves the drive is really mounted, not an empty mountpoint.",
      `${text(s, "safety.sentinel_file", 'style="width:200px" class="mono"')} ${check(s, "safety.require_sentinel", "Required on both sides")}`)}
    ${field("Require mountpoint", "Each side must live on a different device than the root file system.",
      check(s, "safety.require_mountpoint", "Enabled"))}
    ${field("Empty source guard", "Refuse to mirror when the source is empty but the target is not.", check(s, "safety.empty_source_guard", "Enabled"))}
    ${field("Max deletions", "Block if a run would delete more than this share or count of files. You can override after reviewing.",
      `${num(s, "safety.max_delete_percent", 'min="0" max="100" step="any"')}<span class="unit">% (0 = off)</span>
       ${num(s, "safety.max_delete_count", 'min="0"')}<span class="unit">files (0 = off)</span>`)}
    ${field("Free space", "", check(s, "safety.check_free_space", "Check the target has room before copying"))}
    ${field("Retries", "rsync is retried on transient errors (I/O, vanished files, timeouts).",
      `${num(s, "safety.retries", 'min="0" max="10"')}<span class="unit">times, wait</span>${num(s, "safety.retry_delay", 'min="0"')}<span class="unit">s</span>`)}
    ${field("On error", "", select(s, "safety.on_error", [["continue", "Continue with other pairs"], ["stop", "Stop the whole run"]]))}
    ${field("Flush to disk", "Run sync() before declaring success.", check(s, "safety.flush_to_disk", "Enabled"))}
    ${field("Watch drives during run", "Abort if a drive disappears or stops responding mid-transfer.", check(s, "safety.watch_mount_during_run", "Enabled"))}`;
}

function triggerFields(s, i, t, jobs) {
  const p = `schedule.triggers.${i}`;
  const rows = [];
  switch (t.type) {
    case "daily":
      rows.push(field("Time", "", `<input type="time" data-bind="${p}.time" value="${esc(t.time)}">`));
      rows.push(field("Days", "", `<div class="days">${DAY_NAMES.map((n, d) =>
        `<label><input type="checkbox" data-bind="${p}.days" data-type="day" data-day="${d}" ${t.days.includes(d) ? "checked" : ""}>${n}</label>`).join("")}</div>`));
      break;
    case "interval":
      rows.push(field("Every", "", `${num(s, `${p}.every_minutes`, 'min="1"')}<span class="unit">minutes</span>`));
      break;
    case "cron":
      rows.push(field("Expression", "minute hour day month weekday", text(s, `${p}.cron`, 'class="mono" style="width:220px"')));
      break;
    case "once":
      rows.push(field("At", "", `<input type="datetime-local" data-bind="${p}.at" data-type="textnull" value="${esc((t.at || "").slice(0, 16))}">`));
      break;
    case "realtime":
      rows.push(field("Idle time", "Waits until nothing has changed for this long, then syncs once.",
        `${num(s, `${p}.idle_seconds`, 'min="1"')}<span class="unit">seconds</span>`));
      rows.push(field("Polling", "Use on network shares and FUSE mounts where inotify doesn't see changes.",
        check(s, `${p}.poll`, "Poll instead of inotify")));
      break;
    case "mount":
      rows.push(`<p class="dim" style="font-size:12.5px;margin:6px 0">Checks every 20 s. Fires once when every enabled folder (and its sentinel) becomes available — plug in the archive drive and it backs itself up.</p>`);
      break;
    case "startup":
      rows.push(`<p class="dim" style="font-size:12.5px;margin:6px 0">Runs about a minute after the container or service starts — catches up after reboots.</p>`);
      break;
    case "after_job":
      rows.push(field("Job", "", select(s, `${p}.after_job_id`,
        [["", "— choose —"], ...jobs.filter((j) => j.id !== s.id).map((j) => [j.id, j.name])])));
      rows.push(field("When it", "", select(s, `${p}.after_on`, [["success", "succeeds"], ["failure", "fails"], ["any", "finishes"]])));
      break;
  }
  if (["daily", "interval", "cron", "once"].includes(t.type)) {
    rows.push(field("Random delay", "Spreads jobs that share a time.", `${num(s, `${p}.jitter_seconds`, 'min="0"')}<span class="unit">seconds max</span>`));
  }
  return rows.join("");
}

function paneSchedule(s, ctx) {
  const hook = s.id && s.webhook_token ? `${location.origin}/api/hooks/${s.id}?token=${s.webhook_token}` : "";
  return `<h3>Schedule & automation</h3>
    <p class="hint">Any number of triggers per job. Scheduled runs compare fresh and sync without a preview, still behind every safety guard.</p>
    ${field("Automation", "", check(s, "schedule.enabled", "Enabled for this job"))}
    ${field("Allowed hours", "Scheduled runs outside this window are skipped. Leave empty for any time. Overnight windows (22:00–06:00) work.",
      `<input type="time" data-bind="schedule.window_start" value="${val(s, "schedule.window_start")}"> <span class="unit">to</span>
       <input type="time" data-bind="schedule.window_end" value="${val(s, "schedule.window_end")}">`)}
    ${field("If already running", "", check(s, "schedule.queue_if_running", "Queue one more run instead of skipping"))}
    <div class="field" style="grid-template-columns:1fr"><label>Triggers</label></div>
    <div class="card-list">
      ${s.schedule.triggers.map((t, i) => `
        <div class="subcard" data-trig="${i}">
          <div class="subcard-head">
            ${check(s, `schedule.triggers.${i}.enabled`, "")}
            ${select(s, `schedule.triggers.${i}.type`, Object.entries(TRIGGER_TYPES).map(([k, v]) => [k, v.label]), true)}
            ${select(s, `schedule.triggers.${i}.mode`, [["sync", "Synchronize"], ["dry_run", "Dry run only"]])}
            <span class="grow1"></span>
            <span class="dim" style="font-size:12px">${ctx.watching?.[t.id] ? `${icon("bolt", "sm")} watching` : ""}</span>
            <button class="btn ghost sm danger" data-del-trig="${i}">${icon("x", "sm")}</button>
          </div>
          ${triggerFields(s, i, t, ctx.jobs)}
          <div class="next-times" data-next="${i}"></div>
        </div>`).join("") || `<div class="dim">No triggers yet — this job only runs when you press Synchronize.</div>`}
    </div>
    <button class="btn sm" id="addTrig" style="margin-top:10px">${icon("plus", "sm")} Add trigger</button>
    <div class="field" style="margin-top:18px">
      <label>Webhook<small>Start this job from anything that can make an HTTP request: other scripts, Home Assistant, a cron on another box. Add <span class="mono">&amp;dry_run=true</span> for a preview run.</small></label>
      <div class="control">${hook ? `
        <div class="copyline"><input class="input" readonly value="${esc(hook)}" id="hookUrl"><button class="btn sm" id="hookCopy">Copy</button>
          <button class="btn sm ghost" id="hookRotate" title="Invalidate the old URL">Rotate</button></div>
        <div class="codeblock" style="width:100%">curl -fsS -X POST '${esc(hook)}'</div>` : `<span class="dim">Save the job first.</span>`}
      </div>
    </div>`;
}

function targetFields(s, i, t) {
  const p = `notify.targets.${i}`;
  const f = [];
  if (t.type === "ntfy") {
    f.push(field("Server", "", text(s, `${p}.url`, 'placeholder="https://ntfy.sh" class="mono"')));
    f.push(field("Topic", "", text(s, `${p}.topic`, 'class="mono"')));
    f.push(field("Token", "optional", `<input type="password" data-bind="${p}.token" value="${val(s, `${p}.token`)}">`));
  } else if (t.type === "discord") {
    f.push(field("Webhook URL", "", text(s, `${p}.url`, 'class="mono" placeholder="https://discord.com/api/webhooks/…"')));
  } else if (t.type === "gotify") {
    f.push(field("Server", "", text(s, `${p}.url`, 'class="mono"')));
    f.push(field("App token", "", `<input type="password" data-bind="${p}.token" value="${val(s, `${p}.token`)}">`));
  } else if (t.type === "webhook") {
    f.push(field("URL", "Receives a JSON POST with the run result.", text(s, `${p}.url`, 'class="mono"')));
    f.push(field("Bearer token", "optional", `<input type="password" data-bind="${p}.token" value="${val(s, `${p}.token`)}">`));
  } else if (t.type === "email") {
    f.push(field("SMTP server", "", `${text(s, `${p}.smtp_host`, 'style="width:220px"')} ${num(s, `${p}.smtp_port`, 'style="width:90px"')}
      ${select(s, `${p}.smtp_security`, [["starttls", "STARTTLS"], ["ssl", "SSL/TLS"], ["none", "None"]])}`));
    f.push(field("Login", "", `${text(s, `${p}.smtp_user`, 'style="width:200px" placeholder="user"')}
      <input type="password" data-bind="${p}.smtp_password" value="${val(s, `${p}.smtp_password`)}" placeholder="password">`));
    f.push(field("From / To", "", `${text(s, `${p}.email_from`, 'style="width:220px"')} ${text(s, `${p}.email_to`, 'style="width:220px"')}`));
  }
  return f.join("");
}

function paneNotify(s) {
  return `<h3>Notifications & hooks</h3>
    <p class="hint">Get told when something happens, and run your own commands around each sync.</p>
    ${field("Notify when", "", select(s, "notify.when", [
      ["failure", "A run fails, is blocked or has errors"], ["changes", "Files actually changed (or failure)"], ["always", "Every run"], ["never", "Never"]]))}
    <div class="card-list">
      ${s.notify.targets.map((t, i) => `
        <div class="subcard">
          <div class="subcard-head">
            ${check(s, `notify.targets.${i}.enabled`, "")}
            ${select(s, `notify.targets.${i}.type`, [["ntfy", "ntfy"], ["discord", "Discord"], ["gotify", "Gotify"], ["webhook", "Webhook (JSON)"], ["email", "Email"]], true)}
            <span class="grow1"></span>
            <button class="btn sm" data-test-target="${i}">Send test</button>
            <button class="btn ghost sm danger" data-del-target="${i}">${icon("x", "sm")}</button>
          </div>
          ${targetFields(s, i, t)}
        </div>`).join("")}
    </div>
    <button class="btn sm" id="addTarget" style="margin:10px 0 18px">${icon("plus", "sm")} Add notification target</button>
    ${field("Before sync", "Shell command run first (e.g. spin up disks, stop a container). Non-zero exit can abort.",
      `<textarea class="mono" rows="2" data-bind="notify.pre_command" spellcheck="false">${val(s, "notify.pre_command")}</textarea>
       ${check(s, "notify.pre_command_abort", "Abort the run if it fails")}`)}
    ${field("After sync", "Environment: PROTOSYNC_JOB, _STATUS, _FILES_COPIED, _BYTES_COPIED, _FILES_DELETED, _ERRORS, _CHANGED, _LOG.",
      `<textarea class="mono" rows="2" data-bind="notify.post_command" spellcheck="false">${val(s, "notify.post_command")}</textarea>
       ${select(s, "notify.post_command_when", [["success", "on success"], ["failure", "on failure"], ["always", "always"]])}`)}
    ${field("Command timeout", "", `${num(s, "notify.command_timeout", 'min="5"')}<span class="unit">seconds</span>`)}`;
}

// ------------------------------------------------------------------ editor -
/**
 * Open the job editor.
 * @param {object|null} job  full job object (null = new job)
 * @param {string} tab       initial pane
 * @param {object} ctx       { jobs, onSaved(job) }
 */
export function openEditor(job, tab = "general", ctx = {}) {
  const isNew = !job?.id;
  const s = structuredClone(job || {});
  if (isNew) Object.assign(s, freshJob());
  const watching = {};
  for (const ti of job?.triggers_info || []) watching[ti.id] = ti.watching;
  const pctx = { jobs: ctx.jobs || [], watching };
  let pane = PANES[tab] ? tab : "general";
  let dirty = false;

  const d = openModal(`
    <div class="modal-inner">
      <div class="modal-head"><h2>${isNew ? "New job" : `Edit — ${esc(s.name)}`}</h2>
        <button class="btn ghost icon-only" data-close>${icon("x")}</button></div>
      <div class="editor" style="min-height:0">
        <nav class="editor-nav">${Object.entries(PANES).map(([k, v]) => `<button data-pane="${k}">${v.label}</button>`).join("")}</nav>
        <div class="editor-pane" id="edPane"></div>
      </div>
      <div class="modal-foot">
        <span class="dim" id="edDirty" style="margin-right:auto;align-self:center;font-size:12.5px"></span>
        <button class="btn ghost" data-close>Cancel</button>
        <button class="btn primary" id="edSave">${isNew ? "Create job" : "Save"}</button>
      </div>
    </div>`, { onClose: () => { $("#modal").oncancel = null; } });
  const paneEl = $("#edPane", d);

  const markDirty = () => { dirty = true; $("#edDirty", d).textContent = "Unsaved changes"; };

  const draw = () => {
    $$(".editor-nav button", d).forEach((b) => b.classList.toggle("on", b.dataset.pane === pane));
    const top = paneEl.scrollTop;
    paneEl.innerHTML = PANES[pane].render(s, pctx);
    paneEl.scrollTop = top;
    wire();
  };

  const previews = debounce(async () => {
    if (pane !== "schedule") return;
    for (const el of $$("[data-next]", paneEl)) {
      const t = s.schedule.triggers[Number(el.dataset.next)];
      if (!t) continue;
      if (!["daily", "interval", "cron"].includes(t.type)) {
        el.textContent = t.type === "once" && t.at ? `Fires ${rel(new Date(t.at).getTime() / 1000)}` : "";
        continue;
      }
      try {
        const r = await post("/api/schedule/preview", t);
        el.innerHTML = r.times.length ? `Next: ${r.times.map((x) => esc(dtime(x))).join("  ·  ")}` : "Never fires";
        el.style.color = "";
      } catch (e) {
        el.textContent = e.message;
        el.style.color = "var(--del)";
      }
    }
  }, 350);

  const checkDrives = debounce(async () => {
    if (pane !== "general") return;
    const sentinel = s.safety?.sentinel_file || ".mounted";
    for (const el of $$("[data-drive]", paneEl)) {
      const [i, side] = el.dataset.drive.split(":");
      const path = s.pairs[Number(i)]?.[side];
      if (!path) { el.innerHTML = ""; continue; }
      try {
        const c = await get(`/api/fs/check?path=${encodeURIComponent(path)}&sentinel=${encodeURIComponent(sentinel)}`);
        el.innerHTML = driveLine(c, sentinel);
        const b = $("[data-mk-sentinel]", el);
        if (b) b.onclick = async () => {
          try { await post("/api/fs/sentinel", { path: c.path, name: sentinel }); toast(`Created ${sentinel}`, "success"); checkDrives(); }
          catch (e) { toast(e.message, "error"); }
        };
      } catch (e) { el.textContent = e.message; }
    }
  }, 400);

  function wire() {
    const on = (sel, fn) => { const el = $(sel, paneEl); if (el) el.onclick = fn; };
    // lists
    on("#addPair", () => { s.pairs.push({ left: "", right: "", enabled: true }); markDirty(); draw(); });
    $$("[data-del-pair]", paneEl).forEach((b) => (b.onclick = () => { s.pairs.splice(Number(b.dataset.delPair), 1); markDirty(); draw(); }));
    $$("[data-swap-pair]", paneEl).forEach((b) => (b.onclick = () => {
      const p = s.pairs[Number(b.dataset.swapPair)];
      [p.left, p.right] = [p.right, p.left];
      markDirty(); draw();
    }));
    $$("[data-browse]", paneEl).forEach((b) => (b.onclick = async () => {
      const [i, side] = b.dataset.browse.split(":");
      const got = await pickFolder(s.pairs[Number(i)][side], `${side === "left" ? "Left" : "Right"} folder`);
      if (got) { s.pairs[Number(i)][side] = got; markDirty(); draw(); }
    }));
    on("[data-browse-vers]", async () => {
      const got = await pickFolder(s.sync.versioning_path, "Versioning folder");
      if (got) { s.sync.versioning_path = got; markDirty(); draw(); }
    });
    on("#resetExcl", () => { s.filter.exclude = [...DEFAULT_EXCLUDES]; markDirty(); draw(); });
    on("#addTrig", () => { s.schedule.triggers.push(freshTrigger()); markDirty(); draw(); });
    $$("[data-del-trig]", paneEl).forEach((b) => (b.onclick = () => { s.schedule.triggers.splice(Number(b.dataset.delTrig), 1); markDirty(); draw(); }));
    on("#addTarget", () => { s.notify.targets.push(freshTarget()); markDirty(); draw(); });
    $$("[data-del-target]", paneEl).forEach((b) => (b.onclick = () => { s.notify.targets.splice(Number(b.dataset.delTarget), 1); markDirty(); draw(); }));
    $$("[data-test-target]", paneEl).forEach((b) => (b.onclick = async () => {
      b.disabled = true;
      try { await post("/api/notify/test", s.notify.targets[Number(b.dataset.testTarget)]); toast("Test notification sent", "success"); }
      catch (e) { toast(e.message, "error"); }
      b.disabled = false;
    }));
    on("#hookCopy", async () => {
      const u = $("#hookUrl", paneEl);
      try { await navigator.clipboard.writeText(u.value); } catch { u.select(); document.execCommand("copy"); }
      toast("Webhook URL copied", "success");
    });
    on("#hookRotate", async () => {
      try { const r = await post(`/api/jobs/${s.id}/token`); s.webhook_token = r.webhook_token; draw(); toast("Old webhook URL no longer works", "info"); }
      catch (e) { toast(e.message, "error"); }
    });
    previews();
    checkDrives();
  }

  const onInput = (e) => {
    const el = e.target.closest("[data-bind]");
    if (!el) return;
    const path = el.dataset.bind;
    const type = el.dataset.type || (el.type === "checkbox" ? "bool" : "text");
    let v;
    switch (type) {
      case "bool": v = el.checked; break;
      case "num": v = el.value === "" ? 0 : Number(el.value); break;
      case "numnull": v = el.value === "" ? null : Number(el.value); break;
      case "mb": v = el.value === "" ? null : Math.round(Number(el.value) * 1048576); break;
      case "lines": v = el.value.split("\n").map((x) => x.trim()).filter(Boolean); break;
      case "ints": v = el.value.split(/[,\s]+/).filter(Boolean).map(Number).filter(Number.isFinite); break;
      case "textnull": v = el.value || null; break;
      case "radio": if (!el.checked) return; v = el.value; break;
      case "day": {
        const arr = new Set(getPath(s, path) || []);
        el.checked ? arr.add(Number(el.dataset.day)) : arr.delete(Number(el.dataset.day));
        v = [...arr].sort();
        break;
      }
      default: v = el.value;
    }
    setPath(s, path, v);
    markDirty();
    if (e.type === "change" && el.hasAttribute("data-rerender")) draw();
    if (path.startsWith("schedule.triggers")) previews();
    if (path.startsWith("pairs.") || path === "safety.sentinel_file") checkDrives();
    if (path === "name") $(".modal-head h2", d).textContent = isNew ? "New job" : `Edit — ${s.name}`;
  };
  paneEl.addEventListener("input", onInput);
  paneEl.addEventListener("change", onInput);

  $$(".editor-nav button", d).forEach((b) => (b.onclick = () => { pane = b.dataset.pane; paneEl.scrollTop = 0; draw(); }));
  d.oncancel = (e) => {
    if (dirty && !window.confirm("Discard unsaved changes?")) e.preventDefault();
  };
  $$("[data-close]", d).forEach((b) => (b.onclick = () => {
    if (!dirty || window.confirm("Discard unsaved changes?")) d.close();
  }));

  $("#edSave", d).onclick = async () => {
    const problem = validate(s);
    if (problem) { toast(problem.msg, "error"); pane = problem.pane; draw(); return; }
    const btn = $("#edSave", d);
    btn.disabled = true;
    try {
      const saved = isNew ? await post("/api/jobs", s) : await put(`/api/jobs/${s.id}`, s);
      dirty = false;
      closeModal();
      toast(isNew ? "Job created" : "Job saved", "success");
      ctx.onSaved?.(saved);
    } catch (e) {
      toast(e.message, "error");
      btn.disabled = false;
    }
  };
  draw();
}

function driveLine(c, sentinel) {
  if (!c.allowed) return `<span style="color:var(--del)">Outside the allowed roots</span>`;
  if (!c.exists) return `<span style="color:var(--del)">Folder does not exist</span>`;
  const parts = [`${esc(c.fstype || "?")}`];
  if (c.usage) parts.push(`${bytes(c.usage.free)} free of ${bytes(c.usage.total)}`);
  if (c.system_disk) parts.push(`<span style="color:var(--warn)">on the system disk</span>`);
  parts.push(c.sentinel ? `<span style="color:var(--ok)">${esc(sentinel)} found</span>`
    : `<span style="color:var(--del)">no ${esc(sentinel)}</span> <button class="linkish" type="button" data-mk-sentinel>create it</button>`);
  return parts.join(" · ");
}

function validate(s) {
  if (!s.name?.trim()) return { pane: "general", msg: "Give the job a name" };
  const active = s.pairs.filter((p) => p.enabled);
  if (!active.length) return { pane: "general", msg: "Enable at least one folder pair" };
  for (const p of active) {
    if (!p.left || !p.right) return { pane: "general", msg: "Every enabled pair needs a left and a right folder" };
    if (!p.left.startsWith("/") || !p.right.startsWith("/")) return { pane: "general", msg: "Folder paths must be absolute" };
  }
  if (s.sync.deletion === "versioning" && !s.sync.versioning_path) return { pane: "sync", msg: "Choose a versioning folder" };
  for (const t of s.schedule.triggers) {
    if (t.type === "after_job" && !t.after_job_id) return { pane: "schedule", msg: "Pick the job an 'after job' trigger waits for" };
    if (t.type === "once" && !t.at) return { pane: "schedule", msg: "Set a date for the one-time trigger" };
  }
  return null;
}

function freshTrigger() {
  return {
    type: "daily", enabled: true, mode: "sync", cron: "0 3 * * *", every_minutes: 360, time: "03:00",
    days: [0, 1, 2, 3, 4, 5, 6], at: null, idle_seconds: 30, poll: false, after_job_id: "", after_on: "success", jitter_seconds: 0,
  };
}

function freshTarget() {
  return {
    type: "ntfy", enabled: true, url: "https://ntfy.sh", topic: "", token: "", smtp_host: "", smtp_port: 587,
    smtp_security: "starttls", smtp_user: "", smtp_password: "", email_from: "", email_to: "",
  };
}

export function freshJob() {
  const mirror = { left_only: "copy_lr", right_only: "delete_right", left_newer: "copy_lr", right_newer: "copy_lr", different: "copy_lr", conflict: "copy_lr", equal: "none" };
  return {
    name: "New job", description: "",
    pairs: [{ left: "", right: "", enabled: true }],
    compare: { variant: "time_size", time_tolerance: 2, ignore_time_shift: [], symlinks: "direct", detect_moves: true },
    filter: { include: ["*"], exclude: [...DEFAULT_EXCLUDES], min_size: null, max_size: null, max_age_days: null, newer_than: null },
    sync: {
      variant: "mirror", custom: mirror, conflict: "skip", deletion: "recycle", recycle_retention_days: 14,
      versioning_path: "", versioning_style: "timestamp_folder", versioning_max_age_days: 0, versioning_keep_min: 1,
      versioning_keep_max: 0, delete_timing: "after", preserve_permissions: "auto", preserve_xattrs: false,
      prune_empty_dirs: true, verify: "off", bandwidth_limit_kbps: 0, resume_partial: true,
    },
    safety: {
      sentinel_file: ".mounted", require_sentinel: true, require_mountpoint: false, empty_source_guard: true,
      max_delete_percent: 50, max_delete_count: 0, check_free_space: true, retries: 2, retry_delay: 15,
      on_error: "continue", flush_to_disk: true, watch_mount_during_run: true,
    },
    schedule: { enabled: true, triggers: [], window_start: "", window_end: "", queue_if_running: true },
    notify: { when: "failure", targets: [], pre_command: "", pre_command_abort: true, post_command: "", post_command_when: "success", command_timeout: 600 },
  };
}

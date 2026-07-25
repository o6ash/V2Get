"use strict";

// ── helpers ──────────────────────────────────────────────────────────────────
const api = {
  async get(path) { return this._req("GET", path); },
  async post(path, body) { return this._req("POST", path, body); },
  async put(path, body) { return this._req("PUT", path, body); },
  async patch(path, body) { return this._req("PATCH", path, body); },
  async del(path) { return this._req("DELETE", path); },
  async _req(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    const res = await fetch("/api" + path, opts);
    if (!res.ok) { let m = res.statusText; try { m = (await res.json()).detail || m; } catch {} throw new Error(m); }
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res.text();
  },
};

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) { if (kid != null) e.append(kid.nodeType ? kid : document.createTextNode(kid)); }
  return e;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let toastTimer;
function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), 3200);
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString();
}
function fmtAgo(iso) {
  if (!iso) return "never";
  const s = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}
function fmtDur(sec) {
  if (sec <= 0) return "expired";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return `${h}h ${m}m`;
}
async function copy(text) { try { await navigator.clipboard.writeText(text); toast("Copied"); } catch { toast("Copy failed", true); } }
function download(name, text) {
  const a = el("a", { href: URL.createObjectURL(new Blob([text], { type: "text/plain" })), download: name });
  a.click(); URL.revokeObjectURL(a.href);
}

const card = (value, label, sub) => el("div", { class: "card stat" },
  el("div", { class: "value" }, String(value)),
  el("div", { class: "label" }, label),
  sub ? el("div", { class: "sub" }, sub) : null);

// ── topbar status polling ─────────────────────────────────────────────────────
async function refreshStatus() {
  try {
    const o = await api.get("/overview");
    const s = o.scheduler;
    const chip = $("#chip-status");
    chip.textContent = "Status: " + (s.status === "running" ? "Running" : "Idle");
    chip.classList.toggle("running", s.status === "running");
    $("#chip-last").textContent = "Last: " + fmtAgo(s.last_run);
    $("#chip-next").textContent = "Next: " + (s.next_run ? fmtTime(s.next_run).split(",")[1]?.trim() || fmtTime(s.next_run) : "—");
  } catch { /* ignore transient */ }
}

// ── page renderers ─────────────────────────────────────────────────────────────
const pages = {};

pages.overview = async (root) => {
  const o = await api.get("/overview");
  const s = o.stats, sch = o.scheduler, gh = o.github, dup = o.duplicates;
  root.append(
    el("h1", {}, "Overview"),
    !o.telegram_configured ? el("div", { class: "card", style: "border-color:var(--warn);margin-bottom:16px" },
      el("span", { class: "muted" }, "⚠ Telegram not configured — set TELEGRAM_API_ID / API_HASH / SESSION in .env to collect from channels. The rest of the platform works regardless.")) : null,
    el("div", { class: "grid section" },
      card(s.active_count, "Active Configs"),
      card(s.archive_count, "Archive Size"),
      card(s.cooldown_count, "In Cooldown"),
      card(s.alive_count, "Alive"),
      card(s.failed_count, "Failed"),
      card(s.success_rate + "%", "Success Rate"),
      card(dup.total_duplicates_removed, "Duplicates Removed", dup.duplicate_ratio + "% ratio"),
    ),
    el("div", { class: "grid section" },
      el("div", { class: "card" },
        el("h2", {}, "Collector"),
        el("div", {}, "Status: ", el("b", {}, sch.status)),
        el("div", { class: "muted" }, "Last run: " + fmtTime(sch.last_run)),
        el("div", { class: "muted" }, "Next run: " + fmtTime(sch.next_run)),
        el("div", { class: "muted" }, "Interval: " + sch.interval_minutes + " min"),
      ),
      el("div", { class: "card" },
        el("h2", {}, "GitHub"),
        el("div", {}, "Status: ", statusBadge(gh.last_status)),
        el("div", { class: "muted" }, "Repo: " + (gh.repository || "—")),
        el("div", { class: "muted" }, "Commit: " + (gh.last_commit ? gh.last_commit.slice(0, 8) : "—")),
        el("div", { class: "muted" }, "Pushed: " + fmtAgo(gh.last_push_at)),
      ),
      el("div", { class: "card" },
        el("h2", {}, "By Protocol"),
        ...Object.entries(s.by_protocol || {}).map(([p, n]) =>
          el("div", { class: "row", style: "justify-content:space-between;margin:0" },
            el("span", { class: "proto" }, p), el("b", {}, String(n)))),
        Object.keys(s.by_protocol || {}).length === 0 ? el("span", { class: "muted" }, "No configs yet") : null,
      ),
    ),
    el("div", { class: "card section" }, el("h2", {}, "History"), canvasChart(await api.get("/stats/history?limit=120"))),
    el("div", { class: "card" }, el("h2", {}, "Recent Runs"), await runsTable()),
  );
};

function statusBadge(status) {
  const map = { success: "ok", failed: "err", skipped: "dim", unconfigured: "dim", never: "dim" };
  return el("span", { class: "badge " + (map[status] || "dim") }, status || "—");
}

async function runsTable() {
  const runs = await api.get("/runs?limit=15");
  const wrap = el("div", { class: "table-wrap" });
  const t = el("table");
  t.append(el("tr", {}, ...["Time", "Chans", "Msgs", "Found", "Dupes", "Failed", "Cooldown", "Added", "Removed", "Pool", "GitHub"].map((h) => el("th", {}, h))));
  for (const r of runs) {
    t.append(el("tr", {},
      el("td", { class: "muted" }, fmtAgo(r.started_at)),
      ...["channels_scanned", "messages_read", "configs_found", "duplicates_removed", "tcp_failed", "cooldown_skipped", "added", "removed", "active_pool"].map((k) => el("td", {}, String(r[k]))),
      el("td", {}, statusBadge(r.github_push)),
    ));
  }
  if (!runs.length) t.append(el("tr", {}, el("td", { colspan: 11, class: "muted" }, "No runs yet")));
  wrap.append(t);
  return wrap;
}

function canvasChart(history) {
  const c = el("canvas", { class: "chart" });
  requestAnimationFrame(() => drawChart(c, history));
  return c;
}
function drawChart(canvas, history) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 600, h = canvas.clientHeight || 180;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  if (!history.length) { ctx.fillStyle = "#9aa3bd"; ctx.fillText("No data yet", 10, 20); return; }
  const series = [["active", "#5b8cff"], ["archive", "#7c5bff"], ["cooldown", "#f4b740"]];
  const max = Math.max(1, ...history.flatMap((p) => [p.active, p.archive, p.cooldown]));
  const pad = 24;
  for (const [key, color] of series) {
    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 2;
    history.forEach((p, i) => {
      const x = pad + (i / Math.max(1, history.length - 1)) * (w - pad * 2);
      const y = h - pad - (p[key] / max) * (h - pad * 2);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
  }
  ctx.fillStyle = "#9aa3bd"; ctx.font = "11px sans-serif";
  series.forEach(([k, c], i) => { ctx.fillStyle = c; ctx.fillRect(pad + i * 90, 6, 10, 10); ctx.fillStyle = "#9aa3bd"; ctx.fillText(k, pad + 14 + i * 90, 15); });
}

pages.channels = async (root) => {
  const render = async () => {
    const channels = await api.get("/channels");
    root.innerHTML = "";
    root.append(
      el("h1", {}, "Channels"),
      el("div", { class: "row" },
        el("input", { id: "ch-input", class: "input-grow", placeholder: "Channel username or t.me link (e.g. @durov)" }),
        el("button", { class: "btn btn-primary", onclick: async () => {
          const v = $("#ch-input").value.trim(); if (!v) return;
          try { await api.post("/channels", { username: v }); toast("Channel added"); render(); }
          catch (e) { toast(e.message, true); }
        } }, "+ Add channel"),
      ),
      channelsTable(channels, render),
    );
  };
  await render();
};

function channelsTable(channels, refresh) {
  const wrap = el("div", { class: "table-wrap" });
  const t = el("table");
  t.append(el("tr", {}, ...["Channel", "Enabled", "Scan/run", "Last ID", "Msgs", "Found", "Accepted", "Dupes", "Accept%", "Dup%", ""].map((h) => el("th", {}, h))));
  for (const c of channels) {
    t.append(el("tr", {},
      el("td", {}, el("div", {}, el("b", {}, "@" + c.username)), el("div", { class: "muted" }, c.title || "")),
      el("td", {}, el("input", { type: "checkbox", ...(c.enabled ? { checked: "" } : {}), onchange: async (e) => {
        try { await api.patch(`/channels/${c.id}`, { enabled: e.target.checked }); toast("Updated"); } catch (err) { toast(err.message, true); }
      } })),
      el("td", {}, el("input", { type: "number", min: 1, max: 1000, value: String(c.scan_limit ?? 15), class: "scan-input",
        title: "Messages scanned per run for this channel", onchange: async (e) => {
          const v = Math.max(1, Math.min(1000, parseInt(e.target.value, 10) || 15));
          e.target.value = String(v);
          try { await api.patch(`/channels/${c.id}`, { scan_limit: v }); toast("Scan limit updated"); } catch (err) { toast(err.message, true); }
        } })),
      el("td", { class: "mono" }, String(c.last_message_id)),
      el("td", {}, String(c.messages_scanned)),
      el("td", {}, String(c.configs_found)),
      el("td", {}, String(c.configs_accepted)),
      el("td", {}, String(c.duplicates_removed)),
      el("td", {}, c.acceptance_rate + "%"),
      el("td", {}, c.duplicate_rate + "%"),
      el("td", {}, el("button", { class: "btn btn-sm btn-danger", onclick: async () => {
        if (!confirm(`Remove @${c.username}?`)) return;
        try { await api.del(`/channels/${c.id}`); toast("Removed"); refresh(); } catch (e) { toast(e.message, true); }
      } }, "Delete")),
    ));
  }
  if (!channels.length) t.append(el("tr", {}, el("td", { colspan: 11, class: "muted" }, "No channels configured")));
  wrap.append(t);
  return wrap;
}

function fmtChannel(src) {
  if (!src) return "—";
  if (src.startsWith("@") || src.includes(":")) return src; // already @, or "npvt:chan"
  return "@" + src;
}

function configTable(items, { onDelete, startIndex = 0 } = {}) {
  const wrap = el("div", { class: "table-wrap" });
  const t = el("table");
  const headers = ["#", "Proto", "Name", "Host", "Port", "Alive", "Channel", "Seen", "Raw"];
  if (onDelete) headers.push("");
  t.append(el("tr", {}, ...headers.map((h) => el("th", {}, h))));
  items.forEach((c, i) => {
    const row = el("tr", {},
      el("td", { class: "mono muted" }, String(startIndex + i + 1)),
      el("td", {}, el("span", { class: "proto" }, c.protocol)),
      el("td", { class: "truncate" }, c.name || "—"),
      el("td", { class: "mono" }, c.host),
      el("td", { class: "mono" }, String(c.port)),
      el("td", {}, el("span", { class: "badge " + (c.alive ? "ok" : "err") }, c.alive ? "alive" : "down")),
      el("td", { class: "mono" }, fmtChannel(c.source_channel)),
      el("td", { class: "muted" }, fmtAgo(c.last_seen)),
      el("td", {}, el("button", { class: "btn btn-sm", onclick: () => copy(c.raw) }, "Copy")),
    );
    if (onDelete) row.append(el("td", {}, el("button", { class: "btn btn-sm btn-danger", onclick: () => onDelete(c) }, "✕")));
    t.append(row);
  });
  if (!items.length) t.append(el("tr", {}, el("td", { colspan: headers.length, class: "muted" }, "Nothing here")));
  wrap.append(t);
  return wrap;
}

const PROTOCOLS = ["", "vmess", "vless", "trojan", "ss", "ssr", "hysteria2", "tuic"];

pages.active = async (root) => {
  let search = "", protocol = "";
  const list = el("div", {});
  const refresh = async () => {
    const q = new URLSearchParams(); if (search) q.set("search", search); if (protocol) q.set("protocol", protocol);
    const items = await api.get("/active?" + q);
    list.innerHTML = "";
    list.append(el("div", { class: "muted", style: "margin-bottom:10px" }, `${items.length} active configs`),
      configTable(items, { onDelete: async (c) => {
        try { await api.del("/active/" + encodeURIComponent(c.fingerprint)); toast("Removed from pool"); refresh(); }
        catch (e) { toast(e.message, true); }
      } }));
  };
  root.append(
    el("div", { class: "brand-header" },
      el("span", { class: "brand-mark" }, "🛰 v2get"),
      el("span", { class: "brand-sub" }, "Active configurations — each tied to its source @channel")),
    el("div", { class: "row" },
      el("input", { class: "input-grow", placeholder: "Search host / name / raw…", oninput: (e) => { search = e.target.value; refresh(); } }),
      protoSelect((v) => { protocol = v; refresh(); }),
      el("button", { class: "btn", onclick: async () => download("active.txt", await api.get("/active/export")) }, "⬇ Export"),
    ),
    list,
  );
  await refresh();
};

function protoSelect(onchange) {
  const s = el("select", { onchange: (e) => onchange(e.target.value) });
  for (const p of PROTOCOLS) s.append(el("option", { value: p }, p || "all protocols"));
  return s;
}

pages.archive = async (root) => {
  let search = "", protocol = "", offset = 0; const limit = 100;
  const list = el("div", {}); const info = el("div", { class: "muted", style: "margin:10px 0" });
  const refresh = async () => {
    const q = new URLSearchParams({ limit, offset }); if (search) q.set("search", search); if (protocol) q.set("protocol", protocol);
    const data = await api.get("/archive?" + q);
    info.textContent = `${data.total} total · showing ${offset + 1}–${Math.min(offset + limit, data.total)}`;
    list.innerHTML = ""; list.append(configTable(data.items, { startIndex: offset }));
    pager.dataset.total = data.total;
  };
  const pager = el("div", { class: "row" },
    el("button", { class: "btn btn-sm", onclick: () => { offset = Math.max(0, offset - limit); refresh(); } }, "‹ Prev"),
    el("button", { class: "btn btn-sm", onclick: () => { if (offset + limit < +pager.dataset.total) { offset += limit; refresh(); } } }, "Next ›"),
  );
  root.append(
    el("h1", {}, "Archive"),
    el("div", { class: "row" },
      el("input", { class: "input-grow", placeholder: "Search archive…", oninput: (e) => { search = e.target.value; offset = 0; refresh(); } }),
      protoSelect((v) => { protocol = v; offset = 0; refresh(); }),
      el("button", { class: "btn", onclick: async () => download("archive.txt", await api.get("/archive/export")) }, "⬇ Export"),
      el("button", { class: "btn btn-danger", onclick: async () => {
        if (!confirm("Delete all non-active archived configs? Active pool is preserved.")) return;
        try { const r = await api.post("/archive/cleanup", {}); toast(`Removed ${r.deleted}`); offset = 0; refresh(); } catch (e) { toast(e.message, true); }
      } }, "Cleanup"),
    ),
    info, list, pager,
  );
  await refresh();
};

pages.cooldown = async (root) => {
  const list = el("div", {});
  const refresh = async () => {
    const items = await api.get("/cooldown?active_only=false");
    const wrap = el("div", { class: "table-wrap" });
    const t = el("table");
    t.append(el("tr", {}, ...["Fingerprint", "Fail Count", "Remaining", "Last Seen", "Actions"].map((h) => el("th", {}, h))));
    for (const c of items) {
      t.append(el("tr", {},
        el("td", { class: "mono truncate" }, c.fingerprint),
        el("td", {}, el("span", { class: "badge " + (c.fail_count >= 3 ? "err" : "dim") }, String(c.fail_count))),
        el("td", {}, c.remaining_seconds > 0 ? fmtDur(c.remaining_seconds) : el("span", { class: "muted" }, "—")),
        el("td", { class: "muted" }, fmtAgo(c.last_seen)),
        el("td", {},
          el("button", { class: "btn btn-sm", onclick: async () => { await api.post("/cooldown/reset", { fingerprint: c.fingerprint }); toast("Fail count reset"); refresh(); } }, "Reset"),
          " ",
          el("button", { class: "btn btn-sm btn-danger", onclick: async () => { await api.post("/cooldown/remove", { fingerprint: c.fingerprint }); toast("Removed"); refresh(); } }, "Remove"),
        ),
      ));
    }
    if (!items.length) t.append(el("tr", {}, el("td", { colspan: 5, class: "muted" }, "No cooldown entries")));
    wrap.append(t); list.innerHTML = ""; list.append(wrap);
  };
  root.append(el("h1", {}, "Cooldown"), list);
  await refresh();
};

pages.blacklist = async (root) => {
  let kind = "domains";
  const editor = el("div", {});
  const render = async () => {
    const data = await api.get("/blacklist");
    editor.innerHTML = "";
    const ta = el("textarea", { id: "bl-text" }, (data[kind] || []).join("\n"));
    editor.append(
      el("div", { class: "row" },
        el("input", { id: "bl-add", class: "input-grow", placeholder: `Add ${kind.slice(0, -1)} entry…` }),
        el("button", { class: "btn btn-primary", onclick: async () => {
          const v = $("#bl-add").value.trim(); if (!v) return;
          await api.post("/blacklist/add", { kind, entry: v }); toast("Added"); render();
        } }, "+ Add"),
      ),
      ta,
      el("div", { class: "row", style: "margin-top:10px" },
        el("button", { class: "btn btn-primary", onclick: async () => {
          const entries = $("#bl-text").value.split("\n").map((s) => s.trim()).filter(Boolean);
          await api.post("/blacklist/replace", { kind, entries }); toast("Saved (hot-reloaded)");
        } }, "💾 Save"),
        el("button", { class: "btn", onclick: async () => download(`blacklist_${kind}.txt`, await api.get("/blacklist/export/" + kind)) }, "⬇ Export"),
        el("label", { class: "btn" }, "⬆ Import",
          el("input", { type: "file", accept: ".txt", style: "display:none", onchange: async (e) => {
            const f = e.target.files[0]; if (!f) return;
            const text = await f.text();
            const entries = text.split("\n").map((s) => s.trim()).filter(Boolean);
            await api.post("/blacklist/replace", { kind, entries }); toast("Imported"); render();
          } })),
      ),
    );
  };
  const tabs = el("div", { class: "tabs" });
  ["domains", "ips", "keywords"].forEach((k) => tabs.append(el("div", { class: "tab" + (k === kind ? " active" : ""), onclick: () => { kind = k; [...tabs.children].forEach((c) => c.classList.toggle("active", c.textContent === k)); render(); } }, k)));
  root.append(el("h1", {}, "Blacklist"), tabs, editor);
  await render();
};

pages.logs = async (root) => {
  let query = "", live = true;
  const box = el("div", { class: "logbox" });
  const refresh = async () => {
    const q = new URLSearchParams({ tail: 400 }); if (query) q.set("q", query);
    const data = await api.get("/logs?" + q);
    box.innerHTML = "";
    for (const ln of data.lines) box.append(el("div", { class: "ln" }, ln));
    box.scrollTop = box.scrollHeight;
  };
  root.append(
    el("h1", {}, "Logs"),
    el("div", { class: "row" },
      el("input", { class: "input-grow", placeholder: "Filter live logs…", oninput: (e) => { query = e.target.value; refresh(); } }),
      el("label", { class: "btn toggle" }, el("input", { type: "checkbox", checked: "", onchange: (e) => (live = e.target.checked) }), " Live"),
      el("button", { class: "btn", onclick: async () => download("collector.log", await api.get("/logs/download")) }, "⬇ Download"),
    ),
    box,
  );
  await refresh();
  const timer = setInterval(() => { if (live && currentPage === "logs") refresh(); }, 3000);
  cleanups.push(() => clearInterval(timer));
};

pages.settings = async (root) => {
  const s = await api.get("/settings");
  const fields = [
    ["scan_interval_minutes", "Scan interval (minutes)", "number"],
    ["tcp_timeout_seconds", "TCP timeout (seconds)", "number"],
    ["max_pool_size", "Maximum pool size", "number"],
    ["cooldown_hours", "Cooldown duration (hours)", "number"],
    ["fail_threshold", "Failure threshold", "number"],
    ["tcp_concurrency", "TCP check concurrency", "number"],
    ["github_repository", "GitHub repository (owner/repo)", "text"],
    ["github_branch", "GitHub branch", "text"],
    ["github_target_dir", "GitHub target directory", "text"],
    ["github_token", "GitHub token", "password"],
  ];
  const grid = el("div", { class: "settings-grid" });
  for (const [key, label, type] of fields) {
    grid.append(el("label", {}, label),
      el("input", { id: "set-" + key, type, value: s[key] ?? "", placeholder: key === "github_token" ? (s.github_token_set ? s.github_token : "not set") : "" }));
  }
  const sources = el("div", { class: "settings-grid" },
    el("label", {}, "Collect links from channel text"),
    checkbox("set-collect_text_links", s.collect_text_links));

  const outputs = el("div", { class: "settings-grid" },
    el("label", {}, "Output: Clash"), checkbox("set-output_clash", s.output_clash),
    el("label", {}, "Output: Stash"), checkbox("set-output_stash", s.output_stash),
    el("label", {}, "Output: sing-box"), checkbox("set-output_singbox", s.output_singbox));

  root.append(
    el("h1", {}, "Settings"),
    el("div", { class: "card section" }, el("h2", {}, "Collector & GitHub"), grid),
    el("div", { class: "card section" }, el("h2", {}, "Link sources"), sources,
      el("p", { class: "muted", style: "margin-top:10px" }, "Off = links are never taken from channel messages; the subscription is built only from unlocked .npvt files. The active pool is still TCP-checked, rotated and published every run.")),
    el("div", { class: "card section" }, el("h2", {}, "Output formats"), outputs,
      el("p", { class: "muted", style: "margin-top:10px" }, "active.txt and subscription_base64.txt are always generated.")),
    el("button", { class: "btn btn-primary", onclick: async () => {
      const values = {};
      for (const [key, , type] of fields) {
        const v = $("#set-" + key).value;
        if (key === "github_token" && (v.includes("*") || v === "")) continue;
        values[key] = type === "number" ? Number(v) : v;
      }
      values.collect_text_links = $("#set-collect_text_links").checked;
      values.output_clash = $("#set-output_clash").checked;
      values.output_stash = $("#set-output_stash").checked;
      values.output_singbox = $("#set-output_singbox").checked;
      try { await api.put("/settings", { values }); toast("Settings saved — applied without restart"); }
      catch (e) { toast(e.message, true); }
    } }, "💾 Save settings"),
    el("div", { class: "card section", style: "margin-top:24px" }, el("h2", {}, "Output files"), await outputPanel()),
  );
};

function checkbox(id, checked) {
  return el("input", { id, type: "checkbox", ...(checked ? { checked: "" } : {}) });
}

async function outputPanel() {
  const gh = await api.get("/github");
  const data = await api.get("/output");
  const box = el("div", {});
  const preview = el("pre", { class: "preview" }, "Select a file to preview");
  const row = el("div", { class: "row" });
  for (const name of data.files) {
    row.append(el("button", { class: "btn btn-sm", onclick: async () => {
      try { preview.textContent = await api.get("/output/" + name) || "(empty)"; } catch { preview.textContent = "(not generated yet)"; }
    } }, name));
    row.append(el("button", { class: "btn btn-sm", onclick: async () => download(name, await api.get("/output/" + name).catch(() => "")) }, "⬇"));
    if (gh.raw_base) row.append(el("button", { class: "btn btn-sm", onclick: () => copy(gh.raw_base + name) }, "🔗 URL"));
  }
  box.append(row, preview);
  return box;
}

// ── npvt (.npvt → local unlock → V2Ray links) ───────────────────────────────---
const NPVT_TOGGLES = [
  ["collection_enabled", "NPVT collection", "Detect & download .npvt files from channels"],
  ["link_collection_enabled", "Link collection", "Inject the unlocked V2Ray links into the pipeline"],
];
const NPVT_STATUS_BADGE = { done: "ok", failed: "err", skipped: "dim", pending: "dim", processing: "warn" };
const NPVT_FIELD_LABELS = {
  scan_interval_seconds: "Scan interval (s)",
  scan_message_limit: "Scan message limit", max_file_bytes: "Max file size (bytes)",
  unlock_concurrency: "Unlock concurrency (restart)",
  max_retries: "Max retries", retry_backoff_seconds: "Retry backoff (s)",
  publish_after_ingest: "Regenerate outputs / push after ingest",
};

pages.npvt = async (root) => {
  const filesBox = el("div", {});
  const statsBox = el("div", { class: "grid section" });

  const refreshState = async () => {
    const st = await api.get("/npvt/state");
    const s = st.stats || {};
    statsBox.innerHTML = "";
    statsBox.append(
      card(st.queue_size, "Queue"),
      card(st.files_total, "Files seen"),
      card(s.files_done || 0, "Unlocked"),
      card(s.files_failed || 0, "Failed"),
      card(s.files_skipped || 0, "Skipped (dupe)"),
      card(s.files_filtered || 0, "Filtered (pre-unlock)"),
      card(s.links_collected || 0, "Links extracted"),
      card(s.links_injected || 0, "Links injected"),
    );
  };

  const refreshFiles = async () => {
    const files = await api.get("/npvt/files?limit=100");
    const wrap = el("div", { class: "table-wrap" });
    const t = el("table");
    t.append(el("tr", {}, ...["File", "Channel", "Msg", "Status", "Tries", "Links", "Injected", "Detail", ""].map((h) => el("th", {}, h))));
    for (const f of files) {
      t.append(el("tr", {},
        el("td", { class: "truncate" }, f.file_name || "—"),
        el("td", { class: "muted" }, f.source_channel || "—"),
        el("td", { class: "mono" }, String(f.source_message_id)),
        el("td", {}, el("span", { class: "badge " + (NPVT_STATUS_BADGE[f.status] || "dim") }, f.status)),
        el("td", {}, String(f.attempts)),
        el("td", {}, String(f.links_found)),
        el("td", {}, String(f.links_injected)),
        el("td", { class: "muted truncate" }, f.error || "—"),
        el("td", {}, el("button", { class: "btn btn-sm", onclick: async () => {
          try { await api.post(`/npvt/files/${f.id}/retry`); toast("Re-queued"); refreshFiles(); refreshState(); }
          catch (e) { toast(e.message, true); }
        } }, "Retry")),
      ));
    }
    if (!files.length) t.append(el("tr", {}, el("td", { colspan: 9, class: "muted" }, "No .npvt files seen yet")));
    wrap.append(t);
    filesBox.innerHTML = "";
    filesBox.append(wrap);
  };

  const data = await api.get("/npvt/settings");
  const values = data.values, defaults = data.defaults;

  // master toggles — saved immediately
  const toggleRow = el("div", { class: "row" });
  for (const [key, label, desc] of NPVT_TOGGLES) {
    toggleRow.append(el("label", { class: "btn toggle", title: desc },
      el("input", { type: "checkbox", ...(values[key] ? { checked: "" } : {}), onchange: async (e) => {
        try { await api.put("/npvt/settings", { values: { [key]: e.target.checked } }); toast(label + (e.target.checked ? " on" : " off")); }
        catch (err) { toast(err.message, true); e.target.checked = !e.target.checked; }
      } }), " " + label));
  }

  // advanced settings form (everything except the master toggles)
  const grid = el("div", { class: "settings-grid" });
  const inputs = {};
  for (const key of Object.keys(defaults)) {
    if (NPVT_TOGGLES.some(([k]) => k === key)) continue;
    const dv = defaults[key], cur = values[key];
    const label = NPVT_FIELD_LABELS[key] || key;
    let input;
    if (Array.isArray(dv)) {
      input = el("textarea", { id: "npvt-" + key, rows: 4 }, (cur || []).join("\n"));
    } else if (typeof dv === "boolean") {
      input = checkbox("npvt-" + key, cur);
    } else {
      input = el("input", { id: "npvt-" + key, type: typeof dv === "number" ? "number" : "text", value: cur ?? "" });
    }
    inputs[key] = { input, dv };
    grid.append(el("label", {}, label), input);
  }

  const save = el("button", { class: "btn btn-primary", onclick: async () => {
    const out = {};
    for (const [key, { input, dv }] of Object.entries(inputs)) {
      if (Array.isArray(dv)) out[key] = input.value.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
      else if (typeof dv === "boolean") out[key] = input.checked;
      else if (typeof dv === "number") out[key] = Number(input.value);
      else out[key] = input.value;
    }
    try { await api.put("/npvt/settings", { values: out }); toast("NPVT settings saved"); }
    catch (e) { toast(e.message, true); }
  } }, "💾 Save NPVT settings");

  root.append(
    el("h1", {}, "NPVT pipeline"),
    el("p", { class: "muted" }, "Isolated: .npvt file → local unlock (whitebox AES-CTR, in-process) → V2Ray links → existing pipeline. No external bot. Failures here never affect the core collector."),
    el("div", { class: "card section" }, el("h2", {}, "Pipeline controls"), toggleRow,
      el("div", { class: "row", style: "margin-top:10px" },
        el("button", { class: "btn", onclick: async () => { try { await api.post("/npvt/scan"); toast("Scan triggered"); setTimeout(() => { refreshState(); refreshFiles(); }, 600); } catch (e) { toast(e.message, true); } } }, "🔍 Scan now"),
        el("button", { class: "btn", onclick: () => { refreshState(); refreshFiles(); } }, "↻ Refresh"),
        el("button", { class: "btn btn-danger", onclick: async () => {
          if (!confirm("Clear the queue? This cancels and deletes all pending/processing files. Finished files are kept; files still in their channels are re-discovered on the next scan.")) return;
          try { const r = await api.post("/npvt/queue/clear"); toast(`Queue cleared — ${r.deleted} file(s) removed`); refreshState(); refreshFiles(); }
          catch (e) { toast(e.message, true); }
        } }, "🗑 Clear queue"))),
    statsBox,
    el("div", { class: "card section" }, el("h2", {}, "Settings"), grid, el("div", { style: "margin-top:12px" }, save)),
    el("div", { class: "card section" }, el("h2", {}, "Recent files"), filesBox),
  );
  await refreshState();
  await refreshFiles();
  const timer = setInterval(() => { if (currentPage === "npvt") { refreshState(); refreshFiles(); } }, 5000);
  cleanups.push(() => clearInterval(timer));
};

// ── router ─────────────────────────────────────────────────────────────────────
let currentPage = $("#content").dataset.page || "overview";
let cleanups = [];

async function loadPage() {
  const root = $("#content");
  cleanups.forEach((fn) => fn()); cleanups = [];
  currentPage = root.dataset.page || "overview";
  root.innerHTML = '<div class="loading">Loading…</div>';
  try {
    root.innerHTML = "";
    await (pages[currentPage] || pages.overview)(root);
  } catch (e) {
    root.innerHTML = "";
    root.append(el("div", { class: "card", style: "border-color:var(--err)" }, "Error: " + e.message));
  }
}

// theme
const THEME_KEY = "v2get-theme";
function applyTheme(t) { document.documentElement.dataset.theme = t; localStorage.setItem(THEME_KEY, t); }
applyTheme(localStorage.getItem(THEME_KEY) || "dark");
$("#theme-toggle").addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));

$("#run-now").addEventListener("click", async () => {
  try { const r = await api.post("/run", {}); toast(r.status === "triggered" ? "Run triggered" : "Already running"); }
  catch (e) { toast(e.message, true); }
});

// mobile nav drawer — toggle the off-canvas sidebar (closes on link tap / backdrop)
const _layout = $(".layout");
const setNav = (open) => _layout.classList.toggle("nav-open", open);
$("#nav-toggle")?.addEventListener("click", () => setNav(!_layout.classList.contains("nav-open")));
$("#nav-backdrop")?.addEventListener("click", () => setNav(false));
$("#nav")?.addEventListener("click", (e) => { if (e.target.tagName === "A") setNav(false); });

loadPage();
refreshStatus();
setInterval(refreshStatus, 5000);

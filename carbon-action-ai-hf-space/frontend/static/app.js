/* Carbon-Action AI dashboard (vanilla JS + Chart.js) */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const S = {
  me: null, fid: null, page: "overview", now: null, charts: {}, sound: true, audio: null,
  seen: new Set(), polls: 0, timer: null, cache: {}, openAlerts: [], simState: null, startNow: null,
};
const PAGES = {
  overview: ["Plant Overview", "Real-time insights into energy use, emissions and production"],
  boiler: ["Boiler Intelligence", "Live combustion health, expected vs actual emissions and drivers"],
  stenter: ["Stenter Intelligence", "Drying efficiency, exhaust losses and moisture control"],
  diagnosis: ["AI Diagnosis", "Anomaly detection and root-cause drivers for every machine"],
  simulator: ["Action Simulator", "Test a fix on the digital twin before anyone touches the machine"],
  forecast: ["No-Action Forecast", "When the plant will cross its emission caps if nothing changes"],
  live: ["Live Data", "Every new reading from each machine, checked against what it should be"],
  incident: ["AI Diagnosis", "What went wrong, the simulated fix, and the verified saving"],
  history: ["Action History", "Decisions taken, crew progress, measured savings, and what the AI learned"],
  data: ["Data & Evidence", "Where the data comes from, instrument health, reports, and how far to trust the results"],
};

/* ---------------- utils ---------------- */
const fmt = (n, d = 0) => n == null || isNaN(n) ? "—" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: d });
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const tm = iso => new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false });
const dt = iso => new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
const day = iso => new Date(iso).toLocaleDateString("en-IN", { day: "2-digit", month: "short" });
function ago(iso) {
  const m = Math.round((new Date(S.now) - new Date(iso)) / 60000);
  if (m < 1) return "just now"; if (m < 60) return m + " min ago";
  const h = Math.round(m / 60); return h < 48 ? h + "h ago" : Math.round(h / 24) + "d ago";
}
async function api(path, opts = {}) {
  const fid = S.fid;
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, credentials: "same-origin", ...opts });
  if (r.status === 401) { showLogin(); throw new Error("401"); }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  const data = await r.json();
  if (fid !== S.fid) throw new Error("stale");  // the user switched factory: this answer no longer applies
  return data;
}
const F = p => `/api/${S.fid}${p}`;
function chart(id, cfg) {
  const el = document.getElementById(id); if (!el) return;
  const old = S.charts[id];
  // same canvas & type: update in place (no flicker, keeps hover state); otherwise rebuild
  if (old && old.canvas === el && old.config.type === cfg.type) {
    old.data.labels = cfg.data.labels;
    cfg.data.datasets.forEach((ds, i) => { if (old.data.datasets[i]) Object.assign(old.data.datasets[i], ds); else old.data.datasets.push(ds); });
    old.data.datasets.length = cfg.data.datasets.length;
    old.options = cfg.options; old.update("none"); return old;
  }
  if (old) old.destroy();
  S.charts[id] = new Chart(el, cfg); return S.charts[id];
}
Chart.defaults.font.family = getComputedStyle(document.documentElement).fontFamily;
Chart.defaults.color = "#55636f";
Chart.defaults.plugins.legend.labels.boxWidth = 10;
Chart.defaults.plugins.legend.labels.usePointStyle = true;
const C = { green: "#1f9d63", blue: "#2563eb", orange: "#f08a24", purple: "#7c4ddb", red: "#d93636", grey: "#9aa6b2", teal: "#0ea5a5" };
const lineDs = (label, data, color, extra = {}) => ({ label, data, borderColor: color, backgroundColor: color + "22", borderWidth: 2, pointRadius: 0, tension: .3, ...extra });
const baseOpts = (extra = {}) => ({ responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: "index", intersect: false },
  plugins: { legend: { position: "top", align: "end" } }, scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 8 } }, y: { grid: { color: "#eef2f5" } } }, ...extra });

/* ---------------- sound ---------------- */
function chime(sev) {
  if (!S.sound) return;
  try {
    S.audio = S.audio || new (window.AudioContext || window.webkitAudioContext)();
    const ctx = S.audio, t = ctx.currentTime;
    const notes = sev === "High" ? [880, 660, 880] : [740, 988];
    notes.forEach((f, i) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(0, t + i * .18); g.gain.linearRampToValueAtTime(.12, t + i * .18 + .02);
      g.gain.exponentialRampToValueAtTime(.001, t + i * .18 + .35);
      o.connect(g); g.connect(ctx.destination); o.start(t + i * .18); o.stop(t + i * .18 + .4);
    });
  } catch (e) { }
}

/* ---------------- auth ---------------- */
function showLogin() {
  clearInterval(S.timer); $("#app-view").classList.add("hidden"); $("#chat-fab").classList.add("hidden");
  $("#login-view").classList.remove("hidden");
}
$("#login-form").addEventListener("submit", async e => {
  e.preventDefault(); $("#lg-err").textContent = "";
  try { S.audio = S.audio || new (window.AudioContext || window.webkitAudioContext)(); } catch (_) { }
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ username: $("#lg-user").value, password: $("#lg-pass").value }) });
    boot();
  } catch (err) { $("#lg-err").textContent = err.message === "401" ? "Wrong username or password" : err.message; }
});
$("#avatar").onclick = () => $("#user-menu").classList.toggle("hidden");
$("#logout-btn").onclick = async () => { await api("/api/logout", { method: "POST" }); location.reload(); };
$("#sound-btn").onclick = () => { S.sound = !S.sound; $("#sound-btn").textContent = S.sound ? "🔔" : "🔕"; };

async function boot() {
  try { S.me = await api("/api/me"); } catch (e) { showLogin(); return; }
  $("#login-view").classList.add("hidden"); $("#app-view").classList.remove("hidden"); $("#chat-fab").classList.remove("hidden");
  $("#avatar").textContent = S.me.name.split(" ").map(w => w[0]).join("").slice(0, 2).toUpperCase();
  $("#um-name").textContent = S.me.name;
  const sel = $("#factory-select");
  sel.innerHTML = S.me.factories.map(f => `<option value="${f.id}">${esc(f.name)} — ${esc(f.location)}</option>`).join("");
  sel.classList.toggle("hidden", S.me.factories.length < 2);
  S.fid = localStorage.getItem("fid"); if (!S.me.factories.find(f => f.id === S.fid)) S.fid = S.me.factories[0].id;
  sel.value = S.fid;
  sel.onchange = () => { S.fid = sel.value; localStorage.setItem("fid", S.fid); S.seen.clear(); S.now = null; S.startNow = null; setFactory(); };
  $("#llm-status").textContent = S.me.llm.provider === "nugen" ? `AI advisor: Nugen (${S.me.llm.model})` : "AI advisor: local mode (add Nugen key in .env)";
  refreshAdvisor();
  const cat = await api("/api/catalog"); S.catalog = cat; buildDemo(cat);
  $("#demo-btn").classList.toggle("hidden", !S.me.demo); if (!S.me.demo) $("#demo").classList.add("hidden");
  setFactory();
}
function setFactory() {
  const f = S.me.factories.find(x => x.id === S.fid);
  $("#sf-name").textContent = f.name; $("#sf-loc").textContent = f.location;
  S.cache = {};
  if (S.page === "incident") S.page = "overview";  // incidents belong to one factory
  go(S.page);
  startPolling();
}
/* real time: data arrives every 5 min, poll every 10 s; fast-forward: poll every reading */
function startPolling() {
  const c = S.me.clock;
  clearInterval(S.timer); poll();
  S.timer = setInterval(poll, (c.mode === "realtime" ? 10 : Math.max(c.seconds_per_reading, 0.5)) * 1000);
  // the clock mode can differ per factory (one may be replaying an upload)
}
function clockText(c, paused) {
  if (paused) return "Paused";
  if (c.mode === "replay") return c.finished ? "Replay finished" : "Replaying uploaded data";
  if (c.mode === "fast") return `Fast-forward ×${Math.round(300 / c.seconds_per_reading)}`;
  return c.ahead_min ? `Waiting ${c.ahead_min} min for real time` : "Live";
}

/* ---------------- navigation ---------------- */
$$("#nav a").forEach(a => a.onclick = () => { if (a.dataset.page === "simulator") S.simState = null; go(a.dataset.page); });
function go(page) {
  S.page = page;
  $$("#nav a").forEach(a => a.classList.toggle("active", a.dataset.page === (page === "incident" ? "diagnosis" : page)));
  $("#page-title").textContent = PAGES[page][0]; $("#page-sub").textContent = PAGES[page][1];
  Object.values(S.charts).forEach(c => c.destroy()); S.charts = {};
  $("#page").innerHTML = `<div class="empty"><span class="spinner"></span> Loading…</div>`;
  RENDER[page]().catch(e => { if (e.message !== "401" && e.message !== "stale") $("#page").innerHTML = `<div class="card empty">Could not load: ${esc(e.message)}</div>`; });
}

/* ---------------- live polling ---------------- */
async function poll() {
  if (!S.fid) return;
  let d;
  try { d = await api(F(`/live${S.now ? "?since=" + encodeURIComponent(S.now) : ""}`)); } catch (e) { return; }
  const first = !S.startNow;
  S.now = d.now; if (first) S.startNow = d.now;
  $("#clock").textContent = new Date(d.now).toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
  $("#clock").title = d.clock.mode === "realtime" ? "Plant data is current up to this time (a new 5-minute reading arrives every 5 minutes)" : "Demo fast-forward: plant time runs faster than real time";
  $("#live-txt").textContent = clockText(d.clock, d.paused);
  $("#source-tag").textContent = d.clock.mode === "replay" ? "Uploaded data" : "Simulated data";
  $("#source-tag").classList.toggle("real", d.clock.mode === "replay");
  if (d.clock.mode !== S.me.clock.mode || d.clock.seconds_per_reading !== S.me.clock.seconds_per_reading) { S.me.clock = d.clock; startPolling(); }
  if ($("#demo-speed") && d.clock.mode !== "replay") $("#demo-speed").value = d.clock.mode === "realtime" ? "0" : String(d.clock.seconds_per_reading);
  S.openAlerts = d.open_alerts;
  const n = d.open_alerts.length; $("#nav-badge").textContent = n; $("#nav-badge").classList.toggle("hidden", !n);
  for (const a of d.new_alerts) {
    if (S.seen.has(a.id)) continue; S.seen.add(a.id);
    if (!first && !a.resolved) { toast(a); chime(a.severity); }
  }
  S.latest = d.latest;
  S.polls++;
  const fast = S.me.clock.mode !== "realtime";
  const every = fast ? ({ overview: 2, boiler: 2, stenter: 2, diagnosis: 2, history: 2, forecast: 5, simulator: 4, live: 1, incident: 2 }[S.page] || 3) : 1;
  // don't redraw under the user's pointer: a button replaced mid-click loses the click
  const busy = $("#page").querySelector("button:hover, a:hover, .row:hover, .kpi:hover, select:focus, input:focus");
  if (!first && !busy && S.polls % every === 0 && REFRESH[S.page]) REFRESH[S.page]().catch(() => { });
}

/* ---------------- toasts ---------------- */
function toast(a) {
  const el = document.createElement("div");
  el.className = "toast " + a.severity;
  el.innerHTML = `<div class="ti">!</div><button class="icon-btn x">✕</button>
    <b>${esc(a.title)}</b><div class="muted small">${esc(a.machine)} · ${tm(a.time)} · ${a.severity} priority</div>
    <p>${esc(a.message)}</p>
    <div class="acts">${a.kind === "sensor" ? `<button class="btn primary" data-go="data">Check instruments</button>` : a.machine_id === "PLANT" ? `<button class="btn primary" data-go="forecast">View forecast</button>`
      : `<button class="btn primary" data-inc="${a.id}">View diagnosis</button>`}</div>`;
  el.querySelector(".x").onclick = () => el.remove();
  el.querySelectorAll("[data-go]").forEach(b => b.onclick = () => { go(b.dataset.go); el.remove(); });
  el.querySelectorAll("[data-inc]").forEach(b => b.onclick = () => { openIncident(b.dataset.inc); el.remove(); });
  $("#toasts").prepend(el);
  setTimeout(() => el.remove(), 18000);
  while ($("#toasts").children.length > 2) $("#toasts").lastChild.remove();
}

/* ---------------- shared bits ---------------- */
const SVG_BOILER = `<svg class="machine-svg" viewBox="0 0 120 100"><rect x="10" y="30" width="80" height="52" rx="10" fill="#5b7fa6"/><rect x="18" y="38" width="64" height="36" rx="6" fill="#dbe6f2"/><rect x="26" y="50" width="48" height="18" rx="3" fill="#ff7a1a"/><path d="M32 66c4-10 8-2 12-10 3 6 7 0 10 8 3-6 6-2 10 2" fill="#ffd34d"/><rect x="62" y="8" width="10" height="24" fill="#3f5f84"/><rect x="78" y="14" width="8" height="18" fill="#3f5f84"/><rect x="14" y="82" width="12" height="8" fill="#3f5f84"/><rect x="74" y="82" width="12" height="8" fill="#3f5f84"/><circle cx="102" cy="52" r="10" fill="#b8c7d8"/><rect x="90" y="48" width="8" height="8" fill="#b8c7d8"/></svg>`;
const SVG_STENTER = `<svg class="machine-svg" viewBox="0 0 120 100"><rect x="4" y="34" width="112" height="40" rx="6" fill="#5b7fa6"/><rect x="10" y="40" width="22" height="28" rx="3" fill="#dbe6f2"/><rect x="36" y="40" width="22" height="28" rx="3" fill="#dbe6f2"/><rect x="62" y="40" width="22" height="28" rx="3" fill="#dbe6f2"/><rect x="88" y="40" width="22" height="28" rx="3" fill="#dbe6f2"/><path d="M0 56h120" stroke="#2f9e6e" stroke-width="4"/><rect x="20" y="16" width="8" height="18" fill="#3f5f84"/><rect x="72" y="20" width="8" height="14" fill="#3f5f84"/><path d="M22 14c2-4-2-6 0-10M74 18c2-4-2-6 0-10" stroke="#9fb3c8" stroke-width="2" fill="none"/><rect x="8" y="74" width="10" height="10" fill="#3f5f84"/><rect x="102" y="74" width="10" height="10" fill="#3f5f84"/></svg>`;

function machineByType(t) { return S.me.factories.find(f => f.id === S.fid).machines.find(m => m.type === t); }

function paramsHtml(m, r) {
  if (!r) return "";
  const P = (l, v, unit, note, cls = "") => `<div class="param ${cls}"><div class="pl">${l}</div><div class="pv">${v} <small>${unit}</small></div><div class="pn">${note}</div></div>`;
  if (m.type === "boiler") {
    const o2bad = r.flue_o2_pct > (m.fuel === "coal" ? 7.6 : 5.0);
    return P("Steam Pressure", fmt(r.steam_pressure_bar, 1), "bar", "(Target: 8–9)", r.steam_pressure_bar < 8 ? "warn" : "")
      + P("Boiler Efficiency", fmt(r.boiler_efficiency_pct, 1), "%", `(Design: ${m.fuel === "coal" ? "76" : "84"}%)`)
      + P("Flue Gas O2", fmt(r.flue_o2_pct, 1), "%", `(Normal: ${m.fuel === "coal" ? "6–7" : "3.5–4.5"}%)`, o2bad ? "bad" : "")
      + P("Stack Temperature", fmt(r.exhaust_gas_temp_c, 0), "°C", "(vs expected for load)", r.exhaust_gas_temp_c > (m.fuel === "coal" ? 232 : 205) ? "bad" : r.exhaust_gas_temp_c > (m.fuel === "coal" ? 212 : 190) ? "warn" : "")
      + P("Feedwater Temp", fmt(r.feedwater_temp_c, 0), "°C", "(Normal: 80–85)", r.feedwater_temp_c < 75 ? "bad" : "")
      + P("Steam Generated", fmt(r.steam_generated_kg_h, 0), "kg/h", `FD ${fmt(r.fd_fan_speed_pct, 0)}% · ID ${fmt(r.id_fan_speed_pct, 0)}%`);
  }
  const run = r.status === "running";
  return P("Chamber Temp", fmt(r.chamber_temp_c, 0), "°C", run ? `(Set: ${fmt(r.temperature_setpoint_c, 0)})` : "(idle)", !run && r.chamber_temp_c > 130 ? "bad" : "")
    + P("Exhaust Humidity", fmt(r.exhaust_humidity_kg_kg, 3), "kg/kg", "(Good: ≥ 0.08)", run && r.exhaust_humidity_kg_kg < 0.06 ? "bad" : "")
    + P("Exhaust Damper", fmt(r.exhaust_damper_pct, 0), "%", "(Normal: 45–60)", r.exhaust_damper_pct > 65 ? "bad" : "")
    + P("Airflow", fmt(r.airflow_pct, 0), "%", `Filter ΔP ${fmt(r.filter_dp_kpa, 2)} kPa`, r.airflow_pct < 84 ? "bad" : "")
    + P("Outlet Moisture", run ? fmt(r.outlet_moisture_pct, 1) : "—", "%", "(Target: 7%)", run && (r.outlet_moisture_pct < 4.5 || r.outlet_moisture_pct > 9.5) ? "warn" : "")
    + P("Fabric Speed", fmt(r.fabric_speed_m_min, 0), "m/min", `${esc(r.fabric_type || "")} · ${r.gsm_g_m2 || ""} gsm`);
}
function statusPill(r) {
  if (!r) return "";
  if (r.sensor_issues?.length) return `<span class="status-pill warn" title="${esc(r.sensor_issues[0].detail)}">● Check ${esc(r.sensor_issues[0].label)}</span>`;
  if (r.anomaly) return `<span class="status-pill bad">● Anomaly detected</span>`;
  if (r.status === "idle") return `<span class="status-pill idle">● Idle (changeover)</span>`;
  return `<span class="status-pill ok">● Operating Normally</span>`;
}
function alertItem(a) {
  const cls = a.resolved ? "resolved" : a.severity;
  const icon = a.resolved ? "✓" : a.severity === "Low" ? "i" : "!";
  const open = a.kind === "anomaly" ? `onclick="openIncident('${a.id}')" style="cursor:pointer" title="View diagnosis"` : "";
  return `<div class="al ${cls}" ${open}><div class="ai">${icon}</div><span class="t">${ago(a.time)}</span>
    <b>${esc(a.title)}${a.resolved ? " — resolved" : ""}</b><p>${esc(a.machine)}: ${esc(a.message)}</p></div>`;
}
function driverRows(drivers, max) {
  const pos = drivers.filter(d => d.contribution_kg_h > 0.3);
  if (!pos.length) return `<div class="muted small">Nothing is adding meaningful extra CO2 right now.</div>`;
  const top = max || Math.max(...pos.map(d => d.contribution_kg_h));
  return pos.slice(0, 5).map(d => `<div class="drv"><div><div class="lbl">${esc(d.label)}</div>
    <div class="ev">${esc(d.evidence_label)}: ${fmt(d.current, 1)} ${esc(d.unit)} (healthy: ${fmt(d.healthy, 1)} ${esc(d.unit)})</div></div>
    <div class="bar"><span style="width:${Math.min(100, d.contribution_kg_h / top * 100)}%"></span></div>
    <div class="kg">+${fmt(d.contribution_kg_h, 1)}<div class="ev">kg CO2 per hour</div></div></div>`).join("");
}
function kpiCard(icon, color, label, val, unit, chg, lowerIsGood = true, tip = "") {
  let c = `<div class="chg">&nbsp;</div>`;
  if (chg != null) { const good = lowerIsGood ? chg <= 0 : chg >= 0; c = `<div class="chg ${good ? "good" : "bad"}">${chg >= 0 ? "▲" : "▼"} ${fmt(Math.abs(chg), 1)}%<span>vs last week</span></div>`; }
  return `<div class="card kpi" ${tip ? `data-tip="${esc(tip)}"` : ""}><div class="ic" style="background:${color}">${icon}</div>
    <div class="txt"><div class="lab" title="${esc(label)}">${label}</div><div class="val">${val}<small>${unit}</small></div>${c}</div></div>`;
}
/* large weekly totals read better in t / MWh than as 6-digit kg / kWh */
function compact(v, unit) {
  if (unit === "kg" && Math.abs(v) >= 10000) return [fmt(v / 1000, 1), "t"];
  if (unit === "kWh" && Math.abs(v) >= 10000) return [fmt(v / 1000, 1), "MWh"];
  if (unit === "Nm3" && Math.abs(v) >= 10000) return [fmt(v / 1000, 1), "k Nm3"];
  return [fmt(v), unit];
}
function kpiRow(k, b) {
  const K = (icon, color, label, item, unit, tip, lowerIsGood = true, digits) => {
    const [v, u] = digits != null ? [fmt(item.value, digits), unit] : compact(item.value, unit);
    return kpiCard(icon, color, label, v, " " + u, item.change_pct, lowerIsGood, tip);
  };
  return K("⚡", C.green, "Electricity", k.energy_kwh, "kWh", "Electricity used by the boiler and stenter in the last 7 days, mostly for fans and motors. Lower is better.")
    + K("🔥", C.orange, `Boiler fuel (${b.fuel === "coal" ? "coal" : "gas"})`, k.boiler_fuel, b.fuel_unit, `How much ${b.fuel === "coal" ? "coal" : "gas"} the boiler burned in the last 7 days. Burning fuel is the plant's biggest source of CO2. Lower is better.`)
    + K("♨", C.blue, "Steam generated", k.steam_kg, "kg", "Steam the boiler made for dyeing and washing fabric. It rises and falls with production, so more steam is not always a problem.")
    + K("☁", C.purple, "CO2 emitted", k.co2_t, "t", "Total CO2 from burning fuel and using electricity in the last 7 days. This is the number the plant is trying to reduce.", true, 1)
    + K("▦", C.teal, "Fabric produced", k.production_kg, "kg", "Finished fabric that came off the stenter in the last 7 days. Higher is better.", false)
    + K("◐", "#334e68", "CO2 intensity", k.intensity, "kg/kg", "CO2 released for every 1 kg of fabric made. It is the fairest measure because it allows for how much was produced. Lower is better.", true, 3);
}

/* ================= PAGES ================= */
const RENDER = {}, REFRESH = {};

/* ---------- overview ---------- */
RENDER.overview = async () => {
  const [o, recs, acts, fc] = await Promise.all([api(F("/overview")), api(F("/recommendations")), api(F("/actions")), api(F("/forecast"))]);
  S.cache.overview = o;
  const b = machineByType("boiler");
  $("#page").innerHTML = `
  <div class="attention" id="attention">${attentionHtml(o)}</div>
  <div class="kpi-head"><b>Last 7 days</b><span class="muted small">plant totals, compared with the week before</span></div>
  <div class="kpis" id="kpis">${kpiRow(o.kpis, b)}</div>
  <div class="grid g-mid">
    <div class="card"><div class="card-head"><h3>CO2 emissions</h3><span class="muted small">last 7 days · hourly average</span></div><div class="chart-box"><canvas id="ch-trend"></canvas></div></div>
    <div class="card"><div class="card-head"><h3>No-Action Forecast</h3><a class="link" onclick="go('forecast')">Open forecast →</a></div>
      <div id="ov-fc-txt">${fcMini(fc)}</div>
      <div class="chart-box sm"><canvas id="ov-fc"></canvas></div></div>
  </div>
  <div class="grid g3">
    <div class="card"><div class="card-head"><h3>Machine status</h3><a class="link" onclick="go('live')">Live data →</a></div><div id="equip">${equipHtml(o)}</div></div>
    <div class="card"><div class="card-head"><h3>Recommended Interventions</h3><a class="link" onclick="go('simulator')">View all →</a></div><div class="rows" id="mini-recs">${miniRecs(recs.items)}</div></div>
    <div class="card"><div class="card-head"><h3>Action History</h3><a class="link" onclick="go('history')">View all →</a></div><div id="mini-hist">${miniHist(acts.items, acts.summary)}</div></div>
  </div>`;
  drawTrend(o.trend, b);
  rollingChart("ov-fc", fc);
};
function attentionHtml(o) {
  const order = { High: 0, Medium: 1, Low: 2 };
  const open = [...o.open_alerts].sort((a, b) => order[a.severity] - order[b.severity] || (a.time < b.time ? 1 : -1));
  if (!open.length) return `<div class="inc-card ok"><div class="sev"></div><div class="main"><div class="ttl">All clear</div><div class="msg">Every machine is within its expected range. New problems will appear here first.</div></div></div>`;
  const card = a => {
    const btn = a.kind === "sensor" ? `<button class="btn ghost" onclick="go('data')">Check instruments</button>`
      : a.machine_id === "PLANT" ? `<button class="btn ghost" onclick="go('forecast')">View forecast</button>`
      : `<button class="btn primary" onclick="openIncident('${a.id}')">View diagnosis →</button>`;
    return `<div class="inc-card ${a.severity}"><div class="sev"></div><div class="main"><div class="ttl">${esc(a.title)}</div><div class="msg">${esc(a.message)}</div></div><div class="meta">${esc(a.machine)}<br>${ago(a.time)}</div>${btn}</div>`;
  };
  const more = open.length > 3 ? `<a class="link small" onclick="go('diagnosis')">${open.length - 3} more in AI Diagnosis →</a>` : "";
  return open.slice(0, 3).map(card).join("") + more;
}
function equipHtml(o) {
  return Object.values(o.latest).map(r => {
    const type = r.machine_type;
    return `<div class="equip" onclick="go('${type}')"><div class="grow"><div class="nm">${esc(r.name)}</div><div class="sub">expected ${fmt(r.expected_co2_kg_h, 0)} kg/h · ${r.excess_pct >= 0 ? "+" : ""}${fmt(r.excess_pct, 1)}%</div></div><div class="num">${fmt(r.co2_kg_h, 0)} <small>kg/h</small></div>${statusPill(r)}</div>`;
  }).join("");
}
function fcMini(fc) {
  const r = fc.rolling;
  const msg = r.cross_date
    ? `<div class="fc-line bad">30-day CO2 budget (${fmt(r.cap_t)} t) crossed on <b>${day(r.cross_date)}</b>, in ${r.days_to_cross} days if nothing is done. ${r.cross_date_with_actions ? `With the top actions: ${day(r.cross_date_with_actions)}.` : "The top actions avoid it."}</div>`
    : `<div class="fc-line ok">No breach of the ${fmt(r.cap_t)} t 30-day budget in the next 3 weeks (now ${fmt(r.current_t, 1)} t).</div>`;
  return msg + todayBox(fc.today);
}
REFRESH.overview = async () => {
  const o = await api(F("/overview"));
  $("#attention") && ($("#attention").innerHTML = attentionHtml(o));
  $("#kpis") && ($("#kpis").innerHTML = kpiRow(o.kpis, machineByType("boiler")));
  $("#equip") && ($("#equip").innerHTML = equipHtml(o));
  if (S.polls % 4 === 0 || S.me.clock.mode === "realtime") {
    const [recs, acts, fc] = await Promise.all([api(F("/recommendations")), api(F("/actions")), api(F("/forecast"))]);
    $("#ov-fc-txt") && ($("#ov-fc-txt").innerHTML = fcMini(fc));
    rollingChart("ov-fc", fc);
    $("#mini-recs") && ($("#mini-recs").innerHTML = miniRecs(recs.items));
    $("#mini-hist") && ($("#mini-hist").innerHTML = miniHist(acts.items, acts.summary));
    drawTrend(o.trend);
  }
};
function drawTrend(trend) {
  trend = trend.slice(1);
  chart("ch-trend", { type: "line", data: { labels: trend.map(r => dt(r.t)), datasets: [
    lineDs("CO2 (kg/h)", trend.map(r => r.co2), C.purple, { fill: true, borderWidth: 1.6 })] },
    options: baseOpts({ plugins: { legend: { display: false } }, scales: { x: { grid: { display: false }, ticks: { autoSkip: false, maxRotation: 0, callback: (v, i) => new Date(trend[i].t).getHours() === 0 ? day(trend[i].t) : "" } }, y: { grid: { color: "#eef2f5" }, title: { display: true, text: "kg CO2 per hour" } } } }) });
}
function perfCard(m, r) {
  return `<div class="card-head"><h3>${m.type === "boiler" ? "Boiler" : "Stenter"} Performance</h3>${statusPill(r)}</div>
    <div class="machine-head">${m.type === "boiler" ? SVG_BOILER : SVG_STENTER}
    <div><div class="muted small">${esc(m.name)}</div><div style="font-size:22px;font-weight:800">${fmt(r?.co2_kg_h, 0)} <small style="font-size:13px">kg CO2/h</small></div>
    <div class="small ${r?.excess_pct > 4 ? "pr-High" : "muted"}">expected ${fmt(r?.expected_co2_kg_h, 0)} · ${r?.excess_pct >= 0 ? "+" : ""}${fmt(r?.excess_pct, 1)}%</div></div></div>
    <div class="params">${paramsHtml(m, r)}</div>`;
}
function todayBox(ft) {
  if (!ft) return `<div class="muted small">Forecast warming up…</div>`;
  const pct = Math.min(120, ft.pct_of_cap);
  return `<div style="display:flex;justify-content:space-between;align-items:baseline"><b>Today vs daily CO2 cap</b><span class="muted small">no-action projection</span></div>
    <div class="gauge"><span style="width:${pct / 1.2}%"></span><i style="left:${100 / 1.2}%"></i></div>
    <div class="small"><b>${fmt(ft.so_far_t, 1)} t</b> so far · projected <b class="${ft.pct_of_cap >= 100 ? "pr-High" : ft.pct_of_cap >= 95 ? "pr-Medium" : ""}">${fmt(ft.projected_t, 1)} t</b> of ${fmt(ft.cap_t, 1)} t (${fmt(ft.pct_of_cap, 0)}%)${ft.cross_time ? ` · <span class="pr-High">crosses at ${ft.cross_time}</span>` : ""}</div>`;
}
function miniRecs(items) {
  if (!items.length) return `<div class="muted">Nothing to recommend — all machines efficient.</div>`;
  return items.slice(0, 3).map((r, i) => `<div class="row" style="cursor:pointer" onclick="openSim('${r.machine_id}','${r.action}')"><div class="num n-${r.priority}">${i + 1}</div><div class="grow"><div class="ttl">${esc(r.title)}</div><div class="meta">Saves about ₹${fmt(r.saving.inr_per_day)} a day and ${fmt(r.saving.co2_t_day, 2)} t CO2 a day · <span class="pr-${r.priority}">${r.priority} priority</span></div></div><span class="muted">›</span></div>`).join("");
}
function miniHist(items, sv) {
  if (!items.length) return `<div class="muted small">No decisions yet. Apply a recommendation and the system will verify its real impact.</div>`;
  return `${sv && sv.co2_avoided_t ? `<div class="small" style="margin-bottom:6px">Verified so far: <b>${fmt(sv.co2_avoided_t, 2)} t CO2</b> avoided · <b>₹${fmt(sv.inr_saved)}</b> saved</div>` : ""}<table><tr><th>Time</th><th>Action</th><th>Status</th><th>Impact</th></tr>${items.slice(0, 5).map(a => `<tr><td>${tm(a.decided_at)}</td><td>${esc(a.title)}</td><td><span class="st ${a.status.replace(/ /g, "")}">${a.status}</span></td><td>${a.actual_kg_h != null ? "CO2 ↓ " + fmt(a.actual_kg_h, 1) + " kg/h" : "—"}</td></tr>`).join("")}</table>`;
}

/* ---------- machine pages ---------- */
async function machinePage(type) {
  const m = machineByType(type);
  const d = await api(F(`/machine/${m.id}?hours=24`));
  const r = S.latest?.[m.id] || { ...d.rows[d.rows.length - 1], anomaly: !!d.open_alert };
  $("#page").innerHTML = `
  <div class="grid g2">
    <div class="card" id="mp-perf">${perfCard(m, r)}</div>
    <div class="card"><div class="card-head"><h3>Root-cause drivers (now)</h3><span class="muted small">counterfactual attribution</span></div>
      <div id="mp-drv">${driverRows(d.drivers)}</div>
      <div class="metric-strip" style="margin-top:14px"><div><b>${fmt(d.metrics.fuel_r2, 3)}</b><span>Digital-twin R² (fuel)</span></div><div><b>${fmt(d.metrics.fuel_mape_pct, 1)}%</b><span>Prediction error (MAPE)</span></div><div><b>${fmt(d.metrics.test_rows)}</b><span>Unseen test points</span></div><div><b>${fmt(d.metrics.naive_mae / Math.max(d.metrics.fuel_mae, .01), 0)}×</b><span>Better than average-baseline</span></div></div></div>
  </div>
  <div class="card"><div class="card-head"><h3>Actual vs expected CO2 — last 24 h</h3><span class="muted small">expected = what this production should emit when healthy · red dots = AI anomaly · dashed = CO2 threshold</span></div><div class="chart-box lg"><canvas id="mp-main"></canvas></div></div>
  <div class="grid g2">
    <div class="card"><h3>${type === "boiler" ? "Flue O2 & stack temperature" : "Exhaust damper & humidity"}</h3><div class="chart-box sm"><canvas id="mp-a"></canvas></div></div>
    <div class="card"><h3>${type === "boiler" ? "Steam generated vs fabric processed" : "Chamber temperature & airflow"}</h3><div class="chart-box sm"><canvas id="mp-b"></canvas></div></div>
  </div>`;
  drawMachine(m, d);
}
function drawMachine(m, d) {
  const rows = d.rows, labels = rows.map(r => tm(r.timestamp));
  chart("mp-main", { type: "line", data: { labels, datasets: [
    lineDs("Actual CO2 (kg/h)", rows.map(r => r.co2_kg_h), C.purple, { fill: false }),
    lineDs("Expected CO2 (kg/h)", rows.map(r => r.expected_co2), C.green, { borderDash: [6, 4] }),
    lineDs("Threshold", rows.map(() => m.co2_threshold_kg_h ?? d.machine.co2_threshold_kg_h), C.red, { borderDash: [3, 3], borderWidth: 1.2 }),
    { label: "AI anomaly", data: rows.map(r => r.anomaly ? r.co2_kg_h : null), showLine: false, borderColor: C.red, backgroundColor: C.red, pointRadius: 3.5, pointHoverRadius: 5 }] },
    options: baseOpts() });
  const A = m.type === "boiler" ? ["flue_o2_pct", "Flue O2 (%)", "exhaust_gas_temp_c", "Stack temp (°C)"] : ["exhaust_damper_pct", "Damper (%)", "exhaust_humidity_kg_kg", "Exhaust humidity (kg/kg)"];
  const B = m.type === "boiler" ? ["steam_generated_kg_h", "Steam (kg/h)", "fabric_processed_kg_h", "Fabric processed (kg/h)"] : ["chamber_temp_c", "Chamber temp (°C)", "airflow_pct", "Airflow (%)"];
  [["mp-a", A, C.orange, C.blue], ["mp-b", B, C.teal, C.purple]].forEach(([id, f, c1, c2]) => chart(id, { type: "line", data: { labels, datasets: [lineDs(f[1], rows.map(r => r[f[0]]), c1, { yAxisID: "y" }), lineDs(f[3], rows.map(r => r[f[2]]), c2, { yAxisID: "y1" })] },
    options: baseOpts({ scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 8 } }, y: { grid: { color: "#eef2f5" } }, y1: { position: "right", grid: { display: false } } } }) }));
}
async function machineRefresh(type) {
  const m = machineByType(type);
  const d = await api(F(`/machine/${m.id}?hours=24`));
  const r = S.latest?.[m.id];
  $("#mp-perf") && ($("#mp-perf").innerHTML = perfCard(m, r));
  $("#mp-drv") && ($("#mp-drv").innerHTML = driverRows(d.drivers));
  const ch = S.charts["mp-main"]; if (!ch) return;
  const fullM = S.me.factories.find(f => f.id === S.fid).machines.find(x => x.id === m.id);
  drawMachine({ ...fullM, co2_threshold_kg_h: d.machine.co2_threshold_kg_h }, d);
}
RENDER.boiler = () => machinePage("boiler"); REFRESH.boiler = () => machineRefresh("boiler");
RENDER.stenter = () => machinePage("stenter"); REFRESH.stenter = () => machineRefresh("stenter");

/* ---------- diagnosis ---------- */
RENDER.diagnosis = async () => {
  const [dx, rep] = await Promise.all([api(F("/diagnosis")), api(F("/model-report")).catch(() => null)]);
  $("#page").innerHTML = `<div class="grid g2" id="dx-cards">${dxCards(dx)}</div>
  <div class="card"><div class="card-head"><h3>Why AI beats a fixed threshold — back-test on unseen plant data</h3><span class="muted small">trained on one month, tested on a different month with new, weaker faults</span></div><div id="rep">${repHtml(rep)}</div></div>
  <div class="card"><div class="card-head"><h3>Alert log</h3></div><div class="alerts" style="max-height:none">${(await api(F("/overview"))).alerts.map(alertItem).join("")}</div></div>`;
};
REFRESH.diagnosis = async () => {
  const dx = await api(F("/diagnosis")); $("#dx-cards") && ($("#dx-cards").innerHTML = dxCards(dx));
  if ($("#rep [data-building]")) { const rep = await api(F("/model-report")).catch(() => null); $("#rep") && ($("#rep").innerHTML = repHtml(rep)); }
};
function dxCards(dx) {
  return dx.machines.map(m => {
    const sev = !m.anomaly ? "Ok" : m.excess_pct >= 10 ? "High" : m.excess_pct >= 5 ? "Medium" : "Low";
    const top = m.drivers[0];
    return `<div class="card"><div class="card-head"><h3>${m.machine.type === "boiler" ? "♨" : "≋"} ${esc(m.machine.name)}</h3>${m.anomaly ? `<span class="status-pill bad">● Anomaly</span>` : `<span class="status-pill ok">● Normal</span>`}</div>
      <div class="dx ${sev}"><div class="ai">${m.anomaly ? "!" : "✓"}</div><span class="tag">${sev === "Ok" ? "Info" : sev}</span>
      <b>${m.anomaly ? `CO2 ${fmt(m.excess_pct, 1)}% above expected` : "Emissions in line with production"}</b>
      <p>Actual ${fmt(m.co2_kg_h, 0)} kg/h · expected ${fmt(m.expected_kg_h, 0)} kg/h for current load${m.anomaly && top ? ` · most likely cause: <b>${esc(top.label)}</b>` : ""}</p></div>
      <div class="card-head" style="margin:10px 0 4px"><b>What is adding extra CO2</b><span class="muted small">root-cause drivers</span></div>${driverRows(m.drivers)}
      ${m.anomaly && m.alert ? `<div style="display:flex;gap:8px;margin-top:12px"><button class="btn green" onclick="openIncident('${m.alert.id}')">View diagnosis →</button></div>` : ""}</div>`;
  }).join("");
}
function repHtml(rep) {
  if (!rep || rep.status === "error") return `<div class="muted">Report unavailable.</div>`;
  if (rep.status !== "ready") return `<div class="muted" data-building="1"><span class="spinner"></span> Running the back-test on a fresh month of plant data (first start only, about a minute)…</div>`;
  const view = v => {
    const tot = { f: 0, a: 0, t: 0, rc: 0, fa: 0, fat: 0, da: [], dt: [] };
    Object.values(rep.machines).forEach(m => { const r = m[v]; tot.f += r.faults; tot.a += r.ai.detected; tot.t += r.threshold.detected; tot.rc += r.ai.root_cause_correct; tot.fa += r.ai.false_alarms_per_day; tot.fat += r.threshold.false_alarms_per_day; tot.da.push(r.ai.median_delay_min); tot.dt.push(r.threshold.median_delay_min); });
    return tot;
  };
  const avg = a => { a = a.filter(x => x != null); return a.length ? a.reduce((x, y) => x + y, 0) / a.length : null; };
  const h = view("held_out"), ins = view("in_sample");
  const rows = Object.entries(rep.machines).map(([k, m]) => { const r = m.held_out; return `<tr><td>${esc(machineName(k))}</td><td>${r.faults}</td><td>${r.ai.detected}</td><td>${fmt(r.ai.median_delay_min)} min</td><td>${r.ai.root_cause_correct}</td><td>${fmt(r.ai.false_alarms_per_day, 2)}</td><td>${r.threshold.detected}</td><td>${r.threshold.median_delay_min == null ? "—" : fmt(r.threshold.median_delay_min) + " min"}</td><td>${fmt(r.threshold.false_alarms_per_day, 2)}</td><td>${fmt(m.in_sample.model.fuel_r2, 3)}</td></tr>`; }).join("");
  return `<div class="vs"><div class="col ai"><h4>Carbon-Action AI</h4><div class="big">${h.a}/${h.f}</div><div>faults detected · ~${fmt(avg(h.da), 0)} min after onset</div><div style="margin-top:6px">Root cause correct: <b>${h.rc}/${h.a}</b> · false alarms: <b>${fmt(h.fa, 2)}</b>/day</div></div>
    <div class="col th"><h4>Fixed CO2 threshold alarm</h4><div class="big">${h.t}/${h.f}</div><div>faults detected · ~${fmt(avg(h.dt), 0)} min after onset</div><div style="margin-top:6px">Root cause: <b>not provided</b> · false alarms: <b>${fmt(h.fat, 2)}</b>/day</div></div></div>
    <table style="margin-top:12px"><tr><th>Machine</th><th>Faults</th><th>AI detected</th><th>AI delay</th><th>Root cause ✓</th><th>AI false alarms/day</th><th>Threshold detected</th><th>Threshold delay</th><th>Threshold false alarms/day</th><th>Twin R²</th></tr>${rows}</table>
    <p class="muted small">Held-out test: models trained on one month, scored on a different month (new random seed, randomised fault timing, weaker faults). On the training month itself the AI scores ${ins.a}/${ins.f}. Faults are injected into a physics-based synthetic plant, not real plant data — the method must be re-validated on real meter data.</p>`;
}

/* ---------- simulator ---------- */
window.openSim = (mid, action) => { S.simState = { mid, action }; go("simulator"); };
RENDER.simulator = async () => {
  const recs = await api(F("/recommendations"));
  const machines = S.me.factories.find(f => f.id === S.fid).machines;
  if (!S.simState) S.simState = recs.items[0] ? { mid: recs.items[0].machine_id, action: recs.items[0].action } : { mid: machines[0].id, action: "tune_o2" };
  $("#page").innerHTML = `<div class="grid" style="grid-template-columns:1fr 1.3fr">
    <div class="card"><div class="card-head"><h3>Recommended Interventions</h3><span class="muted small">best options first</span></div><div class="rows" id="rec-list">${recList(recs.items)}</div></div>
    <div class="card" id="sim-card"><div class="card-head"><h3>Action Simulator</h3></div>
      <div style="display:flex;gap:10px;flex-wrap:wrap"><select id="sim-m" class="select">${machines.map(m => `<option value="${m.id}">${esc(m.name)}</option>`).join("")}</select>
      <select id="sim-a" class="select" style="flex:1"></select></div>
      <div id="sim-custom"></div><div id="sim-out" style="margin-top:14px"></div></div></div>`;
  $("#sim-m").value = S.simState.mid;
  fillActions(); $("#sim-a").value = S.simState.action;
  $("#sim-m").onchange = () => { S.simState.mid = $("#sim-m").value; fillActions(); S.simState.action = $("#sim-a").value; runSim(); };
  $("#sim-a").onchange = () => { S.simState.action = $("#sim-a").value; runSim(); };
  runSim();
};
REFRESH.simulator = async () => { const recs = await api(F("/recommendations")); $("#rec-list") && ($("#rec-list").innerHTML = recList(recs.items)); };
function recList(items) {
  if (!items.length) return `<div class="empty">All machines are running efficiently — no action needed.</div>`;
  return items.map((r, i) => `<div class="row" style="cursor:pointer" onclick="openSim('${r.machine_id}','${r.action}')"><div class="num n-${r.priority}">${i + 1}</div>
    <div class="grow"><div class="ttl">${esc(r.title)}</div><div class="meta">${esc(r.machine_name)} · <span class="pr-${r.priority}">${r.priority} priority</span> · ${Math.round(r.confidence * 100)}% confidence</div>
    <div class="meta">${esc(r.detail)}</div>
    <div class="meta"><b>Saves about ₹${fmt(r.saving.inr_per_day)} a day and ${fmt(r.saving.co2_t_day, 2)} t CO2 a day</b> (${fmt(r.saving.co2_pct, 1)}% less CO2) · takes ${esc(r.time)}${r.downtime_min ? ` · machine stops ${r.downtime_min} min` : ""}</div></div><span class="muted">›</span></div>`).join("");
}
function fillActions() {
  const mid = $("#sim-m").value, m = S.me.factories.find(f => f.id === S.fid).machines.find(x => x.id === mid);
  const acts = Object.entries(S.catalog.actions).filter(([, a]) => a.machine === m.type);
  $("#sim-a").innerHTML = acts.map(([k, a]) => `<option value="${k}">${esc(a.title)}</option>`).join("") + `<option value="custom">Custom scenario (set values)…</option>`;
}
function customSliders(m) {
  const r = S.latest?.[m.id] || {};
  const sl = (k, l, min, max, step, v, u) => `<div class="slider"><label>${l}<span><b id="v-${k}">${v}</b> ${u}</span></label><input type="range" data-k="${k}" min="${min}" max="${max}" step="${step}" value="${v}"></div>`;
  if (m.type === "boiler") return sl("flue_o2_pct", "Flue O2 target", m.fuel === "coal" ? 5.5 : 3, 10, .1, (+r.flue_o2_pct || 6).toFixed(1), "%")
    + sl("exhaust_gas_temp_c", "Stack temperature", 150, 260, 1, Math.round(+r.exhaust_gas_temp_c || 200), "°C")
    + sl("feedwater_temp_c", "Feedwater temperature", 50, 95, 1, Math.round(+r.feedwater_temp_c || 80), "°C");
  return sl("exhaust_damper_pct", "Exhaust damper", 35, 90, 1, Math.round(+r.exhaust_damper_pct || 55), "%")
    + sl("chamber_temp_c", "Chamber temperature", 130, 200, 1, Math.round(+r.chamber_temp_c || 160), "°C")
    + sl("airflow_pct", "Airflow", 60, 95, 1, Math.round(+r.airflow_pct || 90), "%");
}
let simDebounce;
async function runSim() {
  const mid = $("#sim-m").value, action = $("#sim-a").value;
  const m = S.me.factories.find(f => f.id === S.fid).machines.find(x => x.id === mid);
  const cust = $("#sim-custom");
  if (action === "custom") {
    if (!cust.dataset.on || cust.dataset.on !== mid) { cust.innerHTML = customSliders(m); cust.dataset.on = mid; }
    $$("input[type=range]", cust).forEach(inp => inp.oninput = () => { $("#v-" + inp.dataset.k).textContent = inp.value; clearTimeout(simDebounce); simDebounce = setTimeout(runSim, 250); });
  } else { cust.innerHTML = ""; cust.dataset.on = ""; }
  const overrides = {}; $$("input[type=range]", cust).forEach(i => overrides[i.dataset.k] = +i.value);
  const s = await api(F("/simulate"), { method: "POST", body: JSON.stringify({ machine_id: mid, action, overrides }) });
  const unit = m.fuel_unit + "/h";
  const neg = v => v < 0 ? "neg" : "";
  $("#sim-out").innerHTML = `<div style="font-weight:700;margin-bottom:8px">Scenario: ${esc(s.title || "Custom operating point")}</div>
    ${s.detail ? `<div class="muted small" style="margin-bottom:10px">${esc(s.detail)}${s.time ? ` · takes ~${esc(s.time)}` : ""}${s.capex ? ` · capex: ${esc(s.capex)}` : ""}</div>` : ""}
    <div class="cmp"><div class="side"><h4>Right now</h4>
      <div class="kv">CO2<b>${fmt(s.current.co2_kg_h, 1)} kg/h</b></div><div class="kv">Fuel<b>${fmt(s.current.fuel_rate, 2)} ${unit}</b></div>
      <div class="kv">Power<b>${fmt(s.current.power_kw, 1)} kW</b></div><div class="kv">Production<b>${fmt(s.current.production_kg_h, 0)} kg/h</b></div>
      <div class="kv">CO2 per kg fabric<b>${fmt(s.current.co2_intensity_kg_per_kg, 3)}</b></div></div>
    <div class="side sim"><h4>Simulated (with action) ${s.calibration !== 1 ? `<span class="pill" title="Adjusted using measured results from past fixes">adjusted from past results</span>` : ""}</h4>
      <div class="kv">CO2<b>${fmt(s.simulated.co2_kg_h, 1)} kg/h</b></div><div class="kv">Fuel<b>${fmt(s.simulated.fuel_rate, 2)} ${unit}</b></div>
      <div class="kv">Power<b>${fmt(s.simulated.power_kw, 1)} kW</b></div><div class="kv">Production<b>${fmt(s.simulated.production_kg_h, 0)} kg/h</b></div>
      <div class="kv">CO2 per kg fabric<b>${fmt(s.simulated.co2_intensity_kg_per_kg, 3)}</b></div></div></div>
    <div class="impact"><div><span class="pill">Expected benefit (simulated)</span></div><div></div><div></div><div class="muted small">${s.learned_from ? `${Math.round(s.confidence * 100)}% confidence · based on ${esc(s.learned_from)}` : ""}</div>
      <div><div class="iv ${neg(s.saving.fuel_pct)}">${fmt(s.saving.fuel_pct, 1)}%</div><div class="il">less fuel</div></div>
      <div><div class="iv ${neg(s.saving.co2_pct)}">${fmt(s.saving.co2_t_day, 2)} t</div><div class="il">CO2 avoided per day${s.saving.range_kg_h ? `<br>likely ${fmt(s.saving.range_kg_h[0] * .024, 2)}–${fmt(s.saving.range_kg_h[1] * .024, 2)} t` : ""}</div></div>
      <div><div class="iv ${neg(s.saving.inr_per_day)}">₹${fmt(s.saving.inr_per_day)}</div><div class="il">cost saving / day</div></div>
      <div><div class="iv ${s.saving.production_loss_kg ? "neg" : ""}">${s.saving.production_loss_kg ? "−" + fmt(s.saving.production_loss_kg) + " kg" : "No loss"}</div><div class="il">production impact</div></div></div>
    ${safetyBox(s.safety)}
    ${action !== "custom" ? `<div id="explain" class="explain"><button class="btn ghost" id="explain-btn">✦ Explain this recommendation</button></div>
    ${s.saving.co2_kg_h < 1 ? `<div class="callout warn" style="margin-top:12px">No measurable saving expected from this action right now — the cause it fixes is not present.</div>` : ""}
    ${s.can_act === false ? `<div class="callout warn" style="margin-top:12px">This factory is replaying uploaded history, so a decision cannot change the machine.</div>` : S.me.can_decide ? `<div style="display:flex;gap:10px;margin-top:14px;flex-wrap:wrap"><input id="assignee" class="text-input" style="flex:1 1 180px" maxlength="80" placeholder="Assign to (e.g. shift maintenance)"><button class="btn green" id="apply-btn" style="flex:1 1 180px" ${s.saving.co2_kg_h < 1 || s.safety?.status === "blocked" ? "disabled" : ""}>Approve action</button><button class="btn danger" id="reject-btn">Reject</button></div>
    <p class="muted small">After approval, the crew does the work (about ${esc(s.time || "")}). The sensors must then confirm the machine really changed. The system measures the real saving for ${s.measure_h || 2} h, compares it with this simulation, and learns from the result.</p>` : `<p class="muted small">Your account is read-only: an operator or manager approves actions.</p>`}` : `<p class="muted small">Custom scenarios are what-if only. Pick a named action to apply it.</p>`}`;
  if (action !== "custom") {
    $("#explain-btn").onclick = async () => {
      $("#explain").innerHTML = `<span class="spinner"></span> Asking the AI advisor…`;
      const e = await api(F(`/explain?machine_id=${mid}&action=${action}`));
      $("#explain").innerHTML = `${esc(e.answer)}${aiSource(e)}`;
    };
    if (!S.me.can_decide || s.can_act === false) return;
    $("#apply-btn").onclick = () => { const assignee = $("#assignee").value.trim(); confirmBox(`Approve: “${s.title}”?`, `Assigned to ${assignee || "Maintenance crew"}. Predicted saving ${fmt(s.saving.co2_kg_h, 1)} kg CO2/h (₹${fmt(s.saving.inr_per_day)}/day). The result is verified from the sensors after the work is done.`, "Approve", async () => {
      let rec;
      try { rec = await api(F("/actions"), { method: "POST", body: JSON.stringify({ machine_id: mid, action, decision: "apply", assignee: assignee }) }); }
      catch (e) { if (e.message !== "401") toast({ id: "err", severity: "Medium", title: "Could not approve this action", machine: m.name, time: S.now, message: e.message, machine_id: "PLANT" }); return; }
      toast({ id: "x", severity: "Low", title: "Action approved", machine: m.name, time: S.now, message: `${rec.title}: assigned to ${rec.assignee}, due about ${tm(rec.implement_due)}. Predicted −${fmt(rec.predicted_kg_h, 1)} kg CO2/h; the real saving is measured for ${rec.measure_h} h after the work.`, machine_id: "PLANT", kind: "info" });
      go("history");
    }); };
    $("#reject-btn").onclick = () => confirmBox("Reject this action?", "It will be logged so the history shows the decision.", "Reject", async () => {
      await api(F("/actions"), { method: "POST", body: JSON.stringify({ machine_id: mid, action, decision: "reject", reason: "manager decision" }) }); go("history");
    });
  }
}
function safetyBox(sf) {
  if (!sf || sf.status === "ok") return "";
  const title = sf.status === "blocked" ? "⛔ Blocked by a safety limit" : "⚠ Before doing this";
  return `<div class="safety ${sf.status}"><b>${title}</b><ul>${sf.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul></div>`;
}
function confirmBox(title, text, ok, fn) {
  $("#modal-card").innerHTML = `<h3>${esc(title)}</h3><p class="muted">${esc(text)}</p><div class="acts"><button class="btn ghost" id="m-no">Cancel</button><button class="btn green" id="m-ok">${ok}</button></div>`;
  $("#modal").classList.remove("hidden");
  $("#m-no").onclick = () => $("#modal").classList.add("hidden");
  $("#m-ok").onclick = async () => { $("#modal").classList.add("hidden"); await fn(); };
}

/* ---------- forecast ---------- */
RENDER.forecast = async () => {
  const fc = await api(F("/forecast"));
  $("#page").innerHTML = `<div id="fc-top">${forecastHtml(fc)}</div>
  <div class="card"><div class="card-head"><h3>Rolling 30-day CO2 — no action vs with recommended actions</h3><span class="muted small">projection from today · dashed red = no action</span></div><div class="chart-box lg"><canvas id="fc-roll"></canvas></div></div>
  <div class="card"><div class="card-head"><h3>Daily CO2 (t)</h3><span class="muted small">last 14 days + 3-week projection</span></div><div class="chart-box"><canvas id="fc-daily"></canvas></div></div>`;
  drawForecast(fc);
};
REFRESH.forecast = async () => { const fc = await api(F("/forecast")); $("#fc-top") && ($("#fc-top").innerHTML = forecastHtml(fc)); drawForecast(fc); };
function forecastHtml(fc) {
  const t = fc.today, r = fc.rolling;
  const call = r.cross_date
    ? `<div class="callout bad"><b>If nothing is done, the plant crosses its 30-day CO2 budget (${fmt(r.cap_t)} t) on ${day(r.cross_date)} — in ${r.days_to_cross} days.</b><br>
       ${r.excess_t_day > 0 ? `Unresolved anomalies are adding <b>${fmt(r.excess_t_day, 2)} t/day</b>. ` : ""}${r.cross_date_with_actions ? `With the top recommendations the breach moves to <b>${day(r.cross_date_with_actions)}</b>.` : `Applying the top recommendations (−${fmt(r.saving_t_day, 2)} t/day) <b>avoids the breach</b>.`}</div>`
    : `<div class="callout ok"><b>No breach forecast in the next 3 weeks.</b> Rolling 30-day total ${fmt(r.current_t, 1)} t of ${fmt(r.cap_t)} t. Applying the top recommendations would save a further ${fmt(r.saving_t_day, 2)} t/day.</div>`;
  return `${call}
  <div class="grid g3">
    <div class="card"><h3>Today</h3><div class="big-num">${fmt(t.projected_t, 1)} t</div><div class="muted">projected end-of-day vs ${fmt(t.cap_t, 1)} t cap</div>${todayBox(t)}</div>
    <div class="card"><h3>Rolling 30-day total</h3><div class="big-num">${fmt(r.current_t, 1)} t</div><div class="muted">of ${fmt(r.cap_t)} t budget (${fmt(r.current_t / r.cap_t * 100, 1)}%)</div>
      <div class="gauge"><span style="width:${Math.min(100, r.current_t / r.cap_t * 100)}%"></span></div><div class="small muted">daily emissions trend: ${r.trend_t_per_day >= 0 ? "rising" : "falling"} ${fmt(Math.abs(r.trend_t_per_day) * 1000, 0)} kg/day each day</div></div>
    <div class="card"><h3>Time to breach</h3><div class="big-num ${r.cross_date ? "pr-High" : ""}">${r.cross_date ? r.days_to_cross + " days" : "None"}</div><div class="muted">${r.cross_date ? "on " + day(r.cross_date) + " with no action" : "within the 3-week horizon"}</div>
      <div class="small" style="margin-top:8px">With actions: <b>${r.cross_date_with_actions ? day(r.cross_date_with_actions) : "no breach"}</b></div></div>
  </div>`;
}
function rollingChart(id, fc) {
  const s = fc.series, labels = s.map(x => day(x.date));
  chart(id, { type: "line", data: { labels, datasets: [
    lineDs("No action", s.map(x => x.rolling_none), C.red, { segment: { borderDash: c => s[c.p1DataIndex]?.projected ? [6, 4] : undefined } }),
    lineDs("With actions", s.map(x => x.projected ? x.rolling_actions : null), C.green, { borderDash: [6, 4] }),
    lineDs("30-day cap", s.map(() => fc.rolling.cap_t), "#13202b", { borderWidth: 1.4, borderDash: [2, 3] })] },
    options: id === "ov-fc"  // small card: one-line legend, flat date labels
      ? baseOpts({ plugins: { legend: { position: "bottom", labels: { boxWidth: 8, font: { size: 11 } } } },
          scales: { x: { grid: { display: false }, ticks: { maxRotation: 0, maxTicksLimit: 5, font: { size: 11 } } }, y: { grid: { color: "#eef2f5" }, ticks: { font: { size: 11 } } } } })
      : baseOpts() });
}
function drawForecast(fc) {
  const s = fc.series, labels = s.map(x => day(x.date));
  rollingChart("fc-roll", fc);
  chart("fc-daily", { type: "bar", data: { labels, datasets: [{ label: "Daily CO2 (t)", data: s.map(x => x.daily_t), backgroundColor: s.map(x => x.projected ? "#c9b6f2" : C.purple), borderRadius: 4 }] },
    options: baseOpts({ plugins: { legend: { display: false } } }) });
}

/* ---------- history ---------- */
RENDER.history = async () => {
  const d = await api(F("/actions"));
  $("#page").innerHTML = `<div id="hist-top">${histHtml(d)}</div>
  <div class="card"><h3>Prediction accuracy over time</h3><div class="chart-box sm"><canvas id="acc-ch"></canvas></div></div>`;
  drawHist(d);
};
REFRESH.history = async () => { const d = await api(F("/actions")); $("#hist-top") && ($("#hist-top").innerHTML = histHtml(d)); drawHist(d); };
const machineName = mid => S.me.factories.find(f => f.id === S.fid)?.machines.find(m => m.id === mid)?.name || mid;
window.workOrder = async (id, what) => {
  try { await api(F(`/actions/${id}/${what}`), { method: "POST" }); await REFRESH[S.page]?.(); }
  catch (e) { if (e.message !== "401") toast({ id: "err", severity: "Medium", title: "Could not update this action", machine: "", time: S.now, message: e.message, machine_id: "PLANT" }); }
};
function stageNote(a) {
  if (a.status === "Assigned") return `${esc(a.assignee)} · due ~${dt(a.implement_due)}${a.overdue ? ` · <span class="warn-txt">overdue</span>` : ""}`;
  if (a.status === "Measuring") return a.waiting_for ? `waiting for ${esc(a.waiting_for)}` : `done by ${esc(a.done_by)} ${tm(a.implemented_at)}${a.confirmed ? " · change confirmed" : ""} · result ~${dt(a.measure_until)}`;
  if (a.status === "Verified") return `done by ${esc(a.done_by)} · ${a.credit_ended ? "no longer counted: " + esc(a.credit_ended) : `counted for up to ${a.credit_days} days`}`;
  return a.reason ? esc(a.reason) : "";
}
function histHtml(d) {
  const sm = d.summary;
  const learned = Object.entries(d.learning).flatMap(([mid, acts]) => Object.entries(acts).map(([k, v]) => ({ mid, k, v })));
  return `<div class="grid g4">
    ${kpiCard("✓", C.green, "Actions verified", sm.verified, "", null)}
    ${kpiCard("◎", C.blue, "Prediction accuracy", sm.mean_accuracy_pct == null ? "—" : fmt(sm.mean_accuracy_pct, 1), "%", null)}
    ${kpiCard("☁", C.purple, "CO2 avoided (verified)", fmt(sm.co2_avoided_t, 2), " t", null)}
    ${kpiCard("₹", C.orange, "Money saved (verified)", "₹" + fmt(sm.inr_saved), "", null)}
  </div>
  <div class="card"><div class="card-head"><h3>Decisions & verified outcomes</h3><span class="muted small">actual = adjusted-baseline measurement after the action · <a class="link" href="${F("/actions.csv")}">CSV</a> · <a class="link" href="${F("/report.pdf")}">PDF report</a></span></div>
  ${d.items.length ? `<table><tr><th>Decided</th><th>Machine</th><th>Action</th><th>Approved by</th><th>Status</th><th>Predicted</th><th>Actual</th><th>Accuracy</th><th>Model learned</th></tr>
  ${d.items.map(a => `<tr><td>${dt(a.decided_at)}</td><td>${esc(machineName(a.machine_id))}</td><td>${esc(a.title)}</td><td>${esc(a.decided_by)}</td><td><span class="st ${a.status.replace(/ /g, "")}">${a.status}</span><div class="muted small">${stageNote(a)}</div>${a.status === "Assigned" && S.me.can_decide ? `<button class="btn green xs" onclick="workOrder('${a.id}','done')">Mark done</button><button class="btn ghost xs" onclick="workOrder('${a.id}','cancel')">Cancel</button>` : ""}</td>
    <td>${a.predicted_kg_h != null ? "−" + fmt(a.predicted_kg_h, 1) + " kg/h" + (a.predicted_range_kg_h ? `<div class="muted small">range ${fmt(a.predicted_range_kg_h[0], 1)}–${fmt(a.predicted_range_kg_h[1], 1)}</div>` : "") : "—"}</td><td>${a.actual_kg_h != null ? "−" + fmt(a.actual_kg_h, 1) + " kg/h" : "—"}</td>
    <td>${a.accuracy_pct != null ? fmt(a.accuracy_pct, 1) + "%" : "—"}</td><td>${a.calibration_after ? "factor → ×" + a.calibration_after : "—"}</td></tr>`).join("")}</table>`
      : `<div class="empty">No decisions yet. Go to the Action Simulator, approve a recommendation, and watch it get verified here.</div>`}</div>
  <div class="card"><div class="card-head"><h3>What the AI has learned</h3><span class="muted small">per machine · calibration = real ÷ predicted saving · machines without their own result borrow from similar machines</span></div>
    ${learned.length ? `<table><tr><th>Machine</th><th>Action</th><th>Times verified</th><th>Calibration</th><th>Confidence</th><th>Recent accuracy</th></tr>${learned.map(({ mid, k, v }) => `<tr><td>${esc(machineName(mid))}</td><td>${esc(S.catalog.actions[k]?.title || k)}</td><td>${v.n}</td><td>×${v.calibration}</td><td>${Math.round(v.confidence * 100)}%</td><td>${v.accuracy.map(x => fmt(x, 0) + "%").join(", ")}</td></tr>`).join("")}</table>`
      : `<div class="muted small">Nothing learned yet.</div>`}</div>`;
}
function drawHist(d) {
  const a = d.summary.accuracy_trend;
  chart("acc-ch", { type: "line", data: { labels: a.map((_, i) => "#" + (i + 1)), datasets: [lineDs("Accuracy %", a, C.green, { pointRadius: 4, fill: true })] },
    options: baseOpts({ scales: { y: { min: 0, max: 100 } }, plugins: { legend: { display: false } } }) });
}

/* ---------- incident ---------- */
window.openIncident = id => { S.incident = id; S.incAssignee = ""; S.incExplain = null; go("incident"); };
RENDER.incident = async () => {
  const d = await api(F(`/incident/${S.incident}`));
  $("#page").innerHTML = `<div id="inc-body">${incidentHtml(d)}</div>
    <div class="card"><div class="card-head"><h3>Actual vs expected CO2</h3><span class="muted small">last 6 hours · kg/h</span></div><div class="chart-box sm"><canvas id="inc-ch"></canvas></div></div>`;
  incidentWire(d); drawIncident(d);
};
REFRESH.incident = async () => {
  const d = await api(F(`/incident/${S.incident}`));
  if (!$("#inc-body")) return;
  $("#inc-body").innerHTML = incidentHtml(d); incidentWire(d); drawIncident(d);
};
function drawIncident(d) {
  const x = d.series;
  chart("inc-ch", { type: "line", data: { labels: x.t.map(tm), datasets: [
    lineDs("Actual", x.actual, C.purple), lineDs("Expected", x.expected, C.green, { borderDash: [6, 4] })] }, options: baseOpts() });
}
function incidentSteps(d) {
  const st = d.action?.status;
  const steps = [["Anomaly detected", "done"], ["Simulate and decide", "now"], ["Crew work", ""], ["Saving verified", ""]];
  if (st === "Assigned") { steps[1][1] = "done"; steps[2][1] = "now"; }
  if (st === "Measuring") { steps[1][1] = steps[2][1] = "done"; steps[3][1] = "now"; }
  if (st === "Verified") { steps[1][1] = steps[2][1] = steps[3][1] = "done"; }
  if (st === "Not confirmed") { steps[1][1] = "done"; steps[2][1] = "fail"; }
  if (d.alert.resolved && !d.action) steps[1][1] = "";
  return `<div class="stepper">${steps.map(([n, c]) => `<div class="s ${c}">${c === "done" ? "✓ " : c === "fail" ? "✕ " : ""}${n}</div>`).join("")}</div>`;
}
function incidentHtml(d) {
  const a = d.alert, top = a.drivers[0];
  const status = a.resolved ? `<span class="status-pill ok">● Resolved ${tm(a.resolved_at)}</span>` : `<span class="status-pill bad">● ${esc(a.severity)} · open ${ago(a.time).replace(" ago", "")}</span>`;
  return `<div class="card"><div class="card-head"><div><div class="muted small">${esc(d.machine.name)} · started ${dt(a.time)}</div><h3 style="font-size:19px;margin-top:2px">${esc(top ? top.label : a.title)}</h3></div>${status}</div>${incidentSteps(d)}</div>
  <div class="inc-grid">
    <div class="card"><div class="card-head"><h3>Root-cause drivers</h3><span class="muted small">extra CO2 from each likely cause</span></div>
      <p class="small muted" style="margin:0 0 10px">${esc(a.message)}</p>${driverRows(a.drivers)}</div>
    <div class="card">${incidentAction(d)}</div>
  </div>`;
}
function incidentAction(d) {
  const r = d.action, p = d.proposal, note = [];
  if (r && !["Rejected", "Cancelled", "Not confirmed"].includes(r.status)) {
    const acts = r.status === "Assigned" && S.me.can_decide ? `<div class="inc-actions"><button class="btn green" id="inc-done">Mark as done</button><button class="btn ghost" id="inc-cancel">Cancel action</button></div>` : "";
    const result = r.status === "Verified" ? `<div class="stat3"><div><span class="l">Predicted</span><b>${fmt(r.predicted_kg_h, 1)} kg/h</b></div><div class="good"><span class="l">Measured</span><b>${fmt(r.actual_kg_h, 1)} kg/h</b></div><div><span class="l">Accuracy</span><b>${r.accuracy_pct == null ? "—" : fmt(r.accuracy_pct, 0) + "%"}</b></div></div>` : "";
    return `<div class="card-head"><h3>Action progress</h3><span class="st ${r.status}">${r.status}</span></div>
      <div style="font-weight:700">${esc(r.title)}</div><p class="small muted">${stageNote(r)}</p>${result}
      ${(r.safety_notes || []).length ? safetyBox({ status: "caution", notes: r.safety_notes }) : ""}${acts}`;
  }
  if (r) note.push(`<div class="callout warn" style="margin-bottom:10px"><b>${r.status}.</b> ${esc(r.reason || "")}</div>`);
  if (d.alert.resolved) return `<div class="card-head"><h3>Outcome</h3></div>${note.join("")}<p class="muted">This problem cleared without a verified action.</p>`;
  if (!p) return `<div class="card-head"><h3>Simulated fix</h3></div>${note.join("")}<p class="muted">No fix is available for this problem yet.</p>`;
  const blocked = p.safety?.status === "blocked", tiny = p.saving.co2_kg_h < 1;
  const range = p.saving.range_kg_h;
  const buttons = !d.can_act ? `<div class="callout warn" style="margin-top:12px">This factory is replaying uploaded history, so a decision cannot change the machine.</div>`
    : !S.me.can_decide ? `<p class="muted small">Your account is read-only: an operator or manager approves actions.</p>`
    : `<div class="inc-actions"><input id="inc-assignee" class="text-input" style="flex:1 1 170px" maxlength="80" placeholder="Assign to (e.g. shift maintenance)" value="${esc(S.incAssignee || "")}"><button class="btn green" id="inc-apply" ${blocked || tiny ? "disabled" : ""}>Approve action</button><button class="btn danger" id="inc-reject">Reject</button></div>`;
  return `<div class="card-head"><h3>Simulated fix</h3><a class="link" onclick="openSim('${d.machine.id}','${p.action}')">Open in Action Simulator →</a></div>${note.join("")}
    <div style="font-weight:700;font-size:15px">${esc(p.title)}</div><p class="small muted" style="margin:4px 0 0">${esc(p.detail)}</p><div class="muted small" style="margin-top:4px">Takes ${esc(p.time)} · saving is measured for ${p.measure_h} h afterwards · new equipment cost: ${esc(p.capex)}</div>
    <div class="stat3"><div class="good"><span class="l">Less CO2 per day</span><b>${range ? `${fmt(range[0] * .024, 1)}–${fmt(range[1] * .024, 1)} t` : fmt(p.saving.co2_t_day, 2) + " t"}</b></div><div class="good"><span class="l">Money saved per day</span><b>₹${fmt(p.saving.inr_per_day)}</b></div><div><span class="l">Confidence</span><b>${Math.round(p.confidence * 100)}%</b></div></div>
    ${safetyBox(p.safety)}${tiny ? `<div class="callout warn" style="margin-top:12px">No measurable saving is expected from this right now.</div>` : ""}
    <div id="inc-explain" class="explain" style="margin-top:12px">${S.incExplain === "busy" ? `<span class="spinner"></span> Asking the AI advisor…` : S.incExplain ? `${esc(S.incExplain.answer)}${aiSource(S.incExplain)}` : `<button class="btn ghost" id="inc-explain-btn">✦ Explain in plain words</button>`}</div>${buttons}`;
}
function incidentWire(d) {
  const p = d.proposal, mid = d.machine.id, r = d.action;
  const input = $("#inc-assignee");
  if (input) input.oninput = () => { S.incAssignee = input.value; };
  const send = async (body, what) => {
    try { await api(F("/actions"), { method: "POST", body: JSON.stringify(body) }); REFRESH.incident(); }
    catch (e) { if (e.message !== "401") toast({ id: "err", severity: "Medium", title: what, machine: d.machine.name, time: S.now, message: e.message, machine_id: "PLANT", kind: "info" }); }
  };
  if ($("#inc-apply")) $("#inc-apply").onclick = () => confirmBox(`Approve: “${p.title}”?`, `Assigned to ${S.incAssignee.trim() || "Maintenance crew"}. The result is verified from the sensors after the work is done.`, "Approve",
    () => send({ machine_id: mid, action: p.action, decision: "apply", assignee: S.incAssignee.trim() }, "Could not approve this action"));
  if ($("#inc-reject")) $("#inc-reject").onclick = () => confirmBox("Reject this action?", "The decision is logged in the action history.", "Reject",
    () => send({ machine_id: mid, action: p.action, decision: "reject", reason: "manager decision" }, "Could not reject"));
  if ($("#inc-explain-btn")) $("#inc-explain-btn").onclick = async () => {
    S.incExplain = "busy"; $("#inc-explain").innerHTML = `<span class="spinner"></span> Asking the AI advisor…`;
    try { S.incExplain = await api(F(`/explain?machine_id=${mid}&action=${p.action}`)); }
    catch (e) { S.incExplain = e.message === "stale" || e.message === "401" ? null : { answer: "Could not get an explanation: " + e.message, source: "local" }; }
    REFRESH.incident();
  };
  if ($("#inc-done")) $("#inc-done").onclick = () => workOrder(r.id, "done");
  if ($("#inc-cancel")) $("#inc-cancel").onclick = () => workOrder(r.id, "cancel");
}

/* ---------- live data ---------- */
RENDER.live = async () => {
  S.liveFilter = S.liveFilter || "all"; S.liveNewest = null;
  const machines = S.me.factories.find(f => f.id === S.fid).machines;
  $("#page").innerHTML = `<div class="card"><div class="live-head"><div><h3>Machine readings</h3><div class="muted small" id="live-next"></div></div>
    <div class="seg" id="live-seg"><button data-m="all">All machines</button>${machines.map(m => `<button data-m="${m.id}">${m.type === "boiler" ? "Boiler" : "Stenter"}</button>`).join("")}</div></div>
    <div id="live-issues"></div><div class="table-wrap" id="live-table"></div>
    <p class="ev-note"><b style="color:#a11f1f">Red</b>: the AI flags this reading. <b style="color:#8a6100">Amber</b>: more than 4% above expected. O2 and stack temperature are shown for the boiler, damper and chamber temperature for the stenter.</p></div>`;
  $$("#live-seg button").forEach(b => b.onclick = () => { S.liveFilter = b.dataset.m; S.liveNewest = null; REFRESH.live(); });
  await REFRESH.live();
};
REFRESH.live = async () => {
  const d = await api(F("/readings?limit=48"));
  if (!$("#live-table")) return;
  $$("#live-seg button").forEach(b => b.classList.toggle("on", b.dataset.m === S.liveFilter));
  const rows = d.rows.filter(r => S.liveFilter === "all" || r.machine_id === S.liveFilter);
  const newest = rows[0]?.time;
  const sig = r => r.type === "boiler" ? `O2 ${fmt(r.signal_a, 1)}% · ${fmt(r.signal_b, 0)} °C` : `Damper ${fmt(r.signal_a, 0)}% · ${fmt(r.signal_b, 0)} °C`;
  $("#live-table").innerHTML = `<table class="live-table"><tr><th>Time</th><th>Machine</th><th>Status</th><th>Key signals</th><th>Fuel</th><th>Power kW</th><th>CO2 kg/h</th><th>Expected</th><th>Difference</th></tr>
    ${rows.map(r => `<tr class="${r.alarm ? "alarm" : r.excess_pct > 4 ? "watch" : ""} ${r.time === newest && S.liveNewest && S.liveNewest !== newest ? "fresh" : ""}"><td>${tm(r.time)}</td><td>${esc(r.machine)}</td><td>${esc(r.status)}</td><td>${sig(r)}</td><td>${fmt(r.fuel, 1)} ${esc(r.fuel_unit)}</td><td>${fmt(r.power_kw, 0)}</td><td><b>${fmt(r.co2, 0)}</b></td><td>${fmt(r.expected, 0)}</td><td>${r.excess_pct >= 0 ? "+" : ""}${fmt(r.excess_pct, 1)}%</td></tr>`).join("")}</table>`;
  S.liveNewest = newest;
  const issues = Object.values(d.issues).flat();
  $("#live-issues").innerHTML = issues.length ? `<div class="safety caution" style="margin:0 0 10px"><b>Check instruments:</b> ${issues.map(i => esc(i.detail)).join(" ")}</div>` : "";
  S.liveClock = d;
  liveTick();
};
function liveTick() {
  const el = $("#live-next"), d = S.liveClock;
  if (!el || !d) return;
  if (d.clock.mode === "replay") { el.textContent = d.clock.finished ? "Replay finished" : `Replaying ${d.source.name || "uploaded data"}`; return; }
  if (d.clock.mode === "fast") { el.textContent = `Fast-forward: a new reading every ${d.clock.seconds_per_reading} s`; return; }
  const left = Math.max(0, new Date(d.now).getTime() + 300000 - Date.now());
  el.textContent = d.clock.ahead_min ? `Waiting for real time to catch up (${d.clock.ahead_min} min)` : `Next reading in ${String(Math.floor(left / 60000)).padStart(2, "0")}:${String(Math.floor(left / 1000) % 60).padStart(2, "0")}`;
}
setInterval(liveTick, 1000);

/* ---------- data & evidence ---------- */
RENDER.data = async () => {
  const [st, ev, nugen] = await Promise.all([api(F("/data/status")), api("/api/evidence").catch(() => ({ report: null })), api("/api/nugen/status").catch(() => null)]);
  $("#page").innerHTML = `<div class="grid g2">
    <div class="card"><div class="card-head"><h3>Data source</h3></div><div id="data-src">${dataSrc(st)}</div>
      ${S.me.role === "admin" ? `<div id="upload-box" style="margin-top:12px;${st.source.kind === "csv" ? "display:none" : ""}"><b class="small">Use your own plant data</b>
        <div class="link-row"><input id="csv-file" type="file" accept=".csv" class="text-input" style="flex:1 1 200px"><button id="csv-go" class="btn primary">Upload &amp; analyse</button></div>
        <p class="ev-note">CSV with readings for both machines, at least ${7} days. The AI trains on the first 70% and replays the rest as if live. <a class="link" href="${F("/data/template.csv")}">Download template</a></p><div id="csv-msg" class="ev-note"></div></div>` : ""}</div>
    <div class="card"><div class="card-head"><h3>Instrument health</h3><span class="muted small">is the sensor wrong, or the machine?</span></div><div id="data-health">${dataHealth(st)}</div></div>
  </div>
  <div class="card"><div class="card-head"><h3>AI advisor (Nugen)</h3><span class="muted small">since this app started</span></div><div id="data-nugen">${nugenCard(nugen)}</div></div>
  <div class="card"><div class="card-head"><h3>Reports</h3></div><div class="link-row">
    <a class="btn primary" href="${F("/report.pdf")}">Monthly PDF report</a><a class="btn ghost" href="${F("/actions.csv")}">Decisions (CSV)</a><a class="btn ghost" href="${F("/export.csv")}">All readings (CSV)</a></div></div>
  <div class="card"><div class="card-head"><h3>How far to trust the results</h3><span class="muted small">stress tests on simulated plant data</span></div>${evidenceHtml(ev.report)}</div>`;
  const go = $("#csv-go");
  if (go) go.onclick = uploadCsv;
  const stop = $("#stop-replay");
  if (stop) stop.onclick = stopReplay;
};
REFRESH.data = async () => {
  const st = await api(F("/data/status"));
  $("#data-src") && ($("#data-src").innerHTML = dataSrc(st));
  $("#data-health") && ($("#data-health").innerHTML = dataHealth(st));
  const nugen = await refreshAdvisor();
  $("#data-nugen") && ($("#data-nugen").innerHTML = nugenCard(nugen));
  const stop = $("#stop-replay");
  if (stop) stop.onclick = stopReplay;
};
async function stopReplay() { await api(F("/data/stop"), { method: "POST" }); S.now = null; go("data"); }
function nugenCard(n) {
  if (!n) return `<div class="muted">Status unavailable.</div>`;
  if (n.provider !== "nugen") return `<div class="muted">Not configured: answers come from local rules. Add <code>NUGEN_API_KEY</code> and <code>NUGEN_MODEL_ID</code> to <code>backend/.env</code>.</div>`;
  const u = n.usage;
  const state = n.deployment === "DEPLOYED" ? `<span class="ok-txt">Connected</span>` : `<span class="warn-txt">${n.reachable ? "Model not deployed" : "Nugen unreachable"}</span>`;
  return `<div class="stat3" style="grid-template-columns:repeat(4,minmax(0,1fr));margin-top:0">
      <div><span class="l">Status</span><b style="font-size:14px">${state}</b></div>
      <div><span class="l">AI answers from Nugen</span><b>${fmt(u.ok)}</b></div>
      <div><span class="l">Failed calls</span><b>${fmt(u.failed)}</b></div>
      <div><span class="l">Tokens used</span><b>${fmt(u.tokens)}</b></div></div>
    <p class="ev-note">Model: ${esc(n.model_name || "")} (${esc(n.model)}), aligned from ${esc(n.base_model || "—")}. ${u.last_ok_at ? `Last answer ${esc(u.last_ok_at.replace("T", " "))} in ${fmt(u.last_latency_s, 1)} s.` : "No answers yet."}${u.last_error ? ` Last error: ${esc(u.last_error)}.` : ""}</p>
    <p class="ev-note">Nugen writes the chat answers and explanations. Fault detection, root cause and savings come from this project's own models.</p>`;
}
function dataSrc(st) {
  const s = st.source, m0 = st.machines[0];
  if (s.kind === "simulator") return `<div><b>Simulated plant</b> (physics-based, not real meters)</div>
    <p class="ev-note">Data from ${dt(m0.first)} to ${dt(m0.last)}. Readings, alerts and decisions are saved and survive a restart.</p>`;
  return `<div><b>Uploaded file:</b> ${esc(s.name)}</div>
    <div class="gauge"><span style="width:${s.total ? s.played / s.total * 100 : 0}%"></span></div>
    <p class="ev-note">${s.finished ? "Replay finished." : `Replaying reading ${s.played} of ${s.total}.`} Replayed history cannot be changed, so decisions are off. Not saved after a restart.</p>
    ${(s.warnings || []).map(w => `<p class="ev-note">⚠ ${esc(w)}</p>`).join("")}
    ${S.me.role === "admin" ? `<button id="stop-replay" class="btn ghost">Back to the simulated plant</button>` : ""}`;
}
function dataHealth(st) {
  return `<table><tr><th>Machine</th><th>Status</th><th>No fuel data (24 h)</th></tr>${st.machines.map(m => `<tr><td>${esc(m.name)}</td><td>${m.issues.length ? m.issues.map(i => `<div class="warn-txt">${esc(i.label)}: ${esc(i.problem)}</div><div class="muted small">${esc(i.detail)}</div>`).join("") : `<span class="ok-txt">All instruments OK</span>`}</td><td>${fmt(m.missing_pct_24h, 1)}%</td></tr>`).join("")}</table>
  <p class="ev-note">Checks: frozen signals, lost signals, values outside the physical range, fuel that the process sensors cannot explain, and flue O2 that the fan speed does not support.</p>`;
}
function evidenceHtml(r) {
  if (!r) return `<div class="muted">Not run yet. From the backend folder run <code>python -m app.robustness</code> (several minutes).</div>`;
  const rng = (s, d = 0) => s.mean == null ? "—" : `${fmt(s.mean, d)} <span class="muted small">(${fmt(s.min, d)}–${fmt(s.max, d)})</span>`;
  const names = { baseline: "Unseen months, 60–100% fault strength", noisy_meters: "Noisier meters (±1.5% extra error)", weak_faults: "Weak faults, 30–60% strength" };
  const rows = Object.entries(r.scenarios).map(([k, s]) => `<tr><td>${names[k] || k}</td><td>${s.detected}/${s.faults}<div class="muted small">${rng(s.detected_pct)}%</div></td><td>${s.root_cause_correct}/${s.detected}</td><td>${rng(s.delay_min)} min</td><td>${rng(s.false_alarms_per_machine_day, 2)}</td><td>${s.threshold_detected}/${s.faults}</td><td>${rng(s.threshold_delay_min)} min</td></tr>`).join("");
  const o = r.overlapping, ins = r.instruments, v = r.verification;
  return `${r.current ? "" : `<div class="callout warn">These results were built with an older version of the model code. Re-run <code>python -m app.robustness</code> before quoting them.</div>`}
    <table><tr><th>Test</th><th>AI detected</th><th>Correct cause</th><th>AI delay</th><th>False alarms per machine per day</th><th>Fixed alarm detected</th><th>Fixed alarm delay</th></tr>${rows}</table>
    <p class="ev-note">Brackets show the lowest and highest result across ${r.months_per_plant} test months per plant (2 plants, 2 machines each).</p>
    <div class="grid g3" style="margin-top:12px">
      <div><b>Two faults at once</b><p class="ev-note">${o.detected}/${o.episodes} detected. The top cause was one of the two in ${o.top_cause_is_one_of_them}; both were in the top two in ${o.both_in_top_two}.</p></div>
      <div><b>Faulty instruments</b><p class="ev-note">${ins.detected}/${ins.total} sensor faults found. A process alert came first in ${ins.process_alert_first}. False instrument alarms: ${fmt(ins.false_alarms_per_machine_day.mean, 2)} per machine per day.</p></div>
      <div><b>Is the "actual saving" right?</b><p class="ev-note">Same plant run twice, with and without the fix, so the true saving is known. Measured saving: median error ${fmt(v.measured.median_error_pct, 1)}%, within ±20% in ${v.measured.within_20pct}/${v.measured.cases} cases (average bias ${fmt(v.measured.mean_bias_pct, 1)}%). Prediction before the fix: median error ${fmt(v.predicted.median_error_pct, 1)}%.</p></div>
    </div>
    <p class="ev-note"><b>Limit of this evidence:</b> every test uses simulated plant data from the same family the models learned from. It shows the method holds up under stress; it does not prove real-plant performance.</p>`;
}
async function uploadCsv() {
  const f = $("#csv-file").files[0], msg = $("#csv-msg");
  if (!f) { msg.textContent = "Choose a CSV file first."; return; }
  msg.innerHTML = `<span class="spinner"></span> Checking the file and training the models (about a minute)…`;
  try {
    const r = await fetch(F(`/data/upload?name=${encodeURIComponent(f.name)}`), { method: "POST", headers: { "Content-Type": "text/csv" }, credentials: "same-origin", body: f });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.statusText);
    S.now = null; S.seen.clear(); go("data");
  } catch (e) { msg.textContent = "Upload failed: " + e.message; }
}

/* ---------------- AI answers ---------------- */
function aiSource(r) {
  const check = r.unverified_numbers?.length ? `<div class="ai-check">⚠ Check before relying on this: ${r.unverified_numbers.map(esc).join(", ")} not found in the plant data.</div>` : "";
  if (r.source === "nugen") return `${check}<span class="src">Nugen-aligned model · ${esc(r.model || "")}${r.confidence != null ? ` · Nugen confidence ${fmt(r.confidence, 0)}%` : ""}</span>`;
  return `<span class="src">${r.error ? "Local advisor (Nugen failed: " + esc(r.error) + ")" : "Local advisor (Nugen not configured)"}</span>`;
}
function advisorFooter(n) {
  if (n.provider !== "nugen") return "AI advisor: local rules (add Nugen key in .env)";
  if (n.deployment === "DEPLOYED") return `AI advisor: Nugen connected (${n.model})`;
  return n.reachable ? `AI advisor: Nugen model not deployed (${n.model}), using local rules` : "AI advisor: Nugen unreachable, using local rules";
}
async function refreshAdvisor() {
  try { const n = await api("/api/nugen/status"); $("#llm-status").textContent = advisorFooter(n); return n; } catch (e) { return null; }
}

/* ---------------- assistant ---------------- */
$("#chat-fab").onclick = () => { $("#chat").classList.toggle("hidden"); $("#chat-in").focus(); };
$("#chat-x").onclick = () => $("#chat").classList.add("hidden");
$$("#chat .chips button").forEach(b => b.onclick = () => askAI(b.dataset.q));
$("#chat-form").onsubmit = e => { e.preventDefault(); const q = $("#chat-in").value.trim(); if (q) askAI(q); $("#chat-in").value = ""; };
async function askAI(q) {
  const log = $("#chat-log");
  log.insertAdjacentHTML("beforeend", `<div class="msg me">${esc(q)}</div><div class="msg bot" id="pending"><span class="spinner"></span></div>`);
  log.scrollTop = log.scrollHeight;
  try {
    const r = await api(F("/assistant"), { method: "POST", body: JSON.stringify({ question: q }) });
    $("#pending").outerHTML = `<div class="msg bot">${esc(r.answer)}${aiSource(r)}</div>`;
  } catch (e) { $("#pending").outerHTML = `<div class="msg bot">Sorry, ${esc(e.message)}</div>`; }
  log.scrollTop = log.scrollHeight;
}

/* ---------------- demo panel ---------------- */
function buildDemo(cat) {
  $("#demo-faults").innerHTML = Object.entries(cat.faults).map(([k, f]) => `<button data-f="${k}"><b>${f.machine === "boiler" ? "♨" : "≋"} ${esc(f.label)}</b>${esc(f.desc)}</button>`).join("");
  $("#demo-sensors").innerHTML = Object.entries(cat.sensor_faults || {}).map(([k, f]) => `<button data-f="${k}"><b>${f.machine === "boiler" ? "♨" : "≋"} ${esc(f.label)}</b>${esc(f.desc)}</button>`).join("");
  $$("#demo-faults button, #demo-sensors button").forEach(b => b.onclick = async () => { await api(F("/demo/inject"), { method: "POST", body: JSON.stringify({ fault: b.dataset.f }) }); b.style.borderColor = "#f08a24"; });
}
$("#demo-btn").onclick = () => $("#demo").classList.toggle("hidden");
$("#demo-x").onclick = () => $("#demo").classList.add("hidden");
document.addEventListener("keydown", e => { if (S.me?.demo && e.shiftKey && e.key === "D" && !/input|select|textarea/i.test(e.target.tagName)) $("#demo").classList.toggle("hidden"); });
$("#demo-clear").onclick = async () => { await api(F("/demo/clear"), { method: "POST" }); $$("#demo-faults button, #demo-sensors button").forEach(b => b.style.borderColor = ""); };
$("#demo-pause").onclick = async () => { const r = await api("/api/demo/pause", { method: "POST" }); $("#demo-pause").textContent = r.paused ? "Resume" : "Pause"; };
$("#demo-speed").onchange = async () => {
  const v = +$("#demo-speed").value;
  S.me.clock = await api(`/api/demo/speed?seconds=${v}`, { method: "POST" });
  startPolling();
};
document.addEventListener("click", e => { if (!e.target.closest(".top-right")) $("#user-menu").classList.add("hidden"); });

/* ---------------- start ---------------- */
boot();

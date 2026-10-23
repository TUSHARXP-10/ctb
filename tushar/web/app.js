/* Tushar.ai dashboard. Vanilla JS, no build step. */
(() => {
  "use strict";

  // ------------------------------------------------------------------ helpers
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const isNum = (n) => typeof n === "number" && isFinite(n);
  const fmt = {
    usd(n, d = 2) { return isNum(n) ? (n < 0 ? "-" : "") + "$" + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }) : "-"; },
    signed(n, d = 2) { return isNum(n) ? (n > 0 ? "+" : n < 0 ? "-" : "") + "$" + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }) : "-"; },
    pct(n, d = 2) { return isNum(n) ? (n > 0 ? "+" : "") + n.toFixed(d) + "%" : "-"; },
    price(n) {
      if (!isNum(n)) return "-";
      const a = Math.abs(n);
      const d = a >= 1000 ? 2 : a >= 100 ? 3 : a >= 1 ? 4 : a >= 0.01 ? 5 : 7;
      return n.toLocaleString(undefined, { minimumFractionDigits: Math.min(d, 2), maximumFractionDigits: d });
    },
    num(n, d = 2) { return isNum(n) ? n.toLocaleString(undefined, { maximumFractionDigits: d }) : "-"; },
    big(n) { if (!isNum(n)) return "-"; const a = Math.abs(n); return a >= 1e9 ? (n / 1e9).toFixed(2) + "B" : a >= 1e6 ? (n / 1e6).toFixed(1) + "M" : a >= 1e3 ? (n / 1e3).toFixed(1) + "K" : n.toFixed(0); },
    time(ts) { return ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "-"; },
    dt(ts) { return ts ? new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "-"; },
    ago(ts) {
      if (!ts) return "-";
      const s = Date.now() / 1000 - ts;
      if (s < 60) return Math.max(0, Math.round(s)) + "s ago";
      if (s < 3600) return Math.round(s / 60) + "m ago";
      if (s < 86400) return (s / 3600).toFixed(1) + "h ago";
      return Math.round(s / 86400) + "d ago";
    },
    dur(s) { if (!isNum(s)) return "-"; s = Math.max(0, Math.round(s)); const m = Math.floor(s / 60); return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : m ? `${m}m ${s % 60}s` : `${s}s`; },
  };
  const cls = (n) => (isNum(n) ? (n > 0 ? "up" : n < 0 ? "down" : "") : "");
  const sym = (s) => String(s || "").replace(/USDT$/, "");

  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
    if (res.status === 401) { location.href = "/login"; throw new Error("login required"); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || data.error || res.statusText);
    return data;
  }

  function toast(text, kind = "") {
    const t = document.createElement("div");
    t.className = "toast " + kind;
    t.textContent = text;
    $("toasts").appendChild(t);
    setTimeout(() => t.remove(), 6500);
  }

  // ------------------------------------------------------------------ state
  const S = {
    st: null, symbol: "BTCUSDT", interval: "15m", tab: "positions", tickers: {}, account: null, openTrades: [],
    trades: [], candles: [], toolEls: {}, priceLines: [], lastSim: null, settings: null, mindItems: 0,
    team: null, liq: null, reports: [], outlook: null, corr: null,
  };
  let voice = false;
  try { voice = localStorage.getItem("tushar.voice") === "1"; } catch (e) { /* storage blocked */ }
  function speak(text) {
    if (!voice || !window.speechSynthesis || !text) return;
    const u = new SpeechSynthesisUtterance(String(text).slice(0, 220));
    u.rate = 1.05;
    window.speechSynthesis.speak(u);
  }
  function agentMeta(agent) {
    if (!agent || agent === "pm" || !S.team || !S.team.meta || !S.team.meta[agent]) return { emoji: "T", color: null, name: "Head Trader" };
    return S.team.meta[agent];
  }
  function avatar(agent) {
    const m = agentMeta(agent);
    return m.color ? `<div class="av" style="background:${m.color}22;border:1px solid ${m.color}66;font-size:13px">${m.emoji}</div>` : `<div class="av">T</div>`;
  }
  try { S.symbol = localStorage.getItem("tushar.symbol") || S.symbol; S.interval = localStorage.getItem("tushar.iv") || S.interval; } catch (e) { /* storage blocked */ }

  // ------------------------------------------------------------------ charts
  const chartOpts = {
    autoSize: true,
    layout: { background: { type: "solid", color: "transparent" }, textColor: "#8a93a8", fontFamily: "Inter, system-ui, sans-serif", fontSize: 11 },
    grid: { vertLines: { color: "rgba(28,36,54,.45)" }, horzLines: { color: "rgba(28,36,54,.45)" } },
    rightPriceScale: { borderColor: "#1c2436" },
    timeScale: { borderColor: "#1c2436", timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 },
  };
  const chart = LightweightCharts.createChart($("chart"), chartOpts);
  const candle = chart.addCandlestickSeries({ upColor: "#18c98a", downColor: "#f0475b", borderVisible: false, wickUpColor: "#18c98a", wickDownColor: "#f0475b" });
  const volume = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
  const ema20 = chart.addLineSeries({ color: "#f5b13d", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  const ema50 = chart.addLineSeries({ color: "#8b6cff", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });

  const eqChart = LightweightCharts.createChart($("equity-chart"), { ...chartOpts, crosshair: { mode: 1 } });
  const eqSeries = eqChart.addAreaSeries({ lineColor: "#8b6cff", topColor: "rgba(139,108,255,.35)", bottomColor: "rgba(25,211,245,0)", lineWidth: 2 });

  const IV_SEC = { "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400 };

  function emaSeries(data, period) {
    const out = []; const k = 2 / (period + 1); let prev = null;
    data.forEach((c, i) => {
      if (i < period - 1) return;
      if (prev === null) { prev = data.slice(0, period).reduce((a, b) => a + b.close, 0) / period; }
      else { prev = c.close * k + prev * (1 - k); }
      out.push({ time: c.time, value: prev });
    });
    return out;
  }

  function precisionFor(p) { const a = Math.abs(p || 1); return a >= 1000 ? 2 : a >= 10 ? 3 : a >= 1 ? 4 : a >= 0.01 ? 5 : 7; }

  async function loadChart() {
    let data;
    try { data = await api(`/api/klines?symbol=${S.symbol}&interval=${S.interval}&limit=500`); }
    catch (e) { toast("Chart: " + e.message, "bad"); return; }
    S.candles = data.candles;
    S.chartTrades = data.trades;
    const p = precisionFor(data.price || (data.candles.at(-1) || {}).close);
    candle.applyOptions({ priceFormat: { type: "price", precision: p, minMove: 1 / Math.pow(10, p) } });
    candle.setData(data.candles);
    volume.setData(data.candles.map((c) => ({ time: c.time, value: c.volume, color: c.close >= c.open ? "rgba(24,201,138,.28)" : "rgba(240,71,91,.28)" })));
    ema20.setData(emaSeries(data.candles, 20));
    ema50.setData(emaSeries(data.candles, 50));
    drawMarkers();
    drawPriceLines();
    updateLegend();
  }

  function drawMarkers() {
    const iv = IV_SEC[S.interval]; const c0 = S.candles[0]?.time || 0; const cN = S.candles.at(-1)?.time || 0;
    const snap = (ts) => Math.floor(ts / iv) * iv;
    const m = [];
    (S.chartTrades || []).forEach((t) => {
      const ot = snap(t.opened_at);
      if (ot >= c0 && ot <= cN) m.push({ time: ot, position: t.side === "long" ? "belowBar" : "aboveBar", color: t.side === "long" ? "#18c98a" : "#f0475b", shape: t.side === "long" ? "arrowUp" : "arrowDown", text: `${t.side === "long" ? "L" : "S"} #${t.id}` });
      if (t.closed_at) {
        const ct = snap(t.closed_at);
        if (ct >= c0 && ct <= cN) m.push({ time: ct, position: t.side === "long" ? "aboveBar" : "belowBar", color: (t.pnl || 0) >= 0 ? "#19d3f5" : "#f5b13d", shape: "circle", text: `${(t.pnl || 0) >= 0 ? "+" : ""}${(t.pnl || 0).toFixed(1)}` });
      }
    });
    m.sort((a, b) => a.time - b.time);
    candle.setMarkers(m);
  }

  function drawPriceLines() {
    S.priceLines.forEach((l) => candle.removePriceLine(l));
    S.priceLines = [];
    const t = S.openTrades.find((x) => x.symbol === S.symbol);
    if (!t) return;
    const add = (price, color, title, style = 2) => { if (isNum(price)) S.priceLines.push(candle.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title })); };
    add(t.entry_price, "#4da3ff", `ENTRY ${t.side.toUpperCase()}`, 0);
    add(t.stop_loss, "#f0475b", "STOP");
    add(t.take_profit, "#18c98a", "TARGET");
  }

  function updateLegend() {
    const last = S.candles.at(-1); const tk = S.tickers[S.symbol] || {};
    const price = tk.price || last?.close;
    $("chart-legend").innerHTML = `<b>${esc(S.symbol)}</b> &nbsp; <b class="${cls(tk.chg24)}">${fmt.price(price)}</b> &nbsp; <span class="${cls(tk.chg24)}">${fmt.pct(tk.chg24)}</span> &nbsp; <span style="color:#f5b13d">EMA20</span> <span style="color:#8b6cff">EMA50</span>`;
  }

  function liveCandle(price) {
    if (!isNum(price) || !S.candles.length) return;
    const iv = IV_SEC[S.interval]; const now = Math.floor(Date.now() / 1000);
    let last = S.candles.at(-1);
    if (now >= last.time + iv) {
      last = { time: Math.floor(now / iv) * iv, open: last.close, high: price, low: price, close: price, volume: 0 };
      S.candles.push(last);
    } else {
      last.high = Math.max(last.high, price); last.low = Math.min(last.low, price); last.close = price;
    }
    candle.update(last);
  }

  // ------------------------------------------------------------------ top bar
  function renderStatus(st) {
    if (!st) return;
    S.st = st;
    const badge = $("state-badge");
    badge.className = "badge st-" + st.state;
    $("state-text").textContent = { active: "TRADING", paused: "PAUSED", halted: "HALTED TODAY", kill: "KILL SWITCH" }[st.state] || st.state;
    $("claude-state").classList.toggle("thinking", !!st.cycle_running);
    $("claude-label").textContent = st.cycle_running ? `Claude is thinking... (cycle #${st.current_cycle})` : "Claude idle";
    $("brain-name").textContent = `${st.brain}${st.brain !== "mock" ? " · " + st.model : ""}`;
    const pb = $("btn-pause");
    pb.innerHTML = st.state === "paused" ? '&#9654; <span class="lbl">Resume</span>' : '&#10074;&#10074; <span class="lbl">Pause</span>';
    const kb = $("btn-kill");
    kb.innerHTML = st.state === "kill" ? '&#8635; <span class="lbl">Reset kill</span>' : '&#9888; <span class="lbl">Kill</span>';
    const u = st.usage || {};
    if (isNum(u.utilization)) { $("usage-fill").style.width = Math.round(u.utilization * 100) + "%"; $("usage-pct").textContent = Math.round(u.utilization * 100) + "%"; }
    else { $("usage-pct").textContent = { allowed: "ok", allowed_warning: "near limit", rejected: "limit hit" }[u.status] || u.status || "-"; }
    renderBanners();
  }

  function tickClock() {
    const st = S.st;
    if (!st) return;
    let txt = "-";
    if (st.cycle_running) txt = "running";
    else if (st.state === "kill") txt = "stopped";
    else if (st.state === "paused") txt = "paused";
    else if (st.next_cycle_at) { const s = st.next_cycle_at - Date.now() / 1000; txt = s <= 0 ? "now" : fmt.dur(s); }
    $("next-cycle").textContent = txt;
  }

  function renderBanners() {
    const b = []; const st = S.st || {}; const set = S.settings || {};
    if (S.startError) b.push(["err", `Engine failed to start: ${S.startError}`]);
    if (set.mode_label === "LIVE") b.push(["err", "LIVE TRADING: real money is at risk."]);
    if (set.market_source === "sim") b.push(["warn", "SIMULATED MARKET: prices are synthetic (offline test mode). Set MARKET_SOURCE=binance for real data."]);
    if (st.brain === "mock") b.push(["warn", "Mock brain: a simple rule-based trader is standing in for Claude. Set BRAIN=agent to let Claude trade."]);
    if (st.claude && st.claude.auth && !["none", "/login managed key"].includes(st.claude.auth)) b.push(["warn", `Claude is using ${st.claude.auth} (API billing), not your subscription.`]);
    if (st.usage_blocked_until && st.usage_blocked_until > Date.now() / 1000) b.push(["warn", `Claude usage limit reached. Cycles resume ${fmt.dt(st.usage_blocked_until)}.`]);
    if (st.halt_reason) b.push(["warn", `Halted for today: ${st.halt_reason}`]);
    if (st.consecutive_failures >= 2) b.push(["err", `The last ${st.consecutive_failures} Claude cycles failed. Check the Cycles tab and run "python -m tushar doctor".`]);
    $("banners").innerHTML = b.map(([k, t]) => `<div class="banner ${k}">${esc(t)}</div>`).join("");
  }

  // ------------------------------------------------------------------ KPIs
  function renderKpis() {
    const a = S.account; if (!a || a.error) return;
    const stats = S.stats || {};
    const sod = S.sod || a.equity;
    const today = a.equity - sod;
    $("k-equity").textContent = fmt.usd(a.equity);
    $("k-equity-sub").textContent = `wallet ${fmt.usd(a.wallet_balance)}`;
    $("k-today").innerHTML = `<span class="${cls(today)}">${fmt.signed(today)}</span>`;
    $("k-today-sub").textContent = sod ? fmt.pct((today / sod) * 100) + " since 00:00 UTC" : "";
    $("k-upnl").innerHTML = `<span class="${cls(a.unrealized_pnl)}">${fmt.signed(a.unrealized_pnl)}</span>`;
    $("k-upnl-sub").textContent = `${(a.positions || []).length} open position(s)`;
    $("k-avail").textContent = fmt.usd(a.available);
    const used = (a.positions || []).reduce((s, p) => s + (p.margin || 0), 0);
    $("k-avail-sub").textContent = `margin used ${fmt.usd(used)}`;
    $("k-winrate").textContent = stats.trades ? stats.win_rate.toFixed(1) + "%" : "-";
    $("k-winrate-sub").textContent = stats.trades ? `${stats.wins}W / ${stats.losses}L` : "no closed trades yet";
    $("k-total").innerHTML = stats.trades ? `<span class="${cls(stats.total_pnl)}">${fmt.signed(stats.total_pnl)}</span>` : "-";
    $("k-total-sub").textContent = stats.trades ? `avg ${isNum(stats.avg_r) ? stats.avg_r.toFixed(2) + "R" : "-"} · PF ${stats.profit_factor ?? "-"}` : "";
    const risk = S.openTrades.reduce((s, t) => {
      if (!t.stop_loss) return s;
      const l = t.side === "long" ? (t.entry_price - t.stop_loss) * t.qty : (t.stop_loss - t.entry_price) * t.qty;
      return s + Math.max(l, 0);
    }, 0);
    $("k-risk").textContent = fmt.usd(risk);
    $("k-risk-sub").textContent = a.equity ? `${((risk / a.equity) * 100).toFixed(2)}% of equity at stops` : "";
  }

  // ------------------------------------------------------------------ book tabs
  function renderBook() {
    const body = $("book-body");
    const a = S.account || {}; const positions = a.positions || [];
    $("c-positions").textContent = positions.length;
    $("c-orders").textContent = (S.orders || []).length;
    $("c-journal").textContent = (S.trades || []).length;
    $("c-alerts").textContent = (S.alerts || []).length + (S.pending || []).length;
    if (S.tab === "positions") body.innerHTML = positionsHtml(positions);
    else if (S.tab === "orders") body.innerHTML = ordersHtml();
    else if (S.tab === "journal") body.innerHTML = journalHtml();
    else if (S.tab === "cycles") body.innerHTML = cyclesHtml();
    else if (S.tab === "alerts") body.innerHTML = alertsHtml();
    else if (S.tab === "reports") body.innerHTML = reportsHtml();
  }

  function reportsHtml() {
    const r = S.reports || [];
    return `<div style="padding:10px 14px;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center"><span class="muted" style="font-size:12px">Claude writes a trading diary every day (and sends it to Telegram if linked).</span><div class="spacer"></div><button class="btn small" data-action="report">Write today's report now</button></div>` +
      (r.length ? r.map((x) => `<div class="report-card"><b style="color:var(--text)">&#128211; ${esc(x.day)}</b> <span class="dim" style="font-size:11px">${fmt.dt(x.ts)}</span>\n${esc(x.text)}</div>`).join("") : `<div class="empty">No daily reports yet.</div>`);
  }

  // ------------------------------------------------------------------ war room
  const STATUS_TXT = { working: "analyzing...", waiting: "waiting", done: "reported", off: "off (solo mode)", idle: "idle", error: "failed" };
  const PHASE = { chartist: "Phase 1 · parallel", macro: "Phase 1 · parallel", flow: "Phase 1 · parallel", risk: "Phase 2 · challenge", pm: "Phase 3 · decision" };

  function pmReport() {
    const o = S.outlook;
    return o ? { stance: o.bias, headline: o.regime, summary: o.summary, confidence: null } : null;
  }

  function renderTeam() {
    const t = S.team; if (!t || !t.meta) return;
    const solo = String(t.mode || "").startsWith("solo") || t.desk_mode === "solo";
    $("team-mode").textContent = t.mode || t.desk_mode;
    $("team-cycle").textContent = t.cycle_id ? `cycle #${t.cycle_id} · analysts ${t.analyst_model} · head trader ${t.pm_model}` : `analysts ${t.analyst_model} · head trader ${t.pm_model}`;
    $("team").innerHTML = t.order.map((k) => {
      const m = t.meta[k]; const a = (t.agents || {})[k] || {};
      const rep = k === "pm" ? (a.status === "working" ? null : pmReport()) : a.report;
      let status = a.status || (solo && k !== "pm" ? "off" : "idle");
      const stTxt = k === "pm" ? ({ working: "deciding...", waiting: "waiting for the desk", done: "decided" }[status] || STATUS_TXT[status]) : STATUS_TXT[status] || status;
      const conf = rep && isNum(rep.confidence) ? Math.round(rep.confidence * 100) : null;
      const stanceCls = rep ? (rep.stance === "bullish" ? "long" : rep.stance === "bearish" ? "short" : "neutral") : "neutral";
      const ideas = (rep?.ideas || []).slice(0, 3).map((i) => `<span class="pill ${i.side === "long" ? "long" : "short"}">${esc(sym(i.symbol))} ${i.side === "long" ? "L" : "S"}</span>`).join("");
      const veto = (rep?.veto || []).slice(0, 3).map((v) => `<span class="pill short">veto ${esc(String(v).slice(0, 18))}</span>`).join("");
      const foot = rep && (rep.turns || rep.seconds) ? `${rep.turns ?? "-"} turns · ${rep.seconds ?? "-"}s` : (a.ts ? fmt.ago(a.ts) : "");
      return `<div class="agent ${status}" data-agent="${k}">
        <div class="phase">${PHASE[k]}</div>
        <div class="top"><div class="av" style="background:${m.color}22;border:1px solid ${m.color}66">${m.emoji}</div>
          <div style="min-width:0"><div class="nm">${esc(m.name)}</div><div class="st">${esc(stTxt)}</div></div>
          <div class="spacer"></div>${rep && rep.stance ? `<span class="pill ${stanceCls}">${esc(rep.stance)}</span>` : ""}</div>
        ${conf !== null ? `<div class="conf" title="confidence ${conf}%"><i style="width:${conf}%;background:${m.color}"></i></div>` : ""}
        <div class="hl">${rep ? esc(rep.headline || "") : (status === "working" ? "Working on it..." : '<span class="dim">No report yet</span>')}</div>
        <div class="sm">${rep ? esc(rep.error || rep.summary || "") : ""}</div>
        ${ideas || veto ? `<div class="ideas">${ideas}${veto}</div>` : ""}
        <div class="foot">${esc(foot)}</div></div>`;
    }).join("");
  }

  async function openAgent(k) {
    const t = S.team; if (!t) return;
    const m = t.meta[k]; const a = (t.agents || {})[k] || {};
    const rep = k === "pm" ? pmReport() : a.report;
    let html = "";
    if (rep) {
      html += `<div class="outlook-head"><span class="bias ${esc(rep.stance || "neutral")}">${esc(rep.stance || "-")}</span><b>${esc(rep.headline || "")}</b>${isNum(rep.confidence) ? `<span class="muted">confidence ${Math.round(rep.confidence * 100)}%</span>` : ""}</div>`;
      if (rep.error) html += `<div class="banner err">${esc(rep.error)}</div>`;
      html += `<div class="outlook-sum" style="margin-bottom:10px">${richText(rep.summary || "")}</div>`;
      if (rep.key_points?.length) html += `<div class="form-sec">Key points</div><ul style="margin:0;padding-left:18px;line-height:1.6">${rep.key_points.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
      if (rep.ideas?.length) html += `<div class="form-sec">Ideas</div><table><thead><tr><th>Symbol</th><th>Side</th><th class="num">Entry</th><th class="num">Stop</th><th class="num">Target</th><th>Setup</th><th class="num">Conv.</th></tr></thead><tbody>${rep.ideas.map((i) => `<tr><td><b>${esc(sym(i.symbol))}</b></td><td><span class="pill ${i.side}">${esc(i.side)}</span></td><td class="num">${fmt.price(i.entry)}</td><td class="num">${fmt.price(i.stop)}</td><td class="num">${fmt.price(i.target)}</td><td>${esc(i.setup || "")}</td><td class="num">${i.conviction ?? "-"}</td></tr><tr class="detail"><td colspan="7">${esc(i.rationale || "")}</td></tr>`).join("")}</tbody></table>`;
      if (rep.warnings?.length) html += `<div class="form-sec">Warnings</div>${rep.warnings.map((w) => `<div class="m-notice" style="padding-left:0">&#9888; ${esc(w)}</div>`).join("")}`;
      if (rep.veto?.length) html += `<div class="form-sec">Veto</div>${rep.veto.map((w) => `<span class="pill short" style="margin-right:4px">${esc(w)}</span>`).join("")}`;
      if (isNum(rep.max_risk_pct)) html += `<div class="form-sec">Recommended max risk</div><div class="mono">${rep.max_risk_pct}% per trade</div>`;
    } else html += `<div class="empty">No report yet.</div>`;
    $("agent-title").innerHTML = `${m.emoji} ${esc(m.name)}`;
    $("agent-body").innerHTML = html + `<div class="form-sec">Its thinking this cycle</div><div id="agent-log" class="muted">Loading...</div>`;
    $("agent-overlay").classList.add("open");
    if (t.cycle_id) {
      try {
        const r = await api(`/api/cycles/${t.cycle_id}`);
        const evs = r.events.filter((e) => (e.data?.agent || "pm") === k && ["thought", "tool_call"].includes(e.kind));
        $("agent-log").innerHTML = evs.length ? evs.map((e) => e.kind === "thought" ? `<div class="m-thought" style="margin:6px 0">${avatar(k)}<div class="tx">${richText(e.data.text)}</div></div>` : `<div class="m-tool" style="margin:4px 0 4px 33px"><summary style="display:inline-flex">&#9881; ${esc(e.data.name)} ${esc(JSON.stringify(e.data.input || {}).slice(0, 120))}</summary></div>`).join("") : "Nothing recorded.";
      } catch (e) { $("agent-log").textContent = "Unavailable."; }
    } else { $("agent-log").textContent = "No cycle yet."; }
  }

  // ------------------------------------------------------------------ liquidation radar
  function renderLiq(fresh = []) {
    const L = S.liq; if (!L) return;
    const s = L.summary || {};
    $("liq-status").textContent = s.status || "-";
    const longs = s.longs_liquidated_usd || 0, shorts = s.shorts_liquidated_usd || 0, tot = longs + shorts;
    const lp = tot ? (longs / tot) * 100 : 50;
    const b = L.buckets || [];
    const max = Math.max(1, ...b.map((x) => x.longs + x.shorts));
    const W = 100 / Math.max(1, b.length);
    const bars = b.map((x, i) => {
      const hl = (x.longs / max) * 46, hs = (x.shorts / max) * 46;
      return `<rect x="${(i * W + W * 0.12).toFixed(2)}%" y="${(48 - hl).toFixed(1)}" width="${(W * 0.76).toFixed(2)}%" height="${hl.toFixed(1)}" fill="#f0475b" rx="1.5"/><rect x="${(i * W + W * 0.12).toFixed(2)}%" y="50" width="${(W * 0.76).toFixed(2)}%" height="${hs.toFixed(1)}" fill="#18c98a" rx="1.5"/>`;
    }).join("");
    const top = (s.by_symbol || []).slice(0, 6).map((x) => `<span class="tag coin" data-chart="${esc(x.symbol)}" style="cursor:pointer">${esc(sym(x.symbol))} ${fmt.big(x.longs_usd + x.shorts_usd)}</span>`).join(" ");
    $("liq-top").innerHTML = `<div style="display:flex;justify-content:space-between;font-size:12.5px;font-family:var(--mono)"><span class="down">longs rekt ${fmt.big(longs)}</span><b>${fmt.big(tot)} total · ${esc(s.dominant || "-")}</b><span class="up">shorts rekt ${fmt.big(shorts)}</span></div>
      <div class="liq-bar"><i style="width:${lp}%;background:var(--red)"></i><i style="width:${100 - lp}%;background:var(--green)"></i></div>
      <svg width="100%" height="98" style="display:block">${bars}<line x1="0" x2="100%" y1="49" y2="49" stroke="#26304a"/></svg>
      <div class="muted" style="font-size:11px;margin:2px 0 6px">last 2 hours, 5-minute buckets · red = longs liquidated (forced selling), green = shorts liquidated (forced buying)</div><div>${top}</div>`;
    const freshKeys = new Set(fresh.map((e) => e.ts + e.symbol));
    $("liq-feed").innerHTML = (L.recent || []).slice(0, 40).map((e) => `<div class="liq-row ${freshKeys.has(e.ts + e.symbol) ? "new " + e.side : ""}">
      <span class="dim mono" style="min-width:52px">${fmt.ago(e.ts).replace(" ago", "")}</span><b style="min-width:70px" data-chart="${esc(e.symbol)}">${esc(sym(e.symbol))}</b>
      <span class="pill ${e.side === "long" ? "short" : "long"}">${e.side === "long" ? "LONG rekt" : "SHORT rekt"}</span>
      <span class="spacer"></span><span class="mono">${fmt.usd(e.usd, 0)}</span><span class="dim mono" style="min-width:80px;text-align:right">@ ${fmt.price(e.price)}</span></div>`).join("") || `<div class="empty">${s.status === "live" ? "Quiet: no liquidations in the window." : "Radar " + esc(s.status || "off") + "."}</div>`;
  }

  // ------------------------------------------------------------------ correlations
  async function loadCorr() {
    try { S.corr = await api("/api/correlations"); renderCorr(); } catch (e) { $("corr").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  }
  function renderCorr() {
    const c = S.corr; if (!c || !c.symbols) return;
    const syms = c.symbols, m = c.matrix;
    if (!m.length) { $("corr").innerHTML = `<div class="empty">Not enough data.</div>`; return; }
    const col = (v) => v >= 0 ? `rgba(139,108,255,${(0.12 + v * 0.88).toFixed(2)})` : `rgba(25,211,245,${(0.12 - v * 0.88).toFixed(2)})`;
    const head = `<tr><th></th>${syms.map((s) => `<th>${esc(sym(s))}</th>`).join("")}</tr>`;
    const rows = syms.map((s, i) => `<tr><th style="text-align:right">${esc(sym(s))}</th>${m[i].map((v, j) => `<td><div style="background:${col(v)}" title="${esc(sym(s))}/${esc(sym(syms[j]))} ${v}">${i === j ? "" : v.toFixed(2)}</div></td>`).join("")}</tr>`).join("");
    const beta = `<tr><th style="text-align:right">β BTC</th>${syms.map((s) => `<td><div style="background:var(--panel2);color:var(--muted)">${c.beta?.[s] ?? "-"}</div></td>`).join("")}</tr>`;
    const ex = c.exposure || {};
    const pairs = (c.highly_correlated_pairs || []).slice(0, 4).map((p) => `${esc(sym(p.a))}/${esc(sym(p.b))} ${p.corr}`).join(" · ");
    $("corr").innerHTML = `<div style="overflow-x:auto"><table class="heat">${head}${rows}${beta}</table></div>
      <div class="mono" style="font-size:12px;margin-top:10px;display:flex;gap:16px;flex-wrap:wrap">
        <span>gross <b>${fmt.usd(ex.gross_usd, 0)}</b> (${ex.gross_x_equity ?? 0}x)</span><span>net <b class="${cls(ex.net_usd)}">${fmt.signed(ex.net_usd, 0)}</b></span>
        <span>BTC-beta net <b class="${cls(ex.btc_beta_net_usd)}">${fmt.signed(ex.btc_beta_net_usd, 0)}</b> (${ex.btc_beta_net_x_equity ?? 0}x equity)</span></div>
      ${pairs ? `<div class="muted" style="font-size:11.5px;margin-top:6px">Most correlated: ${pairs}</div>` : ""}`;
  }

  function positionsHtml(positions) {
    if (!positions.length) return `<div class="empty">No open positions. Claude is ${S.st?.cycle_running ? "thinking" : "waiting for an A-grade setup"}.</div>`;
    const rows = positions.map((p) => {
      const t = S.openTrades.find((x) => x.symbol === p.symbol) || {};
      const risk = t.entry_price && t.initial_stop ? Math.abs(t.entry_price - t.initial_stop) : null;
      const r = risk ? ((p.mark_price - p.entry_price) * (p.side === "long" ? 1 : -1)) / risk : null;
      return `<tr class="clickable" data-chart="${esc(p.symbol)}">
        <td><b>${esc(sym(p.symbol))}</b> <span class="dim" style="font-size:11px">${esc(t.setup || "")}</span></td>
        <td><span class="pill ${p.side}">${p.side.toUpperCase()}</span></td>
        <td class="num">${fmt.num(p.qty, 6)}</td>
        <td class="num">${fmt.price(p.entry_price)}</td>
        <td class="num">${fmt.price(p.mark_price)}</td>
        <td class="num ${cls(p.unrealized_pnl)}">${fmt.signed(p.unrealized_pnl)}<div class="dim" style="font-size:11px">${fmt.pct(p.pnl_pct)} margin</div></td>
        <td class="num ${cls(r)}">${isNum(r) ? r.toFixed(2) + "R" : "-"}</td>
        <td class="num down">${fmt.price(t.stop_loss)}</td>
        <td class="num up">${fmt.price(t.take_profit)}</td>
        <td class="num">${p.leverage}x</td>
        <td class="num dim">${t.opened_at ? fmt.ago(t.opened_at).replace(" ago", "") : "-"}</td>
        <td><button class="btn small danger" data-close="${esc(p.symbol)}">Close</button></td></tr>`;
    }).join("");
    return `<table><thead><tr><th>Symbol</th><th>Side</th><th class="num">Size</th><th class="num">Entry</th><th class="num">Mark</th><th class="num">PnL</th><th class="num">R</th><th class="num">Stop</th><th class="num">Target</th><th class="num">Lev</th><th class="num">Age</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function ordersHtml() {
    const o = S.orders || [];
    if (!o.length) return `<div class="empty">No open orders.</div>`;
    return `<table><thead><tr><th>Symbol</th><th>Type</th><th>Side</th><th class="num">Qty</th><th class="num">Trigger</th><th>Route</th></tr></thead><tbody>${o.map((x) => `<tr>
      <td><b>${esc(sym(x.symbol))}</b></td><td><span class="pill ${x.kind === "SL" ? "short" : "long"}">${x.kind === "SL" ? "STOP" : x.kind === "TP" ? "TARGET" : esc(x.type)}</span></td>
      <td>${esc(x.side)}</td><td class="num">${fmt.num(x.qty, 6)}</td><td class="num">${fmt.price(x.trigger_price)}</td><td class="dim">${x.algo ? "algo" : "classic"}</td></tr>`).join("")}</tbody></table>`;
  }

  function journalHtml() {
    const t = S.trades || [];
    if (!t.length) return `<div class="empty">No trades yet. Every trade lands here with Claude's thesis and its own post-trade review.</div>`;
    return `<table><thead><tr><th>Opened</th><th>Symbol</th><th>Side</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">PnL</th><th class="num">R</th><th>Exit</th><th>Setup</th></tr></thead><tbody>${t.map((x) => `
      <tr class="clickable" data-trade="${x.id}">
        <td class="dim">${fmt.dt(x.opened_at)}</td><td><b>${esc(sym(x.symbol))}</b></td><td><span class="pill ${x.side}">${x.side.toUpperCase()}</span></td>
        <td class="num">${fmt.price(x.entry_price)}</td><td class="num">${x.status === "open" ? '<span class="pill run">OPEN</span>' : fmt.price(x.exit_price)}</td>
        <td class="num ${cls(x.pnl)}">${x.pnl != null ? fmt.signed(x.pnl) : "-"}</td><td class="num ${cls(x.r_multiple)}">${x.r_multiple != null ? x.r_multiple.toFixed(2) + "R" : "-"}</td>
        <td class="dim">${esc(x.exit_reason || "")}</td><td class="dim">${esc(x.setup || "")}</td></tr>
      <tr class="detail hidden" data-detail="${x.id}"><td colspan="9">${tradeDetail(x)}</td></tr>`).join("")}</tbody></table>`;
  }

  function tradeDetail(x) {
    const tm = x.tm || {}; const ex = x.extra || {};
    return `<div><b style="color:var(--text)">Thesis:</b> ${esc(x.thesis || "-")}</div>
      ${x.review ? `<div style="margin-top:6px"><b style="color:var(--text)">Claude's review:</b> ${esc(x.review)}</div>` : ""}
      ${tm.verdict ? `<div style="margin-top:6px"><b style="color:var(--text)">Time Machine:</b> ${esc(tm.verdict)} (target first ${tm.mc?.target_first_pct}% · stop first ${tm.mc?.stop_first_pct}% · avg R after fees ${tm.mc?.avg_r_after_fees})</div>` : ""}
      <div style="margin-top:6px" class="mono">qty ${fmt.num(x.qty, 6)} · ${x.leverage}x · risk ${fmt.usd(x.risk_usd)} (${isNum(x.risk_pct) ? x.risk_pct.toFixed(2) : "-"}%) · stop ${fmt.price(x.initial_stop)} → ${fmt.price(x.stop_loss)} · target ${fmt.price(x.take_profit)} · confidence ${x.confidence ?? "-"} · fees ${fmt.usd(x.fees)}</div>
      ${ex.notes && ex.notes.length ? `<div style="margin-top:4px">Notes: ${esc(ex.notes.join("; "))}</div>` : ""}
      ${x.close_reason ? `<div style="margin-top:4px">Close: ${esc(x.close_reason)}</div>` : ""}`;
  }

  function cyclesHtml() {
    const c = S.cycles || [];
    if (!c.length) return `<div class="empty">No cycles yet.</div>`;
    return `<table><thead><tr><th>#</th><th>Started</th><th>Trigger</th><th>Status</th><th class="num">Turns</th><th class="num">Time</th><th>Summary</th></tr></thead><tbody>${c.map((x) => `
      <tr class="clickable" data-cycle="${x.id}"><td class="mono">${x.id}</td><td class="dim">${fmt.dt(x.started_at)}</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${esc(x.trigger)}</td>
      <td><span class="pill ${x.status === "done" ? "ok" : x.status === "running" ? "run" : "err"}">${esc(x.status)}</span></td>
      <td class="num">${x.num_turns ?? "-"}</td><td class="num">${x.duration_ms ? fmt.dur(x.duration_ms / 1000) : "-"}</td>
      <td style="max-width:420px;overflow:hidden;text-overflow:ellipsis" class="dim">${esc(x.error || x.summary || "")}</td></tr>`).join("")}</tbody></table>`;
  }

  function alertsHtml() {
    const a = S.alerts || [];
    const p = S.pending || [];
    const pend = p.length ? `<div class="form-sec" style="margin:10px 14px 4px">Pending entries: fire automatically through the risk gate</div><table><thead><tr><th>Symbol</th><th>Side</th><th>Fires when</th><th class="num">Entry</th><th class="num">Stop</th><th class="num">Target</th><th>Expires</th><th></th></tr></thead><tbody>${p.map((x) => `
      <tr><td><b>${esc(sym(x.symbol))}</b></td><td><span class="pill ${x.side}">${x.side.toUpperCase()}</span></td><td>price ${esc(x.condition)}</td><td class="num">${fmt.price(x.entry)}</td>
      <td class="num down">${fmt.price(x.stop_loss)}</td><td class="num up">${fmt.price(x.take_profit)}</td><td class="dim">in ${fmt.dur(x.expires_at - Date.now() / 1000)}</td>
      <td><button class="btn small" data-cancel-pending="${x.id}">Cancel</button></td></tr>
      <tr class="detail"><td colspan="8">${esc((x.thesis || "").slice(0, 300))}</td></tr>`).join("")}</tbody></table>` : "";
    if (!a.length) return pend + `<div class="empty">No active price alerts. Claude sets alerts on key levels so it is woken when price gets there.</div>`;
    return pend + `<div class="form-sec" style="margin:10px 14px 4px">Price alerts: wake Claude</div><table><thead><tr><th>Symbol</th><th>When</th><th class="num">Price</th><th>Note</th><th>Set</th><th></th></tr></thead><tbody>${a.map((x) => `
      <tr><td><b>${esc(sym(x.symbol))}</b></td><td>${esc(x.condition)}</td><td class="num">${fmt.price(x.price)}</td><td style="white-space:normal">${esc(x.note || "")}</td>
      <td class="dim">${fmt.ago(x.created_at)}</td><td><button class="btn small" data-cancel-alert="${x.id}">Cancel</button></td></tr>`).join("")}</tbody></table>`;
  }

  // ------------------------------------------------------------------ mind feed
  const mind = $("mind");
  function nearBottom() { return mind.scrollHeight - mind.scrollTop - mind.clientHeight < 120; }

  function mindAdd(node) {
    const stick = nearBottom();
    mind.appendChild(node);
    S.mindItems++;
    while (mind.children.length > 600) mind.removeChild(mind.firstChild);
    if (stick) mind.scrollTop = mind.scrollHeight;
  }

  function div(className, html) { const d = document.createElement("div"); d.className = className; d.innerHTML = html; return d; }

  function richText(s) {
    return esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, '<code class="mono">$1</code>').replace(/^#{1,4}\s*(.+)$/gm, "<b>$1</b>");
  }

  function renderMind(msg) {
    const d = msg.data || {};
    switch (msg.type) {
      case "cycle_start":
        mindAdd(div("m-cycle", `Cycle #${d.cycle_id} · ${esc(d.trigger)} · ${fmt.time(msg.ts)}`));
        break;
      case "thought": {
        const m = agentMeta(d.agent);
        const who = d.agent && d.agent !== "pm" ? `<div class="m-who" style="color:${m.color}">${esc(m.name)}</div>` : "";
        mindAdd(div("m-thought", `${avatar(d.agent)}<div class="tx">${who}${richText(d.text)}</div>`));
        break;
      }
      case "thinking": {
        const n = div("m-thinking", esc(d.text));
        n.title = "Claude's private reasoning (click to expand)";
        n.onclick = () => n.classList.toggle("open");
        mindAdd(n);
        break;
      }
      case "tool_call": {
        const det = document.createElement("details");
        det.className = "m-tool";
        const args = d.input && Object.keys(d.input).length ? JSON.stringify(d.input, null, 1) : "";
        const brief = d.input ? Object.entries(d.input).filter(([k]) => ["symbol", "side", "stop_loss", "take_profit", "fraction", "condition", "price"].includes(k)).map(([, v]) => v).join(" ") : "";
        const tag = d.agent && d.agent !== "pm" ? agentMeta(d.agent).emoji + " " : "";
        det.innerHTML = `<summary>${tag}&#9881; ${esc(d.name)} ${esc(brief)}</summary>${args ? `<pre>${esc(args)}</pre>` : ""}`;
        if (d.id) S.toolEls[d.id] = det;
        mindAdd(det);
        break;
      }
      case "tool_result": {
        const text = String(d.text || "");
        const n = div("m-result" + (/BLOCK|ERROR/.test(text.slice(0, 200)) ? " block" : ""), esc(text));
        n.title = "click to expand";
        n.onclick = () => n.classList.toggle("open");
        mindAdd(n);
        break;
      }
      case "trade_open": {
        const t = d.trade || {};
        mindAdd(div("m-trade open-t", `<b>OPENED ${esc(t.side?.toUpperCase())} ${esc(sym(t.symbol))}</b> @ ${fmt.price(t.entry_price)} · stop ${fmt.price(t.stop_loss)} · target ${fmt.price(t.take_profit)} · risk ${fmt.usd(t.risk_usd)}<div class="muted" style="margin-top:4px;font-size:12px">${esc((t.thesis || "").slice(0, 280))}</div>`));
        break;
      }
      case "trade_close": {
        if (d.partial) { mindAdd(div("m-trade close-t win", `<b>PARTIAL CLOSE ${esc(sym(d.symbol))}</b> ${fmt.num(d.qty, 6)} @ ${fmt.price(d.price)} · ${esc(d.reason || "")}`)); break; }
        const t = d.trade || {};
        mindAdd(div("m-trade close-t " + ((t.pnl || 0) >= 0 ? "win" : "loss"), `<b>CLOSED ${esc(t.side?.toUpperCase())} ${esc(sym(t.symbol))}</b> @ ${fmt.price(t.exit_price)} · <b class="${cls(t.pnl)}">${fmt.signed(t.pnl)}</b> (${isNum(t.r_multiple) ? t.r_multiple.toFixed(2) + "R" : "-"}) · ${esc(t.exit_reason || "")}`));
        break;
      }
      case "cycle_end":
        mindAdd(div("m-end", `${d.status === "done" ? "&#10003;" : "&#10007;"} cycle #${d.cycle_id} ${esc(d.status)} in ${fmt.dur(d.duration_s)}${d.num_turns ? ` · ${d.num_turns} turns` : ""}${d.error ? ` · <span class="down">${esc(d.error)}</span>` : ""}`));
        break;
      case "notice":
        mindAdd(div("m-notice", "&#9888; " + esc(d.text)));
        break;
      case "error":
        mindAdd(div("m-notice err", "&#10007; " + esc(d.text)));
        break;
      case "alert_hit":
        mindAdd(div("m-notice", "&#128276; " + esc(d.text)));
        break;
      default:
        break;
    }
  }

  // ------------------------------------------------------------------ outlook / watch / scanner / news
  function renderOutlook(o) {
    if (!o) return;
    S.outlook = o;
    $("outlook-time").textContent = fmt.ago(o.ts);
    const watch = (o.watch || []).map((w) => `<div class="watch-row"><b class="clickable" data-chart="${esc(w.symbol)}" style="cursor:pointer">${esc(sym(w.symbol))}</b>${w.bias ? `<span class="pill ${w.bias === "bullish" || w.bias === "long" ? "long" : w.bias === "bearish" || w.bias === "short" ? "short" : "neutral"}">${esc(w.bias)}</span>` : ""}<span class="muted">${esc(w.note || "")}</span></div>`).join("");
    $("outlook").innerHTML = `<div class="outlook-head"><span class="bias ${esc(o.bias)}">${esc(o.bias)}</span><b>${esc(o.regime)}</b>${o.next_check_minutes ? `<span class="muted" style="font-size:12px">next look in ${o.next_check_minutes}m</span>` : ""}</div>
      <div class="outlook-sum">${richText(o.summary)}</div>${watch ? `<div style="margin-top:10px">${watch}</div>` : ""}`;
  }

  const lastPrice = {};
  function renderWatch() {
    const rows = Object.entries(S.tickers);
    $("watch").innerHTML = rows.map(([s, t]) => {
      const prev = lastPrice[s]; const flash = prev && t.price !== prev ? (t.price > prev ? "flash-up" : "flash-down") : "";
      lastPrice[s] = t.price;
      return `<tr class="clickable" data-chart="${esc(s)}"><td><b>${esc(sym(s))}</b>${S.openTrades.some((x) => x.symbol === s) ? ' <span class="pill run" style="font-size:9.5px">POS</span>' : ""}</td>
        <td class="num ${flash}">${fmt.price(t.price)}</td><td class="num ${cls(t.chg24)}">${fmt.pct(t.chg24)}</td>
        <td class="num ${cls(-(t.funding || 0))}">${isNum(t.funding) ? (t.funding * 100).toFixed(4) + "%" : "-"}</td><td class="num dim">${fmt.big(t.vol)}</td></tr>`;
    }).join("");
  }

  function renderSymbolSelect(list) {
    const sel = $("sym-select");
    const all = Array.from(new Set([S.symbol, ...(list || [])]));
    sel.innerHTML = all.map((s) => `<option value="${esc(s)}"${s === S.symbol ? " selected" : ""}>${esc(s)}</option>`).join("");
  }

  function renderScanner(sc) {
    if (!sc) return;
    const hits = sc.hits || [];
    $("scan-count").textContent = hits.length;
    $("scan-time").textContent = sc.as_of ? `${sc.scanned} scanned · ${fmt.ago(sc.as_of)}` : "";
    $("scanner").innerHTML = hits.length ? hits.slice(0, 40).map((h) => `<div class="list-row clickable" data-chart="${esc(h.symbol)}" style="cursor:pointer">
      <b style="min-width:64px">${esc(sym(h.symbol))}</b><span class="pill ${esc(h.direction)}">${esc(h.direction.toUpperCase())}</span>
      <span style="flex:1;min-width:0"><span style="font-weight:600">${esc(h.setup.replace(/_/g, " "))}</span><div class="dim" style="font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(h.description)}</div></span>
      <span class="stars" title="score">${"&#9679;".repeat(Math.min(h.score, 5))}</span><span class="mono ${cls(h.chg_24h)}" style="min-width:58px;text-align:right">${fmt.pct(h.chg_24h)}</span></div>`).join("") : `<div class="empty">No setups right now. The scanner runs every 5 minutes on closed 1h candles.</div>`;
  }

  function fngGauge(v) {
    const ang = Math.PI * (1 - v / 100); const cx = 46, cy = 46, r = 38;
    const x = cx + r * Math.cos(ang), y = cy - r * Math.sin(ang);
    const col = v < 25 ? "#f0475b" : v < 45 ? "#f5b13d" : v < 55 ? "#8a93a8" : v < 75 ? "#7fd17f" : "#18c98a";
    return `<svg width="92" height="54" viewBox="0 0 92 54"><defs><linearGradient id="fg" x1="0" x2="1"><stop offset="0" stop-color="#f0475b"/><stop offset=".5" stop-color="#f5b13d"/><stop offset="1" stop-color="#18c98a"/></linearGradient></defs>
      <path d="M8 46 A38 38 0 0 1 84 46" stroke="url(#fg)" stroke-width="7" fill="none" stroke-linecap="round" opacity=".85"/>
      <line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${col}" stroke-width="3" stroke-linecap="round"/><circle cx="${cx}" cy="${cy}" r="4" fill="${col}"/></svg>`;
  }

  function renderNews(items, fng, status) {
    if (fng) {
      const hist = (fng.history || []).slice(-30);
      const bars = hist.map((v) => `<i style="display:inline-block;width:4px;margin-right:1px;height:${Math.max(3, v / 100 * 26)}px;background:${v < 45 ? "#f0475b" : v < 55 ? "#8a93a8" : "#18c98a"};opacity:.7"></i>`).join("");
      $("fng").innerHTML = `<div class="fng">${fngGauge(fng.value)}<div><div class="fng-val">${fng.value}</div><div class="muted" style="font-size:12px">${esc(fng.label)}</div></div><div class="spacer"></div><div title="last 30 days" style="display:flex;align-items:flex-end;height:28px">${bars}</div></div>`;
    }
    if (!items) return;
    S.news = items;
    $("news-count").textContent = items.length;
    const bad = status && Object.values(status).length && !Object.values(status).some((v) => String(v).startsWith("ok"));
    $("news").innerHTML = items.length ? items.slice(0, 60).map((n) => {
      const sent = n.sentiment > 0.2 ? "#18c98a" : n.sentiment < -0.2 ? "#f0475b" : "#5d667c";
      return `<div class="news-item"><a href="${esc(n.link)}" target="_blank" rel="noopener">${esc(n.title)}</a>
        <div class="news-meta"><span class="sent" style="background:${sent}"></span><span>${esc(n.source)}</span><span>· ${fmt.ago(n.ts)}</span>
        ${(n.tags || []).map((t) => `<span class="tag ${esc(t)}">${esc(t)}</span>`).join("")}${(n.symbols || []).map((s) => `<span class="tag coin">${esc(sym(s))}</span>`).join("")}</div></div>`;
    }).join("") : `<div class="empty">${bad ? "News feeds are unreachable from this machine." : "No headlines yet."}</div>`;
  }

  // ------------------------------------------------------------------ time machine
  async function renderSim(sim) {
    if (!sim) return;
    let res = sim.result || null;
    if (!res && sim.id) { try { const row = await api(`/api/sims/${sim.id}`); res = row.result; } catch (e) { return; } }
    if (!res || !res.ok) return;
    S.lastSim = res;
    const mc = res.mc, p = res.plan, h = res.history || {};
    $("tm-time").textContent = fmt.ago(sim.ts || Date.now() / 1000);
    const vClass = /toward the target/.test(res.verdict) ? "up" : /toward the stop/.test(res.verdict) ? "down" : "";
    $("tm").innerHTML = `
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap"><b>${esc(sym(p.symbol))}</b><span class="pill ${p.side}">${p.side.toUpperCase()}</span>
        <span class="mono muted" style="font-size:12px">entry ${fmt.price(p.entry)} · stop ${fmt.price(p.stop)} (-${p.stop_pct}%) · target ${fmt.price(p.target)} (${p.target_r}R) · ${p.horizon_hours}h</span></div>
      <div class="tm-odds">
        <div class="tm-odd"><div class="l">Target first</div><div class="v up">${mc.target_first_pct}%</div></div>
        <div class="tm-odd"><div class="l">Stop first</div><div class="v down">${mc.stop_first_pct}%</div></div>
        <div class="tm-odd"><div class="l">Neither</div><div class="v">${mc.neither_pct}%</div></div>
        <div class="tm-odd"><div class="l">Avg R after fees</div><div class="v ${cls(mc.avg_r_after_fees)}">${mc.avg_r_after_fees > 0 ? "+" : ""}${mc.avg_r_after_fees}</div></div>
      </div>
      <div class="tm-bar"><i style="width:${mc.target_first_pct}%;background:var(--green)"></i><i style="width:${mc.neither_pct}%;background:#3a4560"></i><i style="width:${mc.stop_first_pct}%;background:var(--red)"></i></div>
      <div class="tm-verdict ${vClass}">${esc(res.verdict)}${(res.notes || []).map((n) => `<div class="muted" style="font-size:12px;margin-top:3px">${esc(n)}</div>`).join("")}</div>
      <svg id="tm-svg"></svg>
      <div class="muted" style="font-size:12px;margin-top:6px">History: the same distances from ${h.tested} past candles hit the target ${h.target} times, the stop ${h.stop}, neither ${h.neither} (avg R ${h.avg_r}). Seed ${mc.seed}. ${esc(res.disclaimer)}</div>`;
    drawFan(res);
  }

  function drawFan(res) {
    const svg = $("tm-svg"); if (!svg || !res.mc.fan) return;
    const f = res.mc.fan, p = res.plan;
    const W = svg.clientWidth || 600, H = 220, padL = 8, padR = 104, padT = 10, padB = 18;
    const all = [...f.p5, ...f.p95, p.stop, p.target, p.entry];
    let lo = Math.min(...all), hi = Math.max(...all); const m = (hi - lo) * 0.06; lo -= m; hi += m;
    const hrs = [0, ...f.hours]; const maxH = hrs.at(-1) || 1;
    const X = (hh) => padL + (hh / maxH) * (W - padL - padR);
    const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);
    const withEntry = (arr) => [p.entry, ...arr];
    const line = (arr) => withEntry(arr).map((v, i) => `${i ? "L" : "M"}${X(hrs[i]).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
    const band = (a, b) => { const A = withEntry(a), B = withEntry(b); return A.map((v, i) => `${i ? "L" : "M"}${X(hrs[i]).toFixed(1)},${Y(v).toFixed(1)}`).join(" ") + " " + B.map((v, i) => [i, v]).reverse().map(([i, v]) => `L${X(hrs[i]).toFixed(1)},${Y(v).toFixed(1)}`).join(" ") + " Z"; };
    const hline = (v, col, label) => `<line x1="${padL}" x2="${W - padR}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" stroke="${col}" stroke-dasharray="4 4" stroke-width="1"/><text x="${W - padR + 4}" y="${(Y(v) + 4).toFixed(1)}" fill="${col}" font-size="10.5" font-family="JetBrains Mono, monospace">${label} ${fmt.price(v)}</text>`;
    const samples = (f.samples || []).map((s) => `<path d="${line(s)}" stroke="rgba(160,170,200,.13)" fill="none" stroke-width="1"/>`).join("");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.innerHTML = `${samples}
      <path d="${band(f.p5, f.p95)}" fill="rgba(139,108,255,.12)"/><path d="${band(f.p25, f.p75)}" fill="rgba(139,108,255,.22)"/>
      <path d="${line(f.p50)}" stroke="#b5a3ff" stroke-width="2" fill="none"/>
      ${hline(p.target, "#18c98a", "TP")}${hline(p.entry, "#4da3ff", "IN")}${hline(p.stop, "#f0475b", "SL")}
      <text x="${padL}" y="${H - 4}" fill="#5d667c" font-size="10">now</text><text x="${W - padR}" y="${H - 4}" fill="#5d667c" font-size="10" text-anchor="end">+${maxH}h</text>`;
  }

  // ------------------------------------------------------------------ memory / perf / equity
  function renderMemory(mem) {
    S.memories = mem || [];
    $("mem-count").textContent = S.memories.length;
    $("memory").innerHTML = S.memories.length ? S.memories.map((m) => `<div class="mem"><span class="k pill ${m.kind === "rule" ? "short" : m.kind === "lesson" ? "long" : "neutral"}">${esc(m.kind)}</span>
      <span class="t">${esc(m.text)}${m.symbol ? ` <span class="tag coin">${esc(sym(m.symbol))}</span>` : ""}<div class="stars">${"&#9733;".repeat(m.importance || 1)}<span class="dim" style="margin-left:6px;letter-spacing:0">${fmt.ago(m.ts)}</span></div></span>
      <button class="btn small ghost x" data-forget="${m.id}" title="Retire this memory">&#10005;</button></div>`).join("") : `<div class="empty">Claude writes lessons here after reviewing its trades. They are fed back into every future decision.</div>`;
  }

  function renderPerf(stats) {
    const s = stats || {};
    if (!s.trades) { $("perf").innerHTML = `<div class="empty">Stats appear after the first closed trade.</div>`; return; }
    const rows = Object.entries(s.by_setup || {}).sort((a, b) => b[1].pnl - a[1].pnl).map(([k, v]) => `<tr><td>${esc(k.replace(/_/g, " "))}</td><td class="num">${v.n}</td><td class="num">${((v.wins / v.n) * 100).toFixed(0)}%</td><td class="num ${cls(v.avg_r)}">${isNum(v.avg_r) ? v.avg_r.toFixed(2) + "R" : "-"}</td><td class="num ${cls(v.pnl)}">${fmt.signed(v.pnl)}</td></tr>`).join("");
    $("perf").innerHTML = `<div style="display:flex;gap:18px;padding:12px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap;font-size:12.5px" class="mono">
      <span>trades <b>${s.trades}</b></span><span>PF <b>${s.profit_factor ?? "-"}</b></span><span>avg <b>${isNum(s.avg_r) ? s.avg_r.toFixed(2) + "R" : "-"}</b></span>
      <span>best <b class="up">${fmt.signed(s.best)}</b></span><span>worst <b class="down">${fmt.signed(s.worst)}</b></span><span>streak <b>${esc(s.streak)}</b></span></div>
      <table><thead><tr><th>Setup</th><th class="num">Trades</th><th class="num">Win</th><th class="num">Avg R</th><th class="num">PnL</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function renderEquity(eq) {
    if (!eq || !eq.length) return;
    const data = []; let lastT = 0;
    eq.forEach((r) => { const t = Math.floor(r.ts); if (t > lastT) { data.push({ time: t, value: r.equity }); lastT = t; } });
    eqSeries.setData(data);
    eqChart.timeScale().fitContent();
    const ch = data.at(-1).value - data[0].value;
    $("eq-change").innerHTML = `<span class="${cls(ch)}">${fmt.signed(ch)} (${fmt.pct((ch / data[0].value) * 100)})</span> since ${fmt.dt(data[0].time)}`;
  }

  // ------------------------------------------------------------------ settings
  const FIELDS = [
    ["Claude", [
      ["claude_model", "Model", "text", "sonnet, opus, haiku or a full model id"],
      ["claude_effort", "Effort", ["", "low", "medium", "high", "xhigh", "max"], "blank = model default"],
      ["claude_max_turns", "Max turns per cycle", "number", "tool calls + replies"],
      ["claude_allow_web", "Web search for news", "bool", "lets Claude use WebSearch/WebFetch"],
      ["chat_can_trade", "Chat can trade", "bool", "allow trades from Ask Tushar"],
    ]],
    ["Desk (multi-agent)", [
      ["desk_mode", "Desk mode", ["team", "solo"], "team = analysts + risk officer + head trader"],
      ["analyst_model", "Analyst model", "text", "haiku is light on usage; sonnet is sharper"],
      ["analyst_max_turns", "Analyst max turns", "number", "tool calls per analyst"],
      ["team_on_events", "Full team on alert/stop cycles", "bool", "off = head trader alone (faster)"],
    ]],
    ["Notifications", [
      ["telegram_cycle_summaries", "Telegram: every cycle summary", "bool", "trades and alerts are always sent"],
      ["daily_report", "Daily report", "bool", "Claude's end-of-day diary"],
      ["daily_report_hour_utc", "Daily report hour (UTC)", "number", "0-23"],
    ]],
    ["Schedule", [
      ["cycle_minutes", "Cycle every (min)", "number", "Claude can ask for sooner/later"],
      ["min_cycle_gap_minutes", "Min gap between cycles (min)", "number", ""],
      ["big_move_pct", "Wake on 15m move (%)", "number", "extra cycle on big moves"],
    ]],
    ["Universe", [
      ["watchlist", "Watchlist", "text", "comma separated symbols"],
      ["scan_top_n", "Scanner universe size", "number", "top perpetuals by volume"],
      ["blocked_symbols", "Blocked symbols", "text", "never trade these"],
    ]],
    ["Risk", [
      ["risk_per_trade_pct", "Max risk per trade (% equity)", "number", "loss at the stop"],
      ["max_leverage", "Max leverage", "number", ""],
      ["default_leverage", "Default leverage", "number", ""],
      ["max_open_positions", "Max open positions", "number", ""],
      ["max_position_margin_pct", "Max margin per position (%)", "number", ""],
      ["max_daily_loss_pct", "Daily loss limit (%)", "number", "halts new entries for the day"],
      ["max_trades_per_day", "Max entries per day", "number", ""],
      ["min_reward_risk", "Min reward:risk", "number", ""],
      ["min_stop_pct", "Min stop distance (%)", "number", ""],
      ["max_stop_pct", "Max stop distance (%)", "number", ""],
      ["cooldown_minutes_after_loss", "Cooldown after a loss (min)", "number", "per symbol"],
      ["allow_widen_stop", "Allow widening stops", "bool", "off = stops only tighten"],
    ]],
    ["Time Machine", [
      ["tm_gate", "Gate entries on the Time Machine", "bool", ""],
      ["tm_min_avg_r", "Min avg R after fees", "number", "block below this"],
      ["tm_replays", "Replays", "number", ""],
    ]],
  ];

  function renderSettingsForm() {
    const s = S.settings || {};
    $("settings-form").innerHTML = FIELDS.map(([sec, fields]) => `<div class="form-sec">${sec}</div><div class="form-grid">${fields.map(([k, label, type, hint]) => {
      let v = s[k]; if (Array.isArray(v)) v = v.join(", ");
      let input;
      if (type === "bool") input = `<select data-key="${k}" data-type="bool"><option value="true"${v ? " selected" : ""}>On</option><option value="false"${!v ? " selected" : ""}>Off</option></select>`;
      else if (Array.isArray(type)) input = `<select data-key="${k}">${type.map((o) => `<option value="${o}"${o === v ? " selected" : ""}>${o || "default"}</option>`).join("")}</select>`;
      else input = `<input data-key="${k}" data-type="${type}" value="${esc(v ?? "")}" ${type === "number" ? 'inputmode="decimal"' : ""}>`;
      return `<div class="field"><label>${label}</label>${input}${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
    }).join("")}</div>`).join("") + `<div class="form-sec">Fixed at startup (.env)</div><div class="muted mono" style="font-size:12px;line-height:1.7">mode ${esc(s.mode_label)} · brain ${esc(s.brain)} · margin ${esc(s.margin_type)} · Binance keys ${s.binance_api_key ? "set" : "not set"} · dashboard password ${s.dashboard_password ? "set" : "not set"} · telegram ${s.telegram_bot_token ? "on" : "off"} · liquidation radar ${s.liquidation_radar ? "on" : "off"}</div>`;
  }

  async function saveSettings() {
    const body = {};
    document.querySelectorAll("#settings-form [data-key]").forEach((el) => {
      const k = el.dataset.key, t = el.dataset.type;
      let v = el.value;
      if (t === "bool") v = v === "true";
      else if (t === "number") v = Number(v);
      body[k] = v;
    });
    try {
      const r = await api("/api/settings", { method: "POST", body });
      S.settings = r.settings;
      const n = Object.keys(r.changed || {}).length;
      toast(n ? `Saved ${n} setting(s)` : "Nothing changed", "good");
      $("settings-overlay").classList.remove("open");
    } catch (e) { toast("Save failed: " + e.message, "bad"); }
  }

  // ------------------------------------------------------------------ cycle modal
  async function openCycle(id) {
    try {
      const r = await api(`/api/cycles/${id}`);
      const c = r.cycle;
      $("cycle-title").textContent = `Cycle #${c.id} · ${c.status} · ${fmt.dt(c.started_at)}`;
      const evs = r.events.map((e) => {
        const d = e.data || {};
        if (e.kind === "briefing") return `<details><summary class="muted" style="cursor:pointer">Briefing Claude received</summary><div class="ev-log">${esc(d.text)}</div></details>`;
        if (e.kind === "thought") return `<div class="m-thought" style="margin:8px 0"><div class="av">T</div><div class="tx">${richText(d.text)}</div></div>`;
        if (e.kind === "thinking") return `<div class="m-thinking open">${esc(d.text)}</div>`;
        if (e.kind === "tool_call") return `<div class="m-tool"><summary style="display:inline-flex">&#9881; ${esc(d.name)}</summary><pre>${esc(JSON.stringify(d.input, null, 1))}</pre></div>`;
        if (e.kind === "tool_result") return `<div class="ev-log" style="margin:4px 0 8px 33px;max-height:160px">${esc(d.text || JSON.stringify(d.result))}</div>`;
        if (e.kind === "action") return `<div class="m-notice">action: ${esc(d.action)} ${esc(d.symbol || "")}</div>`;
        if (e.kind === "decision") return `<details><summary class="muted">Raw decision</summary><div class="ev-log">${esc(JSON.stringify(d, null, 1))}</div></details>`;
        return "";
      }).join("");
      $("cycle-body").innerHTML = `<div class="muted" style="margin-bottom:10px">Trigger: ${esc(c.trigger)} · ${c.num_turns ?? "-"} turns · ${c.duration_ms ? fmt.dur(c.duration_ms / 1000) : "-"} · brain ${esc(c.brain)} (${esc(c.model)})</div>
        ${c.error ? `<div class="banner err">${esc(c.error)}</div>` : ""}${c.summary ? `<div class="tm-verdict">${richText(c.summary)}</div>` : ""}${evs || '<div class="empty">No events recorded.</div>'}`;
      $("cycle-overlay").classList.add("open");
    } catch (e) { toast(e.message, "bad"); }
  }

  // ------------------------------------------------------------------ chat
  function chatMsg(role, text) {
    const log = $("chat-log");
    const d = document.createElement("div");
    d.className = "msg " + role;
    d.innerHTML = role === "tushar" ? richText(text) : esc(text);
    log.appendChild(d);
    log.scrollTop = log.scrollHeight;
    return d;
  }
  let chatPending = null;
  async function sendChat() {
    const inp = $("chat-input"); const msg = inp.value.trim();
    if (!msg) return;
    inp.value = "";
    chatMsg("owner", msg);
    if (!chatPending) chatPending = chatMsg("tushar", "Thinking...");
    try {
      await api("/api/chat", { method: "POST", body: { message: msg } });
    } catch (e) { toast("Chat: " + e.message, "bad"); if (chatPending) { chatPending.remove(); chatPending = null; } }
  }

  // ------------------------------------------------------------------ data
  function applyState(st) {
    S.settings = st.settings; S.account = st.account; S.orders = st.orders; S.openTrades = st.open_trades || [];
    S.pending = st.pending || []; S.trades = st.trades || []; S.stats = st.stats; S.sod = st.start_of_day_equity; S.cycles = st.cycles; S.alerts = st.alerts;
    S.tickers = { ...S.tickers, ...(st.tickers || {}) };
    const set = st.settings || {};
    const mb = $("mode-badge");
    mb.textContent = set.mode_label || "-";
    mb.className = "badge mode" + (set.mode_label === "LIVE" ? " live" : "");
    renderStatus(st.status);
    renderKpis(); renderBook(); renderWatch(); renderOutlook(st.outlook); renderScanner(st.scanner);
    renderNews(st.news, st.fng, st.news_status); renderMemory(st.memories); renderPerf(st.stats); renderEquity(st.equity);
    S.team = st.team; S.liq = st.liquidations; S.reports = st.daily_reports || [];
    renderTeam(); renderLiq();
    if (st.sim && (!S.lastSim || S.lastSimId !== st.sim.id)) { S.lastSimId = st.sim.id; renderSim(st.sim); }
  }

  async function refresh(first = false) {
    try {
      const st = await api("/api/state");
      if (first) {
        (st.mind || []).forEach(renderMind);
        mind.scrollTop = mind.scrollHeight;
        (st.chat || []).forEach((m) => chatMsg(m.role, m.text));
        try { const sy = await api("/api/symbols"); renderSymbolSelect([...sy.watchlist, ...sy.universe]); } catch (e) { renderSymbolSelect(st.settings.watchlist); }
      }
      applyState(st);
      S.startError = null;
      if (first) loadChart();
    } catch (e) {
      if (/engine failed/.test(e.message)) { S.startError = e.message; renderBanners(); }
    }
  }

  // ------------------------------------------------------------------ websocket
  let ws, wsDelay = 1000;
  function connect() {
    ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    ws.onopen = () => { wsDelay = 1000; $("mind-count").textContent = "live"; };
    ws.onclose = () => { $("mind-count").textContent = "reconnecting"; setTimeout(connect, wsDelay); wsDelay = Math.min(wsDelay * 2, 15000); };
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      const d = msg.data;
      renderMind(msg);
      switch (msg.type) {
        case "tick":
          S.tickers = d; renderWatch(); updateLegend();
          if (d[S.symbol]) liveCandle(d[S.symbol].price);
          break;
        case "account":
          S.account = d; S.openTrades = d.open_trades || S.openTrades; renderKpis();
          if (S.tab === "positions") renderBook(); else { $("c-positions").textContent = (d.positions || []).length; }
          break;
        case "status": renderStatus(d); break;
        case "outlook": renderOutlook(d); renderTeam(); break;
        case "team": S.team = d; renderTeam(); break;
        case "agent_status":
          if (S.team) { S.team.agents = S.team.agents || {}; S.team.agents[d.agent] = { ...(S.team.agents[d.agent] || {}), status: d.status, ts: msg.ts }; renderTeam(); }
          break;
        case "agent_report":
          if (S.team) { S.team.agents[d.agent] = { ...(S.team.agents[d.agent] || {}), status: d.report.error ? "error" : "done", report: d.report, ts: msg.ts }; renderTeam(); }
          break;
        case "liquidations":
          if (S.liq) { S.liq.recent = [...d.events.slice().reverse(), ...(S.liq.recent || [])].slice(0, 60); S.liq.summary = { ...S.liq.summary, ...d.summary }; renderLiq(d.events); }
          break;
        case "daily_report":
          S.reports = [d, ...(S.reports || [])]; toast("Daily report is ready (Journal > Daily reports)", "good"); if (S.tab === "reports") renderBook();
          speak("Daily report is ready.");
          break;
        case "scanner": renderScanner(d); break;
        case "news": refresh(); break;
        case "sim": S.lastSimId = d.id; renderSim(d); break;
        case "alerts": S.alerts = d; renderBook(); break;
        case "pending": S.pending = d; renderBook(); break;
        case "memory": case "trade_review": refresh(); break;
        case "settings": S.settings = d; break;
        case "usage": if (S.st) { S.st.usage = d; renderStatus(S.st); } break;
        case "trade_open":
          toast(`Opened ${d.trade.side.toUpperCase()} ${sym(d.trade.symbol)} @ ${fmt.price(d.trade.entry_price)}`, "good");
          speak(`Opened ${d.trade.side} ${sym(d.trade.symbol)} at ${fmt.price(d.trade.entry_price)}`);
          refresh().then(() => { if (d.trade.symbol === S.symbol) loadChart(); });
          break;
        case "trade_close":
          if (d.trade) toast(`Closed ${sym(d.trade.symbol)}: ${fmt.signed(d.trade.pnl)} (${d.trade.exit_reason})`, (d.trade.pnl || 0) >= 0 ? "good" : "bad");
          if (d.trade) speak(`Closed ${sym(d.trade.symbol)}, ${(d.trade.pnl || 0) >= 0 ? "profit" : "loss"} of ${Math.abs(d.trade.pnl || 0).toFixed(0)} dollars`);
          refresh().then(() => loadChart());
          break;
        case "protection": refresh().then(drawPriceLines); break;
        case "cycle_end": refresh(); if (d.summary) speak(String(d.summary).split(/(?<=[.!?])\s/)[0]); break;
        case "notice": toast(d.text, d.level === "warn" ? "warn" : ""); break;
        case "alert_hit": toast(d.text, "warn"); speak(d.text); break;
        case "chat":
          if (d.role === "tushar") { if (chatPending) { chatPending.remove(); chatPending = null; } chatMsg("tushar", d.text); }
          break;
        case "chat_event":
          if (d.kind === "tool_call") chatMsg("tool", "⚙ " + d.name);
          break;
        default: break;
      }
    };
  }

  // ------------------------------------------------------------------ events
  document.addEventListener("click", async (ev) => {
    const t = ev.target.closest("[data-chart],[data-close],[data-trade],[data-cycle],[data-cancel-alert],[data-cancel-pending],[data-forget],[data-tab],[data-iv],[data-agent],[data-action]");
    if (!t) return;
    if (t.dataset.agent) { openAgent(t.dataset.agent); return; }
    if (t.dataset.action === "report") {
      t.textContent = "Claude is writing...";
      try { await api("/api/control/report", { method: "POST" }); } catch (e) { toast(e.message, "bad"); }
      t.textContent = "Write today's report now";
      return;
    }
    if (t.dataset.close !== undefined && t.closest(".overlay") && !t.dataset.close) { t.closest(".overlay").classList.remove("open"); return; }
    if (t.dataset.close) {
      ev.stopPropagation();
      if (!confirm(`Close the ${t.dataset.close} position at market?`)) return;
      try { const r = await api(`/api/positions/${t.dataset.close}/close`, { method: "POST" }); toast(`${t.dataset.close}: ${r.status}${r.error ? " " + r.error : ""}`, r.error ? "bad" : "good"); refresh(); }
      catch (e) { toast(e.message, "bad"); }
      return;
    }
    if (t.dataset.cancelPending) { await api(`/api/pending/${t.dataset.cancelPending}/cancel`, { method: "POST" }); refresh(); return; }
    if (t.dataset.cancelAlert) { await api(`/api/alerts/${t.dataset.cancelAlert}/cancel`, { method: "POST" }); refresh(); return; }
    if (t.dataset.forget) { await api(`/api/memories/${t.dataset.forget}/forget`, { method: "POST" }); refresh(); return; }
    if (t.dataset.tab) { S.tab = t.dataset.tab; document.querySelectorAll("#book-tabs button").forEach((b) => b.classList.toggle("on", b === t)); renderBook(); return; }
    if (t.dataset.iv) { S.interval = t.dataset.iv; try { localStorage.setItem("tushar.iv", S.interval); } catch (e) { /* ignore */ } document.querySelectorAll("#iv-seg button").forEach((b) => b.classList.toggle("on", b === t)); loadChart(); return; }
    if (t.dataset.trade) { const d = document.querySelector(`[data-detail="${t.dataset.trade}"]`); if (d) d.classList.toggle("hidden"); return; }
    if (t.dataset.cycle) { openCycle(t.dataset.cycle); return; }
    if (t.dataset.chart) { setSymbol(t.dataset.chart); window.scrollTo({ top: 0, behavior: "smooth" }); }
  });

  function setSymbol(s) {
    S.symbol = s;
    try { localStorage.setItem("tushar.symbol", s); } catch (e) { /* ignore */ }
    if (![...$("sym-select").options].some((o) => o.value === s)) $("sym-select").insertAdjacentHTML("afterbegin", `<option value="${esc(s)}">${esc(s)}</option>`);
    $("sym-select").value = s;
    loadChart();
  }

  $("sym-select").addEventListener("change", (e) => setSymbol(e.target.value));
  document.querySelectorAll("#iv-seg button").forEach((b) => b.classList.toggle("on", b.dataset.iv === S.interval));
  $("btn-run").onclick = async () => { try { const r = await api("/api/control/run", { method: "POST" }); toast(r.ok ? "Claude cycle starting..." : r.error, r.ok ? "good" : "warn"); } catch (e) { toast(e.message, "bad"); } };
  $("btn-pause").onclick = async () => { const paused = S.st?.state === "paused"; await api(`/api/control/${paused ? "resume" : "pause"}`, { method: "POST" }); toast(paused ? "Resumed" : "Paused: no new entries", "warn"); };
  $("btn-kill").onclick = async () => {
    if (S.st?.state === "kill") { await api("/api/control/reset_kill", { method: "POST" }); toast("Kill switch reset", "good"); return; }
    if (!confirm("KILL SWITCH: stop Claude, close ALL positions at market and block new trades?")) return;
    const r = await api("/api/control/kill", { method: "POST" }); toast(`Kill switch on. Closed ${r.closed.length} position(s).`, "bad"); refresh();
  };
  $("btn-scan").onclick = async () => { $("btn-scan").textContent = "Scanning..."; try { await api("/api/control/scan", { method: "POST" }); } finally { $("btn-scan").textContent = "Rescan"; } };
  $("btn-news").onclick = async () => { $("btn-news").textContent = "..."; try { const r = await api("/api/control/news", { method: "POST" }); toast(`${r.new} new headline(s)`); refresh(); } finally { $("btn-news").textContent = "Refresh"; } };
  $("btn-settings").onclick = () => { renderSettingsForm(); $("settings-overlay").classList.add("open"); };
  const voiceBtn = () => { $("btn-voice").innerHTML = voice ? "&#128266;" : "&#128263;"; $("btn-voice").title = voice ? "Voice on" : "Voice off"; };
  voiceBtn();
  $("btn-voice").onclick = () => {
    voice = !voice; try { localStorage.setItem("tushar.voice", voice ? "1" : "0"); } catch (e) { /* ignore */ }
    voiceBtn(); if (voice) speak("Tushar voice is on."); toast(voice ? "Voice announcements on" : "Voice off");
  };
  $("btn-corr").onclick = loadCorr;
  function qrSvg(text, cell = 6) {
    try { const qr = qrcode(0, "L"); qr.addData(text); qr.make(); return qr.createSvgTag(cell, 3); } catch (e) { return ""; }
  }
  let phoneTimer = null;
  async function renderPhone() {
    let n;
    try { n = await api("/api/network"); } catch (e) { $("phone-body").innerHTML = `<div class="banner err">${esc(e.message)}</div>`; return; }
    const remote = location.hostname !== "127.0.0.1" && location.hostname !== "localhost";
    let html;
    if (!n.phone_ready && !remote) {
      html = `<p style="line-height:1.6">The dashboard is only open on this computer right now. Close the black window (or press Ctrl+C in it) and start phone mode:</p>
        <div class="ev-log" style="font-size:15px;color:var(--text)">run.bat phone</div>
        <p class="muted" style="font-size:12.5px;line-height:1.6">Mac/Linux: <code>./run.sh phone</code>. This page then opens by itself with a QR code for your phone.</p>`;
    } else {
      const link = n.public_login || n.lan_login;
      const shown = n.public_url || (n.urls && n.urls[0]) || "";
      const waiting = !n.public_url && ["starting", "connecting", "reconnecting"].includes(n.tunnel);
      html = `<div style="text-align:center">
          <div style="margin-bottom:10px">${n.public_url ? `<span class="pill ok">works on Wi-Fi and mobile data</span>` : waiting ? `<span class="pill run">creating the internet link... (Wi-Fi link below works now)</span>` : `<span class="pill neutral">same Wi-Fi only</span>`}</div>
          <div style="background:#fff;display:inline-block;padding:10px;border-radius:14px">${link ? qrSvg(link) : ""}</div>
          <div class="mono" style="font-size:14px;margin:12px 0 2px;word-break:break-all"><b>${esc(shown)}</b></div>
          ${n.password_set ? `<div class="muted" style="font-size:12px">The QR logs you in. Typing the link instead? Use the access key from the PC window.</div>` : ""}
        </div>
        <ol style="line-height:1.7;font-size:13px;margin:14px 0 0;padding-left:20px">
          <li>Open your phone's <b>camera</b> and point it at the QR code.</li>
          <li>Tap the link that pops up. You're in.</li>
          <li>Tap <b>Share &gt; Add to Home Screen</b> (iPhone) or <b>&#8942; &gt; Add to Home screen</b> (Android) to keep it like an app.</li>
        </ol>
        ${n.public_url && n.urls && n.urls.length ? `<p class="muted" style="font-size:12px;margin-top:10px">Same Wi-Fi alternative: <span class="mono">${esc(n.urls[0])}</span></p>` : ""}
        ${!n.public_url ? `<p class="muted" style="font-size:12px;line-height:1.6;margin-top:10px">Wi-Fi link not loading? Phone must be on the same Wi-Fi (not mobile data), and Windows Firewall must allow Python. The internet link avoids both.</p>` : ""}`;
      clearTimeout(phoneTimer);
      if (waiting && $("phone-overlay").classList.contains("open")) phoneTimer = setTimeout(renderPhone, 2500);
    }
    $("phone-body").innerHTML = html;
  }
  $("btn-phone").onclick = async () => {
    $("phone-body").innerHTML = `<div class="empty">Loading...</div>`;
    $("phone-overlay").classList.add("open");
    await renderPhone();
  };
  if (new URLSearchParams(location.search).get("phone") === "1") {
    history.replaceState(null, "", "/");
    setTimeout(() => $("btn-phone").click(), 1200);
  }
  $("settings-save").onclick = saveSettings;
  document.querySelectorAll("[data-close]").forEach((b) => { if (!b.dataset.close) b.addEventListener("click", () => b.closest(".overlay").classList.remove("open")); });
  document.querySelectorAll(".overlay").forEach((o) => o.addEventListener("click", (e) => { if (e.target === o) o.classList.remove("open"); }));
  $("mind-clear").onclick = () => { mind.innerHTML = ""; };
  $("fab-chat").onclick = () => { $("chat-drawer").classList.add("open"); $("chat-input").focus(); };
  $("chat-close").onclick = () => $("chat-drawer").classList.remove("open");
  $("chat-send").onclick = sendChat;
  $("chat-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); } });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") document.querySelectorAll(".overlay.open,.drawer.open").forEach((o) => o.classList.remove("open")); });

  refresh(true);
  connect();
  setTimeout(loadCorr, 1500);
  setInterval(loadCorr, 300000);
  setInterval(() => { if (S.liq) renderLiq(); }, 15000);
  setInterval(tickClock, 1000);
  setInterval(() => refresh(), 30000);
  window.addEventListener("resize", () => { if (S.lastSim) drawFan(S.lastSim); });
})();

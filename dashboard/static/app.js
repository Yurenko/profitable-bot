const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const TITLES = {
  overview: "Огляд",
  positions: "Позиції",
  trades: "Угоди",
  backtest: "Бектест",
  settings: "Налаштування",
};

let chart = null;
let ws = null;

function toast(msg, ok = true) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast show " + (ok ? "ok" : "err");
  setTimeout(() => el.classList.remove("show"), 3200);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
  });
  if (r.status === 401 && !path.startsWith("/api/login")) {
    location.href = "/login";
    return { ok: false, error: "Unauthorized" };
  }
  let data = {};
  try {
    data = await r.json();
  } catch (_) {
    data = {};
  }
  if (!r.ok) {
    let err = data.error || data.detail || r.statusText;
    if (Array.isArray(data.detail)) {
      err = data.detail.map((x) => x.msg || JSON.stringify(x)).join("; ");
    }
    return { ok: false, error: String(err) };
  }
  return data;
}

function fmtUsd(n) {
  if (n == null || isNaN(n)) return "—";
  const v = Number(n);
  if (!Number.isFinite(v) || Math.abs(v) > 1e12) return "⚠ пошкоджено — скиньте equity";
  return "$" + v.toLocaleString("uk-UA", { maximumFractionDigits: 2 });
}

function fmtPct(n) {
  if (n == null || isNaN(n)) return "—";
  const v = Number(n);
  if (!Number.isFinite(v) || Math.abs(v) > 1e6) return "—";
  return (v * 100).toFixed(2) + "%";
}

/** Europe/Kyiv — e.g. 21.07.2026, 08:35:35 */
function fmtTs(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return d.toLocaleString("uk-UA", {
    timeZone: "Europe/Kyiv",
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function fmtTsShort(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return d.toLocaleString("uk-UA", {
    timeZone: "Europe/Kyiv",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

/** Audit row details without raw UTC `ts` (already shown via fmtTs). */
function auditDetails(a) {
  const copy = { ...a };
  delete copy.ts;
  delete copy.event;
  const s = JSON.stringify(copy);
  return s === "{}" ? "" : s.slice(0, 160);
}

function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.remove("active"));
  $$(".nav-btn, .bottom-nav button").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  $(`#tab-${name}`).classList.add("active");
  $("#pageTitle").textContent = TITLES[name] || name;
  $("#sidebar").classList.remove("open");
  $("#overlay").classList.remove("show");
  if (name === "trades") {
    loadTradeHistory();
    loadAuditFull();
  }
  if (name === "settings") loadConfig();
}

function renderStatus(d) {
  const isLive = d.bot_running && d.bot_mode === "live";
  const paperEq = d.paper_equity != null ? d.paper_equity : d.equity;
  const displayEq = isLive ? d.equity : paperEq;
  const pnlPct = !isLive && d.initial_capital ? (displayEq - d.initial_capital) / d.initial_capital : 0;

  const eqLabel = document.getElementById("statEquityLabel");
  if (eqLabel) eqLabel.textContent = isLive ? "Equity (live)" : "Equity (paper)";
  $("#statEquity").textContent = fmtUsd(displayEq);
  if (isLive) {
    $("#statPnl").textContent = "від балансу біржі · 1% входу від нього";
    $("#statPnl").className = "sub";
  } else {
    const paperPnl = displayEq - d.initial_capital;
    $("#statPnl").textContent = `${paperPnl >= 0 ? "+" : ""}${fmtUsd(paperPnl)} (${fmtPct(pnlPct)})`;
    $("#statPnl").className = "sub " + (paperPnl >= 0 ? "positive" : "negative");
  }

  // Always show real futures wallet when keys work
  const xb = d.exchange_balance || {};
  const exEl = document.getElementById("statExchangeEquity");
  const exDet = document.getElementById("statExchangeDetail");
  if (exEl) {
    if (xb.ok && xb.total != null) {
      exEl.textContent = fmtUsd(xb.total);
      if (exDet) {
        const net = xb.testnet ? "testnet" : "mainnet";
        exDet.textContent = `вільно ${fmtUsd(xb.free)} · в позиціях ${fmtUsd(xb.used)} · ${net}`;
      }
    } else {
      exEl.textContent = "—";
      if (exDet) exDet.textContent = xb.error || "Додайте API ключі в .env";
    }
  }

  $("#statMode").textContent = d.bot_running ? (d.bot_mode || "—").toUpperCase() : "Зупинено";
  $("#statTicks").textContent = d.tick_count ? `Тіків: ${d.tick_count}` : "—";

  $("#statPositions").textContent = (d.positions || []).length;
  $("#statSymbols").textContent = (d.symbols || []).join(", ") || "—";

  $("#statTestnet").textContent = d.testnet ? "Так ✓" : "НІ ⚠";
  $("#statLastTick").textContent = d.last_tick ? fmtTsShort(d.last_tick) : "—";

  const pill = $("#statusPill");
  if (d.bot_running) {
    pill.textContent = `● ${d.bot_mode}`;
    pill.className = "status-pill running";
  } else {
    pill.textContent = "Офлайн";
    pill.className = "status-pill";
  }

  // Positions table
  const tbody = $("#positionsBody");
  tbody.innerHTML = "";
  (d.positions || []).forEach((p) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${p.symbol}</td><td>${p.qty.toFixed(6)}</td><td>${p.avg_entry.toFixed(2)}</td><td>${fmtUsd(p.margin)}</td><td>${p.dca_level}</td>`;
    tbody.appendChild(tr);
  });
  if (!d.positions?.length) {
    tbody.innerHTML = '<tr><td colspan="5" style="color:var(--muted)">Немає відкритих позицій</td></tr>';
  }

  // Audit preview — local time
  const preview = d.audit_preview || [];
  $("#auditPreview").innerHTML = preview
    .map(
      (a) =>
        `<div class="log-item"><span class="ts">${fmtTs(a.ts)}</span> <span class="ev">${a.event}</span> ${a.symbol || ""} ${a.action || ""}</div>`
    )
    .join("") || '<div class="hint">Подій поки немає</div>';

  // Equity curve: while bot runs — live SQLite; otherwise show last backtest if any
  const bt = d.backtest || {};
  const hint = document.getElementById("equityChartHint");
  if (d.bot_running && d.equity_points?.length) {
    updateChart(d.equity_points);
    if (hint) hint.textContent = "Крива з роботи бота (paper/live), оновлюється кожен тік.";
  } else if (bt.metrics && bt.equity_points?.length) {
    updateChart(bt.equity_points);
    if (hint) hint.textContent = "Показано криву останнього бектесту.";
  } else if (d.equity_points?.length) {
    updateChart(d.equity_points);
    if (hint) hint.textContent = "Крива з попередньої сесії бота (SQLite).";
  } else if (hint) {
    hint.textContent = "Немає точок equity — запустіть Paper/Live або бектест.";
  }

  // Backtest status
  const btInfo = document.getElementById("btDataInfo");
  const btFileInfo = document.getElementById("btFileInfo");
  if (bt.running) {
    $("#btStatus").textContent = "⏳ Бектест виконується… (2.9M барів може зайняти 30–60+ хв)";
    if (btInfo) btInfo.textContent = "";
  } else if (bt.error) {
    $("#btStatus").textContent = "❌ " + bt.error;
  } else if (bt.metrics) {
    $("#btStatus").textContent = "✅ Завершено " + (bt.finished_at ? fmtTs(bt.finished_at) : "");
    if (bt.data_info && btInfo) {
      const di = bt.data_info;
      const src = di.source === "historical" ? "📁 Реальні дані" : "🧪 Синтетика";
      btInfo.innerHTML = `${src} · <strong>${di.bars_used?.toLocaleString()}</strong> барів (~<strong>${di.days_approx}</strong> дн.)<br>`
        + `Період: ${(di.period_start || "").slice(0, 16)} → ${(di.period_end || "").slice(0, 16)}`
        + (di.note ? `<br><em>${di.note}</em>` : "");
    }
    renderBtMetrics(bt);
  } else {
    const hasFiles = (d.history_files || []).length > 0;
    $("#btStatus").textContent = hasFiles
      ? "📂 Дані завантажено. Натисніть «Запустити бектест»"
      : "Очікує запуску (спочатку завантажте історію або запустіть на синтетиці)";
  }

  if (btFileInfo && d.history_files?.length) {
    btFileInfo.innerHTML = d.history_files
      .map(
        (f) =>
          `📁 <strong>${f.name}</strong>: ${f.rows?.toLocaleString() || "?"} барів`
          + (f.days_approx ? ` (~${f.days_approx} дн.)` : "")
          + ` · ${f.size_mb} MB`
      )
      .join("<br>");
  } else if (btFileInfo) {
    btFileInfo.textContent = "";
  }

  const dl = d.history_download || {};
  const dlStatus = document.getElementById("dlStatus");
  if (dlStatus) {
    if (dl.running) {
      dlStatus.textContent = `⏳ Качаємо ${dl.symbol || ""}… Це може тривати довго.`;
    } else if (dl.error) {
      dlStatus.textContent = `❌ ${dl.error}`;
    } else if (dl.finished_at) {
      dlStatus.textContent = `✅ ${dl.message || "Готово"}: ${dl.out_file || ""}. Тепер натисніть «Запустити бектест».`;
    }
  }

  $("#btnStop").disabled = !d.bot_running;
  $("#btnStartPaper").disabled = d.bot_running;
  $("#btnStartLive").disabled = d.bot_running;
}

function renderBtMetrics(bt) {
  const m = bt.metrics || {};
  const el = $("#btMetrics");
  const closed = m.closed_trades ?? (bt.closed_trades || []).length;
  const wins = m.wins ?? "—";
  const losses = m.losses ?? "—";
  el.innerHTML = `
    <div class="card stat"><span class="label">Net Profit</span><span class="value ${m.net_profit >= 0 ? "positive" : "negative"}">${fmtUsd(m.net_profit)}</span><span class="sub">${fmtPct(m.net_profit_pct)}</span></div>
    <div class="card stat" title="Максимальна просадка від піку equity"><span class="label">Max DD ⓘ</span><span class="value negative">${fmtPct(m.max_drawdown_pct)}</span></div>
    <div class="card stat" title="Прибуток відносно ризику"><span class="label">Sharpe ⓘ</span><span class="value">${(m.sharpe || 0).toFixed(2)}</span></div>
    <div class="card stat"><span class="label">Win Rate</span><span class="value">${fmtPct(m.win_rate)}</span><span class="sub">${wins} win / ${losses} loss · ${closed} закриттів</span></div>
    <div class="card stat"><span class="label">Profit Factor</span><span class="value">${(m.profit_factor || 0).toFixed(2)}</span></div>
    <div class="card stat"><span class="label">Fees</span><span class="value">${fmtUsd(bt.fees_paid)}</span><span class="sub">Funding ${fmtUsd(bt.funding_paid)}</span></div>
  `;
  if (m.monte_carlo) {
    el.innerHTML += `
      <div class="card stat"><span class="label">MC p50</span><span class="value">${fmtUsd(m.monte_carlo.p50)}</span><span class="sub">p5–p95: ${fmtUsd(m.monte_carlo.p5)} – ${fmtUsd(m.monte_carlo.p95)}</span></div>
    `;
  }

  const tbody = document.getElementById("btTradesBody");
  const help = document.getElementById("btWinHelp");
  if (help) {
    help.textContent =
      `Win Rate = wins / усі закриття (sell). Зараз: ${wins} прибуткових + ${losses} збиткових = ${closed}. ` +
      `Часто не 100%, бо в кінці бектесту відкрита позиція примусово закривається (eod_flat) — навіть у мінусі.`;
  }
  if (tbody) {
    const rows = bt.closed_trades || [];
    tbody.innerHTML = rows
      .slice()
      .reverse()
      .map((t) => {
        const pnl = Number(t.pnl || 0);
        const ok = pnl > 0;
        return `<tr>
          <td>${(t.time || "").slice(0, 19)}</td>
          <td>${t.reason || ""}</td>
          <td>${Number(t.price).toFixed(4)}</td>
          <td>${Number(t.qty).toFixed(6)}</td>
          <td class="${ok ? "positive" : "negative"}">${fmtUsd(pnl)}</td>
          <td>${ok ? "✅ win" : "❌ loss"}</td>
        </tr>`;
      })
      .join("") || '<tr><td colspan="6" style="color:var(--muted)">Немає закритих угод</td></tr>';
  }
}

function updateChart(points) {
  const ctx = $("#equityChart");
  if (!ctx || !points?.length) return;
  const labels = points.map((p) => fmtTsShort(p.t));
  const data = points.map((p) => p.v);
  if (chart) {
    chart.data.labels = labels;
    chart.data.datasets[0].data = data;
    chart.update("none");
    return;
  }
  chart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Equity",
          data,
          borderColor: "#38bdf8",
          backgroundColor: "rgba(56, 189, 248, 0.08)",
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { maxTicksLimit: 6, color: "#64748b" }, grid: { color: "rgba(148,163,184,0.06)" } },
        y: { ticks: { color: "#64748b" }, grid: { color: "rgba(148,163,184,0.06)" } },
      },
    },
  });
}

async function loadConfig() {
  const cfg = await api("/api/config");
  const s = cfg.strategy || {};
  const r = cfg.risk || {};
  const form = $("#settingsForm");

  const map = {
    tp_min_pct: s.tp_min_pct,
    tp_strong_pct: s.tp_strong_pct,
    rsi_5m_strong_oversold: s.rsi_5m_strong_oversold,
    max_dca_levels: s.max_dca_levels,
    dca_min_adverse_pct: s.dca_min_adverse_pct,
    rsi_1m_entry: s.rsi_1m_entry,
    rsi_5m_entry: s.rsi_5m_entry,
    rsi_15m_dca: s.rsi_15m_dca,
    adx_max: s.adx_max,
    leverage: s.leverage,
    initial_equity_pct: s.initial_equity_pct,
    entry_margin_usdt: s.entry_margin_usdt,
    post_entry_add_all_margin: s.post_entry_add_all_margin !== false,
    margin_reserve_usdt: s.margin_reserve_usdt ?? 1,
    topup_free_while_open: s.topup_free_while_open !== false,
    initial_capital: cfg.backtest?.initial_capital,
    news_enabled: cfg.news?.enabled,
    trailing_activate_pct: s.trailing_activate_pct,
    trailing_callback_pct: s.trailing_callback_pct,
    partial_close_pct: s.partial_close_pct,
    max_daily_loss_pct: r.max_daily_loss_pct,
    circuit_breaker_drawdown_pct: r.circuit_breaker_drawdown_pct,
    max_open_positions: r.max_open_positions,
    min_adverse_move_pct: r.min_adverse_move_pct,
    taker_fee: r.taker_fee,
    poll_interval_sec: cfg.loop?.poll_interval_sec,
    use_websocket: cfg.loop?.use_websocket !== false,
    symbols: (cfg.symbols || []).join(", "),
    testnet: cfg.exchange?.testnet,
    trailing_tp_enabled: s.trailing_tp_enabled,
    partial_close_enabled: s.partial_close_enabled,
    long_term_mode: s.long_term_mode,
    auto_take_profit: s.auto_take_profit,
    dca_mode: s.dca_mode,
    grid_step_pct: s.grid_step_pct,
    grid_size_multiplier: s.grid_size_multiplier,
  };

  Object.entries(map).forEach(([name, val]) => {
    const el = form.querySelector(`[name="${name}"]`);
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!val;
    else el.value = val ?? "";
  });
}

async function saveConfig() {
  const form = $("#settingsForm");
  const get = (n) => form.querySelector(`[name="${n}"]`);

  const patch = {
    strategy: {
      tp_min_pct: +get("tp_min_pct").value,
      tp_strong_pct: +get("tp_strong_pct").value,
      rsi_5m_strong_oversold: +get("rsi_5m_strong_oversold").value,
      max_dca_levels: +get("max_dca_levels").value,
      dca_min_adverse_pct: +get("dca_min_adverse_pct").value,
      rsi_1m_entry: +get("rsi_1m_entry").value,
      rsi_5m_entry: +get("rsi_5m_entry").value,
      rsi_15m_dca: +get("rsi_15m_dca").value,
      adx_max: +get("adx_max").value,
      leverage: +get("leverage").value,
      initial_equity_pct: +get("initial_equity_pct").value,
      entry_margin_usdt: +get("entry_margin_usdt").value,
      post_entry_add_all_margin: get("post_entry_add_all_margin").checked,
      margin_reserve_usdt: +get("margin_reserve_usdt").value,
      topup_free_while_open: get("topup_free_while_open").checked,
      trailing_tp_enabled: get("trailing_tp_enabled").checked,
      trailing_activate_pct: +get("trailing_activate_pct").value,
      trailing_callback_pct: +get("trailing_callback_pct").value,
      partial_close_enabled: get("partial_close_enabled").checked,
      partial_close_pct: +get("partial_close_pct").value,
      long_term_mode: get("long_term_mode").checked,
      auto_take_profit: get("auto_take_profit").checked,
      dca_mode: get("dca_mode").value.trim() || "grid",
      grid_step_pct: +get("grid_step_pct").value,
      grid_size_multiplier: +get("grid_size_multiplier").value,
    },
    risk: {
      max_daily_loss_pct: +get("max_daily_loss_pct").value,
      circuit_breaker_drawdown_pct: +get("circuit_breaker_drawdown_pct").value,
      max_open_positions: +get("max_open_positions").value,
      min_adverse_move_pct: +get("min_adverse_move_pct").value,
      taker_fee: +get("taker_fee").value,
    },
    symbols: get("symbols").value.split(",").map((s) => s.trim()).filter(Boolean),
    exchange: { testnet: get("testnet").checked },
    loop: {
      poll_interval_sec: +get("poll_interval_sec").value,
      use_websocket: get("use_websocket").checked,
    },
    news: { enabled: get("news_enabled").checked },
    backtest: { initial_capital: +get("initial_capital").value },
  };

  const res = await api("/api/config", { method: "PATCH", body: JSON.stringify(patch) });
  toast(res.ok ? "Налаштування збережено ✓" : res.error, res.ok);
}

function tradeActionLabel(side, action) {
  const a = String(action || "").toLowerCase();
  if (side === "buy") {
    if (a === "enter") return "🟢 Відкриття";
    if (a === "dca") return "🔵 DCA";
    return "🟢 Купівля";
  }
  if (a === "full_tp") return "🔴 Закриття (TP)";
  if (a === "partial_tp") return "🟠 Часткове TP";
  if (a === "trail_exit") return "🔴 Trailing";
  return "🔴 Продаж";
}

async function loadTradeHistory() {
  const rows = await api("/api/trades?limit=120");
  const tbody = $("#tradesBody");
  if (!tbody) return;
  if (!Array.isArray(rows) || !rows.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="color:var(--muted)">Поки немає угод — зʼявляться після enter / DCA / TP</td></tr>';
    return;
  }
  tbody.innerHTML = rows
    .map((t) => {
      const pnl = t.pnl;
      const pnlCls =
        pnl == null || isNaN(pnl) ? "" : Number(pnl) >= 0 ? "positive" : "negative";
      const pnlTxt = pnl == null || isNaN(pnl) ? "—" : fmtUsd(pnl);
      const avg = t.avg_entry != null ? Number(t.avg_entry).toFixed(4) : "—";
      return `<tr>
        <td>${fmtTs(t.ts)}</td>
        <td>${t.symbol || ""}</td>
        <td>${tradeActionLabel(t.side, t.action)}</td>
        <td>${Number(t.price).toFixed(4)}</td>
        <td>${Number(t.qty).toFixed(6)}</td>
        <td>${fmtUsd(t.notional)}</td>
        <td>${avg}</td>
        <td class="${pnlCls}">${pnlTxt}</td>
      </tr>`;
    })
    .join("");
}

async function loadAuditFull() {
  const rows = await api("/api/audit?limit=60");
  $("#auditFull").innerHTML = rows
    .map(
      (a) =>
        `<div class="log-item"><span class="ts">${fmtTs(a.ts)}</span> <span class="ev">${a.event}</span> ${a.symbol || ""} ${a.action || ""} ${auditDetails(a)}</div>`
    )
    .join("") || '<div class="hint">Порожньо</div>';

  const logs = await api("/api/logs?limit=40");
  $("#logConsole").textContent = (logs || []).join("\n") || "Логів немає";
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (e) => {
    try {
      renderStatus(JSON.parse(e.data));
    } catch (_) {}
  };
  ws.onclose = (ev) => {
    // 4401 = not authenticated (server closed before accept)
    if (ev.code === 4401) {
      location.href = "/login";
      return;
    }
    setTimeout(connectWs, 3000);
  };
}

// Events
$$(".nav-btn, .bottom-nav button").forEach((b) => {
  b.addEventListener("click", () => switchTab(b.dataset.tab));
});

$("#menuBtn").addEventListener("click", () => {
  $("#sidebar").classList.toggle("open");
  $("#overlay").classList.toggle("show");
});
$("#overlay").addEventListener("click", () => {
  $("#sidebar").classList.remove("open");
  $("#overlay").classList.remove("show");
});

$("#btnStartPaper").addEventListener("click", async () => {
  const r = await api("/api/bot/start", { method: "POST", body: JSON.stringify({ mode: "paper" }) });
  toast(r.ok ? "Paper бот запущено" : r.error, r.ok);
});

$("#btnStartLive").addEventListener("click", async () => {
  const testnet = (await api("/api/config")).exchange?.testnet;
  let mainnet_ok = false;
  if (!testnet) {
    mainnet_ok = confirm("⚠ MAINNET! Ви впевнені? Це реальні гроші.");
    if (!mainnet_ok) return;
  }
  const r = await api("/api/bot/start", {
    method: "POST",
    body: JSON.stringify({ mode: "live", mainnet_ok }),
  });
  toast(r.ok ? "Live бот запущено" : r.error, r.ok);
});

$("#btnStop").addEventListener("click", async () => {
  const r = await api("/api/bot/stop", { method: "POST" });
  toast(r.ok ? "Бот зупинено" : r.error, r.ok);
});

$("#btnTick").addEventListener("click", async () => {
  const r = await api("/api/bot/tick", { method: "POST" });
  toast(r.ok ? "Тік виконано" : r.error, r.ok);
});

$("#btnBacktest").addEventListener("click", async () => {
  const useAll = $("#btUseAllBars").checked;
  const bars = useAll ? 0 : +$("#btBars").value;
  const monte_carlo = $("#btMonteCarlo").checked;
  const r = await api("/api/backtest", {
    method: "POST",
    body: JSON.stringify({ bars, monte_carlo }),
  });
  toast(r.ok ? "Бектест запущено… зачекайте" : r.error, r.ok);
});

document.getElementById("btUseAllBars")?.addEventListener("change", (e) => {
  const inp = document.getElementById("btBars");
  if (inp) inp.disabled = e.target.checked;
});

$("#btnDownloadHistory").addEventListener("click", async () => {
  const symbol = $("#dlSymbol").value.trim();
  const since = $("#dlSince").value.trim();
  const timeframe = $("#dlTf").value.trim() || "1m";
  const r = await api("/api/history/download", {
    method: "POST",
    body: JSON.stringify({ symbol, since, timeframe }),
  });
  toast(r.ok ? "Завантаження історії запущено" : r.error, r.ok);
});

$("#btnSave").addEventListener("click", saveConfig);

document.getElementById("btnRefreshTrades")?.addEventListener("click", () => {
  loadTradeHistory();
  loadAuditFull();
});

document.getElementById("btnResetEquity")?.addEventListener("click", async () => {
  const r = await api("/api/equity/reset", { method: "POST", body: "{}" });
  toast(r.ok ? `Equity скинуто на $${r.equity}` : r.error, r.ok);
});

document.getElementById("btBars")?.addEventListener("input", (e) => {
  const n = +e.target.value;
  const days = (n / 1440).toFixed(1);
  const hint = document.getElementById("btBarsHint");
  if (hint) hint.textContent = `${n.toLocaleString()} барів ≈ ${days} днів (1m). 5 років ≈ 2.6 млн барів — потрібен файл parquet.`;
});

// Init
switchTab("overview");
loadConfig();
connectWs();

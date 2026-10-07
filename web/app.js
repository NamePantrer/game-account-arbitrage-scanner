const NEON = {
  cyan: "#00f0ff", magenta: "#ff2bd6", green: "#00ff9f",
  purple: "#a855f7", yellow: "#ffd166", red: "#ff4d6d",
  orange: "#ff9142", muted: "#7d8bb0",
};
const CONF_COLOR = { high: NEON.green, medium: NEON.yellow, low: NEON.red };
const LIQ_COLOR = { fast: NEON.green, medium: NEON.cyan, slow: NEON.yellow, dead: NEON.red, unknown: NEON.muted };
const LIQ_ORDER = ["fast", "medium", "slow", "dead", "unknown"];
const LIQ_LABEL = { fast: "FAST", medium: "MEDIUM", slow: "SLOW", dead: "DEAD", unknown: "—" };
const RISK_COLOR = { low: NEON.green, medium: NEON.yellow, high: NEON.orange, critical: NEON.red, unknown: NEON.muted };
const RISK_ORDER = ["low", "medium", "high", "critical"];
const HEALTH_COLOR = { buyers_market: NEON.red, balanced: NEON.cyan, sellers_market: NEON.green, unknown: NEON.muted };
const HEALTH_LABEL = { buyers_market: "Рынок покупателя", balanced: "Баланс", sellers_market: "Рынок продавца", unknown: "—" };

const POLL_ACTIVE_MS = 30_000;
const POLL_HIDDEN_MS = 120_000;
let pollTimer = null;
const q = (id) => document.getElementById(id);
let charts = {};
let state = { stats: null, activeTab: "advisor", scanVersion: 0 };

Chart.defaults.color = NEON.muted;
Chart.defaults.font.family = "Rajdhani, sans-serif";
Chart.defaults.borderColor = "rgba(255,255,255,0.05)";

const fmt = (n) => new Intl.NumberFormat("ru-RU").format(Math.round(n || 0));
const destroy = (name) => { if (charts[name]) { charts[name].destroy(); delete charts[name]; } };

async function fetchJSON(url, params) {
  const usp = new URLSearchParams(params || {});
  const res = await fetch(`${url}?${usp}`);
  return res.json();
}

// Клик по пузырьку → открыть лот сделки в новой вкладке
function bubbleClick(evt, elements, chart) {
  if (!elements.length) return;
  const el = elements[0];
  const pt = chart.data.datasets[el.datasetIndex].data[el.index];
  const url = pt && (pt.url || pt.sell_url);
  if (url) window.open(url, "_blank");
}
function bubbleHover(evt, elements) {
  if (evt.native && evt.native.target) {
    evt.native.target.style.cursor = elements.length ? "pointer" : "default";
  }
}

const doughnutOpts = { responsive: true, maintainAspectRatio: false, cutout: "60%",
  plugins: { legend: { position: "bottom", labels: { padding: 12, usePointStyle: true } } } };
const barOpts = (beginZero = true) => ({ responsive: true, maintainAspectRatio: false,
  plugins: { legend: { display: false } },
  scales: { x: { grid: { display: false } }, y: { grid: { color: "rgba(255,255,255,.05)" }, beginAtZero: beginZero } } });

// ---------- KPI ----------
function renderKPI(s) {
  const cards = [
    { label: "Сделок найдено", value: fmt(s.total_opportunities), sub: "выше порога", accent: NEON.cyan },
    { label: "Прибыль (нетто)", value: fmt(s.total_net_profit) + " ₽", sub: "после комиссий площадок", accent: NEON.green },
    { label: "Expected Value", value: fmt(s.total_expected_value) + " ₽", sub: `по ${fmt(s.actionable_count ?? 0)} выгодным (риск-скорр.)`, accent: NEON.magenta },
    { label: "Средний риск", value: (s.avg_risk ?? 0), sub: "0-100, ниже лучше", accent: NEON.orange },
    { label: "Шанс успеха", value: (s.avg_success_prob ?? 0) + " %", sub: "в среднем", accent: NEON.yellow },
    { label: "EV ROI", value: (s.ev_roi_percent ?? 0) + " %", sub: "EV / вложения в выгодные", accent: NEON.purple },
  ];
  q("kpiGrid").innerHTML = cards.map((c) => `
    <div class="kpi" style="--accent:${c.accent}">
      <div class="k-label">${c.label}</div>
      <div class="k-value">${c.value}</div>
      <div class="k-sub">${c.sub}</div>
    </div>`).join("");
}

// ---------- Overview charts ----------
function chartByGame(s) {
  destroy("byGame");
  const e = Object.entries(s.by_game);
  charts.byGame = new Chart(q("chartByGame"), {
    type: "bar",
    data: { labels: e.map(([g]) => g), datasets: [{ data: e.map(([, v]) => v.profit),
      backgroundColor: e.map((_, i) => i % 2 ? "rgba(255,43,214,.55)" : "rgba(0,240,255,.55)"),
      borderColor: e.map((_, i) => i % 2 ? NEON.magenta : NEON.cyan), borderWidth: 1.5, borderRadius: 6 }] },
    options: barOpts(),
  });
}
function doughnut(canvasId, chartKey, labels, data, colors) {
  destroy(chartKey);
  charts[chartKey] = new Chart(q(canvasId), {
    type: "doughnut",
    data: { labels, datasets: [{ data, backgroundColor: colors.map((c) => c + "cc"), borderColor: colors, borderWidth: 2, hoverOffset: 8 }] },
    options: doughnutOpts,
  });
}
function chartConfidence(s) {
  const order = ["high", "medium", "low"].filter((k) => s.by_confidence[k]);
  doughnut("chartConfidence", "conf", order.map((l) => l.toUpperCase()), order.map((k) => s.by_confidence[k]), order.map((k) => CONF_COLOR[k]));
}
function chartLiquidity(s) {
  const order = LIQ_ORDER.filter((k) => s.by_liquidity && s.by_liquidity[k]);
  doughnut("chartLiquidity", "liq", order.map((l) => LIQ_LABEL[l]), order.map((k) => s.by_liquidity[k]), order.map((k) => LIQ_COLOR[k]));
}
function chartProfitDist(s) {
  destroy("dist");
  const labels = Object.keys(s.profit_distribution), data = Object.values(s.profit_distribution);
  charts.dist = new Chart(q("chartProfitDist"), {
    type: "bar", data: { labels, datasets: [{ data, backgroundColor: "rgba(168,85,247,.5)", borderColor: NEON.purple, borderWidth: 1.5, borderRadius: 6 }] },
    options: barOpts(),
  });
}
function chartScatter(s) {
  destroy("scatter");
  const groups = { fast: [], medium: [], slow: [], dead: [] };
  s.scatter.forEach((p) => { (groups[p.liq] || (groups[p.liq] = [])).push({ x: p.days, y: p.profit, r: Math.max(4, (p.score - 50) / 4), url: p.url, sell_url: p.sell_url, title: p.title }); });
  charts.scatter = new Chart(q("chartScatter"), {
    type: "bubble",
    data: { datasets: Object.entries(groups).filter(([, v]) => v.length).map(([liq, pts]) => ({
      label: LIQ_LABEL[liq] || liq, data: pts, backgroundColor: (LIQ_COLOR[liq] || NEON.muted) + "55", borderColor: LIQ_COLOR[liq] || NEON.muted, borderWidth: 1.5 })) },
    options: { responsive: true, maintainAspectRatio: false, onClick: bubbleClick, onHover: bubbleHover,
      plugins: { legend: { position: "bottom", labels: { usePointStyle: true, padding: 12 } },
        tooltip: { callbacks: { title: (c) => c[0].raw.title || "", label: (c) => `~${c.raw.x} дн · ${fmt(c.raw.y)}₽ · клик → открыть лот` } } },
      scales: { x: { title: { display: true, text: "Дни до продажи", color: NEON.muted }, grid: { color: "rgba(255,255,255,.05)" }, beginAtZero: true },
        y: { title: { display: true, text: "Прибыль ₽", color: NEON.muted }, grid: { color: "rgba(255,255,255,.05)" }, beginAtZero: true } } },
  });
}

// ---------- Risk charts ----------
function chartRiskReward(s) {
  destroy("rr");
  const groups = {}; RISK_ORDER.forEach((r) => groups[r] = []);
  s.risk_reward.forEach((p) => { (groups[p.risklabel] || (groups[p.risklabel] = [])).push({ x: p.risk, y: p.profit, r: Math.max(4, Math.abs(p.ev) / 120), url: p.url, sell_url: p.sell_url, title: p.title }); });
  charts.rr = new Chart(q("chartRiskReward"), {
    type: "bubble",
    data: { datasets: RISK_ORDER.filter((r) => groups[r] && groups[r].length).map((r) => ({
      label: r.toUpperCase(), data: groups[r], backgroundColor: RISK_COLOR[r] + "55", borderColor: RISK_COLOR[r], borderWidth: 1.5 })) },
    options: { responsive: true, maintainAspectRatio: false, onClick: bubbleClick, onHover: bubbleHover,
      plugins: { legend: { position: "bottom", labels: { usePointStyle: true, padding: 12 } },
        tooltip: { callbacks: { title: (c) => c[0].raw.title || "", label: (c) => `риск ${c.raw.x} · прибыль ${fmt(c.raw.y)}₽ · клик → открыть лот` } } },
      scales: { x: { title: { display: true, text: "Риск (0-100)", color: NEON.muted }, grid: { color: "rgba(255,255,255,.05)" }, beginAtZero: true },
        y: { title: { display: true, text: "Прибыль ₽", color: NEON.muted }, grid: { color: "rgba(255,255,255,.05)" }, beginAtZero: true } } },
  });
}
function chartRiskDist(s) {
  const order = RISK_ORDER.filter((k) => s.by_risk && s.by_risk[k]);
  doughnut("chartRiskDist", "riskdist", order.map((l) => l.toUpperCase()), order.map((k) => s.by_risk[k]), order.map((k) => RISK_COLOR[k]));
}
function chartFees(s) {
  destroy("fees");
  const ft = s.fee_totals || {};
  const labels = Object.keys(ft).filter((k) => k !== "Чистая прибыль");
  const data = labels.map((k) => ft[k]);
  const colorMap = {
    "Закуп": NEON.cyan,
    "Комиссия продажи": NEON.yellow,
    "Комиссия вывода": NEON.orange,
    "YooKassa: списание с карты": NEON.magenta,
    "YooKassa: выплата на карту": NEON.purple,
    "Пополнение": NEON.muted,
    "Перевод между площадками": NEON.red,
  };
  const colors = labels.map((l, i) => (colorMap[l] || [NEON.cyan, NEON.green, NEON.red][i % 3]));
  charts.fees = new Chart(q("chartFees"), {
    type: "doughnut",
    data: { labels, datasets: [{ data, backgroundColor: colors.map((c) => c + "cc"), borderColor: colors, borderWidth: 2, hoverOffset: 8 }] },
    options: { ...doughnutOpts, plugins: { ...doughnutOpts.plugins,
      tooltip: { callbacks: { label: (c) => `${c.label}: −${fmt(c.raw)}₽` } } } },
  });
}
function renderRiskMatrix(s) {
  const order = RISK_ORDER;
  q("riskMatrix").innerHTML = order.map((r) => `
    <div class="rm-cell">
      <div class="rm-count" style="color:${RISK_COLOR[r]}">${s.by_risk[r] || 0}</div>
      <div class="rm-label">${r}</div>
    </div>`).join("");
}

// ---------- Market ----------
async function renderMarket() {
  const market = await fetchJSON("/api/market");
  destroy("comp");
  charts.comp = new Chart(q("chartCompetition"), {
    type: "bar",
    data: { labels: market.map((m) => m.game), datasets: [{ data: market.map((m) => m.competition_index),
      backgroundColor: market.map((m) => (m.competition_index > 1 ? NEON.red : NEON.green) + "88"),
      borderColor: market.map((m) => m.competition_index > 1 ? NEON.red : NEON.green), borderWidth: 1.5, borderRadius: 6 }] },
    options: barOpts(),
  });
  const hc = {}; market.forEach((m) => hc[m.health] = (hc[m.health] || 0) + 1);
  const hk = Object.keys(hc);
  doughnut("chartHealth", "health", hk.map((k) => HEALTH_LABEL[k]), hk.map((k) => hc[k]), hk.map((k) => HEALTH_COLOR[k]));

  q("marketTableBody").innerHTML = market.map((m) => `
    <tr>
      <td><span class="game-tag">${m.game}</span></td>
      <td><span class="health-tag health-${m.health}">${HEALTH_LABEL[m.health]}</span></td>
      <td class="num">${m.deals}</td>
      <td class="num">${fmt(m.median_buy)} ₽</td>
      <td class="num" title="медиана честного сегмента">${fmt(m.optimal_sell || m.median_sell)} ₽</td>
      <td class="num muted" title="p25–p75 честного сегмента">${fmt(m.segment_low)}–${fmt(m.segment_high)} ₽</td>
      <td class="num">${m.outliers_filtered || 0}</td>
      <td class="num">${m.avg_margin}%</td>
      <td class="num">${m.avg_risk}</td>
      <td class="num">${m.avg_days}</td>
      <td class="num" style="color:${m.competition_index > 1 ? NEON.red : NEON.green}">${m.competition_index}</td>
    </tr>`).join("");
}

// ---------- Strategies ----------
async function renderStrategies() {
  const st = await fetchJSON("/api/strategies");
  q("strategyCards").innerHTML = st.map((x) => `
    <div class="scard">
      <h4>${x.title}</h4>
      <div class="srow"><span>Сделок</span><b>${x.count}</b></div>
      <div class="srow"><span>Прибыль</span><b class="profit-pos">${fmt(x.total_profit)} ₽</b></div>
      <div class="srow"><span>Expected Value</span><b>${fmt(x.total_ev)} ₽</b></div>
      <div class="srow"><span>Ср. риск</span><b>${x.avg_risk}</b></div>
      <div class="srow"><span>Ср. срок</span><b>${x.avg_days} дн</b></div>
    </div>`).join("");
  destroy("stratEV");
  charts.stratEV = new Chart(q("chartStrategyEV"), {
    type: "bar",
    data: { labels: st.map((x) => x.title), datasets: [{ data: st.map((x) => x.total_ev),
      backgroundColor: "rgba(0,255,159,.5)", borderColor: NEON.green, borderWidth: 1.5, borderRadius: 6 }] },
    options: barOpts(),
  });
  doughnut("chartStrategyShare", "stratShare", st.map((x) => x.title), st.map((x) => x.count),
    [NEON.cyan, NEON.magenta, NEON.green, NEON.yellow, NEON.purple, NEON.orange]);
}

// ---------- Portfolio ----------
async function renderPortfolio() {
  const budget = q("budgetInput").value;
  const pf = await fetchJSON("/api/portfolio", { budget });
  const kpi = [
    { label: "Вложить", value: fmt(pf.invested) + " ₽", sub: `из ${fmt(pf.budget)} ₽`, accent: NEON.cyan },
    { label: "Ожид. прибыль", value: fmt(pf.expected_profit) + " ₽", sub: "если всё продастся", accent: NEON.green },
    { label: "Expected Value", value: fmt(pf.expected_value) + " ₽", sub: "с учётом риска", accent: NEON.magenta },
    { label: "EV ROI", value: pf.roi_percent + " %", sub: "на вложенное", accent: NEON.purple },
    { label: "Ср. риск", value: pf.avg_risk, sub: "портфеля", accent: NEON.orange },
    { label: "Ср. срок", value: pf.avg_days + " дн", sub: "оборот капитала", accent: NEON.yellow },
  ];
  q("portfolioKpi").innerHTML = kpi.map((c) => `
    <div class="kpi" style="--accent:${c.accent}"><div class="k-label">${c.label}</div><div class="k-value">${c.value}</div><div class="k-sub">${c.sub}</div></div>`).join("");
  q("pfCount").textContent = pf.count;
  q("portfolioTableBody").innerHTML = pf.items.map((it) => `
    <tr>
      <td><span class="game-tag">${it.game}</span></td>
      <td><span class="lot-title" title="${it.title}">${it.title}</span></td>
      <td><span class="route">${it.route}</span></td>
      <td>${(it.strategies || []).map((s) => `<span class="stag">${s}</span>`).join("")}</td>
      <td class="num">${fmt(it.allocation)} ₽</td>
      <td class="num profit-pos">+${fmt(it.net_profit)} ₽</td>
      <td class="num ev-pos">${fmt(it.expected_return)} ₽</td>
      <td class="num"><span class="risk-badge risk-${it.risk_label}">${it.risk_score}</span></td>
      <td class="num">${it.days}</td>
    </tr>`).join("");
}

// ---------- Deals table ----------
function feeTooltip(o) {
  const fb = o.fee_breakdown || {};
  const lines = Object.entries(fb)
    .filter(([k]) => k !== "Чистая прибыль")
    .map(([k, v]) => `${k}: −${fmt(v)}₽`);
  if (o.yookassa_charge_rub > 0) lines.push(`YK списание: −${fmt(o.yookassa_charge_rub)}₽`);
  if (o.yookassa_payout_rub > 0) lines.push(`YK выплата: −${fmt(o.yookassa_payout_rub)}₽`);
  if (o.payout_fee_rub > 0) lines.push(`Вывод ${o.sell_platform}: −${fmt(o.payout_fee_rub)}₽`);
  return lines.join(" · ") || "комиссии";
}
function feeCell(o) {
  const tip = feeTooltip(o);
  const yk = (o.yookassa_charge_rub || 0) + (o.yookassa_payout_rub || 0);
  const sub = yk > 0 ? `<div class="fee-sub">YK −${fmt(yk)}₽</div>` : (o.payout_fee_rub > 0 ? `<div class="fee-sub">вывод −${fmt(o.payout_fee_rub)}₽</div>` : "");
  return `<td class="num fee-cell" title="${tip}">-${fmt(o.total_fees_rub)} ₽${sub}</td>`;
}
function liqCell(o) {
  const label = o.liquidity_label || "unknown";
  const days = o.est_days_to_sell ? `~${o.est_days_to_sell} дн` : "—";
  return `<span class="liq"><span class="liq-badge liq-${label}">${LIQ_LABEL[label] || label}</span><span class="liq-days">${days}</span></span>`;
}
function sellCell(o) {
  const parts = [];
  if (o.price_segment_low && o.price_segment_high) {
    parts.push(`сегмент ${fmt(o.price_segment_low)}–${fmt(o.price_segment_high)} ₽`);
  }
  if (o.market_median_rub) parts.push(`медиана ${fmt(o.market_median_rub)} ₽`);
  if (o.price_outliers_filtered) parts.push(`выбросов отфильтровано: ${o.price_outliers_filtered}`);
  if (o.sell_listing_raw_price && o.sell_listing_raw_price !== o.sell_price_rub) {
    parts.push(`референс-лот ${fmt(o.sell_listing_raw_price)} ₽`);
  }
  if ((o.notes || []).length) parts.push((o.notes || []).filter((n) => n.includes("📊") || n.includes("📈") || n.includes("📉")).join(" · "));
  const tip = parts.join(" · ") || "цена продажи по честному сегменту";
  const adjusted = o.sell_listing_raw_price && Math.abs(o.sell_listing_raw_price - o.sell_price_rub) > 1;
  const hint = adjusted ? `<span class="price-hint" title="скорректировано от выброса">*</span>` : "";
  return `<td class="num sell-cell" title="${tip}">${fmt(o.sell_price_rub)} ₽${hint}</td>`;
}
function renderTable(rows) {
  q("rowCount").textContent = rows.length;
  q("oppTableBody").innerHTML = rows.map((o, idx) => {
    const days = Math.max(o.est_days_to_sell || 1, 1);
    const daily = o.net_profit_rub / days;
    const annual = (o.net_profit_rub / Math.max(o.buy_total_rub, 1)) * (365 / days) * 100;
    const evClass = o.expected_value_rub >= 0 ? "ev-pos" : "ev-neg";
    return `
    <tr data-opp-idx="${idx}">
      <td><span class="risk-badge risk-${o.risk_label}" title="risk ${o.risk_score}">${o.risk_label}</span></td>
      <td>${liqCell(o)}</td>
      <td><span class="game-tag">${o.buy.game}</span></td>
      <td><span class="lot-title" title="${o.buy.title}">${o.buy.title}</span></td>
      <td><span class="route">${o.buy.platform} → ${o.sell_platform}</span></td>
      <td class="num">${fmt(o.buy.price_rub)} ₽</td>
      ${sellCell(o)}
      ${feeCell(o)}
      <td class="num profit-pos" title="после комиссий площадок">+${fmt(o.net_profit_rub)} ₽</td>
      <td class="num ${evClass}">${fmt(o.expected_value_rub)} ₽</td>
      <td class="num">${Math.round((o.success_probability || 0) * 100)}%</td>
      <td class="num daily">${fmt(daily)} ₽</td>
      <td class="num">${Math.round(annual)}%</td>
      <td>${(o.strategies || []).map((s) => `<span class="stag">${s}</span>`).join("")}</td>
      <td><button class="chip-btn" data-buy-idx="${idx}" title="Отметить как купленное">Купил</button></td>
    </tr>`;
  }).join("");
  state._dealRows = rows;
  q("oppTableBody").querySelectorAll("[data-buy-idx]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const opp = state._dealRows[Number(btn.dataset.buyIdx)];
      if (!opp) return;
      const res = await postJSON("/api/journal/buy", opp);
      if (res.ok) {
        btn.textContent = "✓";
        btn.disabled = true;
      } else alert(res.error || "Не удалось записать");
    });
  });
}
function dealFilters() {
  return {
    game: q("filterGame").value, confidence: q("filterConfidence").value,
    risk: q("filterRisk").value, liquidity: q("filterLiquidity").value,
    strategy: q("filterStrategy").value, sell_platform: q("filterPlatform").value,
    min_profit: q("filterMinProfit").value, max_risk: q("filterMaxRisk").value,
    max_days: q("filterMaxDays").value, sort: q("filterSort").value,
  };
}
async function loadTableOnly() {
  renderTable(await fetchJSON("/api/opportunities", dealFilters()));
}

// ---------- Advisor ----------
function isDemoLink(url, externalId) {
  if (!url) return true;
  if (externalId && String(externalId).startsWith("sample")) return true;
  if (externalId && String(externalId).startsWith("sellref")) return true;
  return false;
}
function safeLotLink(url, label, externalId) {
  if (!url) return "—";
  if (isDemoLink(url, externalId) || state._scanSource === "offline") {
    return `<button class="neon-link demo-link" type="button" title="OFFLINE: демо-ссылка, лота на площадке нет"
      onclick="alert('Сейчас режим OFFLINE — это учебные данные.\\nСсылки на Lolz/FunPay ненастоящие.\\n\\nЧтобы открывать реальные лоты:\\n1) задай LZT_API_TOKEN в .env\\n2) перезапусти сервер\\n3) дождись LIVE-скана')">🔒 ${label} (демо)</button>`;
  }
  return `<a class="neon-link" href="${url}" target="_blank" rel="noopener">${label} ↗</a>`;
}
function pickSideBuy(p) {
  const seller = (!isDemoLink(p.seller_url) && state._scanSource !== "offline" && p.seller_url)
    ? `<a class="neon-link" href="${p.seller_url}" target="_blank" rel="noopener">${p.seller_name || "продавец"}</a>`
    : (p.seller_name || "продавец");
  const rep = p.seller_reviews != null ? `${p.seller_rating ?? "?"}★ · ${p.seller_reviews} отз.` : "";
  const lot = safeLotLink(p.buy_url, "открыть лот", p.item_id || p.external_id);
  return `<div class="pick-side buy">
    <h5>🟢 Где купить</h5>
    <div class="pline"><b>${p.buy_platform.toUpperCase()}</b> за <b>${fmt(p.buy_price)} ₽</b></div>
    <div class="pline">Продавец: ${seller} ${rep ? "· " + rep : ""}</div>
    <div class="pline">Источник: ${p.origin || "—"}</div>
    <div class="pline">${lot}</div>
  </div>`;
}
function pickSideSell(p) {
  const ref = safeLotLink(p.sell_url, "похожий лот");
  const payout = p.payout_fee > 0
    ? `<div class="pline fee-payout">Вывод с ${p.sell_platform.toUpperCase()}: −${fmt(p.payout_fee)} ₽</div>`
    : "";
  return `<div class="pick-side sell">
    <h5>🟣 Где продать</h5>
    <div class="pline"><b>${p.sell_platform.toUpperCase()}</b> за <b>${fmt(p.sell_price)} ₽</b></div>
    <div class="pline">Продажа: −${fmt(p.sell_fee)} ₽ · всего комиссий: −${fmt(p.total_fees)} ₽</div>
    ${payout}
    ${p.yookassa_charge > 0 ? `<div class="pline fee-payout">YooKassa списание: −${fmt(p.yookassa_charge)} ₽</div>` : ""}
    ${p.yookassa_payout > 0 ? `<div class="pline fee-payout">YooKassa выплата: −${fmt(p.yookassa_payout)} ₽</div>` : ""}
    <div class="pline">На карту: <b>${fmt(p.net_revenue)} ₽</b></div>
    <div class="pline">Ориентир цены: ${ref}</div>
  </div>`;
}
function pickCard(p, isBest, idx) {
  const warns = (p.warnings || []).length
    ? `<div class="pick-warn">⚠️ ${p.warnings.join(" · ")}</div>` : "";
  const strat = (p.strategies || []).map((s) => `<span class="stag">${s}</span>`).join(" ");
  const tradeId = (state._advisorTrades && state._advisorTrades[idx]) || "";
  const bought = Boolean(tradeId);
  return `<div class="pick ${isBest ? "best" : ""}" data-pick-idx="${idx}">
    <div class="pick-top">
      <div class="pick-title">${p.title} <span class="game-tag">${p.game}</span>${isBest ? '<span class="best-tag">ЛУЧШАЯ</span>' : ""}</div>
      <span class="risk-badge risk-${p.risk_label}">риск ${p.risk_score}</span>
    </div>
    <div class="strat-chips">${strat}</div>
    <div class="pick-action">${p.action}</div>
    <div class="pick-grid">${pickSideBuy(p)}${pickSideSell(p)}</div>
    <div class="pick-econ">
      <span>Прибыль: <b>+${fmt(p.net_profit)} ₽</b> <small>(на руки)</small></span>
      <span>EV: <b>${fmt(p.expected_value)} ₽</b></span>
      <span>ROI: <b>${p.roi_percent}%</b></span>
      <span>Шанс успеха: <b>${Math.round(p.success_probability * 100)}%</b></span>
      <span>Срок: <b>~${p.est_days_to_sell} дн</b></span>
    </div>
    ${warns}
    <div class="pick-journal-actions">
      <button class="chip-btn ${bought ? "" : "primary"}" data-adv-buy="${idx}" ${bought ? "disabled" : ""}>${bought ? "✓ Куплено" : "🛒 Купил"}</button>
      <button class="chip-btn" data-adv-sell="${idx}" ${bought ? "" : "title=\"Сначала отметь покупку или укажи цену — создадим сделку\""}>💰 Продал</button>
      ${bought ? `<a class="chip-btn" href="#" data-goto-journal="1">в журнале ↗</a>` : ""}
    </div>
  </div>`;
}

function pickToJournalBuy(p) {
  const margin = p.buy_total > 0 ? (p.net_profit / p.buy_total) * 100 : 0;
  return {
    title: p.title,
    game: p.game,
    buy_platform: p.buy_platform,
    sell_platform: p.sell_platform,
    buy_price: p.buy_price,
    buy_total: p.buy_total,
    buy_url: p.buy_url || "",
    sell_url: p.sell_url || "",
    margin,
    risk: p.risk_score,
    days: p.est_days_to_sell,
    ev: p.expected_value,
    net_profit: p.net_profit,
    source: "advisor",
    notes: (p.strategies || []).join(", "),
  };
}

async function advisorMarkBought(idx) {
  const p = (state._advisorPicks || [])[idx];
  if (!p) return;
  const res = await postJSON("/api/journal/buy", pickToJournalBuy(p));
  if (!res.ok) { alert(res.error || "Не удалось записать покупку"); return; }
  state._advisorTrades = state._advisorTrades || {};
  state._advisorTrades[idx] = res.trade.id;
  await renderAdvisor();
}

async function advisorMarkSold(idx) {
  const p = (state._advisorPicks || [])[idx];
  if (!p) return;
  let tradeId = (state._advisorTrades || {})[idx];

  // если ещё не отметили покупку — создаём позицию, потом сразу закрываем
  if (!tradeId) {
    const buyRes = await postJSON("/api/journal/buy", pickToJournalBuy(p));
    if (!buyRes.ok) { alert(buyRes.error || "Не удалось создать позицию"); return; }
    tradeId = buyRes.trade.id;
    state._advisorTrades = state._advisorTrades || {};
    state._advisorTrades[idx] = tradeId;
  }

  const priceRaw = prompt(`Цена продажи, ₽ (ориентир советника: ${fmt(p.sell_price)}):`, String(Math.round(p.sell_price || 0)));
  if (priceRaw == null || !Number(priceRaw)) return;
  const feesRaw = prompt("Комиссии продажи, ₽ (Enter = 0):", String(Math.round((p.sell_fee || 0) + (p.payout_fee || 0))));
  const nick = prompt("Ник покупателя (необязательно):", "") || "";
  const res = await postJSON("/api/journal/sell", {
    id: tradeId,
    sell_price: Number(priceRaw),
    sell_fees: Number(feesRaw) || 0,
    buyer_nick: nick,
  });
  if (!res.ok) { alert(res.error || "Не удалось записать продажу"); return; }
  delete state._advisorTrades[idx];
  alert(`Продажа записана. PnL: ${res.trade.realized_profit >= 0 ? "+" : ""}${fmt(res.trade.realized_profit)} ₽ — ИИ обновит стратегии.`);
  await renderAdvisor();
}

function bindAdvisorJournalActions() {
  q("picksList").querySelectorAll("[data-adv-buy]").forEach((btn) => {
    btn.onclick = () => advisorMarkBought(Number(btn.dataset.advBuy));
  });
  q("picksList").querySelectorAll("[data-adv-sell]").forEach((btn) => {
    btn.onclick = () => advisorMarkSold(Number(btn.dataset.advSell));
  });
  q("picksList").querySelectorAll("[data-goto-journal]").forEach((btn) => {
    btn.onclick = (e) => { e.preventDefault(); switchTab("journal"); };
  });
}

async function renderAdvisor() {
  const budget = q("advBudget").value;
  const plan = await fetchJSON("/api/advisor", { budget });

  if (!plan.best_pick) {
    q("advisorHero").innerHTML = `<h2>${plan.strategy_name}</h2><div class="adesc">${plan.strategy_desc}</div>
      <div class="warn-box">${(plan.warnings || []).map((w) => `<div class="w-item">⚠️ ${w}</div>`).join("")}</div>`;
    q("advisorSteps").innerHTML = "";
    q("growthTableBody").innerHTML = ""; q("growthCount").textContent = "0";
    q("picksList").innerHTML = ""; q("picksCount").textContent = "0";
    state._advisorPicks = [];
    return;
  }

  q("advisorHero").innerHTML = `
    <h2>${plan.strategy_name}</h2>
    <div class="adesc">${plan.strategy_desc}</div>
    <div class="ametrics">
      <div class="ametric"><div class="am-v">${fmt(plan.budget)} ₽</div><div class="am-l">Старт</div></div>
      <div class="ametric"><div class="am-v">${fmt(plan.projected_capital)} ₽</div><div class="am-l">Прогноз капитала</div></div>
      <div class="ametric"><div class="am-v">+${plan.projected_roi_percent}%</div><div class="am-l">Рост за ~${plan.projected_days} дн</div></div>
      <div class="ametric"><div class="am-v">${plan.affordable_count}</div><div class="am-l">Доступных сделок</div></div>
    </div>`;

  const warns = (plan.warnings || []).length
    ? `<div class="warn-box">${plan.warnings.map((w) => `<div class="w-item">⚠️ ${w}</div>`).join("")}</div>` : "";
  q("advisorSteps").innerHTML = `<div class="card glass steps-card">
    <h3>📋 Пошаговый план</h3>
    ${plan.steps.map((s) => `<div class="step">${s}</div>`).join("")}
    ${warns}
  </div>`;

  q("growthCount").textContent = plan.growth.length;
  q("growthTableBody").innerHTML = plan.growth.map((g) => `
    <tr>
      <td class="num">${g.round_no}</td>
      <td><span class="lot-title" title="${g.pick_title}">${g.pick_title}</span> <span class="game-tag">${g.pick_game}</span></td>
      <td class="num">${fmt(g.capital_start)} ₽</td>
      <td class="num">${g.qty}</td>
      <td class="num">${fmt(g.invested)} ₽</td>
      <td class="num ev-pos">+${fmt(g.expected_gain)} ₽</td>
      <td class="num profit-pos">${fmt(g.capital_end)} ₽</td>
      <td class="num">${g.days}</td>
      <td class="num">${g.cumulative_days}</td>
    </tr>`).join("");

  state._advisorPicks = plan.picks || [];
  state._advisorTrades = state._advisorTrades || {};
  q("picksCount").textContent = plan.picks.length;
  q("picksList").innerHTML = plan.picks.map((p, i) => pickCard(p, i === 0, i)).join("");
  bindAdvisorJournalActions();
}

// ---------- Autobuy ----------
const AB_STATUS_LABEL = {
  purchased: "✅ Куплено",
  dry_run: "🧪 Тест",
  queued: "⏳ В очереди",
  awaiting_funds: "💸 Ждёт средств",
  failed: "❌ Ошибка",
};

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return res.json();
}

async function saveAutobuySettings(patch) {
  return postJSON("/api/autobuy/settings", patch);
}

function abToggle(id, label, checked, key) {
  return `<label class="ab-toggle"><input type="checkbox" id="${id}" data-key="${key}" ${checked ? "checked" : ""} />
    <span class="ab-switch"></span><span>${label}</span></label>`;
}

function abSlider(id, label, val, min, max, step, key, suffix = "") {
  return `<div class="filter-group"><label>${label}: <b id="${id}Val">${fmt(val)}${suffix}</b></label>
    <input type="range" id="${id}" data-key="${key}" min="${min}" max="${max}" step="${step}" value="${val}" /></div>`;
}

async function renderAutobuy() {
  const data = await fetchJSON("/api/autobuy/status");
  const s = data.settings;
  const st = data.state;
  const [history, queue] = await Promise.all([
    fetchJSON("/api/autobuy/history", { limit: 30 }),
    fetchJSON("/api/autobuy/queue"),
  ]);

  const mode = s.dry_run ? "тестовый режим" : "боевой";

  q("autobuyHero").innerHTML = `
    <h2>💳 Автопокупка на Lolz</h2>
    <p class="adesc">После каждого <b>live</b>-скана ИИ покупает лучшие сделки через Lolz fast-buy API.
      Баланс пополняешь <b>вручную</b> на Lolz — при нехватке средств лот встаёт в очередь.
      Продажи на FunPay фиксируй во вкладке «Клиенты».</p>
    <div class="ab-status-grid">
      <div class="ab-stat"><span class="ab-k">Режим</span><span class="ab-v">${s.enabled ? mode : "выкл"}</span></div>
      <div class="ab-stat"><span class="ab-k">Баланс Lolz</span><span class="ab-v">${fmt(st.lolz_balance_rub)} ₽</span></div>
      <div class="ab-stat"><span class="ab-k">Потрачено сегодня</span><span class="ab-v">${fmt(st.spent_today_rub)} ₽</span></div>
      <div class="ab-stat"><span class="ab-k">Покупок сегодня</span><span class="ab-v">${st.purchases_today}</span></div>
      <div class="ab-stat"><span class="ab-k">В очереди</span><span class="ab-v">${st.queue_size}</span></div>
      <div class="ab-stat"><span class="ab-k">Lolz API</span><span class="ab-v">${st.lolz_configured ? "✓" : "нет токена"}</span></div>
    </div>
    ${st.last_error ? `<div class="warn-box"><div class="w-item">⚠️ ${st.last_error}</div></div>` : ""}`;

  q("autobuyFilters").innerHTML = `
    <div class="ab-toggles">
      ${abToggle("abEnabled", "Включить автопокупку", s.enabled, "enabled")}
      ${abToggle("abDryRun", "Тестовый режим (без реальных покупок)", s.dry_run, "dry_run")}
    </div>
    ${abSlider("abMaxDeal", "Макс. на сделку", s.max_per_deal_rub, 500, 20000, 500, "max_per_deal_rub", " ₽")}
    ${abSlider("abMaxDay", "Лимит покупок в день", s.max_daily_rub, 1000, 100000, 1000, "max_daily_rub", " ₽")}
    ${abSlider("abMinProfit", "Мин. прибыль", s.min_profit_rub, 50, 2000, 50, "min_profit_rub", " ₽")}
    ${abSlider("abMaxRisk", "Макс. риск", s.max_risk_score, 10, 80, 5, "max_risk_score")}
    <div class="filter-group" style="justify-content:flex-end;align-self:end;">
      <button class="btn-refresh" id="abSaveBtn">Сохранить настройки</button>
    </div>`;

  bindAutobuyOnce();

  q("abSaveBtn").addEventListener("click", async () => {
    const patch = {};
    q("autobuyFilters").querySelectorAll("[data-key]").forEach((el) => {
      patch[el.dataset.key] = el.type === "checkbox" ? el.checked : Number(el.value);
    });
    await saveAutobuySettings(patch);
    renderAutobuy();
  });

  // Queue
  q("abQueueCount").textContent = queue.length;
  q("abQueueBody").innerHTML = queue.length ? queue.map((it) => `
    <tr>
      <td><span class="lot-title" title="${it.title || ""}">${it.title || it.item_id}</span></td>
      <td><span class="game-tag">${it.game || "—"}</span></td>
      <td class="num">${fmt(it.buy_total || it.price)} ₽</td>
      <td class="num profit-pos">${it.net_profit != null ? "+" + fmt(it.net_profit) + " ₽" : "—"}</td>
      <td class="ab-reason">${it.reason || "—"}</td>
      <td>${it.url ? `<a class="neon-link" href="${it.url}" target="_blank">лот ↗</a> ` : ""}<button class="chip-btn danger" data-cancel="${it.item_id}">✕</button></td>
    </tr>`).join("") : `<tr><td colspan="6" style="text-align:center;color:var(--muted)">Очередь пуста</td></tr>`;

  q("abQueueBody").querySelectorAll("[data-cancel]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await postJSON("/api/autobuy/queue/cancel", { item_id: btn.dataset.cancel });
      renderAutobuy();
    });
  });
  q("abProcessBtn").onclick = async () => { await postJSON("/api/autobuy/queue/process"); renderAutobuy(); };

  q("abHistoryCount").textContent = history.length;
  q("abHistoryBody").innerHTML = history.length ? history.map((h) => `
    <tr>
      <td>${h.at || "—"}</td>
      <td><span class="ab-badge ab-${h.status}">${AB_STATUS_LABEL[h.status] || h.status}</span></td>
      <td><span class="lot-title" title="${h.title || ""}">${h.title || h.item_id}</span></td>
      <td class="num">${fmt(h.price)} ₽</td>
      <td class="num profit-pos">${h.net_profit != null ? "+" + fmt(h.net_profit) + " ₽" : "—"}</td>
      <td>${h.pay_url ? `<a class="neon-link" href="${h.pay_url}" target="_blank">оплатить ↗</a>` : (h.url ? `<a class="neon-link" href="${h.url}" target="_blank">лот ↗</a>` : (h.error || "—"))}</td>
    </tr>`).join("") : `<tr><td colspan="6" style="text-align:center;color:var(--muted)">Пока пусто — включи автопокупку и дождись скана</td></tr>`;
}

function bindAutobuyOnce() {
  if (q("autobuyFilters").dataset.bound) return;
  q("autobuyFilters").dataset.bound = "1";
  q("autobuyFilters").addEventListener("input", (e) => {
    const el = e.target;
    if (!el.dataset.key || el.type !== "range") return;
    const suf = el.id.includes("Risk") ? "" : " ₽";
    const valEl = q(el.id + "Val");
    if (valEl) valEl.textContent = fmt(el.value) + suf;
  });
}

// ---------- Brain / self-learning ----------
function kpiCard(label, value, sub, accent) {
  return `<div class="kpi-card glass"><div class="kpi-accent" style="background:${accent}"></div>
    <div class="kpi-label">${label}</div><div class="kpi-value">${value}</div>
    <div class="kpi-sub">${sub || ""}</div></div>`;
}

async function renderBrain() {
  const b = await fetchJSON("/api/brain/state");
  const perf = b.performance || {};
  const real = b.real_trades || {};
  const fc = b.forecast || {};
  const plugs = b.plugins || {};

  q("brainKpi").innerHTML = [
    kpiCard("Наблюдений", fmt(b.samples), `сканов: ${b.scans_seen || 0}`, NEON.cyan),
    kpiCard("Реальный PnL", real.total_realized_pnl != null ? fmt(real.total_realized_pnl) + " ₽" : "—", `закрыто: ${(real.sold || 0) + (real.lost || 0)}`, NEON.green),
    kpiCard("Ср. реальная прибыль", perf.avg_realized_profit != null ? fmt(perf.avg_realized_profit) + " ₽" : "—", "на сделку (sim+real)", NEON.magenta),
    kpiCard("Прогноз открытых", fc.forecast_pnl != null ? fmt(fc.forecast_pnl) + " ₽" : "—", `${fc.count || 0} позиций`, NEON.yellow),
    kpiCard("Эффект отбора", perf.policy_uplift != null ? (perf.policy_uplift >= 0 ? "+" : "") + fmt(perf.policy_uplift) + " ₽" : "—", "ИИ vs все сделки", perf.policy_uplift >= 0 ? NEON.green : NEON.red),
  ].join("");

  const curve = perf.curve || [];
  destroy("brainCurve");
  charts.brainCurve = new Chart(q("chartBrainCurve"), {
    type: "line",
    data: {
      labels: curve.map((c) => "#" + c.scan),
      datasets: [
        { label: "Отобрано ИИ", data: curve.map((c) => c.policy), borderColor: NEON.green,
          backgroundColor: "rgba(0,255,159,.12)", fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2.5, spanGaps: true },
        { label: "Все сделки (базовая)", data: curve.map((c) => c.baseline), borderColor: NEON.muted,
          borderDash: [5, 4], fill: false, tension: 0.35, pointRadius: 0, borderWidth: 1.5 },
      ],
    },
    options: {
      plugins: { legend: { display: true, labels: { boxWidth: 12, font: { size: 11 } } } },
      scales: { y: { beginAtZero: false } },
    },
  });

  const t = b.tuning || {};
  q("brainTuning").innerHTML = Object.keys(t).length ? [
    ["Мин. маржа", (t.min_margin_percent ?? "—") + "%", "фильтр сделок"],
    ["Потолок риска", t.max_risk_score ?? "—", "portfolio"],
    ["Перцентиль продажи", t.optimal_sell_percentile ?? "—", "цена продажи"],
    ["Доля продаж", t.sold_rate != null ? Math.round(t.sold_rate * 100) + "%" : "—", "основа настройки"],
  ].map(([k, v, s]) => `<div class="tune-row"><span>${k}</span><b>${v}</b><i>${s}</i></div>`).join("")
    : `<div class="empty-hint">Недостаточно данных. Нажми «Прокачать на данных».</div>`;

  const active = plugs.active || [];
  const drafts = plugs.drafts || [];
  q("brainPluginCount").textContent = active.length;
  const pluginRows = [
    ...active.map((p) => ({ ...p, _kind: "active" })),
    ...drafts.filter((d) => !active.some((a) => a.id === d.id)).map((p) => ({ ...p, _kind: "draft" })),
  ];
  q("brainPlugins").innerHTML = pluginRows.length ? pluginRows.map((p) => {
    const bt = p.backtest || {};
    return `<div class="insight ${p._kind === "active" ? "good" : "trick"}">
      <span class="ins-dot"></span>
      <div>
        <div class="ins-title">${p.id} <span class="trick-impact">${p._kind === "active" ? "ACTIVE" : (p.status || "draft")}</span></div>
        <div class="ins-text">ворота: маржа≥${p.gates?.margin_min ?? "—"} · риск≤${p.gates?.risk_max ?? "—"} · дни≤${p.gates?.days_max ?? "—"}
          · uplift ${bt.uplift != null ? (bt.uplift >= 0 ? "+" : "") + bt.uplift + "₽" : "—"}
          · win ${bt.winrate != null ? Math.round(bt.winrate * 100) + "%" : "—"}
          · n=${bt.support ?? "—"}</div>
        <div class="offer-actions" style="margin-top:8px">
          ${p._kind === "draft" ? `<button class="btn-mini primary" data-act-plugin="${p.id}">Активировать</button>` : `<button class="btn-mini" data-deact-plugin="${p.id}">Отключить</button>`}
        </div>
      </div>
    </div>`;
  }).join("") : `<div class="empty-hint">Пока нет блоков — нажми «Самоулучшение» или «Эволюция» (нужны данные / журнал).</div>`;

  q("brainPlugins").querySelectorAll("[data-act-plugin]").forEach((btn) => {
    btn.onclick = async () => { await postJSON("/api/brain/plugins/activate", { id: btn.dataset.actPlugin }); renderBrain(); };
  });
  q("brainPlugins").querySelectorAll("[data-deact-plugin]").forEach((btn) => {
    btn.onclick = async () => { await postJSON("/api/brain/plugins/deactivate", { id: btn.dataset.deactPlugin }); renderBrain(); };
  });

  const strat = b.discovered_strategies || [];
  q("brainStratCount").textContent = strat.length;
  q("brainStratBody").innerHTML = strat.length ? strat.map((s) => `
    <tr>
      <td><span class="stag">${s.id}</span>${s.generation > 1 ? ` <span class="chip">gen ${s.generation}</span>` : ""}</td>
      <td class="num">${s.support}</td>
      <td class="num ${s.avg_realized_profit >= 0 ? "profit-pos" : "ev-neg"}">${fmt(s.avg_realized_profit)} ₽</td>
      <td class="num">${Math.round(s.winrate * 100)}%</td>
      <td class="num">${Math.round(s.confidence * 100)}%</td>
      <td class="num">${s.score}</td>
    </tr>`).join("") : `<tr><td colspan="6" style="text-align:center;color:var(--muted)">Пока нет — отмечай Купил/Продал в журнале и запускай эволюцию</td></tr>`;

  q("brainSkills").innerHTML = (b.skills || []).length ? (b.skills || []).map((s) => `
    <div class="insight ${s.positive ? "good" : "bad"}">
      <span class="ins-dot"></span>
      <div><div class="ins-text">${s.text}</div><div class="ins-meta">уверенность ${Math.round((s.confidence || 0) * 100)}%</div></div>
    </div>`).join("") : `<div class="empty-hint">Навыки появятся после обучения.</div>`;

  q("brainTricks").innerHTML = (b.tricks || []).length ? (b.tricks || []).map((t) => `
    <div class="insight trick">
      <span class="ins-dot"></span>
      <div><div class="ins-title">${t.title} <span class="trick-impact">${t.impact || ""}</span></div>
      <div class="ins-text">${t.text}</div></div>
    </div>`).join("") : `<div class="empty-hint">Приёмы появятся после обучения.</div>`;
}

async function trainBrain() {
  const btn = q("brainTrainBtn");
  btn.disabled = true; btn.textContent = "⏳ учусь...";
  try { await fetch("/api/brain/train?rounds=8", { method: "POST" }); await renderBrain(); }
  finally { btn.disabled = false; btn.textContent = "⚡ Прокачать на данных"; }
}
async function evolveBrain() {
  const btn = q("brainEvolveBtn");
  btn.disabled = true; btn.textContent = "⏳...";
  try { await fetch("/api/brain/evolve", { method: "POST" }); await renderBrain(); }
  finally { btn.disabled = false; btn.textContent = "🧬 Эволюция"; }
}
async function improveBrain() {
  const btn = q("brainImproveBtn");
  btn.disabled = true; btn.textContent = "⏳...";
  try { await postJSON("/api/brain/improve", {}); await renderBrain(); }
  finally { btn.disabled = false; btn.textContent = "🛠 Самоулучшение"; }
}

// ---------- Journal ----------
async function renderJournal() {
  const data = await fetchJSON("/api/journal", { limit: 200 });
  const sum = data.summary || {};
  const fc = data.forecast || {};
  const forecastMap = {};
  (fc.items || []).forEach((it) => { forecastMap[it.id] = it; });

  q("journalKpi").innerHTML = [
    kpiCard("Открыто", fmt(sum.open), `капитал ${fmt(sum.capital_open)} ₽`, NEON.cyan),
    kpiCard("Реальный PnL", fmt(sum.total_realized_pnl) + " ₽", `win ${Math.round((sum.winrate || 0) * 100)}%`, sum.total_realized_pnl >= 0 ? NEON.green : NEON.red),
    kpiCard("Прогноз ИИ", fmt(fc.forecast_pnl) + " ₽", `по ${fc.count || 0} открытым`, NEON.magenta),
    kpiCard("ROI закрытых", (sum.roi_closed_percent ?? 0) + "%", `продано ${sum.sold || 0} · списано ${sum.lost || 0}`, NEON.yellow),
  ].join("");
  q("journalForecastChip").textContent = `прогноз ${fmt(fc.forecast_pnl)} ₽`;

  const opens = (data.trades || []).filter((t) => t.status === "open");
  const closed = (data.trades || []).filter((t) => t.status !== "open");
  q("journalOpenCount").textContent = opens.length;
  q("journalClosedCount").textContent = closed.length;

  q("journalOpenBody").innerHTML = opens.length ? opens.map((t) => {
    const f = forecastMap[t.id] || {};
    return `<tr>
      <td><span class="lot-title" title="${t.title}">${t.title}</span></td>
      <td><span class="game-tag">${t.game}</span></td>
      <td class="num">${fmt(t.buy_total)} ₽</td>
      <td class="num profit-pos">+${fmt(t.net_profit_est)} ₽</td>
      <td class="num">${f.forecast != null ? fmt(f.forecast) + " ₽" : "—"}</td>
      <td>${(t.bought_at || "").replace("T", " ").slice(0, 16)}</td>
      <td>
        <button class="chip-btn" data-sell="${t.id}">Продал</button>
        <button class="chip-btn danger" data-lost="${t.id}">Списать</button>
      </td>
    </tr>`;
  }).join("") : `<tr><td colspan="7" style="text-align:center;color:var(--muted)">Нет открытых — отметь «Купил» во вкладке Сделки</td></tr>`;

  q("journalOpenBody").querySelectorAll("[data-sell]").forEach((btn) => {
    btn.onclick = async () => {
      const price = prompt("Цена продажи, ₽:");
      if (price == null || !Number(price)) return;
      const fees = prompt("Комиссии продажи, ₽ (Enter = 0):", "0");
      const nick = prompt("Ник покупателя (необязательно):", "") || "";
      const res = await postJSON("/api/journal/sell", {
        id: btn.dataset.sell, sell_price: Number(price), sell_fees: Number(fees) || 0, buyer_nick: nick,
      });
      if (!res.ok) alert(res.error || "Ошибка");
      renderJournal();
    };
  });
  q("journalOpenBody").querySelectorAll("[data-lost]").forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm("Списать позицию? ИИ учтёт убыток при обучении.")) return;
      await postJSON("/api/journal/lost", { id: btn.dataset.lost });
      renderJournal();
    };
  });

  q("journalClosedBody").innerHTML = closed.length ? closed.map((t) => {
    const pnl = t.realized_profit || 0;
    return `<tr>
      <td><span class="ab-badge ab-${t.status}">${t.status === "sold" ? "✅ sold" : "❄ lost"}</span></td>
      <td><span class="lot-title" title="${t.title}">${t.title}</span></td>
      <td class="num">${fmt(t.buy_total)} ₽</td>
      <td class="num">${t.sell_price != null ? fmt(t.sell_price) + " ₽" : "—"}</td>
      <td class="num ${pnl >= 0 ? "profit-pos" : "ev-neg"}">${pnl >= 0 ? "+" : ""}${fmt(pnl)} ₽</td>
      <td class="num">${t.roi_percent != null ? t.roi_percent + "%" : "—"}</td>
      <td>${(t.sold_at || "").replace("T", " ").slice(0, 16)}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="7" style="text-align:center;color:var(--muted)">Закрытых пока нет</td></tr>`;
}

async function manualJournalBuy() {
  const title = prompt("Название лота:");
  if (!title) return;
  const buy_price = Number(prompt("Цена покупки, ₽:") || 0);
  if (!buy_price) return;
  const game = prompt("Игра:", "unknown") || "unknown";
  const sell_platform = prompt("Площадка продажи:", "funpay") || "funpay";
  const net = Number(prompt("Ожидаемая прибыль, ₽ (необяз.):", "0") || 0);
  await postJSON("/api/journal/buy", {
    title, buy_price, buy_total: buy_price, game, sell_platform,
    buy_platform: "lolz", net_profit: net, source: "manual",
  });
  renderJournal();
}

// ---------- Clients ----------
const SEGMENT_LABEL = { vip: "👑 VIP", loyal: "💎 Лояльный", returning: "🔁 Вернувшийся", new: "Новый" };
async function renderClients() {
  const data = await fetchJSON("/api/clients/offers");
  const offers = data.offers || [];
  const clients = data.clients || [];

  q("clientsKpi").innerHTML = [
    kpiCard("Клиентов", fmt(data.total_clients || 0), "покупали хоть раз", NEON.cyan),
    kpiCard("Активных", fmt(data.active_clients || 0), "повторные покупатели", NEON.green),
    kpiCard("Предложений", fmt(offers.length), "черновиков со скидкой", NEON.magenta),
    kpiCard("Авто-рассылка", data.auto_send ? "ВКЛ" : "ВЫКЛ", `лимит ${data.daily_limit}/день`, data.auto_send ? NEON.orange : NEON.muted),
  ].join("");

  q("offersMode").textContent = data.auto_send ? "авто-рассылка (лимит)" : "черновики";
  q("offersCount").textContent = offers.length;

  if (offers.length) {
    q("offersList").innerHTML = offers.map((o, i) => `
    <div class="offer-card glass">
      <div class="offer-head">
        <div><b>${o.client}</b> <span class="stag">${SEGMENT_LABEL[o.segment] || o.segment}</span> <span class="buys-tag">${o.purchases}× покупок</span></div>
        <div class="offer-disc">−${o.discount_percent}%</div>
      </div>
      <div class="offer-lot">${o.game ? o.game + " · " : ""}${o.lot}</div>
      <div class="offer-prices">
        <span class="old-price">${fmt(o.base_price)} ₽</span>
        <span class="new-price">${fmt(o.offer_price)} ₽</span>
        <span class="offer-profit">прибыль +${fmt(o.profit_after)} ₽</span>
      </div>
      <div class="offer-msg" id="offerMsg${i}">${o.message}</div>
      <div class="offer-actions">
        <button class="btn-mini" onclick="copyOffer(${i})">Скопировать текст</button>
        ${o.sell_url ? `<a class="btn-mini" href="${o.sell_url}" target="_blank">лот ↗</a>` : ""}
        <button class="btn-mini primary" onclick='sendOffer(${JSON.stringify(o).replace(/'/g, "&#39;")})'>${data.auto_send ? "Отправить" : "Сохранить черновик"}</button>
      </div>
    </div>`).join("");
  } else {
    q("offersList").innerHTML = `<div class="empty-hint">${data.empty_reason || "Нет активных клиентов под текущие лоты."}</div>`;
  }

  q("clientsBody").innerHTML = clients.length ? clients.map((c) => `
    <tr>
      <td><b>${c.nick}</b></td>
      <td><span class="stag">${SEGMENT_LABEL[c.segment] || c.segment}</span></td>
      <td class="num">${c.purchases}</td>
      <td>${(c.fav_games || []).map((g) => `<span class="game-tag">${g}</span>`).join(" ") || "—"}</td>
      <td class="num">${fmt(c.avg_price)} ₽</td>
      <td class="num">${fmt(c.total_spent)} ₽</td>
      <td class="num">${Math.round((c.value || 0) * 100)}</td>
      <td class="num">${c.last_seen_days > 3000 ? "—" : c.last_seen_days + "д назад"}</td>
    </tr>`).join("") : `<tr><td colspan="8" style="text-align:center;color:var(--muted)">Клиентов пока нет — запишите первую продажу выше</td></tr>`;
}
function copyOffer(i) {
  const el = q("offerMsg" + i);
  if (el) navigator.clipboard?.writeText(el.textContent);
}
async function sendOffer(offer) {
  const res = await fetch("/api/clients/offer/send", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(offer),
  });
  const j = await res.json();
  const map = { draft_saved: "Черновик сохранён", sent: "Отправлено", queued: "В очереди", rate_limited: "Достигнут дневной лимит" };
  alert(map[j.status] || j.status);
}
async function addSale() {
  const nick = q("saleNick").value.trim();
  if (!nick) { q("saleMsg").textContent = "Укажите ник покупателя"; return; }
  const res = await fetch("/api/clients/sale", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      nick,
      game: q("saleGame").value.trim(),
      price: Number(q("salePrice").value) || 0,
      platform: q("salePlatform").value.trim(),
      profit: Number(q("saleProfit").value) || undefined,
    }),
  });
  const j = await res.json();
  if (j.ok) {
    q("saleMsg").textContent = `✓ Продажа записана: ${nick} (покупок: ${j.client.purchases})`;
    q("saleNick").value = ""; q("saleGame").value = ""; q("salePrice").value = "";
    q("salePlatform").value = "funpay"; q("saleProfit").value = "";
    renderClients();
  } else {
    q("saleMsg").textContent = j.error || "Ошибка";
  }
}
async function importClients() {
  const csv = prompt("Вставьте CSV (заголовки: nick,game,price,purchases):\nПример:\nnick,game,price,purchases\nProGamer,steam,1500,3");
  if (!csv) return;
  const res = await fetch("/api/clients/import", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ csv }),
  });
  const j = await res.json();
  q("saleMsg").textContent = j.ok ? `✓ Импортировано клиентов: ${j.imported} (всего ${j.total})` : (j.error || "Ошибка импорта");
  if (j.ok) renderClients();
}

// ---------- Tab rendering ----------
function renderTab(name) {
  const s = state.stats;
  if (name === "advisor") { renderAdvisor(); return; }
  if (name === "brain") { renderBrain(); return; }
  if (name === "journal") { renderJournal(); return; }
  if (name === "clients") { renderClients(); return; }
  if (name === "autobuy") { renderAutobuy(); return; }
  if (!s) return;
  if (name === "overview") { chartByGame(s); chartConfidence(s); chartLiquidity(s); chartProfitDist(s); chartScatter(s); }
  else if (name === "risk") { chartRiskReward(s); chartRiskDist(s); chartFees(s); renderRiskMatrix(s); }
  else if (name === "market") { renderMarket(); }
  else if (name === "strategies") { renderStrategies(); }
  else if (name === "portfolio") { renderPortfolio(); }
  else if (name === "deals") { loadTableOnly(); }
}

function updateScanBadge(st) {
  const badge = q("modeBadge");
  const statusEl = q("scanStatus");
  if (!st) return;

  state._scanSource = st.source || "offline";
  badge.textContent = st.source === "live" ? "LIVE" : "OFFLINE";
  badge.classList.toggle("live", st.source === "live");
  badge.classList.toggle("offline", st.source !== "live");

  if (st.scan_in_progress) {
    statusEl.textContent = "⏳ сканирую площадки...";
    statusEl.classList.add("scanning");
    return;
  }
  statusEl.classList.remove("scanning");

  const last = st.last_scan_at ? new Date(st.last_scan_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }) : "—";
  const next = st.next_scan_at ? new Date(st.next_scan_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }) : "—";
  statusEl.textContent = `${st.last_count} сделок · обновлено ${last} · след. ${next}`;
}

async function pollScanStatus() {
  try {
    const st = await fetchJSON("/api/scan/status");
    updateScanBadge(st);
    if (st.scan_version > state.scanVersion) {
      state.scanVersion = st.scan_version;
      await loadAll(false);
    }
  } catch (_) { /* сервер недоступен — тихо */ }
}

function schedulePoll() {
  if (pollTimer) clearInterval(pollTimer);
  const ms = document.hidden ? POLL_HIDDEN_MS : POLL_ACTIVE_MS;
  pollTimer = setInterval(pollScanStatus, ms);
}

function startAutoPoll() {
  pollScanStatus();
  schedulePoll();
  document.addEventListener("visibilitychange", schedulePoll);
}

async function loadAll(refresh = false) {
  if (refresh) await fetch("/api/refresh", { method: "GET" });
  const [stats, st] = await Promise.all([
    fetchJSON("/api/stats"),
    fetchJSON("/api/scan/status"),
  ]);
  state.stats = stats;
  state.scanVersion = st.scan_version || 0;
  updateScanBadge(st);
  renderKPI(state.stats);
  renderTab(state.activeTab);
}

async function initGames() {
  const games = await fetchJSON("/api/games");
  const sel = q("filterGame");
  games.forEach((g) => { const o = document.createElement("option"); o.value = g; o.textContent = g; sel.appendChild(o); });
}

function switchTab(name) {
  state.activeTab = name;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === name));
  renderTab(name);
}

function bindEvents() {
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  ["filterGame", "filterConfidence", "filterRisk", "filterLiquidity", "filterStrategy", "filterPlatform", "filterSort"]
    .forEach((id) => q(id).addEventListener("change", loadTableOnly));
  q("filterMinProfit").addEventListener("input", (e) => q("minProfitVal").textContent = e.target.value + "₽");
  q("filterMinProfit").addEventListener("change", loadTableOnly);
  q("filterMaxRisk").addEventListener("input", (e) => q("maxRiskVal").textContent = e.target.value);
  q("filterMaxRisk").addEventListener("change", loadTableOnly);
  q("filterMaxDays").addEventListener("input", (e) => q("maxDaysVal").textContent = e.target.value === "0" ? "∞" : e.target.value + " дн");
  q("filterMaxDays").addEventListener("change", loadTableOnly);
  q("budgetInput").addEventListener("input", (e) => q("budgetVal").textContent = fmt(e.target.value) + "₽");
  q("budgetInput").addEventListener("change", renderPortfolio);
  q("rebuildBtn").addEventListener("click", renderPortfolio);
  q("advBudget").addEventListener("input", (e) => q("advBudgetVal").textContent = fmt(e.target.value) + "₽");
  q("advBudget").addEventListener("change", renderAdvisor);
  q("brainTrainBtn").addEventListener("click", trainBrain);
  q("brainEvolveBtn").addEventListener("click", evolveBrain);
  q("brainImproveBtn").addEventListener("click", improveBrain);
  q("journalRefreshBtn").addEventListener("click", renderJournal);
  q("journalManualBuyBtn").addEventListener("click", manualJournalBuy);
  q("clientsRefreshBtn").addEventListener("click", renderClients);
  q("saleAddBtn").addEventListener("click", addSale);
  q("clientsImportBtn").addEventListener("click", importClients);
  q("refreshBtn").addEventListener("click", () => loadAll(true));
}

function startClock() {
  const tick = () => q("clock").textContent = new Date().toLocaleTimeString("ru-RU");
  tick(); setInterval(tick, 1000);
}

function unregisterSW() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.getRegistrations().then((regs) => regs.forEach((r) => r.unregister()));
  }
}

(async function main() {
  unregisterSW();
  startClock();
  bindEvents();
  bindAutobuyOnce();
  await initGames();
  await loadAll();
  startAutoPoll();
})();

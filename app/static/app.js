/* ============================================================
   Market Insight AI — Frontend Opções Binárias (MTF)
============================================================ */

let chart = null;
let candleSeries = null;
let ema10Series = null;
let ema20Series = null;
let bbUpperSeries = null;
let bbLowerSeries = null;

let currentAsset = "EURUSD";
let currentInterval = 300;   // timeframe visual do gráfico (5min default)
let currentExpiry = "1min";
let selectedStrategy = "trend_pullback";
let strategyCatalog = {};
let analysisConfirmed = false;
let refreshTimer = null;
let signalTimer = null;
let opportunitiesTimer = null;
let analysisRefreshInFlight = false;
const handledExpiries = new Set();
const expiryTimeouts = {};
const signalExpiries = {};
const expirySeconds = { "1min": 60, "5min": 300, "15min": 900 };
const radarPairs = new Map();
let radarSortExpiry = "1min";
let radarRunId = 0;

const $ = (id) => document.getElementById(id);

/* ============================================================
   Navegação por abas
============================================================ */
function switchView(view) {
  document.querySelectorAll(".view-tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.view === view);
  });
  $("view-trade").classList.toggle("hidden", view !== "trade");
  $("view-radar").classList.toggle("hidden", view !== "radar");
  $("view-opportunities").classList.toggle("hidden", view !== "opportunities");
  $("view-news").classList.toggle("hidden", view !== "news");
  $("view-entrada").classList.toggle("hidden", view !== "entrada");
  $("view-config").classList.toggle("hidden", view !== "config");
  $("view-relatorio").classList.toggle("hidden", view !== "relatorio");
  $("view-strategies").classList.toggle("hidden", view !== "strategies");
  if (view === "radar" && !$("radar-output").dataset.loaded) runRadar();
  if (view === "news") loadNews();
  if (view === "entrada") {
    loadTradeConfig();
    loadEntries();
  }
  if (view === "config") {
    loadTradeConfig();
    loadAiConfig();
  }
  if (view === "relatorio") loadReport();
  if (view === "strategies") loadStrategiesView();
  window.scrollTo({ top: 0, behavior: "smooth" });
}
$("gate-strategy-select").addEventListener("change", (event) => {
  selectedStrategy = event.target.value;
  renderGateDescription();
});

document.querySelectorAll(".view-tab").forEach((tab) => {
  tab.addEventListener("click", () => switchView(tab.dataset.view));
});

$("confirm-strategy").addEventListener("click", confirmStrategy);
$("login-button").addEventListener("click", () => $("login-modal").classList.remove("hidden"));
$("login-close").addEventListener("click", () => $("login-modal").classList.add("hidden"));
$("login-form").addEventListener("submit", loginAccount);
$("asset-search").addEventListener("input", (event) => renderAssets(event.target.value));
$("radar-strategy-select").addEventListener("change", (event) => {
  selectedStrategy = event.target.value;
  runRadar();
});
document.querySelectorAll(".radar-sort").forEach((button) => {
  button.addEventListener("click", () => {
    radarSortExpiry = button.dataset.radarSort;
    renderRadar({ pairs: Array.from(radarPairs.values()) });
  });
});
$("news-importance").addEventListener("change", loadNews);
$("btn-news-refresh").addEventListener("click", loadNews);
$("change-strategy").addEventListener("click", () => {
  analysisConfirmed = false;
  $("strategy-gate").classList.remove("hidden");
  $("main-navigation").classList.add("hidden");
  $("view-opportunities").classList.add("hidden");
  $("view-trade").classList.add("hidden");
});
/* ============================================================
  Seletor de timeframe do gráfico
============================================================ */
document.querySelectorAll(".tf-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const newInterval = parseInt(btn.dataset.interval);
    if (newInterval === currentInterval) return;

    document.querySelectorAll(".tf-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");

    currentInterval = newInterval;
    const labelMap = { 60: "1min", 300: "5min", 900: "15min" };
    $("chart-meta").textContent = `timeframe: ${labelMap[currentInterval] || currentInterval}`;

    loadChart();
  });
});

/* ============================================================
   Boot
============================================================ */
async function boot() {
  await ensureConnected();
  await loadAssets();
  await loadStrategies();
  await checkStatus();
  initChart();
  await loadChart();
  await loadBalance();

  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(loadChart, 3000);
  setInterval(loadBalance, 20000);
  setInterval(ensureConnected, 60000);
  loadTradeConfig(); // conta configurada no label + estado

  // Relogio sempre ativo e independente da analise, para nunca congelar.
  if (signalTimer) clearInterval(signalTimer);
  signalTimer = setInterval(updateTimers, 250);
  document.addEventListener("visibilitychange", updateTimers);
}

async function loadStrategies() {
  try {
    const response = await fetch("/api/strategies");
    const data = await response.json();
    strategyCatalog = data.strategies || {};
    const select = $("gate-strategy-select");
    select.innerHTML = Object.entries(strategyCatalog).map(([key, item]) =>
      `<option value="${key}">${escapeHtml(item.name)}</option>`
    ).join("");
    select.value = selectedStrategy;
    const radarSelect = $("radar-strategy-select");
    radarSelect.innerHTML = select.innerHTML;
    radarSelect.value = selectedStrategy;
    renderGateDescription();
  } catch (error) {
    $("gate-strategy-description").textContent = "Não foi possível carregar as estratégias.";
  }
}

function renderGateDescription() {
  const selected = strategyCatalog[selectedStrategy];
  $("gate-strategy-description").textContent = selected ? selected.description : "";
}

async function confirmStrategy() {
  selectedStrategy = $("gate-strategy-select").value;
  analysisConfirmed = true;
  $("strategy-gate").classList.add("hidden");
  $("main-navigation").classList.remove("hidden");
  switchView("opportunities");
  await loadOpportunities();
  if (opportunitiesTimer) clearInterval(opportunitiesTimer);
  opportunitiesTimer = setInterval(() => {
    if (!$("view-opportunities").classList.contains("hidden")) loadOpportunities();
  }, 60000);
}

async function loadOpportunities() {
  const status = $("opportunities-status");
  const grid = $("opportunities-grid");
  status.className = "status loading";
  status.textContent = "Analisando os pares com a estratégia selecionada…";
  grid.innerHTML = "";
  try {
    const response = await fetch(`/api/opportunities?strategy=${encodeURIComponent(selectedStrategy)}`);
    if (!response.ok) throw new Error("Falha ao rastrear os pares");
    const data = await response.json();
    $("opportunities-title").textContent = `Melhores pares: ${data.strategy_name}`;
    $("opportunities-description").textContent = strategyCatalog[selectedStrategy]?.description || "";
    renderOpportunities(data.pairs || []);
    status.className = "status";
    status.textContent = `${data.pairs.length} pares analisados. Clique em um par para abrir o detalhe.`;
  } catch (error) {
    status.className = "status error";
    status.textContent = error.message;
  }
}

function renderOpportunities(pairs) {
  const grid = $("opportunities-grid");
  pairs.forEach((pair, index) => {
    const card = document.createElement("article");
    card.className = "opportunity-card";
    card.innerHTML = `
      <div class="opportunity-rank">#${index + 1}</div>
      <div class="opportunity-pair">${escapeHtml(pair.asset)}</div>
      <div class="opportunity-score"><span>Confluência técnica</span><strong>${pair.score} de ${pair.max_score || 12} critérios</strong></div>
      <div class="opportunity-signals">
        <span>${signalBadge(pair.signals?.["1min"], "1m", pair.proximity?.["1min"])}</span>
        <span>${signalBadge(pair.signals?.["5min"], "5m", pair.proximity?.["5min"])}</span>
        <span>${signalBadge(pair.signals?.["15min"], "15m", pair.proximity?.["15min"])}</span>
      </div>
      <p>${escapeHtml(pair.reason || "Sem confirmação suficiente.")}</p>
      <button class="btn-secondary">Abrir análise</button>
    `;
    card.querySelector("button").addEventListener("click", () => {
      selectAsset(pair.asset);
      switchView("trade");
      loadChart();
      loadAnalysis(true);
    });
    grid.appendChild(card);
  });
}

function signalBadge(signal, label, proximity) {
  const safe = signal || "AGUARDAR";
  const cls = safe === "CALL" ? "call" : safe === "PUT" ? "put" : "neutral";
  const displayStatus = safe === "CALL" || safe === "PUT" ? safe : (proximity?.label || "AGUARDAR");
  return `<b class="mini-signal ${cls}">${label} ${escapeHtml(displayStatus)}</b>`;
}

async function ensureConnected() {
  try {
    const r = await fetch("/api/connect");
    const d = await r.json();
    const el = $("conn-status");
    if (d.ok) {
      el.classList.remove("err");
      el.classList.add("ok");
      $("conn-text").textContent = "Conectado à IQ Option";
    } else {
      el.classList.remove("ok");
      el.classList.add("err");
      $("conn-text").textContent = d.message || "Falha na conexão";
    }
  } catch {
    $("conn-status").classList.remove("ok");
    $("conn-status").classList.add("err");
    $("conn-text").textContent = "Erro de conexão";
  }
}

async function loginAccount(event) {
  event.preventDefault();
  const status = $("login-status");
  status.textContent = "Conectando…";
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: $("login-email").value, password: $("login-password").value }),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || "Falha no login");
    $("login-password").value = "";
    $("login-modal").classList.add("hidden");
    await checkStatus();
    await loadBalance();
    await loadAnalysis();
    status.textContent = "";
  } catch (error) {
    status.textContent = error.message;
  }
}

async function checkStatus() {
  try {
    const r = await fetch("/api/status");
    const d = await r.json();
    const el = $("conn-status");
    if (d.connected) {
      el.classList.remove("err");
      el.classList.add("ok");
    }
  } catch {}
}

async function loadBalance() {
  try {
    const r = await fetch("/api/status");
    const d = await r.json();
    if (d.balance != null) {
      $("balance-value").textContent = `$ ${d.balance.toFixed(2)}`;
    }
  } catch {}
}

/* ============================================================
   Lista de ativos
============================================================ */
async function loadAssets() {
  try {
    const r = await fetch("/api/assets");
    const d = await r.json();
    window.availableAssets = d.assets || [];
    renderAssets();
    // Sugestões da aba Entradas
    const datalist = $("entry-asset-list");
    datalist.innerHTML = (window.availableAssets || [])
      .map((asset) => `<option value="${escapeHtml(asset)}"></option>`).join("");
  } catch {}
}

function renderAssets(filter = "") {
  const list = $("asset-list");
  const query = filter.trim().toUpperCase();
  list.innerHTML = "";
  (window.availableAssets || []).filter((asset) => asset.includes(query)).forEach((asset) => {
    const btn = document.createElement("button");
    btn.className = "asset-btn" + (asset === currentAsset ? " active" : "");
    btn.textContent = asset;
    btn.addEventListener("click", () => selectAsset(asset));
    list.appendChild(btn);
  });
}

function selectAsset(asset) {
  currentAsset = asset;
  document.querySelectorAll(".asset-btn").forEach((b) => {
    b.classList.toggle("active", b.textContent === asset);
  });
  $("chart-pair").textContent = asset;
  loadChart();
  loadAnalysis();
}

/* ============================================================
   Gráfico
============================================================ */
function initChart() {
  const container = $("chart");
  container.innerHTML = "";

  chart = LightweightCharts.createChart(container, {
    layout: {
      background: { color: "#0b1118" },
      textColor: "#8497b0",
      fontSize: 11,
    },
    grid: {
      vertLines: { color: "rgba(35,50,68,0.4)" },
      horzLines: { color: "rgba(35,50,68,0.4)" },
    },
    rightPriceScale: { borderColor: "#233244" },
    timeScale: {
      borderColor: "#233244",
      timeVisible: true,
      secondsVisible: false,
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: "#4a9eff", width: 1, style: 2, labelBackgroundColor: "#4a9eff" },
      horzLine: { color: "#4a9eff", width: 1, style: 2, labelBackgroundColor: "#4a9eff" },
    },
  });

  candleSeries = chart.addCandlestickSeries({
    upColor: "#26de81",
    downColor: "#ff5c5c",
    borderUpColor: "#26de81",
    borderDownColor: "#ff5c5c",
    wickUpColor: "#26de81",
    wickDownColor: "#ff5c5c",
  });

  ema10Series = chart.addLineSeries({
    color: "#f7b731", lineWidth: 1.5,
    priceLineVisible: false, lastValueVisible: false,
  });
  ema20Series = chart.addLineSeries({
    color: "#a55eea", lineWidth: 1.5,
    priceLineVisible: false, lastValueVisible: false,
  });
  bbUpperSeries = chart.addLineSeries({
    color: "rgba(132,151,176,0.5)", lineWidth: 1,
    lineStyle: 2, priceLineVisible: false, lastValueVisible: false,
  });
  bbLowerSeries = chart.addLineSeries({
    color: "rgba(132,151,176,0.5)", lineWidth: 1,
    lineStyle: 2, priceLineVisible: false, lastValueVisible: false,
  });

  chart.subscribeClick(() => loadAnalysis(true));

  const ro = new ResizeObserver(() => {
    chart.applyOptions({
      width: container.clientWidth,
      height: container.clientHeight,
    });
  });
  ro.observe(container);
}

async function loadChart() {
  try {
    const r = await fetch(`/api/candles/${currentAsset}?interval=${currentInterval}&count=200`);
    if (!r.ok) return;
    const d = await r.json();
    if (!d.candles || d.candles.length === 0) return;

    candleSeries.setData(d.candles.map((c) => ({
      time: c.time, open: c.open, high: c.high, low: c.low, close: c.close,
    })));
    if (d.ema10) ema10Series.setData(d.ema10.map((p) => ({ time: p.time, value: p.value })));
    if (d.ema20) ema20Series.setData(d.ema20.map((p) => ({ time: p.time, value: p.value })));
    if (d.bb_upper) bbUpperSeries.setData(d.bb_upper.map((p) => ({ time: p.time, value: p.value })));
    if (d.bb_lower) bbLowerSeries.setData(d.bb_lower.map((p) => ({ time: p.time, value: p.value })));

    const last = d.candles[d.candles.length - 1];
    const prev = d.candles[d.candles.length - 2] || last;
    const change = prev.close ? ((last.close - prev.close) / prev.close) * 100 : 0;

    $("chart-price").textContent = last.close.toFixed(5);
    const chgEl = $("chart-change");
    chgEl.textContent = `${change >= 0 ? "+" : ""}${change.toFixed(3)}%`;
    chgEl.className = `price-change ${change >= 0 ? "up" : "down"}`;
  } catch (e) {
    console.error("Erro ao carregar gráfico:", e);
  }
}

/* ============================================================
   Análise Multi-Timeframe
============================================================ */
async function loadAnalysis(highlight = false) {
  try {
    const r = await fetch(`/api/analyze/${currentAsset}?strategy=${encodeURIComponent(selectedStrategy)}`);
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      console.error("Erro /api/analyze:", err);
      $("analysis-note").textContent = `Erro ao atualizar a análise: ${err.detail || r.status}`;
      return;
    }
    const d = await r.json();

    // Diagnóstico: se a estratégia vier vazia ou em branco, avisa
    if (!d.strategy) {
      $("analysis-note").textContent = "O backend não retornou uma estratégia válida.";
      return;
    }

    window.latestAnalysis = d;
    renderAnalysis(d, highlight);
  } catch (e) {
    console.error("Erro análise:", e);
    $("analysis-note").textContent = `Erro ao atualizar a análise: ${e.message}`;
  }
}

function renderAnalysis(d, highlight = false) {
  $("active-strategy").textContent = `Estratégia: ${d.strategy_name}`;
  $("analysis-note").textContent = d.warning || (d.news && d.news.warning) || "O sinal só muda quando o prazo correspondente expirar.";
  Object.entries(d.signals || {}).forEach(([expiry, signal]) => renderSignal(expiry, signal));
  if (!signalTimer) signalTimer = setInterval(() => updateTimers(window.latestAnalysis), 1000);
  updateTimers(d);
  scheduleSignalRefresh(d);

  // Bloco de entrada rápida: aparece quando há sinal CALL/PUT em alguma expiração
  const comSinal = Object.entries(d.signals || {}).some(([, s]) => ["CALL", "PUT"].includes(s && s.signal));
  $("quick-entry-block").classList.toggle("hidden", !comSinal);
  if (comSinal) {
    const preSelecionado = ["1min", "5min", "15min"].find((exp) => ["CALL", "PUT"].includes(d.signals?.[exp]?.signal));
    if (preSelecionado) $("quick-expiry").value = preSelecionado.replace("min", "");
  }
}

/**
 * Formata segundos restantes em MM:SS.
 */
function formatCountdown(seconds) {
  const safe = Math.max(0, Math.floor(seconds || 0));
  const minutes = Math.floor(safe / 60).toString().padStart(2, "0");
  const secs = (safe % 60).toString().padStart(2, "0");
  return `${minutes}:${secs}`;
}

function renderSignal(expiry, signal) {
  const card = document.querySelector(`[data-expiry-card="${expiry}"]`);
  const direction = signal.signal || "AGUARDAR";
  const proximity = signal.proximity?.label || "AGUARDAR";
  const displayStatus =
    direction === "CALL" || direction === "PUT"
      ? direction
      : proximity === "ATENÇÃO"
        ? "ATENÇÃO"
        : proximity === "SINAL MUITO PRÓXIMO"
          ? "SINAL MUITO PRÓXIMO"
          : "ANALISANDO MERCADO";
  const statusClass = displayStatus === "ATENÇÃO" ? "attention" : displayStatus === "SINAL MUITO PRÓXIMO" ? "near" : "";
  card.className = `expiry-card ${direction === "CALL" ? "call" : direction === "PUT" ? "put" : "neutral"} ${statusClass}`.trim();
  $( `signal-${expiry}` ).textContent = displayStatus;

  // Percentual de acerto real (janela rolante) + últimos resultados
  const accuracy = signal.historical_accuracy;
  let accuracyHtml;
  let ultimosHtml = "";
  if (accuracy && accuracy.rate !== null) {
    accuracyHtml = `acerto: <b>${Number(accuracy.rate).toFixed(1)}%</b> (${accuracy.sample_size} sinais · ${accuracy.wins} certos)`;
    ultimosHtml = `<div class="acc-streak">${(accuracy.ultimos || []).map((r) =>
      `<span class="streak-dot ${r === "OK" ? "ok" : "erro"}" title="${r}">${r === "OK" ? "✔" : "✘"}</span>`
    ).join("")}</div>`;
  } else {
    accuracyHtml = `acerto: <span class="muted">${accuracy?.label || "sem amostra suficiente"}</span>`;
  }
  $( `reason-${expiry}` ).innerHTML =
    `${escapeHtml(signal.reason || "Sem confirmação suficiente.")} · ` + accuracyHtml + ultimosHtml;

  // Votos dos indicadores ativos
  const votesEl = $(`votes-${expiry}`);
  const resumo = signal.resumo_votos;
  const votos = signal.votos || [];
  if (resumo && votos.length) {
    votesEl.innerHTML = `
      <div class="vote-summary">
        <span class="vote-bull">▲ ${resumo.bulls} CALL</span>
        <span class="vote-bear">▼ ${resumo.bears} PUT</span>
        <span class="vote-neu">● ${resumo.neutros} neutros</span>
      </div>
      <div class="vote-list">
        ${votos.map((v) =>
          `<span class="vote-chip ${v.voto > 0 ? "chip-call" : v.voto < 0 ? "chip-put" : "chip-neu"}" title="${escapeHtml(v.motivo)}">${escapeHtml(v.nome)}</span>`
        ).join("")}
      </div>`;
    votesEl.classList.remove("hidden");
  } else {
    votesEl.classList.add("hidden");
    votesEl.innerHTML = "";
  }

  $( `lock-${expiry}` ).textContent = signal.locked ? "SINAL FIXADO" : "NOVO SINAL";
  card.dataset.expiresAt = signal.expires_at || "";
  const expiresAt = Number(signal.expires_at);
  if (Number.isFinite(expiresAt) && expiresAt > 0) signalExpiries[expiry] = expiresAt;
}

function updateTimers(data) {
  if (data && data.signals) {
    Object.entries(data.signals).forEach(([expiry, signal]) => {
      const expiresAt = Number(signal && signal.expires_at);
      if (Number.isFinite(expiresAt) && expiresAt > 0) signalExpiries[expiry] = expiresAt;
    });
  }

  Object.entries(signalExpiries).forEach(([expiry, storedExpiresAt]) => {
    const timerEl = $(`timer-${expiry}`);
    const interval = expirySeconds[expiry];
    if (!timerEl || !interval) return;

    const now = Date.now() / 1000;
    let expiresAt = Number(storedExpiresAt);
    if (expiresAt <= now) {
      const expiredAt = expiresAt;
      const elapsedCycles = Math.floor((now - expiresAt) / interval) + 1;
      expiresAt += elapsedCycles * interval;
      signalExpiries[expiry] = expiresAt;

      const expiryKey = `${expiry}:${expiredAt}`;
      if (!handledExpiries.has(expiryKey)) {
        handledExpiries.add(expiryKey);
        const lock = $(`lock-${expiry}`);
        if (lock) lock.textContent = "ATUALIZANDO";
        refreshAnalysisAfterExpiry();
      }
    }

    timerEl.textContent = formatCountdown(Math.ceil(expiresAt - now));
  });
}

function scheduleSignalRefresh(data) {
  Object.entries(data.signals || {}).forEach(([expiry, signal]) => {
    const expiresAt = Number(signal.expires_at);
    if (!Number.isFinite(expiresAt)) return;
    if (expiryTimeouts[expiry]) clearTimeout(expiryTimeouts[expiry]);
    const delay = Math.max(0, expiresAt * 1000 - Date.now() + 150);
    expiryTimeouts[expiry] = setTimeout(() => {
      const expiryKey = `${expiry}:${expiresAt}`;
      if (!handledExpiries.has(expiryKey)) {
        handledExpiries.add(expiryKey);
        refreshAnalysisAfterExpiry();
      }
    }, delay);
  });
}

async function refreshAnalysisAfterExpiry() {
  if (analysisRefreshInFlight) return;
  analysisRefreshInFlight = true;
  try {
    await loadAnalysis();
  } finally {
    analysisRefreshInFlight = false;
  }
}

/* ---- Card de Estratégia ---- */
function renderStrategy(strategy) {
  if (!strategy) return;
  const el = $("strategy-card");
  const expiryLabels = { "1min": "1 MINUTO", "5min": "5 MINUTOS", "15min": "15 MINUTOS" };
  $("strategy-label").textContent = `RECOMENDAÇÃO PARA ${expiryLabels[currentExpiry] || currentExpiry}`;

  const rec = strategy.recommendation || "AGUARDAR";
  let cls = "neutral";
  let arrow = "●";
  if (rec === "CALL") { cls = "call"; arrow = "▲"; }
  else if (rec === "PUT") { cls = "put"; arrow = "▼"; }

  el.className = `strategy-card ${cls}`;
  el.querySelector(".strategy-arrow").textContent = arrow;
  $("strategy-rec").textContent = rec === "AGUARDAR" ? "AGUARDAR" : rec;
  $("strategy-reason").textContent = strategy.reason || "";
  $("strategy-warning").textContent = strategy.warning || "";

  const conf = strategy.confidence || 0;
  $("strategy-conf-fill").style.width = `${conf}%`;
  $("strategy-conf-num").textContent = strategy.strength || "";
}

/* ---- Cartões de contexto (15min e 5min) ---- */
function renderContextCards(context) {
  const wrap = $("context-cards");
  wrap.innerHTML = "";
  const order = ["30min", "15min", "5min"];
  const labels = { "30min": "30 MINUTOS", "15min": "15 MINUTOS", "5min": "5 MINUTOS" };

  order.forEach((tfKey) => {
    const tf = context[tfKey];
    if (!tf) return;
    wrap.appendChild(buildSignalCard(tfKey, labels[tfKey], tf));
  });
}

/* ---- Cartão de gatilho (1min) ---- */
function renderTriggerCard(trigger) {
  const wrap = $("trigger-cards");
  wrap.innerHTML = "";
  if (!trigger) return;
  wrap.appendChild(buildSignalCard("1min", "1 MINUTO", trigger));
}

function buildSignalCard(tfKey, label, tf) {
  const cls = tf.signal === "CALL" ? "call" : tf.signal === "PUT" ? "put" : "neutral";
  const arrow = tf.signal === "CALL" ? "▲" : tf.signal === "PUT" ? "▼" : "●";
  const sigLabel = tf.signal === "CALL" ? "CALL" : tf.signal === "PUT" ? "PUT" : "NEUTRO";

  const card = document.createElement("div");
  card.className = `signal-card ${cls}`;
  card.innerHTML = `
    <div class="signal-head">
      <span class="signal-tf">${label}</span>
      <span class="signal-strength">${tf.strength}</span>
    </div>
    <div class="signal-main">
      <div class="signal-label">
        <span class="signal-arrow">${arrow}</span>
        <span>${sigLabel}</span>
      </div>

    </div>
    <div class="signal-bar">
      <div class="signal-bar-fill" style="width:${tf.confidence}%"></div>
    </div>
    <div class="signal-votes">
      <span class="vote-bull">${tf.bulls} CALL</span>
      <span class="vote-bear">${tf.bears} PUT</span>
      <span class="vote-neu">${tf.neutrals} neutros</span>
    </div>
  `;
  return card;
}

/* ---- Indicadores detalhados ---- */
function renderIndicators(context, trigger) {
  const wrap = $("indicators-wrap");
  wrap.innerHTML = "";

  const all = [
    ["15min", context["15min"], "15 minutos"],
    ["5min",  context["5min"],  "5 minutos"],
    ["1min",  trigger,           "1 minuto"],
  ];

  all.forEach(([key, tf, label]) => {
    if (!tf) return;
    const cls = tf.signal === "CALL" ? "call" : tf.signal === "PUT" ? "put" : "neutral";
    const block = document.createElement("div");
    block.className = "ind-tf-block";

    const header = document.createElement("div");
    header.className = "ind-tf-header";
    header.innerHTML = `
      <strong>⏱ ${label}</strong>
      <span class="ind-tf-badge ${cls}">
        ${tf.signal}
      </span>
    `;
    block.appendChild(header);

    (tf.indicators || []).forEach((ind) => {
      const vCls = ind.vote > 0 ? "pos" : ind.vote < 0 ? "neg" : "zero";
      const arrow = ind.vote > 0 ? "▲" : ind.vote < 0 ? "▼" : "●";
      const row = document.createElement("div");
      row.className = "ind-row";
      row.innerHTML = `
        <div class="ind-name">${escapeHtml(ind.name)}</div>
        <div class="ind-arrow ${vCls}">${arrow}</div>
        <div class="ind-reason">${escapeHtml(ind.reason)}</div>
      `;
      block.appendChild(row);
    });

    wrap.appendChild(block);
  });
}

/* ============================================================
   Radar
============================================================ */
async function runRadar() {
  const runId = ++radarRunId;
  const strategy = selectedStrategy;
  const btn = $("btn-radar");
  btn.disabled = true;
  btn.textContent = "Atualizando...";
  const assets = window.availableAssets || [];
  assets.forEach((asset) => {
    const previous = radarPairs.get(asset) || { asset, signals: {}, scores: {}, accuracy: {} };
    radarPairs.set(asset, { ...previous, loading: true, error: null });
  });
  $("radar-output").classList.remove("hidden");
  $("radar-output").dataset.loaded = "true";
  renderRadar({ pairs: Array.from(radarPairs.values()) });
  showRadarStatus("loading", `Analisando 0 de ${assets.length} pares...`);

  try {
    let nextIndex = 0;
    let completed = 0;
    const analyzeNext = async () => {
      while (nextIndex < assets.length && runId === radarRunId) {
        const asset = assets[nextIndex++];
        try {
          const response = await fetch(`/api/opportunity/${encodeURIComponent(asset)}?strategy=${encodeURIComponent(strategy)}`);
          if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.detail || `Falha ao analisar ${asset}`);
          }
          const pair = await response.json();
          if (runId !== radarRunId) return;
          radarPairs.set(asset, { ...pair, loading: false, error: null });
        } catch (error) {
          if (runId !== radarRunId) return;
          const previous = radarPairs.get(asset) || { asset, signals: {}, scores: {}, accuracy: {} };
          radarPairs.set(asset, { ...previous, loading: false, error: error.message });
        }
        if (runId !== radarRunId) return;
        completed += 1;
        renderRadar({ pairs: Array.from(radarPairs.values()) });
        showRadarStatus("loading", `Analisando ${completed} de ${assets.length} pares...`);
      }
    };

    const workers = Array.from({ length: Math.min(3, assets.length) }, analyzeNext);
    await Promise.all(workers);
    if (runId === radarRunId) {
      hideRadarStatus();
    }
  } catch (e) {
    showRadarStatus("error", `Erro: ${e.message}`);
  } finally {
    if (runId === radarRunId) {
      btn.disabled = false;
      btn.textContent = "Atualizar pares";
    }
  }
}

async function loadNews() {
  const status = $("news-status");
  const list = $("news-list");
  const importance = $("news-importance").value;
  status.className = "status loading";
  status.textContent = "Consultando Biquote…";
  try {
    const response = await fetch(`/api/news?hours=24&importance=${importance}`);
    if (!response.ok) throw new Error("Não foi possível consultar as notícias");
    const data = await response.json();
    list.innerHTML = "";
    if (!data.available) throw new Error(data.warning || "Biquote indisponível");
    renderNews(data.events || []);
    $("news-meta").textContent = `${data.events.length} eventos nas próximas 24 horas · Fonte: Biquote`;
    status.className = "status";
    status.textContent = data.events.length ? "Eventos ordenados por horário." : "Nenhum evento encontrado para este filtro.";
  } catch (error) {
    status.className = "status error";
    status.textContent = error.message;
  }
}

function renderNews(events) {
  const list = $("news-list");
  events.forEach((event) => {
    const item = document.createElement("article");
    const impact = event.importance || "low";
    const time = new Date(event.time);
    item.className = `news-item impact-${impact}`;
    item.innerHTML = `
      <div class="news-time">${time.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })}<small>${time.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" })}</small></div>
      <div class="news-currency">${escapeHtml(event.currency || "—")}</div>
      <div class="news-content"><strong>${escapeHtml(event.title)}</strong><span>${escapeHtml(event.sector || "Indicador econômico")}</span><small>${escapeHtml(event.description || "Evento macroeconômico agendado.")}</small></div>
      <div class="news-impact">${impact.toUpperCase()}</div>
      <div class="news-values"><span>Prev. ${formatValue(event.forecast)}</span><span>Ant. ${formatValue(event.previous)}</span></div>
    `;
    list.appendChild(item);
  });
}

function formatValue(value) {
  return value === null || value === undefined ? "—" : String(value);
}

function showRadarStatus(kind, msg) {
  const el = $("radar-status");
  el.className = `status ${kind}`;
  el.textContent = msg;
  el.classList.remove("hidden");
}
function hideRadarStatus() { $("radar-status").classList.add("hidden"); }

function renderRadar(d) {
  const marketRank = (pair) => (pair.market === "fechado" ? 1 : 0);
  const pairs = [...(d.pairs || [])].sort((first, second) => {
    const marketDifference = marketRank(first) - marketRank(second);
    if (marketDifference !== 0) return marketDifference;
    const firstRawRate = first.accuracy?.[radarSortExpiry]?.rate;
    const secondRawRate = second.accuracy?.[radarSortExpiry]?.rate;
    const firstRate = firstRawRate === null || firstRawRate === undefined ? -1 : Number(firstRawRate);
    const secondRate = secondRawRate === null || secondRawRate === undefined ? -1 : Number(secondRawRate);
    const safeFirstRate = Number.isFinite(firstRate) ? firstRate : -1;
    const safeSecondRate = Number.isFinite(secondRate) ? secondRate : -1;
    if (safeSecondRate !== safeFirstRate) return safeSecondRate - safeFirstRate;
    const scoreDifference = (second.scores?.[radarSortExpiry] || 0) - (first.scores?.[radarSortExpiry] || 0);
    return scoreDifference || first.asset.localeCompare(second.asset);
  });
  const selectedSignals = pairs.map((pair) => pair.signals?.[radarSortExpiry] || "AGUARDAR");
  $("sum-call").textContent = selectedSignals.filter((signal) => signal === "CALL").length;
  $("sum-neu").textContent = selectedSignals.filter((signal) => signal === "AGUARDAR").length;
  $("sum-put").textContent = selectedSignals.filter((signal) => signal === "PUT").length;
  const shortExpiry = radarSortExpiry.replace("min", " min");
  $("sum-call-label").textContent = `CALL (${shortExpiry})`;
  $("sum-neutral-label").textContent = `AGUARDAR (${shortExpiry})`;
  $("sum-put-label").textContent = `PUT (${shortExpiry})`;
  const openCount = pairs.filter((pair) => pair.market === "aberto").length;
  const closedCount = pairs.filter((pair) => pair.market === "fechado").length;
  $("radar-meta").textContent = `${pairs.length} pares · ${openCount} abertos · ${closedCount} fechados · ordenado por assertividade em ${shortExpiry}`;
  document.querySelectorAll(".radar-sort").forEach((button) => {
    const active = button.dataset.radarSort === radarSortExpiry;
    button.classList.toggle("active", active);
    button.setAttribute("aria-sort", active ? "descending" : "none");
  });

  const tbody = $("radar-body");
  tbody.innerHTML = "";

  if (pairs.length === 0) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted" style="text-align:center;padding:24px">Nenhum par disponível.</td></tr>`;
    return;
  }

  pairs.forEach((p) => {
    const signals = p.signals || {};
    const tr = document.createElement("tr");
    tr.className = `radar-row${p.loading ? " loading" : ""}${p.market === "fechado" ? " closed" : ""}`;
    tr.title = p.error || p.reason || "";
    tr.innerHTML = `
      <td class="pair">${escapeHtml(p.asset)}${marketBadge(p.market)}${p.error ? `<small>${escapeHtml(p.error)}</small>` : ""}</td>
      <td>${tfBadge(signals["1min"], p.proximity?.["1min"], p.accuracy?.["1min"], p.loading)}</td>
      <td>${tfBadge(signals["5min"], p.proximity?.["5min"], p.accuracy?.["5min"], p.loading)}</td>
      <td>${tfBadge(signals["15min"], p.proximity?.["15min"], p.accuracy?.["15min"], p.loading)}</td>
    `;
    tr.addEventListener("click", () => {
      selectAsset(p.asset);
      switchView("trade");
    });
    tbody.appendChild(tr);
  });
}

function marketBadge(market) {
  if (market === "aberto") return `<span class="mt-badge open" title="Mercado aberto">ABERTO</span>`;
  if (market === "fechado") return `<span class="mt-badge closed" title="Mercado fechado">FECHADO</span>`;
  return "";
}

function tfBadge(signal, proximity, accuracy, loading = false) {
  if (!signal) return `<span class="tf-badge analyzing">ANALISANDO</span>`;
  const safe = signal || "AGUARDAR";
  const cls = safe === "CALL" ? "call" : safe === "PUT" ? "put" : "neutral";
  const arrow = safe === "CALL" ? "▲" : safe === "PUT" ? "▼" : "●";
  const label = safe === "CALL" || safe === "PUT" ? safe : (proximity?.label || "AGUARDAR");
  const rate = accuracy && accuracy.rate !== null ? `${Number(accuracy.rate).toFixed(1)}%` : "sem amostra";
  const state = loading ? " · atualizando" : "";
  return `<span class="tf-badge ${cls}">${arrow} ${escapeHtml(label)}<em>${rate}${state}</em></span>`;
}

/* ============================================================
   Utils
============================================================ */
function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

$("btn-radar").addEventListener("click", runRadar);

/* ============================================================
   Entradas — configuração, execução e histórico
============================================================ */
let tradeEstado = {};
let entryDirection = "CALL";
let entryPeriod = "dia";
let reportPeriod = "dia";

function setStatus(el, kind, msg) {
  el.className = `status ${kind}`;
  el.textContent = msg;
  el.classList.remove("hidden");
  if (!kind) el.classList.add("hidden");
}

function setEntryDirection(dir) {
  entryDirection = dir;
  document.querySelectorAll("#entry-direction .dir-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.dir === dir);
  });
}

function setConfigAccount(conta) {
  document.querySelectorAll("#config-account .dir-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.conta === conta);
  });
}

document.querySelectorAll("#entry-direction .dir-btn").forEach((b) => {
  b.addEventListener("click", () => setEntryDirection(b.dataset.dir));
});

document.querySelectorAll("#config-account .dir-btn").forEach((b) => {
  b.addEventListener("click", () => {
    setConfigAccount(b.dataset.conta);
    if (b.dataset.conta === "REAL") {
      $("config-account-warning").textContent = "⚠ CONTA OFICIAL (REAL) — operação com dinheiro de verdade. Redobre a atenção.";
      $("config-account-warning").classList.add("real-warning");
    } else {
      $("config-account-warning").textContent = "Use a conta demo para testes. Operar na conta oficial envolve dinheiro real.";
      $("config-account-warning").classList.remove("real-warning");
    }
  });
});

$("config-strategy").addEventListener("change", () => {
  $("config-soros-field").classList.toggle("hidden", $("config-strategy").value !== "soros");
});

$("entries-period").addEventListener("change", () => { entryPeriod = $("entries-period").value; loadEntries(); });
$("btn-entries-refresh").addEventListener("click", () => { entryPeriod = $("entries-period").value; loadEntries(); });
$("report-period").addEventListener("change", () => { reportPeriod = $("report-period").value; });
$("btn-report-refresh").addEventListener("click", loadReport);
$("btn-save-config").addEventListener("click", saveTradeConfig);
$("btn-entry-execute").addEventListener("click", () => createEntry(true));
$("btn-entry-manual").addEventListener("click", () => createEntry(false));
$("quick-entry").addEventListener("click", quickEntry);

async function loadTradeConfig() {
  try {
    const r = await fetch("/api/trade/config");
    if (!r.ok) throw new Error("Falha ao carregar configurações");
    tradeEstado = await r.json();
    renderTradeConfig(tradeEstado);
  } catch (e) {
    console.error("Erro ao carregar configurações:", e);
  }
}

function renderTradeConfig(d) {
  const config = d.config || {};
  const est = d.estado || {};

  setConfigAccount(config.conta === "REAL" ? "REAL" : "PRACTICE");
  $("config-value").value = config.valor_entrada;
  $("config-max-loss").value = config.valor_max_perda;
  $("config-strategy").value = config.estrategia;
  $("config-soros-level").value = config.soros_nivel;
  $("config-soros-field").classList.toggle("hidden", config.estrategia !== "soros");

  // Entrada automática
  $("auto-enabled").value = config.auto_ativado === "1" ? "1" : "0";
  $("auto-payout-min").value = Number(config.auto_payout_min || 0.8).toFixed(2);
  $("auto-confianca-min").value = Number(config.auto_confianca_min || 60).toFixed(0);
  $("auto-direcao").value = config.auto_direcao || "ambos";
  $("auto-entrada").value = config.auto_entrada || "atual";
  let autoPares = [];
  try { autoPares = JSON.parse(config.auto_pares || "[]"); } catch {}
  $("auto-pares").value = autoPares.join(", ");
  let autoHorarios = {};
  try { autoHorarios = JSON.parse(config.auto_horarios || "{}"); } catch {}
  $("auto-horarios").value = Object.entries(autoHorarios)
    .map(([par, lista]) => `${par} ${(Array.isArray(lista) ? lista : [lista]).join(", ")}`)
    .join("\n");
  $("auto-max-simultaneas").value = config.auto_max_simultaneas;
  $("auto-expiry").value = config.auto_expiracao || 1;
  const autoStrategySelect = $("auto-strategy");
  const catalogo = (strategyCatalog && Object.keys(strategyCatalog).length)
    ? strategyCatalog
    : {
        trend_pullback: { name: "Retração na tendência", description: "" },
        breakout: { name: "Rompimento de faixa", description: "" },
        mean_reversion: { name: "Reversão à média", description: "" },
        support_resistance: { name: "Suporte e resistência", description: "" },
        momentum: { name: "Momentum", description: "" },
      };
  autoStrategySelect.innerHTML = Object.entries(catalogo)
    .map(([key, item]) => `<option value="${key}">${escapeHtml(item.name)}</option>`)
    .join("");
  autoStrategySelect.value = config.auto_strategy || "trend_pullback";
  if (catalogo[autoStrategySelect.value]) {
    $("auto-strategy-desc").textContent = catalogo[autoStrategySelect.value].description;
  }

  const labelConta = config.conta === "REAL" ? "REAL" : "PRACTICE";
  $("balance-label").textContent = labelConta;

  if (config.conta === "REAL") {
    $("config-account-warning").textContent = "⚠ CONTA OFICIAL (REAL) — operação com dinheiro de verdade. Redobre a atenção.";
    $("config-account-warning").classList.add("real-warning");
  } else {
    $("config-account-warning").textContent = "Use a conta demo para testes. Operar na conta oficial envolve dinheiro real.";
    $("config-account-warning").classList.remove("real-warning");
  }

  // Estado atual
  $("state-next-value").textContent = `US$ ${Number(est.valor_sugerido || 0).toFixed(2)}`;
  if (config.estrategia === "soros") {
    const proxima = est.contador_proxima ? ` · próxima entra no nível ${est.contador_proxima}` : "";
    $("state-soros-cycle").textContent = `${est.soros_nivel_atual}/${est.soros_nivel_max}${proxima}`;
    $("state-soros-profit").textContent = `US$ ${Number(est.soros_lucro || 0).toFixed(2)}`;
  } else {
    $("state-soros-cycle").textContent = "—";
    $("state-soros-profit").textContent = "—";
  }
  $("state-day-result").textContent = `US$ ${Number(est.perda_dia || 0).toFixed(2)}`;
  $("state-day-limit").textContent = Number(est.perda_dia_limite) > 0 ? `US$ ${Number(est.perda_dia_limite).toFixed(2)}` : "sem limite";

  // Aviso de bloqueio por perda + hint do valor sugerido
  $("entry-value-hint").textContent = `Valor sugerido: US$ ${Number(est.valor_sugerido || 0).toFixed(2)}. Deixe em branco para usá-lo.`;
  if (est.perda_dia_bloqueado) {
    $("entrada-aviso").textContent =
      `⚠ Limite de perda diária atingido (${Number(est.perda_dia).toFixed(2)} ≤ -${Number(est.perda_dia_limite).toFixed(2)}). Novas execuções automáticas estão bloqueadas.`;
    $("entrada-aviso").classList.remove("hidden");
    $("btn-entry-execute").disabled = true;
  } else {
    $("entrada-aviso").classList.add("hidden");
    $("btn-entry-execute").disabled = false;
  }

  $("entrada-resumo").textContent =
    `Conta ${config.conta === "REAL" ? "Oficial (REAL)" : "Demo (PRACTICE)"} · Estratégia ${config.estrategia === "soros" ? `Soros (nível ${config.soros_nivel})` : "Fixa"} · Entrada mínima US$ ${Number(config.valor_entrada).toFixed(2)}`;
}

async function saveTradeConfig() {
  const status = $("config-status");
  setStatus(status, "loading", "Salvando…");
  try {
    const body = {
      conta: $("config-account .dir-btn.active")?.dataset.conta || "PRACTICE",
      valor_entrada: parseFloat($("config-value").value),
      valor_max_perda: parseFloat($("config-max-loss").value),
      estrategia: $("config-strategy").value,
      soros_nivel: parseInt($("config-soros-level").value, 10) || 3,
      // Entrada automática
      auto_ativado: $("auto-enabled").value,
      auto_payout_min: parseFloat($("auto-payout-min").value),
      auto_confianca_min: parseFloat($("auto-confianca-min").value),
      auto_direcao: $("auto-direcao").value || "ambos",
      auto_entrada: $("auto-entrada").value || "atual",
      auto_pares: $("auto-pares").value,
      auto_horarios: $("auto-horarios").value,
      auto_max_simultaneas: parseInt($("auto-max-simultaneas").value, 10) || 1,
      auto_expiracao: parseInt($("auto-expiry").value, 10) || 1,
      auto_strategy: $("auto-strategy").value || "trend_pullback",
    };
    const r = await fetch("/api/trade/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.message || d.detail || "Falha ao salvar");
    tradeEstado = d;
    renderTradeConfig(d);
    setStatus(status, "", "Configurações salvas com sucesso.");
    setTimeout(() => status.classList.add("hidden"), 2500);
  } catch (e) {
    setStatus(status, "error", e.message);
  }
}

async function createEntry(executar) {
  const ativo = $("entry-asset").value.trim().toUpperCase();
  const direcao = entryDirection;
  const expiracao = parseInt($("entry-expiry").value, 10);
  const valorRaw = $("entry-value").value.trim();
  const valor = valorRaw === "" ? null : parseFloat(valorRaw);
  const observacao = $("entry-observation").value.trim();

  if (!ativo) { alert("Informe o ativo (ex.: EURUSD)."); return; }
  if (valor !== null && (!Number.isFinite(valor) || valor <= 0)) { alert("Valor inválido."); return; }

  // Execução na IQ Option não pede confirmação — segundos importam.
  if (!executar && !window.confirm("Confirmar registro manual da entrada?")) return;

  const status = $("entries-status");
  setStatus(status, executar ? "loading" : "loading", executar ? "Executando ordem na IQ Option…" : "Registrando entrada…");
  try {
    const r = await fetch("/api/trade/entradas", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ativo, direcao, expiracao, valor, executar, observacao }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.message || d.detail || "Falha ao registrar");
    $("entry-observation").value = "";
    $("entry-value").value = "";
    loadTradeConfig();
    loadEntries();
    setStatus(status, "", d.entrada.ordem_id ? `Ordem executada (ID ${d.entrada.ordem_id}). Resultado será apurado automaticamente.` : "Entrada registrada.");
    setTimeout(() => status.classList.add("hidden"), 4000);
  } catch (e) {
    loadEntries(); // mostra a tentativa registrada (ERRO) no histórico
    setStatus(status, "error", e.message);
  }
}

async function loadEntries() {
  if ($("view-entrada").classList.contains("hidden")) return;
  const status = $("entries-status");
  try {
    const r = await fetch(`/api/trade/entradas?periodo=${encodeURIComponent(entryPeriod)}`);
    if (!r.ok) throw new Error("Falha ao carregar entradas");
    const d = await r.json();
    renderEntries(d.entradas || []);
    setStatus(status, "", `${(d.entradas || []).length} entradas no período.`);
    setTimeout(() => status.classList.add("hidden"), 2000);
  } catch (e) {
    setStatus(status, "error", e.message);
  }
}

const statusLabels = {
  ABERTA: "ABERTA", WIN: "WIN", LOSS: "LOSS",
  EMPATE: "EMPATE", CANCELADA: "CANCELADA", ERRO: "ERRO",
};

function renderEntries(entradas) {
  const tbody = $("entries-body");
  tbody.innerHTML = "";
  if (!entradas.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="muted" style="text-align:center;padding:24px">Nenhuma entrada no período.</td></tr>`;
    return;
  }
  entradas.forEach((e) => {
    const tr = document.createElement("tr");
    const cls = e.status === "WIN" ? "call" : e.status === "LOSS" ? "put" : "neutral";
    const resultado = e.resultado === null || e.resultado === undefined
      ? "—"
      : `<span class="${Number(e.resultado) >= 0 ? "result-pos" : "result-neg"}">${Number(e.resultado) >= 0 ? "+" : ""}${Number(e.resultado).toFixed(2)}</span>`;

    let acoes = "";
    if (e.status === "ABERTA") {
      acoes = `
        <div class="row-actions">
          ${e.ordem_id ? `<button class="btn-mini" data-act="apurar" data-id="${e.id}" title="Consultar resultado na IQ Option">⏳ Apurar</button>` : ""}
          <select class="mini-select" data-act="status" data-id="${e.id}">
            <option value="">Fechar…</option>
            <option value="WIN">WIN</option>
            <option value="LOSS">LOSS</option>
            <option value="EMPATE">EMPATE</option>
            <option value="CANCELADA">CANCELAR</option>
          </select>
          <button class="btn-mini danger" data-act="del" data-id="${e.id}" title="Excluir">🗑</button>
        </div>`;
    } else {
      acoes = `<button class="btn-mini danger" data-act="del" data-id="${e.id}" title="Excluir">🗑</button>`;
    }

    tr.innerHTML = `
      <td class="muted small">${escapeHtml(e.criado_em)}</td>
      <td class="pair">${escapeHtml(e.ativo)}${e.ordem_id ? `<small>ordem #${escapeHtml(e.ordem_id)}</small>` : ""}</td>
      <td><span class="mini-signal ${e.direcao === "CALL" ? "call" : "put"}">${e.direcao === "CALL" ? "▲" : "▼"} ${e.direcao}</span></td>
      <td>US$ ${Number(e.valor).toFixed(2)}</td>
      <td>${e.expiracao}min</td>
      <td>${escapeHtml(e.estrategia)}</td>
      <td class="muted small">${e.conta === "REAL" ? "REAL" : "PRACTICE"}</td>
      <td><span class="tf-badge ${cls}">${statusLabels[e.status] || e.status}</span></td>
      <td>${resultado}</td>
      <td>${acoes}</td>
    `;
    tbody.appendChild(tr);

    tr.querySelectorAll("[data-act]").forEach((el) => {
      el.addEventListener("click", (event) => {
        event.stopPropagation();
        const id = el.dataset.id;
        if (el.dataset.act === "apurar") apurarEntrada(id);
        else if (el.dataset.act === "del") excluirEntrada(id);
      });
    });
    tr.querySelectorAll("[data-act='status']").forEach((select) => {
      select.addEventListener("change", () => responderEntrada(select.dataset.id, select.value));
    });
  });
}

async function responderEntrada(id, novoStatus) {
  if (!novoStatus) return;
  if (novoStatus === "CANCELADA") {
    if (!window.confirm("Cancelar esta entrada (sem resultado)?")) return;
    await patchEntrada(id, { status: "CANCELADA" });
    return;
  }
  const valor = window.prompt(`Resultado líquido da entrada #${id} em US$ (use - para perda):`, "0.00");
  if (valor === null) return;
  const numero = parseFloat(valor.replace(",", "."));
  if (!Number.isFinite(numero)) { alert("Valor inválido."); return; }
  await patchEntrada(id, { status: novoStatus, resultado: numero });
}

async function patchEntrada(id, campos) {
  const status = $("entries-status");
  setStatus(status, "loading", "Atualizando…");
  try {
    const r = await fetch(`/api/trade/entradas/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(campos),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.message || d.detail || "Falha ao atualizar");
    loadTradeConfig();
    loadEntries();
    setStatus(status, "", "Entrada atualizada.");
    setTimeout(() => status.classList.add("hidden"), 2500);
  } catch (e) {
    setStatus(status, "error", e.message);
  }
}

async function apurarEntrada(id) {
  const status = $("entries-status");
  setStatus(status, "loading", "Consultando resultado na IQ Option…");
  try {
    const r = await fetch(`/api/trade/entradas/${id}/apurar`, { method: "POST" });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Falha ao apurar");
    setStatus(status, "loading", "Apuração iniciada. O resultado será aplicado quando a ordem fechar (pode levar alguns minutos).");
    setTimeout(() => { loadEntries(); status.classList.add("hidden"); }, 5000);
  } catch (e) {
    setStatus(status, "error", e.message);
  }
}

async function excluirEntrada(id) {
  if (!window.confirm(`Excluir a entrada #${id} do histórico?`)) return;
  const status = $("entries-status");
  try {
    const r = await fetch(`/api/trade/entradas/${id}`, { method: "DELETE" });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.message || d.detail || "Falha ao excluir");
    loadTradeConfig();
    loadEntries();
    setStatus(status, "", "Entrada excluída.");
    setTimeout(() => status.classList.add("hidden"), 2500);
  } catch (e) {
    setStatus(status, "error", e.message);
  }
}

$("quick-execute").addEventListener("click", quickExecuteNow);

function quickExpiryKey() {
  const minutos = parseInt($("quick-expiry").value, 10) || 1;
  return { key: `${minutos}min`, minutos };
}

function quickEntry() {
  const analysis = window.latestAnalysis;
  const { key, minutos } = quickExpiryKey();
  const sinal = analysis?.signals?.[key]?.signal;
  if (!sinal || !["CALL", "PUT"].includes(sinal)) return;
  $("entry-asset").value = analysis.asset || currentAsset;
  setEntryDirection(sinal);
  $("entry-expiry").value = minutos;
  const valorBruto = $("quick-value").value.trim();
  if (valorBruto) $("entry-value").value = valorBruto;
  switchView("entrada");
  $("entry-value").focus();
}

async function quickExecuteNow() {
  const analysis = window.latestAnalysis;
  const { key, minutos } = quickExpiryKey();
  const sinal = analysis?.signals?.[key]?.signal;
  if (!analysis || !["CALL", "PUT"].includes(sinal)) {
    $("analysis-note").textContent = "Sem sinal CALL/PUT na expiração escolhida.";
    return;
  }
  const valorRaw = $("quick-value").value.trim();
  const valor = valorRaw === "" ? null : parseFloat(valorRaw);
  try {
    const r = await fetch("/api/trade/entradas", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ativo: analysis.asset || currentAsset,
        direcao: sinal,
        expiracao: minutos,
        valor,
        executar: true,
        origem: "analise",
      }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.message || d.detail || "Falha ao executar");
    $("analysis-note").textContent =
      `✅ Ordem ${sinal} ${minutos}min executada${d.entrada.ordem_id ? ` (ID ${d.entrada.ordem_id})` : ""}.`;
    setTimeout(() => loadAnalysis(true), 2500);
  } catch (e) {
    loadEntries(); // mostra a tentativa registrada (ERRO) no histórico
    $("analysis-note").textContent = `❌ ${e.message}`;
  }
}

/* ============================================================
   Relatório
============================================================ */
async function loadReport() {
  const status = $("report-status");
  setStatus(status, "loading", "Gerando relatório…");
  try {
    const r = await fetch(`/api/trade/relatorio?periodo=${encodeURIComponent(reportPeriod)}`);
    if (!r.ok) throw new Error("Falha ao gerar relatório");
    const d = await r.json();
    renderReport(d);
    const periodos = { dia: "Hoje", semana: "Esta semana", mes: "Este mês", ano: "Este ano" };
    $("report-meta").textContent = `${periodos[reportPeriod]} · de ${d.inicio} até ${d.fim}.`;
    $("report-output").classList.remove("hidden");
    status.classList.add("hidden");
  } catch (e) {
    setStatus(status, "error", e.message);
  }
}

function reportCard(label, value, cls = "") {
  return `<div class="report-card ${cls}"><span>${label}</span><strong>${value}</strong></div>`;
}

function renderReport(d) {
  const acerto = d.taxa_acerto === null ? "—" : `${d.taxa_acerto}%`;
  $("report-cards").innerHTML = [
    reportCard("Entradas", d.total_entradas),
    reportCard("Abertas", d.abertas),
    reportCard("Wins", d.wins, "call"),
    reportCard("Losses", d.losses, "put"),
    reportCard("Empates", d.empates),
    reportCard("Taxa de acerto", acerto),
    reportCard("Ganho bruto", `US$ ${Number(d.ganho_bruto).toFixed(2)}`, "call"),
    reportCard("Perda bruta", `US$ ${Number(d.perda_bruta).toFixed(2)}`, "put"),
    reportCard("Líquido", `US$ ${Number(d.liquido).toFixed(2)}`, d.liquido >= 0 ? "call" : "put"),
    reportCard("Valor operado", `US$ ${Number(d.valor_operado).toFixed(2)}`),
  ].join("");

  const tbody = $("report-days-body");
  tbody.innerHTML = "";
  if (!(d.por_dia || []).length) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;padding:24px">Sem entradas fechadas no período.</td></tr>`;
    return;
  }
  d.por_dia.forEach((dia) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(dia.dia)}</td>
      <td>${dia.entradas}</td>
      <td class="result-pos">${dia.wins}</td>
      <td class="result-neg">${dia.losses}</td>
      <td>${dia.empates}</td>
      <td class="${dia.liquido >= 0 ? "result-pos" : "result-neg"}">${dia.liquido >= 0 ? "+" : ""}${Number(dia.liquido).toFixed(2)}</td>
    `;
    tbody.appendChild(tr);
  });
}

// Atualiza periodicamente o histórico quando a aba Entradas estiver visível
setInterval(() => { if (!$("view-entrada").classList.contains("hidden")) loadEntries(); }, 15000);

/* ============================================================
   IA — segunda opinião
============================================================ */
$("btn-ai").addEventListener("click", consultarIA);

async function consultarIA() {
  const status = $("ai-status");
  const resultado = $("ai-result");
  const btn = $("btn-ai");
  setStatus(status, "loading", "Consultando a IA… (pode levar alguns segundos)");
  resultado.classList.add("hidden");
  btn.disabled = true;
  try {
    const r = await fetch("/api/ai/analise", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ativo: currentAsset,
        strategy: selectedStrategy,
        instrucao_extra: $("ai-instruction").value,
      }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Falha na consulta");
    if (d.erro) throw new Error(d.erro);
    status.classList.add("hidden");
    const cls = d.direcao === "CALL" ? "call" : d.direcao === "PUT" ? "put" : "neutral";
    const riscos = (d.riscos || []).map((r) => `<li>${escapeHtml(r)}</li>`).join("");
    resultado.innerHTML = `
    <div class="ai-direcao ${cls}">${d.direcao}</div>
      <p class="ai-just">${escapeHtml(d.justificativa || "Sem justificativa fornecida.")}</p>
      ${riscos ? `<ul class="ai-riscos">${riscos}</ul>` : ""}
      <small class="muted">Modelo: ${escapeHtml(d.modelo || "—")} · análise educacional, não é recomendação de investimento.</small>`;
    resultado.classList.remove("hidden");
  } catch (e) {
    setStatus(status, "error", e.message);
  } finally {
    btn.disabled = false;
  }
}

/* ============================================================
   Aba Estratégias — catálogo + parâmetros + indicadores ativos
============================================================ */
const paramLabels = {
  adx_min: "ADX mínimo",
  near_ema_atr: "Prox. da EMA (×ATR)",
  rsi_call_min: "RSI CALL mín.",
  rsi_call_max: "RSI CALL máx.",
  rsi_put_min: "RSI PUT mín.",
  rsi_put_max: "RSI PUT máx.",
  min_score: "Score mínimo",
  expansao_atr: "Expansão (×ATR)",
  rsi_sobrevenda: "RSI sobrevenda",
  rsi_sobrecompra: "RSI sobrecompra",
  zona_atr: "Zona (×ATR)",
  pavio_ratio: "Pavio × corpo",
  rsi_call: "RSI CALL",
  rsi_put: "RSI PUT",
};

async function loadStrategiesView() {
  const grid = $("strategies-grid");
  grid.innerHTML = Object.entries(strategyCatalog || {}).map(([key, item]) => {
    const ativa = key === selectedStrategy;
    return `
      <div class="strategy-card-mini ${ativa ? "active" : ""}" data-strategy="${escapeHtml(key)}">
        <div class="strategy-mini-head">
          <strong>${escapeHtml(item.name)}</strong>
          <div class="strategy-mini-actions">
            <button class="btn-mini" data-act="usar">${ativa ? "✓ Ativa" : "Usar"}</button>
            <button class="btn-mini" data-act="params" title="Ajustar limiares da estratégia">⚙</button>
          </div>
        </div>
        <p>${escapeHtml(item.description)}</p>
        <small class="muted">Indicadores: ${(item.indicadores || []).map(escapeHtml).join(", ")}</small>
        <div class="strategy-params hidden" data-params="${escapeHtml(key)}"></div>
      </div>`;
  }).join("");
  grid.querySelectorAll("[data-act='usar']").forEach((btn) => {
    btn.addEventListener("click", () => {
      selectedStrategy = btn.closest("[data-strategy]").dataset.strategy;
      confirmStrategy(); // re-confirma e parte para o rastreamento com a nova estratégia
    });
  });
  grid.querySelectorAll("[data-act='params']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const card = btn.closest("[data-strategy]");
      const painel = card.querySelector("[data-params]");
      painel.classList.toggle("hidden");
      if (!painel.dataset.loaded) renderParams(painel, card.dataset.strategy);
    });
  });
  await loadStrategyParams();
  await loadIndicators();
}

async function loadStrategyParams() {
  try {
    const r = await fetch("/api/strategy-params");
    const d = await r.json();
    window.strategyParams = d;
    $("use-votes").checked = !!d.usar_votos;
    document.querySelectorAll("[data-params]").forEach((painel) => {
      if (!painel.classList.contains("hidden")) renderParams(painel, painel.dataset.params);
    });
  } catch {}
}

function renderParams(painel, key) {
  const atuais = window.strategyParams?.atuais?.[key] || {};
  const defaults = window.strategyParams?.defaults?.[key] || {};
  painel.dataset.loaded = "true";
  const campos = Object.keys(defaults).map((chave) => `
    <label class="param-field">
      <span>${escapeHtml(paramLabels[chave] || chave)}</span>
      <input type="number" step="any" data-param="${escapeHtml(chave)}" value="${atuais[chave] ?? defaults[chave]}" />
    </label>`).join("");
  painel.innerHTML = `
    <div class="param-grid">${campos}</div>
    <button class="btn-mini" data-save-params="${escapeHtml(key)}">Salvar parâmetros</button>
    <span class="params-status muted small"></span>`;
  painel.querySelector("[data-save-params]").addEventListener("click", () => saveParams(key, painel));
}

async function saveParams(key, painel) {
  const params = {};
  painel.querySelectorAll("[data-param]").forEach((input) => {
    params[input.dataset.param] = parseFloat(input.value);
  });
  const status = painel.querySelector(".params-status");
  status.textContent = "Salvando…";
  try {
    const r = await fetch("/api/strategy-params", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ strategy: key, params }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.message || d.detail || "Falha ao salvar");
    window.strategyParams.atuais[key] = d.atuais[key];
    status.textContent = "✓ " + d.message;
    setTimeout(() => { status.textContent = ""; }, 2500);
    loadAnalysis(true); // recalcula com os novos limiares
  } catch (e) {
    status.textContent = "✗ " + e.message;
  }
}

$("use-votes").addEventListener("change", async (event) => {
  try {
    await fetch("/api/strategy-params/votos", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ usar_votos: event.target.checked }),
    });
    loadAnalysis(true);
  } catch {}
});

async function loadIndicators() {
  try {
    const r = await fetch("/api/indicators");
    const d = await r.json();
    window.indicatorsCatalog = d.indicadores || [];
    renderIndicators();
  } catch {}
}

function renderIndicators() {
  const grid = $("indicators-grid");
  grid.innerHTML = "";
  (window.indicatorsCatalog || []).forEach((ind) => {
    const label = document.createElement("label");
    label.className = `indicator-toggle${ind.ativo ? " active" : ""}`;
    label.innerHTML = `
      <input type="checkbox" data-ind="${escapeHtml(ind.id)}" ${ind.ativo ? "checked" : ""} />
      <div class="ind-toggle-text">
        <strong>${escapeHtml(ind.nome)}</strong>
        <small>${escapeHtml(ind.descricao)}</small>
      </div>
      <span class="ind-peso">peso
        <input type="number" class="ind-peso-input" data-ind="${escapeHtml(ind.id)}"
               value="${Number(ind.peso || 1).toFixed(1)}" min="0.1" max="10" step="0.1"
               title="Peso no cálculo interno (0.1 a 10)" />
      </span>`;
    label.querySelector("input").addEventListener("change", () => {
      label.classList.toggle("active", label.querySelector("input").checked);
    });
    // O clique no campo de peso não deve alternar o checkbox do indicador
    const pesoInput = label.querySelector(".ind-peso-input");
    pesoInput.addEventListener("click", (ev) => ev.stopPropagation());
    pesoInput.addEventListener("mousedown", (ev) => ev.stopPropagation());
    grid.appendChild(label);
  });
}

async function saveIndicators() {
  const ativos = Array.from(document.querySelectorAll("#indicators-grid input[type=checkbox]:checked"))
    .map((input) => input.dataset.ind);
  const status = $("strategies-status");
  setStatus(status, "loading", "Salvando indicadores e pesos…");
  const pesos = {};
  Array.from(document.querySelectorAll("#indicators-grid .ind-peso-input")).forEach((inp) => {
    const peso = parseFloat(inp.value);
    if (Number.isFinite(peso) && peso > 0) pesos[inp.dataset.ind] = peso;
  });
  try {
    const r = await fetch("/api/indicators", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ativos, pesos }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.message || d.detail || "Falha ao salvar");
    window.indicatorsCatalog = d.indicadores;
    renderIndicators();
    setStatus(status, "", d.message + " A análise será recalculada.");
    setTimeout(() => status.classList.add("hidden"), 3000);
    loadAnalysis(true); // força recálculo com os novos indicadores
  } catch (e) {
    setStatus(status, "error", e.message);
  }
}

$("btn-indicators-save").addEventListener("click", saveIndicators);

/* ============================================================
   IA — configuração (link, modelo, token, headers adicionais)
============================================================ */
$("btn-save-ai-config").addEventListener("click", saveAiConfig);
$("btn-test-ai").addEventListener("click", testAiConfig);

async function loadAiConfig() {
  try {
    const r = await fetch("/api/ai/config");
    if (!r.ok) throw new Error("Falha ao carregar configuração da IA");
    renderAiConfig(await r.json());
  } catch (e) {
    console.error("Erro ao carregar configuração da IA:", e);
  }
}

function renderAiConfig(d) {
  $("ai-base-url").value = d.base_url || "";
  $("ai-model").value = d.model || "";
  $("ai-api-key").value = "";
  $("ai-headers").value = d.headers || "";
  if (d.api_key_set) {
    $("ai-api-key-hint").textContent =
      `Token salvo (termina em …${d.api_key_tail}). Deixe em branco para mantê-lo ou digite um novo token para substituir.`;
  } else {
    $("ai-api-key-hint").textContent = "Vazio = usa OPENAI_API_KEY do .env.";
  }
}

function aiFormData() {
  return {
    base_url: $("ai-base-url").value.trim(),
    model: $("ai-model").value.trim(),
    api_key: $("ai-api-key").value.trim(),
    headers: $("ai-headers").value.trim(),
  };
}

async function saveAiConfig() {
  const status = $("ai-status");
  setStatus(status, "loading", "Salvando configuração da IA…");
  try {
    const body = aiFormData();
    if (!body.api_key) delete body.api_key; // campo vazio mantém o token salvo/do .env
    const r = await fetch("/api/ai/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.message || d.detail || "Falha ao salvar");
    renderAiConfig(d);
    setStatus(status, "", d.message);
    setTimeout(() => status.classList.add("hidden"), 4000);
  } catch (e) {
    setStatus(status, "error", e.message);
  }
}

async function testAiConfig() {
  const status = $("ai-status");
  setStatus(status, "loading", "Testando conexão… (pode levar alguns segundos)");
  try {
    const r = await fetch("/api/ai/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(aiFormData()),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Falha ao testar");
    if (!d.ok) {
      setStatus(status, "error", `Falha na conexão: ${d.erro}`);
      return;
    }
    setStatus(status, "", `✓ Conectado (${d.modelo} · ${d.base_url}) — resposta: ${d.resposta || "OK"}`);
    setTimeout(() => status.classList.add("hidden"), 6000);
  } catch (e) {
    setStatus(status, "error", e.message);
  }
}

boot();
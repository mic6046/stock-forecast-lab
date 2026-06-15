const els = {
  apiStatus: document.getElementById("apiStatus"),
  symbolInput: document.getElementById("symbolInput"),
  horizonInput: document.getElementById("horizonInput"),
  forecastButton: document.getElementById("forecastButton"),
  message: document.getElementById("message"),
  results: document.getElementById("results"),
  charts: document.getElementById("charts"),
  transparency: document.getElementById("transparency"),
  signalBadge: document.getElementById("signalBadge"),
  resultSymbol: document.getElementById("resultSymbol"),
  companyName: document.getElementById("companyName"),
  currentPrice: document.getElementById("currentPrice"),
  forecastPrice: document.getElementById("forecastPrice"),
  forecastReturn: document.getElementById("forecastReturn"),
  confidenceScore: document.getElementById("confidenceScore"),
  riskLevel: document.getElementById("riskLevel"),
  riskFill: document.getElementById("riskFill"),
  riskDetails: document.getElementById("riskDetails"),
  aiSummary: document.getElementById("aiSummary"),
  modelWeights: document.getElementById("modelWeights"),
  backtestStats: document.getElementById("backtestStats"),
  ensembleContribution: document.getElementById("ensembleContribution"),
  historyChart: document.getElementById("historyChart"),
  forecastChart: document.getElementById("forecastChart"),
};

let historyChart;
let forecastChart;

const moneyFormatter = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const percentFormatter = new Intl.NumberFormat(undefined, { style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1 });

function formatMoney(value, currency = "") {
  if (!Number.isFinite(value)) return "--";
  return `${currency ? `${currency} ` : ""}${moneyFormatter.format(value)}`;
}

function formatPercent(value) {
  return Number.isFinite(value) ? percentFormatter.format(value) : "--";
}

function formatNumber(value, digits = 2) {
  return Number.isFinite(value) ? value.toFixed(digits) : "--";
}

function setLoading(isLoading) {
  document.body.classList.toggle("is-loading", isLoading);
  els.forecastButton.disabled = isLoading;
  els.apiStatus.textContent = isLoading ? "Forecasting" : "Ready";
}

function showMessage(text, tone = "neutral") {
  els.message.textContent = text || "";
  els.message.className = `message ${tone}`;
}

function setSectionsVisible(visible) {
  els.results.hidden = !visible;
  els.charts.hidden = !visible;
  els.transparency.hidden = !visible;
}

function inferMarket(symbol) {
  return /^\d+(\.HK)?$/i.test(symbol) || /\.HK$/i.test(symbol) ? "HK" : "US";
}

async function runForecast() {
  const symbol = els.symbolInput.value.trim();
  if (!symbol) {
    showMessage("Enter a stock symbol first.", "error");
    return;
  }

  setLoading(true);
  showMessage("Loading forecast data...", "neutral");

  try {
    const query = new URLSearchParams({ symbol, market: inferMarket(symbol), horizon: els.horizonInput.value });
    const response = await fetch(`/api/predict?${query.toString()}`);
    let payload;
    try {
      payload = await response.json();
    } catch {
      throw new Error("API unavailable or returned an invalid response.");
    }
    if (!response.ok) throw new Error(payload.error || "No data returned for this symbol.");
    if (!payload || !payload.summary || !Array.isArray(payload.history) || !Array.isArray(payload.forecast)) {
      throw new Error("No data returned for this symbol.");
    }
    renderForecast(payload);
    showMessage(`Updated ${payload.normalizedSymbol || payload.symbol}.`, "success");
  } catch (error) {
    setSectionsVisible(false);
    destroyCharts();
    showMessage(error.message || "Forecast failed. Check the symbol and try again.", "error");
    els.apiStatus.textContent = "Error";
  } finally {
    setLoading(false);
  }
}

function renderForecast(payload) {
  const summary = payload.summary || {};
  const recommendation = payload.recommendation || {};
  const models = payload.models || {};
  const currency = payload.currency || "";
  const expectedReturn = summary.expectedReturn;
  const signal = getSignal(summary, recommendation);
  const score = Number.isFinite(recommendation.score) ? recommendation.score : summary.probabilityUp * 100;

  setSectionsVisible(true);
  els.signalBadge.textContent = signal.label;
  els.signalBadge.className = signal.className;
  els.resultSymbol.textContent = payload.normalizedSymbol || payload.symbol || "--";
  els.companyName.textContent = payload.name || "Company name unavailable";
  els.currentPrice.textContent = formatMoney(summary.lastPrice, currency);
  els.forecastPrice.textContent = formatMoney(summary.targetPrice, currency);
  els.forecastReturn.textContent = formatPercent(expectedReturn);
  els.forecastReturn.className = Number(expectedReturn) >= 0 ? "positive" : "negative";
  els.confidenceScore.textContent = Number.isFinite(score) ? `${Math.round(score)}/100` : summary.confidence || "--";

  renderRisk(summary);
  renderSummary(payload);
  renderCharts(payload);
  renderTransparency(models);
}

function getSignal(summary, recommendation) {
  const action = String(recommendation.action || summary.bias || "Neutral").toLowerCase();
  if (action.includes("buy") || action.includes("positive") || action.includes("bull")) return { label: "Bullish", className: "badge bullish" };
  if (action.includes("sell") || action.includes("negative") || action.includes("bear")) return { label: "Bearish", className: "badge bearish" };
  return { label: "Neutral", className: "badge neutral" };
}

function renderRisk(summary) {
  const volatility = summary.annualizedVolatility || 0;
  const uncertainty = summary.uncertainty || 0;
  const riskScore = Math.min(100, Math.round((volatility * 150 + uncertainty * 120) * 100));
  let level = "Low";
  if (riskScore >= 66) level = "High";
  else if (riskScore >= 34) level = "Medium";
  els.riskLevel.textContent = level;
  els.riskLevel.className = level.toLowerCase();
  els.riskFill.style.width = `${Math.max(8, riskScore)}%`;
  els.riskDetails.textContent = `Annualized volatility ${formatPercent(volatility)}. Forecast uncertainty ${formatPercent(uncertainty)}.`;
}

function renderSummary(payload) {
  const summary = payload.summary || {};
  const signals = payload.signals || {};
  const bias = String(summary.bias || "neutral").toLowerCase();
  const confidence = String(summary.confidence || "low").toLowerCase();
  const trend = String(signals.trend || "neutral").toLowerCase();
  const momentum = String(signals.momentum || "neutral").toLowerCase();
  const direction = bias.includes("positive") ? "bullish" : bias.includes("negative") ? "bearish" : "neutral";
  els.aiSummary.textContent = `The model is ${confidence} confidence and ${direction} over the selected forecast horizon. Trend is ${trend}, momentum is ${momentum}, and the projected return is ${formatPercent(summary.expectedReturn)}.`;
}

function renderCharts(payload) {
  if (!window.Chart) {
    showMessage("Chart.js could not be loaded. Forecast values are still available above.", "error");
    return;
  }

  const currency = payload.currency || "";
  const history = payload.history || [];
  const forecast = payload.forecast || [];
  const horizon = payload.requestedHorizon || payload.summary?.horizonDays || 30;
  const selectedForecast = forecast.slice(0, horizon);

  destroyCharts();

  historyChart = new Chart(els.historyChart, {
    type: "line",
    data: {
      labels: history.map((row) => row.date),
      datasets: [{ label: "Historical close", data: history.map((row) => row.close), borderColor: "#35d399", backgroundColor: "rgba(53, 211, 153, 0.12)", pointRadius: 0, borderWidth: 2, tension: 0.25, fill: true }],
    },
    options: chartOptions(currency),
  });

  forecastChart = new Chart(els.forecastChart, {
    type: "line",
    data: {
      labels: selectedForecast.map((row) => row.date),
      datasets: [
        { label: "Forecast price", data: selectedForecast.map((row) => row.predicted), borderColor: "#60a5fa", backgroundColor: "rgba(96, 165, 250, 0.14)", pointRadius: 0, borderWidth: 2, tension: 0.28, fill: false },
        { label: "Upper range", data: selectedForecast.map((row) => row.upper), borderColor: "rgba(148, 163, 184, 0.45)", pointRadius: 0, borderWidth: 1, borderDash: [6, 6] },
        { label: "Lower range", data: selectedForecast.map((row) => row.lower), borderColor: "rgba(148, 163, 184, 0.45)", pointRadius: 0, borderWidth: 1, borderDash: [6, 6] },
      ],
    },
    options: chartOptions(currency),
  });
}

function chartOptions(currency) {
  const styles = getComputedStyle(document.documentElement);
  const gridColor = styles.getPropertyValue("--grid").trim() || "rgba(148, 163, 184, 0.18)";
  const textColor = styles.getPropertyValue("--text-muted").trim() || "#94a3b8";
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { intersect: false, mode: "index" },
    plugins: { legend: { labels: { color: textColor, boxWidth: 12 } }, tooltip: { callbacks: { label: (context) => `${context.dataset.label}: ${formatMoney(context.parsed.y, currency)}` } } },
    scales: {
      x: { ticks: { color: textColor, maxTicksLimit: 8 }, grid: { color: gridColor } },
      y: { ticks: { color: textColor, callback: (value) => formatMoney(Number(value), currency).replace(`${currency} `, "") }, grid: { color: gridColor } },
    },
  };
}

function destroyCharts() {
  if (historyChart) historyChart.destroy();
  if (forecastChart) forecastChart.destroy();
  historyChart = null;
  forecastChart = null;
}

function renderTransparency(models) {
  renderList(els.modelWeights, models.weights || {}, (value) => formatPercent(value));
  renderList(els.backtestStats, models.ensemble || {}, (value, key) => {
    if (key.toLowerCase().includes("accuracy")) return formatPercent(value);
    if (key.toLowerCase().includes("rmse")) return formatNumber(value, 4);
    return String(value ?? "--");
  });
  renderList(els.ensembleContribution, models.currentDailyForecasts || {}, (value) => formatPercent(value));
}

function renderList(container, data, formatter) {
  const entries = Object.entries(data || {}).slice(0, 10);
  container.innerHTML = entries.length
    ? entries.map(([key, value]) => `<div><span>${escapeHtml(key.replace(/_/g, " "))}</span><strong>${escapeHtml(Number.isFinite(value) ? formatter(value, key) : String(value ?? "--"))}</strong></div>`).join("")
    : `<p class="empty">No model data returned.</p>`;
}

function escapeHtml(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

document.querySelectorAll("[data-symbol]").forEach((button) => {
  button.addEventListener("click", () => {
    els.symbolInput.value = button.dataset.symbol;
    runForecast();
  });
});

els.forecastButton.addEventListener("click", runForecast);
els.symbolInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") runForecast();
});

runForecast();

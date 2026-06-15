from __future__ import annotations

import datetime as dt
import email.utils
import html
import json
import math
import os
import re
import statistics
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"

NEWS_POSITIVE_TERMS = {
    "beat",
    "beats",
    "boost",
    "boosts",
    "bullish",
    "buy",
    "climb",
    "climbs",
    "gain",
    "gains",
    "growth",
    "higher",
    "jump",
    "jumps",
    "outperform",
    "rally",
    "record",
    "recover",
    "rebound",
    "rise",
    "rises",
    "strong",
    "upgrade",
    "upside",
}

NEWS_NEGATIVE_TERMS = {
    "bearish",
    "concern",
    "concerns",
    "cut",
    "cuts",
    "decline",
    "declines",
    "downgrade",
    "fall",
    "falls",
    "fear",
    "fears",
    "hit",
    "loss",
    "lower",
    "miss",
    "misses",
    "pressure",
    "recession",
    "risk",
    "risks",
    "sell",
    "slump",
    "slumps",
    "slowdown",
    "tariff",
    "tumbles",
    "weak",
    "warning",
}


def json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def normalize_symbol(raw_symbol: str, market: str) -> tuple[str, str]:
    symbol = (raw_symbol or "").strip().upper().replace(" ", "")
    market = (market or "AUTO").upper()
    if not symbol:
        raise ValueError("Enter a ticker symbol.")

    if symbol.endswith(".HK"):
        return symbol, "HK"

    if market == "HK":
        if symbol.isdigit():
            return f"{symbol.zfill(4)}.HK", "HK"
        return f"{symbol}.HK", "HK"

    if market == "US":
        return symbol.replace(".US", ""), "US"

    if symbol.isdigit() and len(symbol) <= 5:
        return f"{symbol.zfill(4)}.HK", "HK"
    return symbol.replace(".US", ""), "US"


def fetch_yahoo_history(symbol: str, range_period: str = "5y") -> dict:
    encoded = urllib.parse.quote(symbol, safe="")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}"
        f"?range={urllib.parse.quote(range_period)}&interval=1d&includeAdjustedClose=true&events=history"
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 stock-forecast-app",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=14) as response:
        raw = response.read().decode("utf-8")
    payload = json.loads(raw)
    chart = payload.get("chart", {})
    if chart.get("error"):
        description = chart["error"].get("description") or "Market data request failed."
        raise ValueError(description)
    results = chart.get("result") or []
    if not results:
        raise ValueError("No market data was returned for this symbol.")

    result = results[0]
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators", {})
    quote = (indicators.get("quote") or [{}])[0]
    adjclose = ((indicators.get("adjclose") or [{}])[0]).get("adjclose") or []
    closes = adjclose if adjclose and any(v is not None for v in adjclose) else quote.get("close", [])

    rows = []
    for idx, timestamp in enumerate(timestamps):
        close = clean_float(closes[idx] if idx < len(closes) else None)
        open_price = clean_float((quote.get("open") or [None])[idx] if idx < len(quote.get("open") or []) else None)
        high = clean_float((quote.get("high") or [None])[idx] if idx < len(quote.get("high") or []) else None)
        low = clean_float((quote.get("low") or [None])[idx] if idx < len(quote.get("low") or []) else None)
        volume = clean_float((quote.get("volume") or [None])[idx] if idx < len(quote.get("volume") or []) else None)
        if close is None or close <= 0:
            continue
        trade_date = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).date()
        rows.append(
            {
                "date": trade_date.isoformat(),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )

    if len(rows) < 90:
        raise ValueError("At least 90 trading days of history are needed for a useful forecast.")

    meta = result.get("meta", {})
    return {
        "symbol": meta.get("symbol") or symbol,
        "name": meta.get("longName") or meta.get("shortName") or meta.get("symbol") or symbol,
        "currency": meta.get("currency") or "",
        "exchange": meta.get("exchangeName") or meta.get("fullExchangeName") or "",
        "instrument": meta.get("instrumentType") or "",
        "rows": rows,
    }


def clean_float(value) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    except (TypeError, ValueError):
        return None


def clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return statistics.pstdev(values)


def log_returns(closes: list[float]) -> list[float]:
    returns = []
    for idx in range(1, len(closes)):
        previous = closes[idx - 1]
        current = closes[idx]
        if previous > 0 and current > 0:
            returns.append(math.log(current / previous))
    return returns


def sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return mean(values[-window:])


def ema_series(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def ewma(values: list[float], span: int) -> float:
    series = ema_series(values, span)
    return series[-1] if series else 0.0


def regression_slope(log_prices: list[float]) -> float:
    n = len(log_prices)
    if n < 3:
        return 0.0
    x_mean = (n - 1) / 2
    y_mean = mean(log_prices)
    numerator = sum((idx - x_mean) * (value - y_mean) for idx, value in enumerate(log_prices))
    denominator = sum((idx - x_mean) ** 2 for idx in range(n))
    return numerator / denominator if denominator else 0.0


def fit_ar1(returns: list[float]) -> float:
    sample = returns[-180:]
    if len(sample) < 30:
        return mean(returns[-20:]) if returns else 0.0
    xs = sample[:-1]
    ys = sample[1:]
    x_mean = mean(xs)
    y_mean = mean(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return y_mean
    beta = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
    alpha = y_mean - beta * x_mean
    return alpha + beta * sample[-1]


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    recent = closes[-(period + 1) :]
    gains = []
    losses = []
    for idx in range(1, len(recent)):
        change = recent[idx] - recent[idx - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
    avg_gain = mean(gains)
    avg_loss = mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd_values(closes: list[float]) -> tuple[float | None, float | None, float | None]:
    if len(closes) < 35:
        return None, None, None
    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    macd_line = [short - long for short, long in zip(ema12[-len(ema26) :], ema26)]
    signal_series = ema_series(macd_line, 9)
    line = macd_line[-1]
    signal = signal_series[-1] if signal_series else None
    hist = line - signal if signal is not None else None
    return line, signal, hist


def analog_next_return(returns: list[float], pattern_length: int = 20, neighbors: int = 12) -> float:
    if len(returns) < pattern_length * 4:
        return mean(returns[-20:])
    current = returns[-pattern_length:]
    current_mean = mean(current)
    current_std = stdev(current) or 1e-6
    current_z = [(value - current_mean) / current_std for value in current]
    candidates = []
    stop = len(returns) - pattern_length - 1
    for start in range(0, stop):
        pattern = returns[start : start + pattern_length]
        future = returns[start + pattern_length]
        pattern_mean = mean(pattern)
        pattern_std = stdev(pattern) or 1e-6
        pattern_z = [(value - pattern_mean) / pattern_std for value in pattern]
        distance = math.sqrt(sum((a - b) ** 2 for a, b in zip(current_z, pattern_z)) / pattern_length)
        candidates.append((distance, future))
    if not candidates:
        return mean(returns[-20:])
    candidates.sort(key=lambda item: item[0])
    top = candidates[:neighbors]
    weights = [1 / (distance + 0.05) for distance, _ in top]
    return sum(weight * future for weight, (_, future) in zip(weights, top)) / sum(weights)


def daily_volatility(returns: list[float]) -> float:
    sample = returns[-252:] if len(returns) >= 30 else returns
    vol = stdev(sample)
    if vol == 0 and returns:
        vol = stdev(returns)
    return max(vol, 0.002)


def cap_prediction(prediction: float, volatility: float) -> float:
    cap = min(0.04, max(0.008, volatility * 2.5))
    return clamp(prediction, -cap, cap)


def daily_model_returns(
    closes: list[float],
    dates: list[str] | None = None,
    include_analog: bool = True,
) -> dict[str, float]:
    returns = log_returns(closes)
    if len(closes) < 40 or not returns:
        baseline = mean(returns[-20:]) if returns else 0.0
        return {"baseline": baseline}

    volatility = daily_volatility(returns)
    last_price = closes[-1]
    models: dict[str, float] = {}

    for lookback in (63, 126, 252):
        if len(closes) >= max(lookback, 30):
            log_prices = [math.log(value) for value in closes[-lookback:] if value > 0]
            models[f"trend_{lookback}d"] = regression_slope(log_prices)

    models["ew_momentum"] = ewma(returns[-90:], 20)
    models["ar1_returns"] = fit_ar1(returns)

    ma120 = sma(closes, 120) or sma(closes, min(60, len(closes)))
    if ma120 and ma120 > 0:
        gap = math.log(ma120 / last_price)
        models["mean_reversion"] = gap * 0.035 + mean(returns[-60:]) * 0.2

    rsi_value = rsi(closes) or 50.0
    macd_line, macd_signal, macd_hist = macd_values(closes)
    ma20 = sma(closes, 20) or last_price
    ma60 = sma(closes, 60) or ma20
    score = 0.0
    score += 0.35 if last_price > ma20 else -0.35
    score += 0.25 if ma20 > ma60 else -0.25
    if macd_hist is not None:
        score += 0.25 if macd_hist > 0 else -0.25
    if rsi_value < 30:
        score += 0.35
    elif rsi_value > 70:
        score -= 0.35
    models["technical_score"] = score * volatility * 0.12

    if dates:
        try:
            weekdays = [dt.date.fromisoformat(day).weekday() for day in dates]
            current_weekday = weekdays[-1]
            weekday_returns = [
                ret for ret, weekday in zip(returns, weekdays[1:]) if weekday == current_weekday
            ]
            if len(weekday_returns) >= 10:
                models["weekday_seasonality"] = mean(weekday_returns[-80:])
        except ValueError:
            pass

    if include_analog:
        models["historical_analogs"] = analog_next_return(returns)
    models["long_run_drift"] = mean(returns[-252:]) if len(returns) >= 252 else mean(returns)

    return {name: cap_prediction(value, volatility) for name, value in models.items()}


def backtest_weights(closes: list[float], dates: list[str]) -> dict:
    current_models = daily_model_returns(closes, dates)
    names = list(current_models.keys())
    if len(closes) < 140:
        equal = 1 / len(names)
        return {
            "weights": {name: equal for name in names},
            "methodStats": {},
            "ensemble": {"directionalAccuracy": None, "rmse": None, "samples": 0},
        }

    start = max(90, len(closes) - 140)
    samples_by_method: dict[str, list[tuple[float, float]]] = defaultdict(list)
    ensemble_rows = []

    for idx in range(start, len(closes) - 1):
        sub_closes = closes[: idx + 1]
        sub_dates = dates[: idx + 1]
        actual = math.log(closes[idx + 1] / closes[idx])
        predictions = daily_model_returns(sub_closes, sub_dates, include_analog=False)
        for name in names:
            if name in predictions:
                samples_by_method[name].append((predictions[name], actual))
        ensemble_rows.append((predictions, actual))

    method_stats = {}
    raw_weights = {}
    for name in names:
        samples = samples_by_method.get(name, [])
        if len(samples) < 20:
            continue
        errors = [pred - actual for pred, actual in samples]
        rmse = math.sqrt(mean([err * err for err in errors]))
        hits = [
            1
            for pred, actual in samples
            if (pred > 0 and actual > 0) or (pred < 0 and actual < 0) or (abs(pred) < 1e-8 and abs(actual) < 1e-8)
        ]
        hit_rate = len(hits) / len(samples)
        stability = clamp(hit_rate / 0.5, 0.55, 1.45)
        raw_weights[name] = stability / ((rmse + 0.00075) ** 2)
        method_stats[name] = {
            "rmse": rmse,
            "directionalAccuracy": hit_rate,
            "samples": len(samples),
        }

    if not raw_weights:
        raw_weights = {name: 1 for name in names}

    total = sum(raw_weights.values())
    weights = {name: raw_weights.get(name, 0.0) / total for name in names}

    ensemble_predictions = []
    for predictions, actual in ensemble_rows:
        available_weight = sum(weights.get(name, 0.0) for name in predictions.keys())
        if available_weight <= 0:
            continue
        pred = sum(weights.get(name, 0.0) * value for name, value in predictions.items()) / available_weight
        ensemble_predictions.append((pred, actual))

    if ensemble_predictions:
        errors = [pred - actual for pred, actual in ensemble_predictions]
        rmse = math.sqrt(mean([err * err for err in errors]))
        hits = [
            1
            for pred, actual in ensemble_predictions
            if (pred > 0 and actual > 0) or (pred < 0 and actual < 0) or (abs(pred) < 1e-8 and abs(actual) < 1e-8)
        ]
        ensemble = {
            "directionalAccuracy": len(hits) / len(ensemble_predictions),
            "rmse": rmse,
            "samples": len(ensemble_predictions),
        }
    else:
        ensemble = {"directionalAccuracy": None, "rmse": None, "samples": 0}

    return {"weights": weights, "methodStats": method_stats, "ensemble": ensemble}


def next_market_dates(last_date: str, horizon: int) -> list[str]:
    current = dt.date.fromisoformat(last_date)
    dates = []
    while len(dates) < horizon:
        current += dt.timedelta(days=1)
        if current.weekday() < 5:
            dates.append(current.isoformat())
    return dates


def technical_regime_score(closes: list[float], signals: dict) -> float:
    if not closes:
        return 0.0
    last = closes[-1]
    ma = signals.get("movingAverages", {})
    score = 0.0
    for key, weight in (("ma5", 0.12), ("ma20", 0.22), ("ma60", 0.2), ("ma200", 0.14)):
        value = ma.get(key)
        if value:
            score += weight if last >= value else -weight
    if ma.get("ma20") and ma.get("ma60"):
        score += 0.16 if ma["ma20"] >= ma["ma60"] else -0.16
    if ma.get("ma60") and ma.get("ma200"):
        score += 0.12 if ma["ma60"] >= ma["ma200"] else -0.12

    rsi_value = signals.get("rsi14")
    if rsi_value is not None:
        if rsi_value < 28:
            score += 0.24
        elif rsi_value < 42:
            score += 0.08
        elif rsi_value > 76:
            score -= 0.24
        elif rsi_value > 62:
            score += 0.08

    macd_hist = (signals.get("macd") or {}).get("histogram")
    if macd_hist is not None:
        score += 0.18 if macd_hist > 0 else -0.18

    support = signals.get("support60")
    resistance = signals.get("resistance60")
    if support and resistance and resistance > support:
        position = (last - support) / (resistance - support)
        if position < 0.2:
            score += 0.12
        elif position > 0.82:
            score -= 0.12

    volume_ratio = signals.get("volumeRatio20v90")
    if volume_ratio and volume_ratio > 1.15:
        score *= 1.08
    elif volume_ratio and volume_ratio < 0.75:
        score *= 0.92
    return clamp(score, -1, 1)


def global_breadth_score(global_indices: dict | None) -> float:
    if not global_indices:
        return 0.0
    positive = global_indices.get("positiveCount") or 0
    negative = global_indices.get("negativeCount") or 0
    active = positive + negative
    return clamp((positive - negative) / active, -1, 1) if active else 0.0


def macro_risk_score(macro_indicators: dict | None) -> float:
    if not macro_indicators:
        return 0.0
    score = 0.0
    for item in macro_indicators.get("items", []):
        symbol = item.get("symbol")
        value = item.get("last")
        change = item.get("changePercent")
        if symbol == "^VIX" and value is not None:
            if value < 18:
                score += 0.28
            elif value < 25:
                score -= 0.08
            else:
                score -= 0.32
            if change is not None and change < -0.04:
                score += 0.08
            elif change is not None and change > 0.05:
                score -= 0.1
        elif symbol == "CL=F" and change is not None:
            if change > 0.035:
                score -= 0.12
            elif change < -0.03:
                score += 0.06
    return clamp(score, -1, 1)


def market_sentinel_score(market_sentinel: dict | None) -> float:
    if not market_sentinel:
        return 0.0
    bullish = market_sentinel.get("bullishPercent")
    bearish = market_sentinel.get("bearishPercent")
    if bullish is None or bearish is None:
        return 0.0
    return clamp(bullish - bearish, -1, 1)


def build_regime_adjustments(
    closes: list[float],
    signals: dict | None,
    global_indices: dict | None,
    macro_indicators: dict | None,
    market_sentinel: dict | None,
    volatility: float,
) -> dict:
    signals = signals or {}
    technical_score = technical_regime_score(closes, signals)
    breadth_score = global_breadth_score(global_indices)
    macro_score = macro_risk_score(macro_indicators)
    sentinel_score = market_sentinel_score(market_sentinel)
    components = {
        "technical_regime": technical_score * volatility * 0.16,
        "global_breadth": breadth_score * volatility * 0.08,
        "macro_risk": macro_score * volatility * 0.07,
        "market_sentinel": sentinel_score * volatility * 0.07,
    }
    combined = sum(components.values())
    cap = volatility * 0.18
    aligned = [value for value in (technical_score, breadth_score, macro_score, sentinel_score) if abs(value) >= 0.12]
    same_direction = len(aligned) >= 3 and (
        all(value > 0 for value in aligned) or all(value < 0 for value in aligned)
    )
    return {
        "components": {name: clamp(value, -cap, cap) for name, value in components.items()},
        "dailyImpact": clamp(combined, -cap, cap),
        "scores": {
            "technical": technical_score,
            "globalBreadth": breadth_score,
            "macroRisk": macro_score,
            "marketSentinel": sentinel_score,
        },
        "confidenceBoost": bool(same_direction),
    }


def forecast_path(
    closes: list[float],
    dates: list[str],
    horizon: int,
    weights: dict[str, float],
    ensemble_metrics: dict,
    news_context: dict | None = None,
    signals: dict | None = None,
    global_indices: dict | None = None,
    macro_indicators: dict | None = None,
    market_sentinel: dict | None = None,
    external_context: dict | None = None,
) -> tuple[list[dict], dict]:
    returns = log_returns(closes)
    last_price = closes[-1]
    last_log = math.log(last_price)
    models = daily_model_returns(closes, dates)
    volatility = daily_volatility(returns)
    news_daily_impact = 0.0
    working_weights = dict(weights)
    if news_context and news_context.get("articleCount", 0) > 0:
        news_daily_impact = clamp(
            news_context.get("sentiment", 0.0) * volatility * 0.05,
            -volatility * 0.08,
            volatility * 0.08,
        )
        models["world_news_sentiment"] = news_daily_impact
        working_weights["world_news_sentiment"] = 0.10

    external_daily_impact = 0.0
    if external_context:
        external_daily_impact = clamp(
            external_context.get("signal", 0.0) * volatility * 0.055,
            -volatility * 0.07,
            volatility * 0.07,
        )
        external_context["dailyImpact"] = external_daily_impact
        models["external_provider_signal"] = external_daily_impact
        working_weights["external_provider_signal"] = 0.055

    regime = build_regime_adjustments(closes, signals, global_indices, macro_indicators, market_sentinel, volatility)
    for name, value in regime["components"].items():
        models[name] = value
    working_weights.update(
        {
            "technical_regime": 0.09,
            "global_breadth": 0.045,
            "macro_risk": 0.045,
            "market_sentinel": 0.045,
        }
    )

    weighted_daily = sum(weights.get(name, 0.0) * value for name, value in models.items())
    if sum(working_weights.values()) > 0:
        weighted_daily = sum(working_weights.get(name, 0.0) * value for name, value in models.items())
        weighted_daily /= sum(working_weights.values())
    rmse = ensemble_metrics.get("rmse") or volatility
    calibration = clamp(rmse / volatility, 0.85, 1.7) if volatility else 1.0
    future_dates = next_market_dates(dates[-1], horizon)

    path = []
    for day, future_date in enumerate(future_dates, start=1):
        damping = 1 / (1 + day / 180)
        log_prediction = last_log + weighted_daily * day * damping
        interval = 1.28 * volatility * math.sqrt(day) * calibration
        path.append(
            {
                "date": future_date,
                "predicted": math.exp(log_prediction),
                "lower": math.exp(log_prediction - interval),
                "upper": math.exp(log_prediction + interval),
            }
        )

    terminal_return = (path[-1]["predicted"] / last_price) - 1 if path else 0.0
    uncertainty = ((path[-1]["upper"] - path[-1]["lower"]) / path[-1]["predicted"]) / 2 if path else 0.0
    probability_up = normal_cdf((math.log(path[-1]["predicted"] / last_price)) / (volatility * math.sqrt(horizon) * calibration)) if path else 0.5
    confidence = "Low"
    hit_rate = ensemble_metrics.get("directionalAccuracy")
    if hit_rate is not None and len(closes) >= 252:
        if hit_rate >= 0.56 and uncertainty < 0.22:
            confidence = "Medium"
        elif hit_rate >= 0.51:
            confidence = "Medium-low"

    return path, {
        "dailyDrift": weighted_daily,
        "expectedReturn": terminal_return,
        "probabilityUp": probability_up,
        "dailyVolatility": volatility,
        "annualizedVolatility": volatility * math.sqrt(252),
        "uncertainty": uncertainty,
        "confidence": confidence,
        "calibration": calibration,
        "newsDailyImpact": news_daily_impact,
        "externalDailyImpact": external_daily_impact,
        "regimeDailyImpact": regime["dailyImpact"],
        "regimeScores": regime["scores"],
        "regimeConfidenceBoost": regime["confidenceBoost"],
        "modelForecasts": models,
    }


def normal_cdf(value: float) -> float:
    return 0.5 * (1 + math.erf(value / math.sqrt(2)))


def strip_markup(text: str | None) -> str:
    clean = re.sub(r"<[^>]+>", " ", text or "")
    clean = html.unescape(clean)
    return re.sub(r"\s+", " ", clean).strip()


def parse_rss_date(raw_date: str | None) -> str | None:
    if not raw_date:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw_date)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


class VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag in {"br", "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        if tag in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip_depth:
            clean = html.unescape(data).strip()
            if clean:
                self.parts.append(clean)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


def fetch_html_text(url: str, timeout: int = 12) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 stock-forecast-app",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="ignore")
    parser = VisibleTextParser()
    parser.feed(payload)
    return parser.text()


def hk_numeric_symbol(symbol: str) -> str | None:
    match = re.search(r"(\d{1,5})", symbol or "")
    if not match:
        return None
    return match.group(1).zfill(5)


def tradingview_symbol(symbol: str, market: str) -> str:
    hk_code = hk_numeric_symbol(symbol)
    if market == "HK" and hk_code:
        return f"HKEX:{hk_code}"
    return re.sub(r"[^A-Za-z0-9._-]", "", symbol).upper()


def provider_source_links(symbol: str, market: str) -> dict:
    hk_code = hk_numeric_symbol(symbol)
    tv_symbol = tradingview_symbol(symbol, market)
    links = {
        "tradingView": f"https://www.tradingview.com/chart/?symbol={urllib.parse.quote(tv_symbol)}",
    }
    if market == "HK" and hk_code:
        links.update(
            {
                "aaStocksQuote": f"https://www.aastocks.com/en/stocks/quote/detail-quote.aspx?symbol={hk_code}",
                "aaStocksTechnical": f"https://www.aastocks.com/en/stocks/quote/detailchart.aspx?symbol={hk_code}",
                "aaStocksMoneyFlow": f"https://www.aastocks.com/en/stocks/analysis/moneyflow.aspx?symbol={hk_code}",
                "aaStocksNews": f"https://www.aastocks.com/en/stocks/news/aafn/company-news?symbol={hk_code}",
                "etnetQuote": f"https://www.etnet.com.hk/www/eng/stocks/realtime/quote.php?code={hk_code}",
                "etnetNews": f"https://www.etnet.com.hk/www/eng/stocks/realtime/quote_news.php?code={hk_code}",
            }
        )
    return links


def finnhub_token() -> str | None:
    return os.environ.get("FINNHUB_API_KEY") or os.environ.get("FINNHUB_TOKEN")


def finnhub_request(endpoint: str, params: dict, timeout: int = 12):
    token = finnhub_token()
    if not token:
        raise ValueError("Finnhub token not configured. Set FINNHUB_API_KEY or FINNHUB_TOKEN.")
    query = dict(params)
    query["token"] = token
    url = f"https://finnhub.io/api/v1/{endpoint}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 stock-forecast-app",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def iso_from_unix(timestamp: int | float | None) -> str | None:
    try:
        value = int(timestamp)
        if value <= 0:
            return None
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def fetch_finnhub_quote(symbol: str) -> dict:
    payload = finnhub_request("quote", {"symbol": symbol})
    last = clean_float(payload.get("c"))
    previous = clean_float(payload.get("pc"))
    if last is None or last <= 0:
        raise ValueError("Finnhub returned no current quote.")
    timestamp = clean_float(payload.get("t"))
    published = iso_from_unix(timestamp)
    return {
        "provider": "Finnhub",
        "symbol": symbol,
        "last": last,
        "previousClose": previous,
        "change": clean_float(payload.get("d")) if payload.get("d") is not None else price_change(last, previous),
        "changePercent": (clean_float(payload.get("dp")) / 100) if clean_float(payload.get("dp")) is not None else pct_change(last, previous),
        "open": clean_float(payload.get("o")),
        "dayHigh": clean_float(payload.get("h")),
        "dayLow": clean_float(payload.get("l")),
        "timestamp": published,
        "date": published[:10] if published else dt.datetime.now(dt.timezone.utc).date().isoformat(),
    }


def map_finnhub_news_item(item: dict, category: str) -> dict | None:
    title = strip_markup(item.get("headline") or item.get("title"))
    if not title:
        return None
    summary = strip_markup(item.get("summary"))
    return {
        "title": title,
        "link": item.get("url") or "",
        "published": iso_from_unix(item.get("datetime")),
        "summary": summary,
        "category": category,
        "source": item.get("source") or "Finnhub",
        "provider": "Finnhub",
        "sentiment": score_news_text(f"{title} {summary}"),
    }


def fetch_finnhub_company_news(symbol: str, limit: int = 10) -> list[dict]:
    end_date = dt.datetime.now(dt.timezone.utc).date()
    start_date = end_date - dt.timedelta(days=45)
    payload = finnhub_request(
        "company-news",
        {"symbol": symbol, "from": start_date.isoformat(), "to": end_date.isoformat()},
    )
    articles = []
    for item in payload or []:
        article = map_finnhub_news_item(item, "ticker")
        if article:
            articles.append(article)
        if len(articles) >= limit:
            break
    return articles


def fetch_finnhub_general_news(limit: int = 8) -> list[dict]:
    payload = finnhub_request("news", {"category": "general"})
    articles = []
    for item in payload or []:
        article = map_finnhub_news_item(item, "world")
        if article:
            articles.append(article)
        if len(articles) >= limit:
            break
    return articles


def fetch_finnhub_context(symbol: str) -> dict:
    configured = bool(finnhub_token())
    context = {
        "configured": configured,
        "quote": None,
        "articles": [],
        "errors": [],
        "quoteStatus": "Not configured" if not configured else "Unavailable",
        "newsStatus": "Not configured" if not configured else "Unavailable",
    }
    if not configured:
        return context
    try:
        context["quote"] = fetch_finnhub_quote(symbol)
        context["quoteStatus"] = "Finnhub live quote"
    except Exception as exc:
        context["errors"].append(f"quote: {exc}")
    try:
        company_articles = fetch_finnhub_company_news(symbol)
        general_articles = fetch_finnhub_general_news()
        context["articles"] = company_articles + general_articles
        context["newsStatus"] = f"Finnhub news ({len(context['articles'])})"
    except Exception as exc:
        context["errors"].append(f"news: {exc}")
    return context


def merge_news_articles(articles: list[dict]) -> list[dict]:
    unique_articles = []
    seen = set()
    for article in articles:
        key = (article.get("link") or article.get("title") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique_articles.append(article)
    unique_articles.sort(key=lambda item: item.get("published") or "", reverse=True)
    return unique_articles


def fetch_google_news(query: str, category: str, limit: int = 8) -> list[dict]:
    encoded_query = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 stock-forecast-app",
            "Accept": "application/rss+xml, application/xml, text/xml",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = response.read()

    root = ET.fromstring(payload)
    articles = []
    seen = set()
    for item in root.findall("./channel/item"):
        title = strip_markup(item.findtext("title"))
        link = item.findtext("link") or ""
        published = parse_rss_date(item.findtext("pubDate"))
        summary = strip_markup(item.findtext("description"))
        if not title or title in seen:
            continue
        seen.add(title)
        articles.append(
            {
                "title": title,
                "link": link,
                "published": published,
                "summary": summary,
                "category": category,
                "sentiment": score_news_text(f"{title} {summary}"),
            }
        )
        if len(articles) >= limit:
            break
    return articles


def extract_recent_phrases(text: str, terms: list[str], limit: int = 5) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+|\s{2,}", text)
    hits = []
    for sentence in sentences:
        clean = sentence.strip(" -|")
        if len(clean) < 18 or len(clean) > 180:
            continue
        lower = clean.lower()
        if any(term.lower() in lower for term in terms):
            hits.append(clean)
        if len(hits) >= limit:
            break
    return hits


def parse_percent_values(text: str) -> list[float]:
    values = []
    for raw in re.findall(r"([+-]?\d+(?:\.\d+)?)\s*%", text):
        try:
            values.append(float(raw) / 100)
        except ValueError:
            pass
    return values


def fetch_aastocks_context(symbol: str, market: str, links: dict) -> dict:
    if market != "HK" or not links.get("aaStocksQuote"):
        return {"provider": "AAStocks", "status": "HK symbols only", "score": 0.0, "items": [], "links": links}
    context = {"provider": "AAStocks", "status": "Unavailable", "score": 0.0, "items": [], "links": links, "errors": []}
    pages = [
        ("Quote", links["aaStocksQuote"]),
        ("Technical", links["aaStocksTechnical"]),
        ("Money Flow", links["aaStocksMoneyFlow"]),
        ("News", links["aaStocksNews"]),
    ]
    text_blocks = []
    for label, url in pages:
        try:
            text = fetch_html_text(url, timeout=6)
            if text:
                text_blocks.append((label, text))
                context["items"].append({"label": label, "state": "Connected", "detail": "Public page parsed"})
        except Exception as exc:
            context["errors"].append(f"{label}: {exc}")
            context["items"].append({"label": label, "state": "Fallback", "detail": "Open source link"})
    combined = " ".join(text for _, text in text_blocks)
    score = score_news_text(combined)
    phrases = extract_recent_phrases(
        combined,
        ["buy", "sell", "positive", "negative", "risen", "dropped", "money flow", "short selling", "technical", "target"],
        4,
    )
    context["score"] = score
    context["signal"] = "Bullish" if score >= 0.16 else "Bearish" if score <= -0.16 else "Neutral"
    context["phrases"] = phrases
    context["status"] = "Connected" if text_blocks else "Links only"
    return context


def fetch_etnet_context(symbol: str, market: str, links: dict) -> dict:
    if market != "HK" or not links.get("etnetQuote"):
        return {"provider": "ETNet", "status": "HK symbols only", "score": 0.0, "items": [], "links": links}
    context = {"provider": "ETNet", "status": "Unavailable", "score": 0.0, "items": [], "links": links, "errors": []}
    try:
        text = fetch_html_text(links["etnetQuote"], timeout=6)
        percent_values = parse_percent_values(text)
        positive = sum(1 for value in percent_values if value > 0)
        negative = sum(1 for value in percent_values if value < 0)
        breadth = (positive - negative) / (positive + negative) if positive + negative else 0.0
        quote_mentions = extract_recent_phrases(text, ["TENCENT", "Nominal", "%Change", "Top 5 Turnover", "Gainers", "Losers"], 5)
        context.update(
            {
                "status": "Connected",
                "score": clamp(breadth, -1, 1),
                "signal": "Bullish breadth" if breadth >= 0.16 else "Bearish breadth" if breadth <= -0.16 else "Mixed breadth",
                "phrases": quote_mentions,
                "items": [
                    {"label": "Quote board", "state": "Connected", "detail": "Quote and hot-list page parsed"},
                    {"label": "Breadth sample", "state": f"{positive} up / {negative} down", "detail": "From public percentage fields"},
                ],
            }
        )
    except Exception as exc:
        context["errors"].append(str(exc))
        context["status"] = "Links only"
        context["items"] = [{"label": "Quote board", "state": "Fallback", "detail": "Open source link"}]
    return context


def build_tradingview_context(symbol: str, market: str, links: dict) -> dict:
    return {
        "provider": "TradingView",
        "status": "Chart link ready",
        "score": 0.0,
        "signal": "External chart confirmation",
        "items": [
            {"label": "Advanced chart", "state": tradingview_symbol(symbol, market), "detail": "Use for visual confirmation"},
            {"label": "Data note", "state": "Widget/datafeed", "detail": "TradingView charting library needs a data source"},
        ],
        "phrases": [],
        "links": links,
    }


def build_external_provider_context(symbol: str, market: str) -> dict:
    links = provider_source_links(symbol, market)
    tradingview = build_tradingview_context(symbol, market, links)
    aa_context = fetch_aastocks_context(symbol, market, links)
    etnet_context = fetch_etnet_context(symbol, market, links)
    contexts = [tradingview, aa_context, etnet_context]
    scores = [item.get("score", 0.0) for item in contexts if item.get("provider") != "TradingView"]
    signal = clamp(sum(scores) / len(scores), -1, 1) if scores else 0.0
    ready_statuses = {"Connected", "Chart link ready"}
    connected = sum(1 for item in contexts if item.get("status") in ready_statuses)
    return {
        "signal": signal,
        "dailyImpact": 0.0,
        "status": f"{connected}/3 sources ready",
        "providers": contexts,
        "links": links,
        "notes": [
            "TradingView is used as chart confirmation, not as a raw prediction-data API.",
            "AAStocks and ETNet public pages are treated as secondary confirmation signals.",
        ],
    }


def score_news_text(text: str) -> float:
    tokens = re.findall(r"[a-zA-Z][a-zA-Z'-]+", text.lower())
    if not tokens:
        return 0.0
    positive = sum(1 for token in tokens if token in NEWS_POSITIVE_TERMS)
    negative = sum(1 for token in tokens if token in NEWS_NEGATIVE_TERMS)
    score = (positive - negative) / max(positive + negative, 3)
    return clamp(score, -1, 1)


def weighted_news_sentiment(articles: list[dict]) -> float:
    if not articles:
        return 0.0
    now = dt.datetime.now(dt.timezone.utc)
    weighted = []
    weights = []
    for article in articles:
        recency_weight = 0.65
        if article.get("published"):
            try:
                published = dt.datetime.fromisoformat(article["published"])
                age_hours = max(0, (now - published).total_seconds() / 3600)
                recency_weight = 1 / (1 + age_hours / 48)
            except ValueError:
                pass
        category_weight = 1.15 if article.get("category") == "ticker" else 1.0
        weight = recency_weight * category_weight
        weighted.append(article.get("sentiment", 0.0) * weight)
        weights.append(weight)
    return clamp(sum(weighted) / sum(weights), -1, 1) if sum(weights) else 0.0


def summarize_news_bias(score: float) -> str:
    if score >= 0.18:
        return "Positive"
    if score <= -0.18:
        return "Negative"
    return "Neutral"


def build_news_context(symbol: str, name: str, market: str, finnhub_context: dict | None = None) -> dict:
    base_symbol = symbol.replace(".HK", "")
    company_query = f'"{name}" OR {base_symbol} stock'
    if market == "HK":
        macro_query = "Hong Kong China stock market economy rates policy"
    else:
        macro_query = "global stock market economy inflation rates earnings"

    articles = []
    errors = []
    sources = ["Google News"]
    if finnhub_context:
        articles.extend(finnhub_context.get("articles") or [])
        errors.extend(finnhub_context.get("errors") or [])
        if finnhub_context.get("articles"):
            sources.insert(0, "Finnhub")

    for query, category, limit in (
        (company_query, "ticker", 8),
        (macro_query, "world", 10),
    ):
        try:
            articles.extend(fetch_google_news(query, category, limit))
        except Exception as exc:
            errors.append(f"{category}: {exc}")

    unique_articles = merge_news_articles(articles)
    sentiment = weighted_news_sentiment(unique_articles)
    return {
        "sentiment": sentiment,
        "bias": summarize_news_bias(sentiment),
        "articleCount": len(unique_articles),
        "articles": unique_articles[:12],
        "errors": errors,
        "sources": sources,
        "providerStatus": {
            "finnhubQuote": (finnhub_context or {}).get("quoteStatus", "Not configured"),
            "finnhubNews": (finnhub_context or {}).get("newsStatus", "Not configured"),
        },
    }


def latest_quote(symbol: str, label: str) -> dict:
    history = fetch_yahoo_history(symbol, "6mo")
    rows = history["rows"]
    last = rows[-1]["close"]
    previous = rows[-2]["close"] if len(rows) >= 2 else None
    return {
        "symbol": symbol,
        "label": label,
        "currency": history.get("currency", ""),
        "date": rows[-1]["date"],
        "last": last,
        "change": price_change(last, previous),
        "changePercent": pct_change(last, previous),
    }


def quote_or_error(symbol: str, label: str) -> dict:
    try:
        return latest_quote(symbol, label)
    except Exception as exc:
        return {"symbol": symbol, "label": label, "error": str(exc)}


def fetch_quote_batch(symbols: list[str]) -> dict[str, dict]:
    if not symbols:
        return {}
    encoded = urllib.parse.quote(",".join(symbols), safe=",=^.")
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={encoded}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 stock-forecast-app",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8"))
    result = payload.get("quoteResponse", {}).get("result") or []
    quotes = {}
    for item in result:
        symbol = item.get("symbol")
        if not symbol:
            continue
        last = clean_float(item.get("regularMarketPrice"))
        previous = clean_float(item.get("regularMarketPreviousClose"))
        change_percent = clean_float(item.get("regularMarketChangePercent"))
        quotes[symbol] = {
            "symbol": symbol,
            "label": item.get("shortName") or item.get("longName") or symbol,
            "currency": item.get("currency") or "",
            "last": last,
            "change": price_change(last, previous),
            "changePercent": change_percent / 100 if change_percent is not None else pct_change(last, previous),
        }
    return quotes


def fetch_quote_from_history(symbol: str) -> dict:
    history = fetch_yahoo_history(symbol, "6mo")
    rows = history.get("rows") or []
    if len(rows) < 2:
        raise ValueError("Not enough recent quote history")
    last = rows[-1]["close"]
    previous = rows[-2]["close"]
    return {
        "symbol": history.get("symbol") or symbol,
        "label": history.get("name") or symbol,
        "currency": history.get("currency") or "",
        "last": last,
        "change": price_change(last, previous),
        "changePercent": pct_change(last, previous),
    }


def build_global_indices_board() -> dict:
    definitions = [
        ("SPX", "^GSPC"),
        ("NASDAQ", "^IXIC"),
        ("DOW", "^DJI"),
        ("RUSSELL", "^RUT"),
        ("VIX", "^VIX"),
        ("FTSE", "^FTSE"),
        ("DAX", "^GDAXI"),
        ("CAC", "^FCHI"),
        ("NIKKEI", "^N225"),
        ("HSI", "^HSI"),
        ("SSE", "000001.SS"),
        ("KOSPI", "^KS11"),
        ("TSX", "^GSPTSE"),
        ("ASX", "^AXJO"),
        ("GOLD", "GC=F"),
        ("OIL", "CL=F"),
    ]
    symbols = [symbol for _, symbol in definitions]
    quotes = {}
    feed_error = None
    try:
        quotes = fetch_quote_batch(symbols)
    except Exception as exc:
        feed_error = str(exc)

    missing_symbols = [symbol for symbol in symbols if symbol not in quotes or quotes[symbol].get("last") is None]
    if missing_symbols:
        with ThreadPoolExecutor(max_workers=min(8, len(missing_symbols))) as pool:
            futures = {pool.submit(fetch_quote_from_history, symbol): symbol for symbol in missing_symbols}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    quotes[symbol] = future.result()
                except Exception as exc:
                    quotes[symbol] = {"symbol": symbol, "error": str(exc)}

    items = []
    positive_count = 0
    negative_count = 0
    for label, symbol in definitions:
        quote = quotes.get(symbol)
        if not quote or quote.get("last") is None:
            items.append({"label": label, "symbol": symbol, "error": quote.get("error") if quote else "No live quote"})
            continue
        change_percent = quote.get("changePercent")
        if change_percent is not None and change_percent >= 0:
            positive_count += 1
        elif change_percent is not None:
            negative_count += 1
        items.append(
            {
                "label": label,
                "symbol": symbol,
                "last": quote["last"],
                "currency": quote.get("currency", ""),
                "changePercent": change_percent,
            }
        )

    status = "AI predicts mixed action"
    if positive_count > negative_count + 3:
        status = "AI predicts risk-on action"
    elif negative_count > positive_count + 3:
        status = "AI predicts risk-off action"
    elif positive_count == 0 and negative_count == 0:
        status = "Unavailable"
    return {
        "items": items,
        "status": status,
        "positiveCount": positive_count,
        "negativeCount": negative_count,
        "feedFallback": bool(feed_error),
    }


def macro_status(symbol: str, value: float | None, change_percent: float | None) -> str:
    if value is None:
        return "Unavailable"
    if symbol == "^VIX":
        if value < 18:
            return "Healthy volatility range"
        if value < 25:
            return "Elevated volatility watch"
        return "Stress volatility regime"
    if symbol == "CL=F":
        if change_percent is not None and abs(change_percent) <= 0.03:
            return "Stabilized range"
        if change_percent is not None and change_percent > 0.03:
            return "Inflation pressure rising"
        return "Energy pressure easing"
    return "Active macro filter"


def build_macro_indicators() -> dict:
    items = [
        quote_or_error("CL=F", "WTI Crude Oil"),
        quote_or_error("^VIX", "CBOE Volatility Index"),
    ]
    for item in items:
        if not item.get("error"):
            item["status"] = macro_status(item["symbol"], item.get("last"), item.get("changePercent"))
    return {
        "items": items,
        "summary": "Crude oil tracks inflation pressure while VIX tracks cross-asset hedging stress.",
    }


def build_market_sentinel(market: str, news_context: dict) -> dict:
    index_symbol = "^HSI" if market == "HK" else "^GSPC"
    index_label = "Hang Seng Index" if market == "HK" else "S&P 500"
    index_quote = quote_or_error(index_symbol, index_label)
    index_return = index_quote.get("changePercent") if not index_quote.get("error") else 0.0
    news_sentiment = news_context.get("sentiment", 0.0)
    articles = news_context.get("articles", [])
    bullish_count = sum(1 for item in articles if item.get("sentiment", 0) > 0.12)
    bearish_count = sum(1 for item in articles if item.get("sentiment", 0) < -0.12)
    neutral_count = max(0, len(articles) - bullish_count - bearish_count)

    blended = clamp(news_sentiment * 0.72 + (index_return or 0) * 3.5, -1, 1)
    bullish_pct = clamp(0.5 + blended * 0.36, 0.05, 0.9)
    bearish_pct = clamp(0.5 - blended * 0.36, 0.05, 0.9)
    neutral_pct = clamp(1 - abs(blended) * 0.72, 0.1, 0.7)
    total_pct = bullish_pct + bearish_pct + neutral_pct
    bullish_pct /= total_pct
    bearish_pct /= total_pct
    neutral_pct /= total_pct

    consensus = "Neutral Consensus"
    if bullish_pct >= 0.45:
        consensus = "Bullish Consensus"
    elif bearish_pct >= 0.45:
        consensus = "Bearish Consensus"

    drivers = sorted(articles, key=lambda item: abs(item.get("sentiment", 0)), reverse=True)[:6]
    return {
        "market": market,
        "index": index_quote,
        "consensus": consensus,
        "bullishPercent": bullish_pct,
        "neutralPercent": neutral_pct,
        "bearishPercent": bearish_pct,
        "counts": {
            "bullish": bullish_count,
            "neutral": neutral_count,
            "bearish": bearish_count,
        },
        "drivers": drivers,
    }


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous <= 0:
        return None
    return (current / previous) - 1


def price_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return current - previous


def period_return(closes: list[float], days: int) -> float | None:
    if len(closes) <= days or closes[-days - 1] <= 0:
        return None
    return (closes[-1] / closes[-days - 1]) - 1


def max_drawdown(closes: list[float], window: int = 252) -> float:
    sample = closes[-window:] if len(closes) > window else closes[:]
    if not sample:
        return 0.0
    peak = sample[0]
    worst = 0.0
    for price in sample:
        peak = max(peak, price)
        if peak > 0:
            worst = min(worst, (price / peak) - 1)
    return worst


def sharpe_ratio(returns: list[float]) -> float | None:
    sample = returns[-252:] if len(returns) >= 30 else returns
    vol = stdev(sample)
    if not sample or vol == 0:
        return None
    return (mean(sample) / vol) * math.sqrt(252)


def percentile_position(value: float, low: float, high: float) -> float | None:
    if high <= low:
        return None
    return clamp((value - low) / (high - low), 0, 1)


def rows_with_live_quote(rows: list[dict], quote: dict | None) -> tuple[list[dict], bool]:
    if not quote or quote.get("last") is None:
        return rows, False
    quote_date = quote.get("date") or dt.datetime.now(dt.timezone.utc).date().isoformat()
    current = quote["last"]
    previous_close = quote.get("previousClose") or (rows[-1]["close"] if rows else current)
    open_price = quote.get("open") or previous_close or current
    high = max(value for value in (quote.get("dayHigh"), current, open_price, previous_close) if value is not None)
    low = min(value for value in (quote.get("dayLow"), current, open_price, previous_close) if value is not None)
    updated = [dict(row) for row in rows]
    if not updated:
        return [
            {
                "date": quote_date,
                "open": open_price,
                "high": high,
                "low": low,
                "close": current,
                "volume": 0,
            }
        ], True

    last_date = updated[-1]["date"]
    if quote_date == last_date:
        updated[-1].update(
            {
                "open": updated[-1].get("open") or open_price,
                "high": max(value for value in (updated[-1].get("high"), high, current) if value is not None),
                "low": min(value for value in (updated[-1].get("low"), low, current) if value is not None),
                "close": current,
            }
        )
        return updated, True

    if quote_date > last_date:
        updated.append(
            {
                "date": quote_date,
                "open": open_price,
                "high": high,
                "low": low,
                "close": current,
                "volume": 0,
            }
        )
        return updated, True

    return rows, False


def build_market_snapshot(rows: list[dict]) -> dict:
    closes = [row["close"] for row in rows]
    returns = log_returns(closes)
    volumes = [row.get("volume") or 0 for row in rows]
    last_row = rows[-1]
    previous_close = rows[-2]["close"] if len(rows) >= 2 else None
    last_price = closes[-1]
    high_52w = max(closes[-252:]) if len(closes) >= 20 else max(closes)
    low_52w = min(closes[-252:]) if len(closes) >= 20 else min(closes)
    avg_volume_20 = mean(volumes[-20:]) if len(volumes) >= 20 else mean(volumes)
    avg_volume_90 = mean(volumes[-90:]) if len(volumes) >= 90 else avg_volume_20

    return {
        "open": last_row.get("open"),
        "dayHigh": last_row.get("high"),
        "dayLow": last_row.get("low"),
        "previousClose": previous_close,
        "dayChange": price_change(last_price, previous_close),
        "dayChangePercent": pct_change(last_price, previous_close),
        "gapFromOpenPercent": pct_change(last_price, last_row.get("open")),
        "volume": last_row.get("volume"),
        "avgVolume20": avg_volume_20,
        "avgVolume90": avg_volume_90,
        "volumeRatio20v90": (avg_volume_20 / avg_volume_90) if avg_volume_90 else None,
        "high52w": high_52w,
        "low52w": low_52w,
        "position52w": percentile_position(last_price, low_52w, high_52w),
        "returns": {
            "1d": pct_change(last_price, previous_close),
            "1w": period_return(closes, 5),
            "1m": period_return(closes, 20),
            "3m": period_return(closes, 63),
            "6m": period_return(closes, 126),
            "1y": period_return(closes, 252),
        },
        "risk": {
            "maxDrawdown1y": max_drawdown(closes, 252),
            "sharpe1y": sharpe_ratio(returns),
            "realizedVolatility20d": stdev(returns[-20:]) * math.sqrt(252) if len(returns) >= 20 else None,
        },
    }


def score_label(score: float) -> str:
    if score >= 70:
        return "Strong"
    if score >= 57:
        return "Positive"
    if score <= 30:
        return "Weak"
    if score <= 43:
        return "Negative"
    return "Neutral"


def build_scores(summary: dict, signals: dict, snapshot: dict, ensemble: dict) -> dict:
    returns = snapshot.get("returns", {})
    ma = signals.get("movingAverages", {})
    last = summary["lastPrice"]
    rsi_value = signals.get("rsi14") or 50
    macd_hist = (signals.get("macd") or {}).get("histogram") or 0
    vol = summary.get("annualizedVolatility") or 0
    drawdown = abs((snapshot.get("risk") or {}).get("maxDrawdown1y") or 0)
    hit_rate = ensemble.get("directionalAccuracy")

    trend_score = 50
    if ma.get("ma20"):
        trend_score += 12 if last > ma["ma20"] else -12
    if ma.get("ma60"):
        trend_score += 10 if last > ma["ma60"] else -10
    if ma.get("ma200"):
        trend_score += 8 if last > ma["ma200"] else -8
    for key, weight in (("1m", 6), ("3m", 8), ("6m", 8)):
        value = returns.get(key)
        if value is not None:
            trend_score += weight if value > 0 else -weight

    momentum_score = 50
    if 45 <= rsi_value <= 62:
        momentum_score += 8
    elif rsi_value < 30:
        momentum_score += 15
    elif rsi_value > 70:
        momentum_score -= 15
    momentum_score += 12 if macd_hist > 0 else -12
    if returns.get("1w") is not None:
        momentum_score += 8 if returns["1w"] > 0 else -8
    if returns.get("1m") is not None:
        momentum_score += 8 if returns["1m"] > 0 else -8

    forecast_score = 50 + (summary.get("probabilityUp", 0.5) - 0.5) * 100
    forecast_score += clamp(summary.get("expectedReturn", 0) * 220, -18, 18)
    if hit_rate is not None:
        forecast_score += clamp((hit_rate - 0.5) * 80, -10, 10)

    risk_score = 100 - min(45, vol * 100) - min(35, drawdown * 100)
    if summary.get("confidence") == "Low":
        risk_score -= 8

    scores = {
        "trend": clamp(trend_score, 0, 100),
        "momentum": clamp(momentum_score, 0, 100),
        "forecast": clamp(forecast_score, 0, 100),
        "riskQuality": clamp(risk_score, 0, 100),
    }
    composite = (
        scores["trend"] * 0.28
        + scores["momentum"] * 0.24
        + scores["forecast"] * 0.32
        + scores["riskQuality"] * 0.16
    )
    scores["composite"] = clamp(composite, 0, 100)
    return {
        "items": {key: {"score": value, "label": score_label(value)} for key, value in scores.items()},
        "overallLabel": score_label(scores["composite"]),
    }


def add_pressure(
    condition: bool,
    bullish: list[dict],
    bearish: list[dict],
    points: float,
    bullish_text: str,
    bearish_text: str,
) -> None:
    if condition:
        bullish.append({"text": bullish_text, "points": points})
    else:
        bearish.append({"text": bearish_text, "points": points})


def build_pressure(
    summary: dict,
    signals: dict,
    snapshot: dict,
    scores: dict,
    news_context: dict | None = None,
) -> dict:
    bullish: list[dict] = []
    bearish: list[dict] = []
    returns = snapshot.get("returns", {})
    ma = signals.get("movingAverages", {})
    last = summary["lastPrice"]
    rsi_value = signals.get("rsi14")
    macd_hist = (signals.get("macd") or {}).get("histogram")
    volume_ratio = signals.get("volumeRatio20v90")

    add_pressure(
        summary.get("expectedReturn", 0) >= 0,
        bullish,
        bearish,
        18,
        "Model target is above the latest close.",
        "Model target is below the latest close.",
    )
    add_pressure(
        summary.get("probabilityUp", 0.5) >= 0.52,
        bullish,
        bearish,
        14,
        "Forecast distribution leans upward.",
        "Forecast distribution does not favor upside.",
    )
    if ma.get("ma20"):
        add_pressure(
            last >= ma["ma20"],
            bullish,
            bearish,
            12,
            "Price is holding above the 20-day average.",
            "Price is trading below the 20-day average.",
        )
    if ma.get("ma60"):
        add_pressure(
            last >= ma["ma60"],
            bullish,
            bearish,
            10,
            "Price is above the 60-day trend line.",
            "Price is below the 60-day trend line.",
        )
    if macd_hist is not None:
        add_pressure(
            macd_hist >= 0,
            bullish,
            bearish,
            10,
            "MACD histogram is positive.",
            "MACD histogram is negative.",
        )
    if rsi_value is not None:
        if rsi_value < 30:
            bullish.append({"text": "RSI is oversold, which can support a rebound.", "points": 8})
        elif rsi_value > 70:
            bearish.append({"text": "RSI is overbought, raising pullback risk.", "points": 8})
        else:
            bullish.append({"text": "RSI is away from extreme stress levels.", "points": 5})
    if returns.get("1m") is not None:
        add_pressure(
            returns["1m"] >= 0,
            bullish,
            bearish,
            8,
            "One-month performance is positive.",
            "One-month performance is negative.",
        )
    if volume_ratio is not None:
        if volume_ratio >= 1.15:
            bullish.append({"text": "Recent volume is above its 90-day pace.", "points": 6})
        elif volume_ratio <= 0.8:
            bearish.append({"text": "Recent volume is below its 90-day pace.", "points": 5})
    if news_context and news_context.get("articleCount", 0) > 0:
        sentiment = news_context.get("sentiment", 0.0)
        if sentiment >= 0.18:
            bullish.append({"text": "World and ticker news sentiment is positive.", "points": 10})
        elif sentiment <= -0.18:
            bearish.append({"text": "World and ticker news sentiment is negative.", "points": 10})
        else:
            bullish.append({"text": "News sentiment is broadly neutral.", "points": 3})

    bullish_score = sum(item["points"] for item in bullish)
    bearish_score = sum(item["points"] for item in bearish)
    total = bullish_score + bearish_score or 1
    bullish_pct = bullish_score / total
    pressure_label = "Mixed Pressure"
    if bullish_pct >= 0.62:
        pressure_label = "Bullish Pressure"
    elif bullish_pct <= 0.38:
        pressure_label = "Bearish Pressure"

    return {
        "label": pressure_label,
        "bullishScore": bullish_score,
        "bearishScore": bearish_score,
        "bullishPercent": bullish_pct,
        "netScore": bullish_score - bearish_score,
        "topBullish": bullish[:5],
        "topBearish": bearish[:5],
        "overallScore": scores["items"]["composite"]["score"],
    }


def build_horizon_cards(path: list[dict], last_price: float, requested_horizon: int) -> list[dict]:
    days = []
    for day in (5, 10, 20, 60, 120, requested_horizon):
        if 1 <= day <= len(path) and day not in days:
            days.append(day)
    days.sort()
    cards = []
    for day in days:
        row = path[day - 1]
        cards.append(
            {
                "label": f"{day}D",
                "days": day,
                "date": row["date"],
                "target": row["predicted"],
                "lower": row["lower"],
                "upper": row["upper"],
                "expectedReturn": (row["predicted"] / last_price) - 1,
            }
        )
    return cards


def build_scenarios(path: list[dict], last_price: float, requested_horizon: int) -> list[dict]:
    selected = path[requested_horizon - 1]
    scenarios = [
        ("Bear", selected["lower"], "Lower confidence band"),
        ("Base", selected["predicted"], "Weighted model forecast"),
        ("Bull", selected["upper"], "Upper confidence band"),
    ]
    return [
        {
            "name": name,
            "price": price,
            "expectedReturn": (price / last_price) - 1,
            "note": note,
        }
        for name, price, note in scenarios
    ]


def build_trade_levels(summary: dict, signals: dict) -> dict:
    last = summary["lastPrice"]
    support = signals.get("support60")
    resistance = signals.get("resistance60")
    minor_support = signals.get("support20") or support
    major_support = signals.get("support120") or support
    minor_resistance = signals.get("resistance20") or resistance
    major_resistance = signals.get("resistance120") or resistance
    target = summary.get("targetPrice")
    downside = ((last - support) / last) if support and support < last else None
    upside = ((target - last) / last) if target else None
    risk_reward = None
    if downside and downside > 0 and upside is not None and upside > 0:
        risk_reward = upside / downside
    return {
        "support": support,
        "resistance": resistance,
        "minorSupport": minor_support,
        "majorSupport": major_support,
        "minorResistance": minor_resistance,
        "majorResistance": major_resistance,
        "breakout": resistance * 1.01 if resistance else None,
        "riskFloor": support * 0.98 if support else None,
        "target": target,
        "riskReward": risk_reward,
    }


def build_strategic_assessment(summary: dict, signals: dict, trade_levels: dict, pressure: dict, recommendation: dict) -> dict:
    expected = summary.get("expectedReturn", 0.0)
    action = recommendation.get("action", "Hold")
    trajectory = "Neutral Pressure"
    if expected <= -0.018 or action in ("Sell", "Strong Sell"):
        trajectory = "Bearish Pressure"
    elif expected >= 0.018 or action in ("Buy", "Strong Buy"):
        trajectory = "Bullish Pressure"

    active_drivers = pressure.get("topBullish", []) if trajectory == "Bullish Pressure" else pressure.get("topBearish", [])
    diagnostics = [item.get("text") for item in active_drivers[:3] if item.get("text")]
    if len(diagnostics) < 3:
        ma = signals.get("movingAverages", {})
        if ma.get("ma20"):
            diagnostics.append(
                "Price is above the 20-day average." if summary["lastPrice"] >= ma["ma20"] else "Price is below the 20-day average."
            )
        if signals.get("rsi14") is not None:
            diagnostics.append(f"RSI oscillator is {signals['rsi14']:.1f}, shaping short-term pressure.")

    return {
        "trajectory": trajectory,
        "levels": [
            {"label": "Resistance 2", "kind": "Major", "value": trade_levels.get("majorResistance")},
            {"label": "Resistance 1", "kind": "Minor", "value": trade_levels.get("minorResistance")},
            {"label": "Support 1", "kind": "Minor", "value": trade_levels.get("minorSupport")},
            {"label": "Support 2", "kind": "Major", "value": trade_levels.get("majorSupport")},
        ],
        "region": "Estimated range" if summary.get("confidence") != "Low" else "Estimated region",
        "diagnostics": diagnostics[:4],
    }


def build_prediction_tuner(summary: dict, forecast: dict, backtest: dict) -> dict:
    horizon = summary.get("horizonDays", 30)
    confidence = summary.get("confidence", "Low")
    band = "STD" if summary.get("uncertainty", 0) < 0.22 else "Wide"
    return {
        "activeModel": "AI-weighted",
        "status": "Active neural forecast",
        "forecastAlgorithms": ["AI-weighted", "Stochastic", "Regression"],
        "horizons": [30, 60, 120, 240],
        "activeHorizon": min([30, 60, 120, 240], key=lambda item: abs(item - horizon)),
        "confidenceBand": band,
        "confidence": confidence,
        "ensembleSamples": backtest.get("ensemble", {}).get("samples", 0),
        "note": "AI hybrid blends model backtests, technical trend, news sentiment, and volatility-adjusted confidence bands.",
    }


def build_recommendation(
    summary: dict,
    scores: dict,
    pressure: dict,
    trade_levels: dict,
    news_context: dict | None = None,
) -> dict:
    expected = summary.get("expectedReturn", 0)
    probability = summary.get("probabilityUp", 0.5)
    composite = (scores.get("items") or {}).get("composite", {}).get("score", 50)
    risk_reward = trade_levels.get("riskReward")
    pressure_tilt = pressure.get("bullishPercent", 0.5) - 0.5

    signal_score = composite
    signal_score += clamp((probability - 0.5) * 120, -18, 18)
    signal_score += clamp(expected * 260, -22, 22)
    signal_score += clamp(pressure_tilt * 34, -12, 12)
    if risk_reward is not None:
        signal_score += clamp((risk_reward - 1) * 8, -8, 10)
    if summary.get("confidence") == "Low":
        signal_score -= 4
    signal_score = clamp(signal_score, 0, 100)

    action = "Hold"
    tone = "hold"
    if signal_score >= 72 and expected > 0.035 and probability >= 0.56:
        action = "Strong Buy"
        tone = "buy"
    elif signal_score >= 58 and expected > 0.012 and probability >= 0.52:
        action = "Buy"
        tone = "buy"
    elif signal_score <= 28 and expected < -0.035 and probability <= 0.44:
        action = "Strong Sell"
        tone = "sell"
    elif signal_score <= 42 and expected < -0.012 and probability <= 0.48:
        action = "Sell"
        tone = "sell"

    reasons = []
    if expected > 0:
        reasons.append("Target is above the latest close.")
    elif expected < 0:
        reasons.append("Target is below the latest close.")
    if probability >= 0.52:
        reasons.append("Modeled upside probability is favorable.")
    elif probability <= 0.48:
        reasons.append("Modeled upside probability is weak.")
    if pressure.get("label"):
        reasons.append(f"{pressure['label']} is the current directional backdrop.")
    if risk_reward is not None:
        reasons.append(f"Risk/reward is {risk_reward:.2f} based on support and target.")
    if news_context and news_context.get("articleCount", 0) > 0:
        reasons.append(
            f"News bias is {news_context.get('bias', 'Neutral').lower()} from {news_context.get('articleCount')} recent headlines."
        )
    if summary.get("confidence") == "Low":
        reasons.append("Confidence is low, so position sizing should be conservative.")

    return {
        "action": action,
        "tone": tone,
        "score": signal_score,
        "reasons": reasons[:5],
    }


def neural_action(score: float) -> str:
    if score >= 70:
        return "Buy"
    if score >= 56:
        return "Watch"
    if score <= 35:
        return "Sell"
    return "Hold"


def build_neural_insight(
    symbol: str,
    summary: dict,
    scores: dict,
    news_context: dict,
    recommendation: dict,
) -> dict:
    items = scores.get("items") or {}
    forecast_score = items.get("forecast", {}).get("score", 50)
    trend_score = items.get("trend", {}).get("score", 50)
    momentum_score = items.get("momentum", {}).get("score", 50)
    risk_score = items.get("riskQuality", {}).get("score", 50)
    technical_score = (trend_score * 0.58) + (momentum_score * 0.42)
    news_score = clamp(50 + news_context.get("sentiment", 0.0) * 50, 0, 100)
    components = [
        {
            "name": "Forecast Edge",
            "weight": 40,
            "score": forecast_score,
            "description": "Probability, target return, and backtest quality.",
        },
        {
            "name": "Technical Trend",
            "weight": 25,
            "score": technical_score,
            "description": "Moving averages, RSI, MACD, and recent momentum.",
        },
        {
            "name": "News Sentiment",
            "weight": 20,
            "score": news_score,
            "description": "Global market and ticker headline tone.",
        },
        {
            "name": "Risk Profile",
            "weight": 15,
            "score": risk_score,
            "description": "Volatility, drawdown, and confidence quality.",
        },
    ]
    quantum_score = sum(item["score"] * item["weight"] for item in components) / 100
    action = neural_action(quantum_score)
    stance = (
        f"{symbol} receives a {quantum_score:.0f}/100 neural score. "
        f"The system stance is {action.lower()}, while the direct model signal is "
        f"{recommendation.get('action', 'Hold').lower()}. "
        f"Forecast edge contributes {forecast_score:.0f}, technical trend contributes {technical_score:.0f}, "
        f"news sentiment contributes {news_score:.0f}, and risk quality contributes {risk_score:.0f}."
    )
    return {
        "score": quantum_score,
        "action": action,
        "range": "70-79" if 70 <= quantum_score < 80 else "0-100",
        "stance": stance,
        "components": components,
    }


def build_sentiment_divergence(summary: dict, scores: dict, news_context: dict, pressure: dict) -> dict:
    items = scores.get("items") or {}
    bars = [
        {
            "label": "Price",
            "value": clamp(summary.get("expectedReturn", 0) * 7, -1, 1),
        },
        {
            "label": "Technical",
            "value": clamp((items.get("trend", {}).get("score", 50) - 50) / 50, -1, 1),
        },
        {
            "label": "News",
            "value": clamp(news_context.get("sentiment", 0), -1, 1),
        },
        {
            "label": "Pressure",
            "value": clamp((pressure.get("bullishPercent", 0.5) - 0.5) * 2, -1, 1),
        },
    ]
    oscillator = mean([item["value"] for item in bars])
    return {
        "oscillator": oscillator,
        "label": "Bullish Divergence" if oscillator > 0.18 else "Bearish Divergence" if oscillator < -0.18 else "Neutral Divergence",
        "bars": bars,
    }


def build_stock_pick_sentinels(
    symbol: str,
    summary: dict,
    snapshot: dict,
    scores: dict,
    news_context: dict,
    trade_levels: dict,
) -> dict:
    items = scores.get("items") or {}
    composite = items.get("composite", {}).get("score", 50)
    forecast_score = items.get("forecast", {}).get("score", 50)
    risk_score = items.get("riskQuality", {}).get("score", 50)
    news_score = clamp(50 + news_context.get("sentiment", 0.0) * 50, 0, 100)
    expected = summary.get("expectedReturn", 0.0)
    volatility = summary.get("annualizedVolatility", 0.0)
    strategy_mode = "Momentum" if expected >= 0.02 else "Defensive Value" if risk_score >= 55 else "Turnaround"
    risk_level = "Conservative" if volatility < 0.22 else "Moderate Beta" if volatility < 0.42 else "Aggressive Alpha"
    filters = [
        {"name": "Momentum", "description": "Trend and AI acceleration", "active": strategy_mode == "Momentum"},
        {"name": "Defensive Value", "description": "Income and margin resilience", "active": strategy_mode == "Defensive Value"},
        {"name": "High Yield", "description": "Cashflow and payout quality", "active": risk_score >= 55},
        {"name": "Trend Chasers", "description": "Short-term momentum", "active": items.get("trend", {}).get("score", 50) >= 58},
        {"name": "Turnaround", "description": "Recovery setups", "active": strategy_mode == "Turnaround"},
        {"name": "Accumulation", "description": "Smart-money volume clues", "active": (snapshot.get("volumeRatio20v90") or 1) >= 1.05},
    ]
    cards = [
        {
            "title": "Premium Growth Multiples",
            "subtitle": "Revenue momentum and AI trend expansion",
            "score": composite,
            "ticker": symbol,
            "metric": f"{summary.get('probabilityUp', 0.5) * 100:.1f}% chance up",
            "target": summary.get("targetPrice"),
            "note": "Best when forecast, news, and trend scores confirm together.",
        },
        {
            "title": "Defensive Earnings Yields",
            "subtitle": "Lower-risk quality and resilient cashflow proxy",
            "score": risk_score,
            "ticker": symbol,
            "metric": f"{risk_score:.0f}/100 risk quality",
            "target": trade_levels.get("riskFloor"),
            "note": "Favors controlled volatility, support defense, and smaller drawdowns.",
        },
        {
            "title": "Maximum AI Target Upside",
            "subtitle": "Highest expected return from model target",
            "score": forecast_score,
            "ticker": symbol,
            "metric": f"{expected * 100:+.1f}% target upside",
            "target": summary.get("upperPrice"),
            "note": "Aggressive screen; compare against support and confidence before acting.",
        },
    ]
    return {
        "strategyMode": strategy_mode,
        "riskLevel": risk_level,
        "filters": filters,
        "cards": cards,
        "newsScore": news_score,
    }


def build_horizon_matrix(
    symbol: str,
    summary: dict,
    scores: dict,
    news_context: dict,
    snapshot: dict,
) -> dict:
    items = scores.get("items") or {}
    expected = summary.get("expectedReturn", 0.0)
    horizon = summary.get("horizonDays", 30)
    annualized_projection = ((1 + expected) ** (252 / max(horizon, 1))) - 1 if expected > -0.95 else -0.95
    min_growth = clamp(annualized_projection, -0.2, 0.6)
    venture_score = clamp(
        items.get("forecast", {}).get("score", 50) * 0.35
        + items.get("trend", {}).get("score", 50) * 0.25
        + (50 + news_context.get("sentiment", 0.0) * 50) * 0.2
        + items.get("riskQuality", {}).get("score", 50) * 0.2,
        0,
        100,
    )
    sectors = ["AI", "Cloud", "Fintech", "Robotics"] if symbol.endswith(".HK") is False else ["AI", "Platform", "Fintech", "China"]
    return {
        "ventureScore": venture_score,
        "minRevenueGrowth": min_growth,
        "evSalesCap": "No valuation cap",
        "marketCapLimit": "< $1B" if venture_score >= 70 else "< $10B" if venture_score >= 55 else "No hard cap",
        "targetSectors": sectors,
        "filters": ["Moat", "Proprietary data depth", "AI expansion scale", "Runway sustainability"],
        "commentary": (
            f"This matrix ranks {symbol} as a {venture_score:.0f}/100 venture-scale candidate. "
            f"The model requires projected growth near {min_growth * 100:.0f}%/yr, positive news confirmation, "
            "and evidence that support zones are being defended."
        ),
    }


def build_smart_money(rows: list[dict], summary: dict, snapshot: dict, signals: dict) -> dict:
    closes = [row["close"] for row in rows]
    volumes = [row.get("volume") or 0 for row in rows]
    recent = rows[-30:]
    up_volume = 0.0
    down_volume = 0.0
    for idx in range(1, len(recent)):
        volume = recent[idx].get("volume") or 0
        if recent[idx]["close"] >= recent[idx - 1]["close"]:
            up_volume += volume
        else:
            down_volume += volume
    total_volume = up_volume + down_volume
    accumulation_ratio = up_volume / total_volume if total_volume else 0.5
    close_position = snapshot.get("position52w") or 0.5
    volume_pulse = snapshot.get("volumeRatio20v90") or 1.0
    ma = signals.get("movingAverages", {})
    trend_bonus = 1 if ma.get("ma20") and summary["lastPrice"] >= ma["ma20"] else 0
    score = clamp(accumulation_ratio * 46 + close_position * 18 + min(volume_pulse, 1.8) * 14 + trend_bonus * 12, 0, 100)
    status = "Accumulation" if score >= 62 else "Distribution Watch" if score <= 38 else "Consolidating"
    triggers = [
        {"name": "Up-volume ratio", "value": f"{accumulation_ratio * 100:.0f}%"},
        {"name": "Volume pulse", "value": f"{volume_pulse:.2f}x"},
        {"name": "52W position", "value": f"{close_position * 100:.0f}%"},
        {"name": "Above MA20", "value": "Yes" if trend_bonus else "No"},
    ]
    return {
        "score": score,
        "status": status,
        "triggers": triggers,
        "funds": [
            {"name": "Consolidation channel", "state": status},
            {"name": "Hidden accumulation", "state": "Possible" if accumulation_ratio >= 0.55 else "Unconfirmed"},
            {"name": "Large prints proxy", "state": "Active" if volume_pulse >= 1.2 else "Quiet"},
        ],
    }


def build_advisory_system(summary: dict, signals: dict, scores: dict, recommendation: dict) -> dict:
    items = scores.get("items") or {}
    rsi_value = signals.get("rsi14") or 50
    corridor = "Stable corridor" if 38 <= rsi_value <= 62 else "Stretch zone"
    regime = "Recovery bias" if summary.get("expectedReturn", 0) > 0 else "Risk-off bias"
    confidence_multiplier = clamp((recommendation.get("score", 50) / 100) * 1.2, 0.2, 1.2)
    feed = [
        {"action": recommendation.get("action", "Hold"), "status": "AI confirmed", "price": summary.get("lastPrice"), "confidence": f"{recommendation.get('score', 50):.0f}%"},
        {"action": "Watch support", "status": "Risk control", "price": signals.get("support60"), "confidence": f"{items.get('riskQuality', {}).get('score', 50):.0f}%"},
        {"action": "Momentum check", "status": signals.get("momentum", "Neutral"), "price": summary.get("targetPrice"), "confidence": f"{items.get('momentum', {}).get('score', 50):.0f}%"},
    ]
    return {
        "framework": "CoreFluent",
        "status": "System online",
        "regime": regime,
        "corridor": corridor,
        "confidenceMultiplier": confidence_multiplier,
        "quantBracket": f"{recommendation.get('score', 50):.1f}",
        "feed": feed,
    }


def build_catalyst_bias(symbol: str, name: str, pressure: dict, news_context: dict, signals: dict, summary: dict) -> dict:
    bullish = [item["text"] for item in pressure.get("topBullish", [])][:4]
    bearish = [item["text"] for item in pressure.get("topBearish", [])][:4]
    if news_context.get("articles"):
        top_news = news_context["articles"][0]["title"]
        bullish.append(f"Current headline catalyst: {top_news}")
    risks = [
        "AI/news scoring can shift quickly as new headlines arrive.",
        "Support breaks can invalidate a short-term setup.",
        f"Annualized volatility is {summary.get('annualizedVolatility', 0) * 100:.1f}%.",
    ]
    return {
        "summary": (
            f"{name} catalyst bias combines live news, technical pressure, and model forecast data. "
            f"The current stance for {symbol} is {summary.get('bias', 'Neutral').lower()} with "
            f"{summary.get('confidence', 'Low').lower()} confidence."
        ),
        "bullish": bullish[:4],
        "bearish": bearish[:4],
        "risks": risks,
    }


def build_market_intelligence(news_context: dict, market_sentinel: dict) -> dict:
    articles = news_context.get("articles", [])
    radar = [
        {"label": "Bullish momentum", "value": market_sentinel.get("bullishPercent", 0.33)},
        {"label": "Bearish pressure", "value": market_sentinel.get("bearishPercent", 0.33)},
        {"label": "Neutral density", "value": market_sentinel.get("neutralPercent", 0.33)},
        {"label": "Headline velocity", "value": clamp(len(articles) / 12, 0, 1)},
        {"label": "News signal", "value": clamp(0.5 + news_context.get("sentiment", 0.0) / 2, 0, 1)},
    ]
    return {
        "feed": articles[:10],
        "radar": radar,
        "summary": market_sentinel.get("consensus", "Neutral Consensus"),
    }


def build_analysis_text(
    symbol: str,
    summary: dict,
    signals: dict,
    snapshot: dict,
    pressure: dict,
    scores: dict,
    requested_horizon: int,
    news_context: dict | None = None,
    external_context: dict | None = None,
) -> dict:
    expected = summary.get("expectedReturn", 0)
    probability = summary.get("probabilityUp", 0.5)
    trend = signals.get("trend", "Neutral").lower()
    momentum = signals.get("momentum", "Neutral").lower()
    volatility = summary.get("annualizedVolatility", 0)
    drawdown = (snapshot.get("risk") or {}).get("maxDrawdown1y")
    composite_label = scores.get("overallLabel", "Neutral")

    thesis = (
        f"{symbol} shows a {composite_label.lower()} composite setup over the next "
        f"{requested_horizon} trading days. The base forecast implies {expected * 100:.1f}% "
        f"with a {probability * 100:.1f}% modeled chance of finishing above the latest close."
    )
    technical = (
        f"Trend is {trend} and momentum is {momentum}. The pressure model is currently "
        f"{pressure['label'].lower()}, with bullish evidence at {pressure['bullishPercent'] * 100:.0f}% "
        "of the scored directional mix."
    )
    news = None
    if news_context and news_context.get("articleCount", 0) > 0:
        news = (
            f"News layer: {news_context.get('bias', 'Neutral').lower()} bias from "
            f"{news_context.get('articleCount')} recent global and ticker headlines. "
            f"News impact on daily drift is {summary.get('newsDailyImpact', 0) * 100:.3f}%."
        )
    risk = (
        f"Annualized volatility is about {volatility * 100:.1f}%. "
        f"The recent one-year max drawdown is {drawdown * 100:.1f}%."
        if drawdown is not None
        else f"Annualized volatility is about {volatility * 100:.1f}%."
    )
    caveat = (
        "The app uses price, volume, technicals, and model backtests. It does not know future news, "
        "earnings surprises, policy moves, liquidity shocks, or overnight events."
    )
    bullets = [technical]
    if news:
        bullets.append(news)
    regime = summary.get("regimeDailyImpact")
    if regime is not None:
        bullets.append(
            "Regime confirmation layer: technical alignment, global breadth, volatility stress, and market sentiment "
            f"adjust daily drift by {regime * 100:.3f}%."
        )
    if external_context:
        bullets.append(
            "External source confirmation: TradingView chart link plus AAStocks/ETNet confirmation signals "
            f"adjust daily drift by {summary.get('externalDailyImpact', 0) * 100:.3f}%."
        )
    bullets.extend([risk, caveat])
    return {
        "headline": thesis,
        "bullets": bullets,
    }


def summarize_signals(rows: list[dict]) -> dict:
    closes = [row["close"] for row in rows]
    volumes = [row.get("volume") or 0 for row in rows]
    last = closes[-1]
    ma_values = {
        "ma5": sma(closes, 5),
        "ma20": sma(closes, 20),
        "ma60": sma(closes, 60),
        "ma200": sma(closes, 200),
    }
    macd_line, macd_signal, macd_hist = macd_values(closes)
    rsi_value = rsi(closes)
    support20 = min(closes[-20:]) if len(closes) >= 20 else min(closes)
    resistance20 = max(closes[-20:]) if len(closes) >= 20 else max(closes)
    support = min(closes[-60:]) if len(closes) >= 60 else min(closes)
    resistance = max(closes[-60:]) if len(closes) >= 60 else max(closes)
    support120 = min(closes[-120:]) if len(closes) >= 120 else support
    resistance120 = max(closes[-120:]) if len(closes) >= 120 else resistance
    recent_volume = mean(volumes[-20:]) if len(volumes) >= 20 else mean(volumes)
    long_volume = mean(volumes[-90:]) if len(volumes) >= 90 else recent_volume
    trend = "Neutral"
    if ma_values["ma20"] and ma_values["ma60"]:
        if last > ma_values["ma20"] > ma_values["ma60"]:
            trend = "Positive"
        elif last < ma_values["ma20"] < ma_values["ma60"]:
            trend = "Negative"
    momentum = "Neutral"
    if rsi_value is not None:
        if rsi_value < 30:
            momentum = "Oversold"
        elif rsi_value > 70:
            momentum = "Overbought"
        elif macd_hist is not None and macd_hist > 0:
            momentum = "Positive"
        elif macd_hist is not None and macd_hist < 0:
            momentum = "Negative"

    return {
        "lastPrice": last,
        "movingAverages": ma_values,
        "rsi14": rsi_value,
        "macd": {"line": macd_line, "signal": macd_signal, "histogram": macd_hist},
        "support20": support20,
        "support60": support,
        "support120": support120,
        "resistance20": resistance20,
        "resistance60": resistance,
        "resistance120": resistance120,
        "volumeRatio20v90": (recent_volume / long_volume) if long_volume else None,
        "trend": trend,
        "momentum": momentum,
    }


def make_prediction(raw_symbol: str, market: str, horizon: int) -> dict:
    requested_horizon = int(clamp(horizon, 5, 120))
    forecast_horizon = 120
    symbol, normalized_market = normalize_symbol(raw_symbol, market)
    history = fetch_yahoo_history(symbol)
    finnhub_context = fetch_finnhub_context(symbol)
    rows, live_quote_applied = rows_with_live_quote(history["rows"], finnhub_context.get("quote"))
    closes = [row["close"] for row in rows]
    dates = [row["date"] for row in rows]
    news_context = build_news_context(symbol, history["name"], normalized_market, finnhub_context)
    global_indices = build_global_indices_board()
    macro_indicators = build_macro_indicators()
    market_sentinel = build_market_sentinel(normalized_market, news_context)
    external_context = build_external_provider_context(symbol, normalized_market)
    signals = summarize_signals(rows)
    backtest = backtest_weights(closes, dates)
    path, forecast = forecast_path(
        closes,
        dates,
        forecast_horizon,
        backtest["weights"],
        backtest["ensemble"],
        news_context,
        signals,
        global_indices,
        macro_indicators,
        market_sentinel,
        external_context,
    )
    snapshot = build_market_snapshot(rows)
    selected_forecast = path[requested_horizon - 1]

    expected = (selected_forecast["predicted"] / closes[-1]) - 1
    volatility = forecast["dailyVolatility"]
    calibration = forecast.get("calibration", 1.0)
    denominator = volatility * math.sqrt(requested_horizon) * calibration
    probability_up = (
        normal_cdf(math.log(selected_forecast["predicted"] / closes[-1]) / denominator)
        if denominator > 0
        else 0.5
    )
    uncertainty = ((selected_forecast["upper"] - selected_forecast["lower"]) / selected_forecast["predicted"]) / 2
    confidence = "Low"
    hit_rate = backtest["ensemble"].get("directionalAccuracy")
    if hit_rate is not None and len(closes) >= 252:
        if hit_rate >= 0.56 and uncertainty < 0.22:
            confidence = "Medium"
        elif hit_rate >= 0.51:
            confidence = "Medium-low"
    if forecast.get("regimeConfidenceBoost") and confidence == "Low" and uncertainty < 0.28:
        confidence = "Medium-low"
    elif forecast.get("regimeConfidenceBoost") and confidence == "Medium-low" and uncertainty < 0.2:
        confidence = "Medium"
    bias = "Neutral"
    if expected > 0.025:
        bias = "Positive"
    elif expected < -0.025:
        bias = "Negative"

    summary = {
        "bias": bias,
        "confidence": confidence,
        "horizonDays": requested_horizon,
        "lastPrice": closes[-1],
        "targetPrice": selected_forecast["predicted"],
        "lowerPrice": selected_forecast["lower"],
        "upperPrice": selected_forecast["upper"],
        "expectedReturn": expected,
        "probabilityUp": probability_up,
        "annualizedVolatility": forecast["annualizedVolatility"],
        "dailyDrift": forecast["dailyDrift"],
        "uncertainty": uncertainty,
        "newsDailyImpact": forecast["newsDailyImpact"],
        "externalDailyImpact": forecast["externalDailyImpact"],
        "regimeDailyImpact": forecast["regimeDailyImpact"],
        "regimeScores": forecast["regimeScores"],
    }
    scores = build_scores(summary, signals, snapshot, backtest["ensemble"])
    pressure = build_pressure(summary, signals, snapshot, scores, news_context)
    trade_levels = build_trade_levels(summary, signals)
    recommendation = build_recommendation(summary, scores, pressure, trade_levels, news_context)
    strategic_assessment = build_strategic_assessment(summary, signals, trade_levels, pressure, recommendation)
    prediction_tuner = build_prediction_tuner(summary, forecast, backtest)
    neural_insight = build_neural_insight(symbol, summary, scores, news_context, recommendation)
    sentiment_divergence = build_sentiment_divergence(summary, scores, news_context, pressure)
    stock_pick_sentinels = build_stock_pick_sentinels(symbol, summary, snapshot, scores, news_context, trade_levels)
    horizon_matrix = build_horizon_matrix(symbol, summary, scores, news_context, snapshot)
    smart_money = build_smart_money(rows, summary, snapshot, signals)
    advisory_system = build_advisory_system(summary, signals, scores, recommendation)
    catalyst_bias = build_catalyst_bias(symbol, history["name"], pressure, news_context, signals, summary)
    market_intelligence = build_market_intelligence(news_context, market_sentinel)
    horizons = build_horizon_cards(path, closes[-1], requested_horizon)
    scenarios = build_scenarios(path, closes[-1], requested_horizon)
    analysis = build_analysis_text(symbol, summary, signals, snapshot, pressure, scores, requested_horizon, news_context, external_context)

    recent_history = rows[-260:]
    data_sources = {
        "history": "Yahoo Finance",
        "quote": "Finnhub" if live_quote_applied else "Yahoo Finance fallback",
        "news": " + ".join((news_context.get("sources") or ["Google News"]) + ["AAStocks/ETNet confirmation"]),
        "finnhubQuote": finnhub_context.get("quoteStatus"),
        "finnhubNews": finnhub_context.get("newsStatus"),
        "finnhubConfigured": finnhub_context.get("configured"),
        "finnhubErrors": finnhub_context.get("errors") or [],
    }
    return {
        "symbol": history["symbol"],
        "name": history["name"],
        "querySymbol": raw_symbol,
        "normalizedSymbol": symbol,
        "market": normalized_market,
        "currency": history["currency"],
        "exchange": history["exchange"],
        "instrument": history["instrument"],
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "dataStart": rows[0]["date"],
        "dataEnd": rows[-1]["date"],
        "dataSources": data_sources,
        "requestedHorizon": requested_horizon,
        "forecastHorizon": forecast_horizon,
        "history": recent_history,
        "forecast": path,
        "summary": summary,
        "snapshot": snapshot,
        "signals": signals,
        "globalIndices": global_indices,
        "scores": scores,
        "pressure": pressure,
        "recommendation": recommendation,
        "strategicAssessment": strategic_assessment,
        "predictionTuner": prediction_tuner,
        "externalProviders": external_context,
        "neuralInsight": neural_insight,
        "sentimentDivergence": sentiment_divergence,
        "stockPickSentinels": stock_pick_sentinels,
        "horizonMatrix": horizon_matrix,
        "smartMoney": smart_money,
        "advisorySystem": advisory_system,
        "catalystBias": catalyst_bias,
        "marketIntelligence": market_intelligence,
        "marketSentinel": market_sentinel,
        "macroIndicators": macro_indicators,
        "news": news_context,
        "tradeLevels": trade_levels,
        "horizons": horizons,
        "scenarios": scenarios,
        "analysis": analysis,
        "models": {
            "weights": backtest["weights"],
            "methodStats": backtest["methodStats"],
            "ensemble": backtest["ensemble"],
            "currentDailyForecasts": forecast["modelForecasts"],
        },
        "notice": "Forecasts are educational estimates, not financial advice. Markets can move sharply on news, liquidity, rates, earnings, policy, and macro shocks.",
    }


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/predict":
            params = urllib.parse.parse_qs(parsed.query)
            symbol = (params.get("symbol") or ["AAPL"])[0]
            market = (params.get("market") or ["AUTO"])[0]
            horizon_raw = (params.get("horizon") or ["30"])[0]
            try:
                horizon = int(horizon_raw)
                payload = make_prediction(symbol, market, horizon)
                json_response(self, payload)
            except Exception as exc:
                json_response(
                    self,
                    {
                        "error": str(exc),
                        "trace": traceback.format_exc(limit=2),
                        "notice": "Check the symbol format and internet access. HK examples use 0700.HK or 9988.HK.",
                    },
                    status=400,
                )
            return

        if parsed.path in ("/", "/index.html"):
            self.serve_static(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return

        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        requested = (STATIC_DIR / parsed.path.lstrip("/")).resolve()
        try:
            requested.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(404)
            return

        content_type = "text/plain; charset=utf-8"
        if requested.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif requested.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif requested.suffix == ".svg":
            content_type = "image/svg+xml"
        self.serve_static(requested, content_type)

    def serve_static(self, file_path: Path, content_type: str) -> None:
        if not file_path.exists() or not file_path.is_file():
            self.send_error(404)
            return
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    port = int(os.environ.get("PORT") or os.environ.get("STOCK_FORECAST_PORT", "8000"))
    host = os.environ.get("STOCK_FORECAST_HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    server = ThreadingHTTPServer((host, port), AppHandler)
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    print(f"Stock Forecast Lab running at http://{display_host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

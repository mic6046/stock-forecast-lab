# Stock Forecast Lab

A local web app for HK and US stock forecasting. It fetches recent daily prices, runs an ensemble of trend, momentum, mean-reversion, AR(1), technical, seasonality, and historical-analog models, then weights those models with recent walk-forward backtesting.

## Run

```powershell
python app.py
```

Open:

```text
http://127.0.0.1:8000
```

HK examples: `0700.HK`, `9988.HK`, `0005.HK`

US examples: `AAPL`, `MSFT`, `NVDA`

Forecasts are educational estimates, not financial advice. No model can make stock-market predictions reliably accurate in all regimes.

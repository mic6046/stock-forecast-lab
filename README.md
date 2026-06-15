# Stock Forecast Lab

AI-powered HK and US stock forecasting web application.

The backend exposes the existing `/api/predict` JSON endpoint and the root route `/` serves a responsive Chart.js web interface.

## Run Locally

```powershell
python app.py
```

Open:

```text
http://127.0.0.1:8000
```

Example symbols:

- HK: `0700.HK`, `9988.HK`, `0005.HK`, `3690.HK`
- US: `AAPL`, `MSFT`, `NVDA`, `TSLA`, `AMZN`

## Deploy To Render

This project includes `render.yaml` for Render free-tier deployment.

1. Push this repository to GitHub.
2. In Render, choose **New +** -> **Blueprint**.
3. Connect this repository.
4. Render reads `render.yaml` and creates the free web service.
5. The app starts with:

```text
python app.py
```

Render provides the `PORT` environment variable automatically. The app binds to `0.0.0.0` in cloud mode and keeps `127.0.0.1:8000` for local development.

## Example API Usage

```text
GET /api/predict?symbol=1818.HK&market=HK&horizon=30
```

```powershell
curl "http://127.0.0.1:8000/api/predict?symbol=NVDA&market=US&horizon=30"
```

Response includes:

- `summary.lastPrice`
- `summary.targetPrice`
- `summary.expectedReturn`
- `recommendation.action`
- `recommendation.score`
- `history`
- `forecast`
- `models.weights`
- `models.ensemble`
- `models.currentDailyForecasts`

## Notes

Forecasts are educational estimates, not financial advice. Markets can move sharply on news, liquidity, rates, earnings, policy, and macro shocks.

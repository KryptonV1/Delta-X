# ⚡ DELTA X — BBMA Crypto Signal Engine

> **Python · Binance · Telegram · Supabase · Render**

A fully automated spot-trading signal provider built around the **BBMA** strategy.
Monitors all valid Binance USDT pairs across M15/M30 (entry) and H1/H4/Daily (trend), detects
Extrem → MHV entry setups, validates risk/reward, and fires real-time Telegram alerts.

---

## 📁 Project Structure

```
delta_x/
├── main.py                    # Entry point + scanner engine
├── requirements.txt
├── Procfile                   # Render web process
├── render.yaml                # Render deployment config
├── supabase_schema.sql        # Run once on Supabase
├── .env.example               # Copy to .env and fill in
├── config/settings.py         # All constants & env vars
├── core/
│   ├── bbma.py                # BBMA indicator calculations
│   └── signals.py             # Signal state machine (Extrem→MHV→Entry)
├── data/
│   ├── binance_feed.py        # Binance REST API client
│   └── pair_filter.py         # Stablecoin / wrapped token filter
├── notifications/
│   └── telegram_bot.py        # Telegram alert formatter + sender
├── database/
│   └── supabase_client.py     # Supabase persistence layer
├── web/
│   ├── app.py                 # Flask dashboard + JSON API
│   └── templates/index.html   # Real-time monitoring UI
└── utils/logger.py
```

---

## 🚀 Quick Start

### 1 — Clone & Install

```bash
git clone https://github.com/YOUR_USERNAME/delta-x.git
cd delta-x
pip install -r requirements.txt
```

### 2 — Configure Environment

```bash
cp .env.example .env
# Fill in: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SUPABASE_URL, SUPABASE_KEY
```

### 3 — Set Up Supabase

1. Create a free project at [supabase.com](https://supabase.com)
2. Open the **SQL Editor** and run `supabase_schema.sql`
3. Copy your **Project URL** and **service_role key** into `.env`

### 4 — Create Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy the token into `TELEGRAM_BOT_TOKEN`
3. Add the bot to your channel/group and copy the chat ID into `TELEGRAM_CHAT_ID`
   - For channels use: `@your_channel_name` or the numeric ID

### 5 — Run Locally

```bash
python main.py
```

Dashboard: [http://localhost:5000](http://localhost:5000)

---

## ☁️ Deploy to Render (Free Tier)

1. Push to GitHub
2. Go to [render.com](https://render.com) → **New Web Service**
3. Connect your GitHub repo
4. Render detects `render.yaml` automatically
5. Add environment variables in the Render dashboard
6. Deploy

### UptimeRobot (keep-alive)

Render free tier sleeps after 15 min inactivity.
1. Sign up at [uptimerobot.com](https://uptimerobot.com) (free)
2. **New Monitor** → HTTP(S) → URL: `https://your-app.onrender.com/ping`
3. Interval: **5 minutes**

---

## 📊 BBMA Indicator Settings

| Indicator | Period | Method | Applied To |
|-----------|--------|--------|-----------|
| Bollinger Bands | 20 | SMA, σ×2 | Close |
| MA5 High | 5 | LWMA | High |
| MA5 Low | 5 | LWMA | Low |
| MA10 High | 10 | LWMA | High |
| MA10 Low | 10 | LWMA | Low |
| MA50 | 50 | EMA | Close |

---

## 🔔 Signal Logic

```
WATCHING
  └─► EXTREM     MA5/MA10 exits BB band
        └─► MHV  Momentum fades, MA returns inside BB
              └─► ENTRY  Price retraces to MA5/MA10 zone  → ALERT 🔔
```

**Risk filter** (signals that fail are silently dropped):

| Rule | Value |
|------|-------|
| Max SL distance | −20 % from entry |
| Min TP1 distance | +20 % from entry |
| SL placement | Below/above Extrem candle wick + 0.5% buffer |
| TP1 | BB Middle (SMA20) |
| TP2 | Opposite BB band |
| TP3 | TP2 ± (SL→TP1 distance) |

**CSM rule**: A strong Candlestick Momentum against an active Extrem cancels it immediately.

---

## 📡 API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard UI |
| `GET /ping` | UptimeRobot keep-alive |
| `GET /api/status` | System health + stats |
| `GET /api/signals` | Recent signals from Supabase |
| `GET /api/active` | In-memory active signals |
| `GET /api/prices` | Cached prices + trends |

---

## ⚙️ Configuration (`config/settings.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_LOSS_PERCENT` | 20.0 | Max SL % from entry |
| `MIN_TP1_PERCENT` | 20.0 | Min TP1 % from entry |
| `BATCH_SIZE` | 20 | Pairs per API batch |
| `CANDLE_LIMIT` | 120 | Candles fetched per call |
| `BB_PERIOD` | 20 | Bollinger Bands period |
| `MA50_PERIOD` | 50 | EMA trend anchor period |

---

## ⚠️ Disclaimer

This software is for **educational and research purposes only**.
Cryptocurrency trading involves significant financial risk.
Always manage your own risk. Past signals do not guarantee future results.

---

*Built with ❤️ · BBMA by J E B A T · Delta X v1.0*

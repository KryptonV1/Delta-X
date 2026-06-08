# 🚀 DELTA X Setup Guide (5 Minit)

**Apakah yang kau perlukan:**
- Python 3.10+
- GitHub account (untuk push code)
- Telegram account
- Browser

---

## Step 1: Local Setup (2 minit)

### Linux / macOS
```bash
unzip delta_x_v1.zip
cd delta_x
bash setup.sh
```

### Windows
```bash
# Extract ZIP → buka PowerShell → 
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Lepas ini, edit `.env`:
```bash
nano .env
# atau bukak pakai text editor apa saja
```

---

## Step 2: Setup Telegram Bot (2 minit)

### A. Create Bot di Telegram

1. **Bukak Telegram** → Cari `@BotFather`
2. **Hantar:** `/newbot`
3. **Jawab soalan:**
   - Bot name: `DeltaX` (apa aja)
   - Bot username: `delta_x_bot_xxx` (mesti unique & akhir dengan _bot)
   - Dapat token: `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`

### B. Isi dalam .env

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
```

### C. Get Chat ID

**Option A: Private group (recommended)**
1. Create group baru di Telegram
2. Add bot ke group
3. Hantar message apa saja ke group
4. Go to: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
5. Replace `<YOUR_TOKEN>` dengan bot token atas
6. Cari `"chat":{"id":-123456789,...}` → copy number
7. Isi dalam .env: `TELEGRAM_CHAT_ID=-123456789`

**Option B: Channel (public/private)**
1. Create channel
2. Add bot sebagai admin
3. Channel username: `@mychannel_alerts`
4. Isi dalam .env: `TELEGRAM_CHAT_ID=@mychannel_alerts`

---

## Step 3: Setup Supabase (2 minit)

### A. Create Project

1. Go to [supabase.com](https://supabase.com)
2. **Sign up** (free tier available)
3. **New Project:**
   - Database password: set something strong
   - Region: choose closest to you
   - Tunggu 2-3 minit untuk ready

### B. Copy Credentials

1. Go to **Settings → API**
2. Copy:
   - **Project URL** → `SUPABASE_URL` in .env
   - **service_role secret** → `SUPABASE_KEY` in .env
   
Example:
```env
SUPABASE_URL=https://abc123def456.supabase.co
SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

### C. Create Tables

1. Go to **SQL Editor**
2. **New Query**
3. Copy-paste entire contents of `supabase_schema.sql`
4. **Run** (top-right)
5. ✅ Done! Tables created

---

## Step 4: Test Locally (Optional)

```bash
source venv/bin/activate    # or: venv\Scripts\activate (Windows)
python main.py
```

**Expected output:**
```
2024-01-15 14:30:22 | INFO     | DELTA X.main | DELTA X v1.0 started
2024-01-15 14:30:24 | INFO     | DELTA X.data | Pair filter: 300+ total → 200+ valid
2024-01-15 14:30:25 | INFO     | DELTA X.main | Scheduler started
```

**Browser:** [http://localhost:5000](http://localhost:5000) → kau boleh lihat dashboard real-time

Press `Ctrl+C` to stop.

---

## Step 5: Deploy to Render (< 1 minit)

### A. Push to GitHub

```bash
git init
git add .
git commit -m "Delta X - BBMA signal provider"
git remote add origin https://github.com/YOUR_USERNAME/delta-x.git
git branch -M main
git push -u origin main
```

### B. Connect to Render

1. Go to [render.com](https://render.com)
2. **Sign up with GitHub** (free)
3. **New Web Service**
4. Select your `delta-x` repo
5. Settings:
   - **Name:** `delta-x`
   - **Branch:** `main`
   - **Build Command:** (auto-detected from Procfile) ✓
   - **Start Command:** (auto-detected from Procfile) ✓
   - **Instance Type:** `Free`

### C. Add Environment Variables

1. In Render dashboard → **Environment**
2. Add these 4:
   ```
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   SUPABASE_URL=...
   SUPABASE_KEY=...
   ```
3. **Deploy** (top-right)

Wait 3-5 minit. Done! ✅ Kamu dapat URL seperti `https://delta-x-abc123.onrender.com`

Test it: `https://delta-x-abc123.onrender.com/ping` → should return `OK`

---

## Step 6: Setup UptimeRobot (1 minit)

Render free tier sleeps selepas 15 minit inactivity. UptimeRobot ping-kan setiap 5 minit untuk keep alive.

1. Go to [uptimerobot.com](https://uptimerobot.com)
2. **Sign up** (free)
3. **Add New Monitor**
   - Type: **HTTP(s)**
   - URL: `https://delta-x-abc123.onrender.com/ping`
   - Monitoring Interval: **5 minutes**
   - **Create Monitor**

Done! System akan tetap running 24/7.

---

## ✅ Full Checklist

- [ ] `.env` filled dengan 4 credentials
- [ ] Supabase tables created (run supabase_schema.sql)
- [ ] Telegram bot token + chat ID working
- [ ] Pushed to GitHub
- [ ] Deployed to Render (should show "Live")
- [ ] UptimeRobot monitor setup

---

## 🔍 Troubleshooting

### "Telegram not sending alerts"
- Check `TELEGRAM_BOT_TOKEN` format (harus ada `:`)
- Check `TELEGRAM_CHAT_ID` format (harus `-` at the front untuk group)
- Bot must be added to group/channel as member/admin

### "No pairs showing in dashboard"
- Binance API might be slow
- Dashboard refresh setiap 30 second - tunggu
- Check browser console (F12) untuk errors

### "Supabase connection error"
- Verify `SUPABASE_URL` (starts with `https://`)
- Verify `SUPABASE_KEY` (long string, no typo)
- Tables must exist - run `supabase_schema.sql`

### "Port already in use"
```bash
# Change PORT in .env
PORT=5001
```

---

**Yang lepas setup, system akan:**
✅ Scan semua Binance USDT pairs setiap M15 & M30
✅ Detect Extrem → MHV → Entry signals secara automatic
✅ Hantar real-time Telegram alerts
✅ Log semua signals dalam Supabase
✅ Show live dashboard at http://your-domain.onrender.com

**Lepas itu, kau tinggal monitor alerts.** Sistem jalan 24/7 di Render.

Apa masalah / ada soalan?

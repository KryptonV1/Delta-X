# ⚡ QUICK START (5 Minit)

## TL;DR - Copy-Paste These Steps

### 1️⃣ Setup (2 minit)
```bash
bash setup.sh
nano .env    # isi 4 values from steps below
```

### 2️⃣ Get Telegram Bot Token
- Telegram: `@BotFather` → `/newbot` → copy token
- Paste ke `.env` as `TELEGRAM_BOT_TOKEN`

### 3️⃣ Get Telegram Chat ID
- Create Telegram group/channel
- Add bot to it
- Go to: `https://api.telegram.org/bot<TOKEN>/getUpdates`
  - (replace `<TOKEN>` dengan token atas)
- Find: `"chat":{"id":-123456789}`
- Paste to `.env` as `TELEGRAM_CHAT_ID` (with minus sign)

### 4️⃣ Setup Supabase (2 minit)
- [supabase.com](https://supabase.com) → Sign up (free)
- New project → wait to build
- Settings → API:
  - Copy **Project URL** → `.env` as `SUPABASE_URL`
  - Copy **service_role secret** → `.env` as `SUPABASE_KEY`
- SQL Editor → paste `supabase_schema.sql` → Run

### 5️⃣ .env File Should Look Like:
```
TELEGRAM_BOT_TOKEN=123456:ABCdef1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_CHAT_ID=-123456789
SUPABASE_URL=https://abc123def456.supabase.co
SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9eyJpc3MiOiJzdXBhYmFzZSIsIm...
```

### 6️⃣ Push to GitHub
```bash
git init
git add .
git commit -m "Delta X initial"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/delta-x.git
git push -u origin main
```

### 7️⃣ Deploy to Render (< 1 minit)
- [render.com](https://render.com) → Sign up with GitHub → Sign in
- **New Web Service**
- Select `delta-x` repo
- (Keep defaults - Procfile will auto-configure)
- **Environment** tab → paste 4 values from .env
- **Create Web Service**
- Wait 3-5 minit for build

### 8️⃣ Keep Alive (1 minit)
- [uptimerobot.com](https://uptimerobot.com) → Sign up → Sign in
- **Add New Monitor**:
  - Type: **HTTP(s)**
  - URL: `https://delta-x-xxxxx.onrender.com/ping` (copy from Render dashboard)
  - Interval: **5 minutes**
- **Create Monitor**

---

## ✅ Verify Everything Working

```bash
# From your computer
curl https://delta-x-xxxxx.onrender.com/ping
# Should return: OK

# Or just open in browser
https://delta-x-xxxxx.onrender.com
# Should show dark dashboard with stats
```

---

## 🎉 Done!

System adalah **LIVE & RUNNING**. Kamu akan dapat:
- ✅ Telegram alerts setiap ada signal
- ✅ Real-time dashboard (auto refresh)
- ✅ Signal history dalam Supabase
- ✅ Running 24/7 di cloud (free tier)

**Signals automatically generated setiap M15 & M30.**

---

## 📖 For More Details

- `README.md` - Full documentation
- `SETUP.md` - Detailed setup guide dengan troubleshooting
- `DEPLOYMENT.md` - Render deployment detail

## 🆘 Quick Troubleshoot

| Problem | Fix |
|---------|-----|
| Alerts not coming | Check `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID` format |
| No signals | Dashboard refresh every 30s - just wait |
| DB error | Verify Supabase tables created (run schema SQL) |
| App won't start | Check Render logs → copy error → Google it |

---

Siapa ada issues, DM or check the detailed guides.

**Happy trading! 🚀**

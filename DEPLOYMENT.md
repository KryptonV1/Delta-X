# 🌐 Deploy to Render · Step-by-Step

**Time: ~5 minit (termasuk GitHub push)**

---

## A. Push Code to GitHub

```bash
cd delta_x
git init
git add .
git commit -m "Initial: Delta X BBMA signal provider"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/delta-x.git
git push -u origin main
```

✅ Code sekarang ada di GitHub.

---

## B. Deploy on Render

### 1. Go to render.com

- Sign up (free tier ada)
- Sign in dengan **GitHub**

### 2. Create New Web Service

Click **New +** → **Web Service**

![image](https://via.placeholder.com/400x300?text=Render+New+Web+Service)

### 3. Connect Repository

1. Click **Connect a repository**
2. Search `delta-x` repo
3. Click **Connect**

### 4. Configure

**Service Settings:**

| Setting | Value |
|---------|-------|
| **Name** | `delta-x` |
| **Environment** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120` |
| **Instance Type** | `Free` |

*(Sebab-sebab sudah ada Procfile/render.yaml, nilai akan auto-fill - confirm aja)*

### 5. Add Environment Variables

Sebelum deploy, click **Environment** tab:

| Key | Value |
|-----|-------|
| `TELEGRAM_BOT_TOKEN` | `123456:ABCdef...` *(dari @BotFather)* |
| `TELEGRAM_CHAT_ID` | `-123456789` atau `@channel` *(dari getUpdates)* |
| `SUPABASE_URL` | `https://abc123.supabase.co` |
| `SUPABASE_KEY` | `eyJhbGci...` *(service_role key)* |

**Important:** Jangan paste dalam code - paste dalam Render dashboard environment variables!

### 6. Deploy

1. **Scroll to bottom**
2. Click **Create Web Service** (orange button)
3. Wait 3-5 minit untuk build & deploy

✅ Lepas selesai, kamu dapat URL: `https://delta-x-xxxxx.onrender.com`

---

## C. Verify Deployment

### 1. Check Status

- Dashboard page harus show **"Live"** (green)
- Click URL untuk buka

### 2. Test Endpoints

```bash
# Test keep-alive
https://delta-x-xxxxx.onrender.com/ping
# Expected: OK

# Test API
https://delta-x-xxxxx.onrender.com/api/status
# Expected: JSON dengan system status
```

### 3. Check Logs

Click **Logs** dalam Render dashboard untuk see output:

```
2024-01-15 14:30:22 | INFO | DELTA X.main | DELTA X v1.0 started
2024-01-15 14:30:25 | INFO | DELTA X.main | Fetching Binance symbol list …
2024-01-15 14:30:28 | INFO | DELTA X.main | Monitoring 250 pairs
```

---

## D. Keep-Alive Setup (IMPORTANT!)

Render free tier **sleeps lepas 15 minit tanpa requests**.

### Setup UptimeRobot

1. Go to [uptimerobot.com](https://uptimerobot.com)
2. Sign up (free)
3. **Monitors** → **Add New Monitor**

| Setting | Value |
|---------|-------|
| **Monitor Type** | HTTP(s) |
| **URL** | `https://delta-x-xxxxx.onrender.com/ping` |
| **Monitoring Interval** | Every 5 minutes |
| **Alert Contact** | (skip or add email) |

4. Click **Create Monitor**

✅ Lepas ni, system akan jaga tetap awake 24/7.

---

## E. View Your Dashboard

Go to: `https://delta-x-xxxxx.onrender.com`

**Kamu akan lihat:**
- System status (Running)
- Pairs being monitored (250+)
- Active signals count
- Recent signals table
- Live trend monitor
- Real-time price ticker

---

## F. Verify Telegram Alerts

1. **Send test alert** *(optional, kena modify code temporarily)*
2. Atau tunggu lepas signal didapat - telegram message akan sampai automatically

---

## ⚠️ Important Notes

### Database
- Supabase free tier cukup untuk tracking 1000+ signals
- Storage: 500MB free (boleh track ~5000 signals dengan details)
- Bandwidth: 2GB/month (cukup)

### Compute
- Render free tier: **512MB RAM, shared CPU**
- Delta X needs ~100-150MB RAM untuk scan 250+ pairs
- Polling approach (vs WebSocket) menjadi ringan pada Render

### Rate Limits
- Binance: 1200 requests/minute free API
- Delta X batches pairs (20 at a time) untuk stay well below limit

### Monitoring
- Dashboard updates setiap 30 detik
- Signals logged instantly ke Supabase
- Telegram alerts sent immediately

---

## 🔧 If Something Goes Wrong

### App won't start
1. Check **Logs** dalam Render
2. Common issues:
   ```
   ModuleNotFoundError: No module named 'x'
   → Jalankan pip install -r requirements.txt locally untuk verify
   
   KeyError: 'TELEGRAM_BOT_TOKEN'
   → Pastikan semua 4 env vars ada dalam Render dashboard
   
   Connection refused (Supabase)
   → Verify SUPABASE_URL & SUPABASE_KEY format (no typo)
   ```

### No signals showing
1. Give it 15 minit - need 2 M15 scan cycles
2. Check Render logs untuk see if scanning happening
3. Verify Binance API working: 
   ```bash
   curl https://api.binance.com/api/v3/exchangeInfo
   ```

### Telegram alerts not sending
1. Test token: `https://api.telegram.org/bot<TOKEN>/getMe`
   - Should return JSON with bot info
2. Test chat ID: `https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>&text=test`
   - Should return `{ok:true}`
3. Verify bot is member of group/channel

### Dashboard slow/not updating
- Reload page (Ctrl+F5)
- Check browser console (F12) untuk JavaScript errors
- Verify Supabase queries working in logs

---

## 🎉 Success Indicators

✅ Render shows "Live" (green)
✅ Ping endpoint returns "OK"
✅ Dashboard loads & shows pair count
✅ UptimeRobot shows monitor as "Up"
✅ Telegram bot connected to group

**Lepas semua ni, DELTA X running 24/7 scanning signals untuk kau.**

Next step: Monitor the alerts coming to Telegram! 🚀

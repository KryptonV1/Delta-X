"""
config/settings.py — Delta X Global Configuration
"""
import os
from dotenv import load_dotenv
load_dotenv()

# ── Identity ─────────────────────────────────────────────────────────────────
SYSTEM_NAME    = "DELTA X"
SYSTEM_VERSION = "1.0.0"
BASE_CURRENCY  = "USDT"

# ── Binance ──────────────────────────────────────────────────────────────────
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_BASE_URL   = "https://api.binance.com"

# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_ADMIN_ID  = os.getenv("TELEGRAM_ADMIN_ID")

# ── Supabase ─────────────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# ── Flask ────────────────────────────────────────────────────────────────────
PORT       = int(os.getenv("PORT", 5000))
SECRET_KEY = os.getenv("SECRET_KEY", "delta-x-secret")

# ── Timeframes ───────────────────────────────────────────────────────────────
ENTRY_TIMEFRAMES = ["15m", "30m"]
TREND_TIMEFRAMES = ["1h", "4h", "1d"]
ALL_TIMEFRAMES   = TREND_TIMEFRAMES + ENTRY_TIMEFRAMES
BINANCE_TF_LABELS = {
    "15m":  "M15",
    "30m":  "M30",
    "1h":   "H1",
    "4h":   "H4",
    "1d":   "D1",
}

# ── BBMA Indicator Settings ──────────────────────────────────────────────────
BB_PERIOD    = 20
BB_DEVIATION = 2
MA5_PERIOD   = 5
MA10_PERIOD  = 10
MA50_PERIOD  = 50
CANDLE_LIMIT = 120

# ── Risk Management ──────────────────────────────────────────────────────────
MAX_LOSS_PERCENT  = 20.0
MIN_TP1_PERCENT   = 20.0
SL_BUFFER         = 0.005

# ── Advanced Filters (NEW) ───────────────────────────────────────────────────
TREND_FILTER_ENABLED = True          # Filter signal lawan trend H1/H4/D1
SPOT_MODE = True                     # True = hanya BUY signal (spot trading)
COOLDOWN_SECONDS = 3600              # 1 jam cooldown per pair (elak spam)
REQUIRE_CONFIRMATION = False         # Tunggu confirmation candle
MIN_BB_WIDTH_PERCENT = 0.02          # Min BB width (elak ranging market)
CONFIRMATION_CANDLES = 2             # Bilangan candle untuk confirm trend
NEAR_ENTRY_THRESHOLD = 0.02          # 2% threshold untuk near-entry warning

# ── Scheduler intervals (seconds) ────────────────────────────────────────────
INTERVAL_M15   = 15 * 60
INTERVAL_M30   = 30 * 60
INTERVAL_H1    = 60 * 60
INTERVAL_H4    = 4 * 60 * 60
INTERVAL_DAILY = 24 * 60 * 60
BATCH_SIZE     = 20
BATCH_DELAY    = 0.5

# ── Pair Filter ──────────────────────────────────────────────────────────────
STABLECOINS = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "FRAX", "GUSD", "LUSD",
    "SUSD", "UST", "USTC", "USDD", "FDUSD", "PYUSD", "EURC", "EURS", "AGEUR",
    "XSGD", "XIDR", "BIDR", "BKRW", "IDRT", "USDJ", "TRIBE", "FEI", "MXNT",
    "PAX", "HUSD", "VAI", "MUSD", "OUSD", "CUSD", "CEUR", "USDK", "USDX",
    "USDS", "USDE", "USD+", "GHO", "DOLA", "MAI", "BEAN", "FLOAT", "ESD",
    "BEUR", "AUSD", "CADC", "EURT", "XAUT", "PAXG", "USDV", "LISUSD",
}
WRAPPED_TOKENS = {
    "WBTC", "WETH", "WBNB", "WMATIC", "WAVAX", "WSOL", "WFTM", "WONE",
    "WCELO", "WKLAY", "BETH", "BBTC", "BTCB", "RENBTC", "SBTC", "HBTC",
    "ABTC", "TBTC", "CBBTC", "STBTC", "WSTETH", "RETH", "CBETH", "SFRXETH",
    "WEETH", "EZETH", "RSETH", "METH", "WBETH", "LSETH",
}
SKIP_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "LONG", "SHORT", "3L", "3S", "5L", "5S")

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

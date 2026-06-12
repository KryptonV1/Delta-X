"""
core/signals.py — BBMA Signal Detection State Machine v6 (BUY ONLY / SPOT)

Full BBMA Oma Ally cycle per SOP (TIDAK DIUBAH):
  EXTREM_MHV path : WATCHING → EXTREM → MHV → CSA → RE-ENTRY → SIGNAL
  CSM_REENTRY path: WATCHING → CSM_PULLBACK → RE-ENTRY → SIGNAL

v6 — accuracy & robustness fixes (logic BBMA kekal 100%):
  • State expiry     : setup luput selepas 20 candle tanpa progress (anti-stale)
  • CSA re-anchor    : Extreme baru semasa CSA → kembali ke EXTREM (ref segar)
  • CSM ref refresh  : CSM berturutan → guna candle CSM terbaru sebagai SL ref
  • Zone close tepat : close mesti ≥ MA10 Low (SOP: close mesti hold zon)
  • block_reason     : trend vs risk dibezakan untuk stats yang betul
  • Same-candle skip : candle sama tak diproses 2x (jimat compute, elak edge)
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
from config.settings import (
    SL_BUFFER,
    MAX_LOSS_PERCENT,
    MIN_SL_PCT,
    MIN_TP1_PCT,
    MIN_RR,
    TREND_FILTER_ENABLED,
    MIN_BB_WIDTH_PERCENT,
)
from utils.logger import get_logger

log = get_logger("signals")

# Candle duration per timeframe (untuk state expiry)
TF_SECONDS = {
    "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400,
}
MAX_STATE_AGE_CANDLES = 20   # setup luput selepas 20 candle tanpa progress


# ────────────────────────────────────────────────────────────────────────────
# Result data class
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class SignalResult:
    pair: str
    timeframe: str
    direction: str          # always "BUY"
    signal_type: str        # EXTREM_MHV | CSM_REENTRY
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    sl_pct: float
    tp1_pct: float
    tp2_pct: float
    tp3_pct: float
    bb_upper: float
    bb_middle: float
    bb_lower: float
    trend_h1: str = "NEUTRAL"
    trend_h4: str = "NEUTRAL"
    trend_daily: str = "NEUTRAL"
    timestamp: float = field(default_factory=time.time)


# ────────────────────────────────────────────────────────────────────────────
# Low-level detectors — formula SOP, TIDAK DIUBAH
# ────────────────────────────────────────────────────────────────────────────
def _detect_extrem_buy(row: pd.Series) -> bool:
    """
    Hukum 1 — EXTREME BUY: MA5 atau MA10 Low keluar BAWAH BB Lower.
    Body (close) MESTI kekal dalam BB — jika close pun keluar, itu CSM (Hukum 2).
    """
    ma_outside  = (row["ma5_low"] < row["bb_lower"]) or (row["ma10_low"] < row["bb_lower"])
    body_inside = row["close"] > row["bb_lower"]
    return bool(ma_outside and body_inside)


def _detect_csm_buy(row: pd.Series) -> bool:
    """Hukum 2 — CSM BUY: body candle (close) tutup ATAS BB Upper."""
    return bool(row["close"] > row["bb_upper"])


def _detect_csm_sell(row: pd.Series) -> bool:
    """CSM SELL: body tutup bawah BB Lower — batalkan semua setup BUY."""
    return bool(row["close"] < row["bb_lower"])


def _detect_mhv_buy(row: pd.Series) -> bool:
    """MHV BUY: MA5 Low DAN MA10 Low kedua-duanya kembali dalam BB."""
    return bool(row["ma5_low"] > row["bb_lower"] and row["ma10_low"] > row["bb_lower"])


def _detect_csa_buy(row: pd.Series) -> tuple[bool, bool]:
    """
    CSA BUY (pengesahan arah selepas Extreme + MHV):
      CSA Awal  → close melepasi MA5 Low & MA10 Low (masih dalam BB)
      CSA Kukuh → close juga melepasi Mid BB
    Returns: (early, strong)
    """
    early = bool(
        row["close"] > row["ma5_low"]
        and row["close"] > row["ma10_low"]
        and row["close"] < row["bb_upper"]       # belum CSM
        and row["ma5_low"]  > row["bb_lower"]    # MA dalam BB
        and row["ma10_low"] > row["bb_lower"]
    )
    strong = early and bool(row["close"] > row["bb_middle"])
    return early, strong


def _in_ma_zone_buy(row: pd.Series, touch_tol: float = 0.01) -> bool:
    """
    Zon RE-ENTRY BUY (SOP: harga turun uji kawasan MA5 Low / MA10 Low):
      • Low candle SENTUH zon (MA5 Low + toleransi 1% — threshold rendah)
      • Close MESTI hold ≥ MA10 Low (SOP: tiada close melawan arah dalam zon)
    """
    touched   = row["low"]   <= row["ma5_low"] * (1 + touch_tol)
    closed_ok = row["close"] >= row["ma10_low"]          # TEPAT — hold zon
    return bool(touched and closed_ok)


# ────────────────────────────────────────────────────────────────────────────
# Event constants
# ────────────────────────────────────────────────────────────────────────────
EV_NONE        = "none"
EV_EXTREM      = "extrem"
EV_MHV         = "mhv"
EV_CSA         = "csa"
EV_CSM         = "csm_detected"
EV_ENTRY_ZONE  = "entry_zone"
EV_RISK_BLOCK  = "risk_blocked"
EV_SIGNAL      = "signal"
EV_CSM_REENTRY = "csm_reentry"
EV_RESET       = "reset"
EV_NEAR_ENTRY  = "near_entry"    # compat untuk main.py import (tidak digunakan)


# ────────────────────────────────────────────────────────────────────────────
# Tracker
# ────────────────────────────────────────────────────────────────────────────
class BBMATracker:
    """
    Stateful BBMA tracker per (pair, timeframe) — BUY sahaja (spot).

    Fall-through loop: satu candle boleh gerakkan beberapa state sekaligus
    (cth. MHV + CSA + zon entry pada candle yang sama → terus SIGNAL).

    Atribut selepas update():
      .last_event   — event untuk stats scanner
      .block_reason — "trend" | "risk" | "" (sebab signal diblock di gate)
    """

    def __init__(self, pair: str, timeframe: str):
        self.pair      = pair
        self.timeframe = timeframe
        self._last_sig: tuple | None = None     # same-candle guard
        self.block_reason: str = ""
        self._reset()

    # ── Public ──────────────────────────────────────────────────────────────

    def update(
        self,
        df: pd.DataFrame,
        trends: dict | None = None,
    ) -> tuple[Optional[SignalResult], None]:
        if df is None or len(df) < 50:
            self.last_event = EV_NONE
            return None, None

        trends = trends or {}
        self.last_event   = EV_NONE
        self.block_reason = ""

        last = df.iloc[-1]

        # ── Same-candle guard: candle sama tak diproses dua kali ─────────────
        sig = (
            float(last["close"]), float(last["high"]),
            float(last["low"]),   float(last["ma50"]),
        )
        if sig == self._last_sig:
            return None, None
        self._last_sig = sig

        # ── State expiry: setup basi auto-luput ──────────────────────────────
        if self.state != "WATCHING":
            tf_sec  = TF_SECONDS.get(self.timeframe, 3600)
            max_age = MAX_STATE_AGE_CANDLES * tf_sec
            if (time.time() - self.state_ts) > max_age:
                log.debug(
                    f"{self.pair}/{self.timeframe} state {self.state} "
                    f"luput ({MAX_STATE_AGE_CANDLES} candle) — reset"
                )
                self._reset()
                self.last_event = EV_RESET
                # teruskan — candle ini mungkin mulakan setup baru

        # ── GLOBAL: CSM SELL batalkan semua setup BUY aktif ──────────────────
        if self.state != "WATCHING" and _detect_csm_sell(last):
            log.debug(
                f"{self.pair}/{self.timeframe} CSM SELL — "
                f"batal {self.state} BUY setup"
            )
            self._reset()
            self.last_event = EV_RESET
            return None, None

        # ════════════════ STATE MACHINE (fall-through loop) ══════════════════
        max_steps = 5
        for _ in range(max_steps):

            # ── WATCHING ─────────────────────────────────────────────────────
            if self.state == "WATCHING":

                if _detect_extrem_buy(last):
                    self._set_extrem(last)
                    self.last_event = EV_EXTREM
                    continue        # semak MHV pada candle sama

                if _detect_csm_buy(last):
                    self._set_state("CSM_PULLBACK")
                    self.csm_candle = last.copy()
                    self.last_event = EV_CSM
                    log.debug(f"{self.pair}/{self.timeframe} CSM BUY dikesan")
                    break           # CSM & zon entry tak boleh wujud serentak

                break

            # ── EXTREM: tunggu MHV ───────────────────────────────────────────
            elif self.state == "EXTREM":

                if _detect_mhv_buy(last):
                    csa_early, csa_strong = _detect_csa_buy(last)
                    if csa_early:
                        self._set_state("CSA")
                        self.csa_strong = csa_strong
                        self.last_event = EV_CSA
                        log.debug(
                            f"{self.pair}/{self.timeframe} MHV+CSA "
                            f"{'KUKUH' if csa_strong else 'AWAL'} (candle sama)"
                        )
                        continue    # semak zon entry pada candle sama
                    else:
                        self._set_state("MHV")
                        self.last_event = EV_MHV
                        log.debug(f"{self.pair}/{self.timeframe} MHV BUY disahkan")
                        continue    # semak CSA pada candle sama

                break               # MHV belum — tunggu

            # ── MHV: tunggu CSA ──────────────────────────────────────────────
            elif self.state == "MHV":

                # MA keluar BB semula → Extreme baru, ref candle segar
                if _detect_extrem_buy(last):
                    log.debug(
                        f"{self.pair}/{self.timeframe} EXTREM baru semasa MHV "
                        f"— ref candle dikemaskini"
                    )
                    self._set_extrem(last)
                    self.last_event = EV_EXTREM
                    break

                csa_early, csa_strong = _detect_csa_buy(last)
                if csa_early:
                    self._set_state("CSA")
                    self.csa_strong = csa_strong
                    self.last_event = EV_CSA
                    log.debug(
                        f"{self.pair}/{self.timeframe} CSA "
                        f"{'KUKUH' if csa_strong else 'AWAL'} disahkan"
                    )
                    continue        # semak zon entry pada candle sama

                break

            # ── CSA: tunggu RE-ENTRY (pullback ke zon MA) ────────────────────
            elif self.state == "CSA":

                # Extreme baru semasa CSA → harga jatuh dalam — JANGAN entry
                # atas pisau jatuh; re-anchor ke EXTREM dengan ref baru
                if _detect_extrem_buy(last):
                    log.debug(
                        f"{self.pair}/{self.timeframe} EXTREM baru semasa CSA "
                        f"— re-anchor (elak pisau jatuh)"
                    )
                    self._set_extrem(last)
                    self.last_event = EV_EXTREM
                    break

                # CSM BUY semasa CSA → upgrade (SL ref lebih ketat & terkini)
                if _detect_csm_buy(last):
                    self._set_state("CSM_PULLBACK")
                    self.csm_candle = last.copy()
                    self.last_event = EV_CSM
                    log.debug(
                        f"{self.pair}/{self.timeframe} CSM BUY semasa CSA — "
                        f"upgrade ke CSM_PULLBACK"
                    )
                    break

                if _in_ma_zone_buy(last):
                    self.last_event = EV_ENTRY_ZONE
                    signal = self._build_signal(last, trends, "EXTREM_MHV")
                    if signal is None:
                        self.last_event = EV_RISK_BLOCK
                        self._reset()
                        return None, None
                    self._reset()
                    self.last_event = EV_SIGNAL
                    return signal, None

                break

            # ── CSM_PULLBACK: tunggu RE-ENTRY selepas CSM ────────────────────
            elif self.state == "CSM_PULLBACK":

                # CSM berturutan → momentum sambung; ref SL guna CSM TERBARU
                if _detect_csm_buy(last):
                    self.csm_candle = last.copy()
                    self.state_ts   = time.time()    # reset umur state
                    self.last_event = EV_CSM
                    log.debug(
                        f"{self.pair}/{self.timeframe} CSM berturutan — "
                        f"ref candle di-refresh"
                    )
                    break

                if _in_ma_zone_buy(last):
                    self.last_event = EV_ENTRY_ZONE
                    signal = self._build_signal(last, trends, "CSM_REENTRY")
                    if signal is None:
                        self.last_event = EV_RISK_BLOCK
                        self._reset()
                        return None, None
                    self._reset()
                    self.last_event = EV_CSM_REENTRY
                    return signal, None

                # Harga tembus terus zon (entry terlepas) → reset
                if last["close"] < last["ma10_low"] * 0.97:
                    log.debug(
                        f"{self.pair}/{self.timeframe} Zon entry ditembusi — reset"
                    )
                    self._reset()
                    self.last_event = EV_RESET
                    return None, None

                break

            else:
                break

        return None, None

    # ── Private ─────────────────────────────────────────────────────────────

    def _reset(self):
        self.state         = "WATCHING"
        self.direction     = "BUY"
        self.extrem_candle = None
        self.csm_candle    = None
        self.csa_strong    = False
        self.last_event    = EV_NONE
        self.state_ts      = time.time()

    def _set_state(self, state: str):
        self.state    = state
        self.state_ts = time.time()

    def _set_extrem(self, candle: pd.Series):
        self._set_state("EXTREM")
        self.direction     = "BUY"
        self.extrem_candle = candle.copy()
        self.csm_candle    = None
        self.csa_strong    = False
        log.debug(
            f"{self.pair}/{self.timeframe} EXTREM BUY @ {candle['close']:.6f}"
        )

    def _build_signal(
        self,
        last: pd.Series,
        trends: dict,
        signal_type: str,
    ) -> Optional[SignalResult]:
        """
        Bina SignalResult BUY dengan 3 gate validasi.
        Jika diblock, self.block_reason di-set: "trend" | "risk".

        Formula SOP (TIDAK DIUBAH):
          SL  = low candle ref (Extreme / CSM) − buffer  [+ lantai min 2%]
          TP1 = terdekat antara Mid BB / MA5 High (atas entry)
          TP2 = BB Upper      [ascending dijamin]
          TP3 = TP2 + jarak (TP1 → SL)  [projection]
        """
        entry = float(last["close"])

        # ── Gate 1: BB width — elak pasaran ranging ──────────────────────────
        if float(last["bb_width"]) < MIN_BB_WIDTH_PERCENT:
            self.block_reason = "risk"
            log.debug(
                f"{self.pair}/{self.timeframe} BB sempit "
                f"({float(last['bb_width']):.4f}) — ranging, skip"
            )
            return None

        # ── Gate 2: Multi-TF (SOP Rule Emas: jangan lawan TF Major) ─────────
        if TREND_FILTER_ENABLED:
            trend_h4    = trends.get("4h", "NEUTRAL")
            trend_daily = trends.get("1d", "NEUTRAL")
            if trend_h4 == "BEARISH" or trend_daily == "BEARISH":
                self.block_reason = "trend"
                log.debug(
                    f"{self.pair}/{self.timeframe} BUY diblock trend — "
                    f"H4={trend_h4} D1={trend_daily}"
                )
                return None

        # ── SL ref candle ─────────────────────────────────────────────────────
        ref_candle = (
            self.extrem_candle if signal_type == "EXTREM_MHV"
            else self.csm_candle
        )
        if ref_candle is None:
            self.block_reason = "risk"
            log.warning(
                f"{self.pair}/{self.timeframe} ref_candle None "
                f"untuk {signal_type} — skip"
            )
            return None

        # ── SL (SOP: di luar low candle ref + ruang bernafas) ────────────────
        ref_low = float(ref_candle["low"])
        sl      = ref_low * (1 - SL_BUFFER)
        min_sl  = entry * (1 - MIN_SL_PCT / 100)   # lantai: min 2% bawah entry
        if sl > min_sl:
            sl = min_sl

        # ── TP (SOP: TP1 = Mid BB ATAU MA5 bertentangan; ascending) ──────────
        # Smart selection: kedua-dua calon adalah sah ikut SOP. Pilih calon
        # TERDEKAT yang memenuhi MIN_TP1_PCT (2%). Jika calon dekat < 2% tapi
        # calon jauh ≥ 2%, guna calon jauh — masih 100% SOP-compliant.
        min_tp1_price = entry * (1 + MIN_TP1_PCT / 100)
        all_cands = [
            x for x in (float(last["bb_middle"]), float(last["ma5_high"]))
            if x > entry
        ]
        valid_cands = [x for x in all_cands if x >= min_tp1_price]

        if valid_cands:
            raw_tp1 = min(valid_cands)           # terdekat yang ≥ 2%
        elif all_cands:
            raw_tp1 = max(all_cands)             # cuba calon paling jauh
        else:
            raw_tp1 = float(last["bb_middle"])   # fallback (akan gagal gate)

        raw_tp2 = float(last["bb_upper"])
        if raw_tp2 <= raw_tp1:
            tp1, tp2 = raw_tp2, raw_tp1          # swap bila BB squeeze
        else:
            tp1, tp2 = raw_tp1, raw_tp2
        tp3 = tp2 + abs(tp1 - sl)

        # ── Gate 3: Risk validation (min SL/TP1 2%, RR mampan) ───────────────
        sl_pct  = abs((sl - entry) / entry * 100)
        tp1_pct = (tp1 - entry) / entry * 100
        rr      = (tp1_pct / sl_pct) if sl_pct > 0 else 0

        fail = (
            sl  >= entry                          # SL mesti bawah entry
            or tp1 <= entry                       # TP1 mesti atas entry
            or sl_pct  > MAX_LOSS_PERCENT         # SL maks 6%
            or sl_pct  < MIN_SL_PCT - 0.01        # SL min 2% (tolerance float)
            or tp1_pct < MIN_TP1_PCT              # TP1 min 2% ← KEPERLUAN UTAMA
            or rr      < MIN_RR                   # TP1 ≥ 50% drpd SL
        )
        if fail:
            self.block_reason = "risk"
            log.debug(
                f"{self.pair}/{self.timeframe} risk gagal — "
                f"SL={sl_pct:.1f}% TP1={tp1_pct:.2f}% RR=1:{rr:.2f}"
            )
            return None

        def _pct(a: float, b: float) -> float:
            return round((a - b) / b * 100, 2)

        return SignalResult(
            pair=self.pair,
            timeframe=self.timeframe,
            direction="BUY",
            signal_type=signal_type,
            entry_price=entry,
            sl_price=round(sl,  8),
            tp1_price=round(tp1, 8),
            tp2_price=round(tp2, 8),
            tp3_price=round(tp3, 8),
            sl_pct=_pct(sl,  entry),
            tp1_pct=_pct(tp1, entry),
            tp2_pct=_pct(tp2, entry),
            tp3_pct=_pct(tp3, entry),
            bb_upper=float(last["bb_upper"]),
            bb_middle=float(last["bb_middle"]),
            bb_lower=float(last["bb_lower"]),
            trend_h1=trends.get("1h", "NEUTRAL"),
            trend_h4=trends.get("4h", "NEUTRAL"),
            trend_daily=trends.get("1d", "NEUTRAL"),
        )

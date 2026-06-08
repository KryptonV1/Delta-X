-- ─────────────────────────────────────────────────────────────────────────────
--  DELTA X · Supabase Schema
--  Run this once in the Supabase SQL Editor before starting the system.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Signals ──────────────────────────────────────────────────────────────────
create table if not exists signals (
    id          uuid primary key default gen_random_uuid(),
    signal_id   text unique not null,
    created_at  timestamptz not null default now(),

    -- Pair info
    pair        text not null,
    base_asset  text not null,
    timeframe   text not null,

    -- Signal classification
    direction   text not null check (direction in ('BUY','SELL')),
    signal_type text not null,

    -- Prices
    entry_price numeric(24,8) not null,
    sl_price    numeric(24,8) not null,
    tp1_price   numeric(24,8) not null,
    tp2_price   numeric(24,8),
    tp3_price   numeric(24,8),

    -- Risk/reward percentages
    sl_pct      numeric(8,2) not null,
    tp1_pct     numeric(8,2) not null,
    tp2_pct     numeric(8,2),
    tp3_pct     numeric(8,2),

    -- Trend context at signal time
    trend_h1    text,
    trend_h4    text,
    trend_daily text,

    -- BB levels at signal time
    bb_upper    numeric(24,8),
    bb_middle   numeric(24,8),
    bb_lower    numeric(24,8),

    -- Lifecycle
    status      text not null default 'ACTIVE'
                check (status in ('ACTIVE','HIT_TP1','HIT_TP2','HIT_TP3','HIT_SL','EXPIRED','CANCELLED')),
    closed_at   timestamptz,
    close_price numeric(24,8),
    pnl_pct     numeric(8,2)
);

create index if not exists idx_signals_pair      on signals(pair);
create index if not exists idx_signals_created   on signals(created_at desc);
create index if not exists idx_signals_status    on signals(status);

-- ── System logs ───────────────────────────────────────────────────────────────
create table if not exists system_logs (
    id         uuid primary key default gen_random_uuid(),
    created_at timestamptz not null default now(),
    level      text not null,
    module     text not null,
    message    text not null
);

create index if not exists idx_logs_created on system_logs(created_at desc);

-- ── Row-Level Security (optional but recommended) ─────────────────────────────
alter table signals     enable row level security;
alter table system_logs enable row level security;

-- Allow service role full access (your backend uses service role key)
create policy "service_role_signals"
  on signals for all
  using (true) with check (true);

create policy "service_role_logs"
  on system_logs for all
  using (true) with check (true);

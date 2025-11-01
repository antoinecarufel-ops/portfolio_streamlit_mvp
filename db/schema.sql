-- Supabase / Postgres schema for MVP

create table if not exists public.holdings (
  symbol text primary key,
  quantity double precision not null default 0,
  cost_basis double precision not null default 0,
  currency text not null default 'CAD',
  updated_at timestamptz default now()
);

create table if not exists public.prices_daily (
  id bigserial primary key,
  symbol text not null,
  price double precision not null,
  asof date not null,
  inserted_at timestamptz default now()
);

-- Optional indexes
create index if not exists idx_prices_symbol_asof on public.prices_daily(symbol, asof desc);

-- (Optional) RLS policies for private use can be disabled; if enabling auth later, add policies accordingly.

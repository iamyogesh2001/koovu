-- Koovu account sync schema
-- Paste this whole file into Supabase: SQL Editor -> New query -> Run.

-- One row per user. API keys are encrypted CLIENT-SIDE before they ever
-- reach this table (PBKDF2 from the user's password + per-user salt),
-- so even a full database leak exposes only ciphertext.
create table if not exists public.user_settings (
  user_id     uuid primary key references auth.users (id) on delete cascade,
  settings    jsonb not null default '{}'::jsonb,
  keys_enc    text,                          -- Fernet blob of {groq, deepgram}
  kdf_salt    text,                          -- per-user PBKDF2 salt (base64)
  updated_at  timestamptz not null default now()
);

-- Row Level Security: enforced by Postgres itself. A user can only ever
-- touch their own row, no matter what the client sends.
alter table public.user_settings enable row level security;

drop policy if exists "select own"  on public.user_settings;
drop policy if exists "insert own"  on public.user_settings;
drop policy if exists "update own"  on public.user_settings;
drop policy if exists "delete own"  on public.user_settings;

create policy "select own" on public.user_settings
  for select using (auth.uid() = user_id);
create policy "insert own" on public.user_settings
  for insert with check (auth.uid() = user_id);
create policy "update own" on public.user_settings
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "delete own" on public.user_settings
  for delete using (auth.uid() = user_id);

-- Keep updated_at accurate on every write.
create or replace function public.touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists user_settings_touch on public.user_settings;
create trigger user_settings_touch
  before update on public.user_settings
  for each row execute function public.touch_updated_at();

-- Lock the table down for anonymous API access entirely (RLS already
-- blocks it, this is belt-and-braces).
revoke all on public.user_settings from anon;
grant select, insert, update, delete on public.user_settings to authenticated;

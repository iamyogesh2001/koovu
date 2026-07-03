-- Koovu landing page: download counter + email waitlist
-- Run in Supabase SQL Editor (project kvvdscjpyvodmnuuqwul)

create table if not exists public.stats (
  key   text primary key,
  count integer not null default 0
);

insert into public.stats (key, count)
values ('downloads', 10)
on conflict (key) do nothing;

create table if not exists public.waitlist (
  id         bigint generated always as identity primary key,
  email      text not null,
  created_at timestamptz not null default now(),
  constraint waitlist_email_unique unique (email)
);

alter table public.stats enable row level security;
alter table public.waitlist enable row level security;

drop policy if exists "stats public read" on public.stats;
create policy "stats public read" on public.stats
  for select using (true);

drop policy if exists "waitlist anon insert" on public.waitlist;
create policy "waitlist anon insert" on public.waitlist
  for insert with check (true);

revoke all on public.stats from anon;
grant select on public.stats to anon;

revoke all on public.waitlist from anon;
grant insert on public.waitlist to anon;

-- Called by the increment-downloads edge function (service role).
create or replace function public.increment_downloads()
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare new_count integer;
begin
  update public.stats
  set count = count + 1
  where key = 'downloads'
  returning count into new_count;
  return coalesce(new_count, 10);
end;
$$;

revoke all on function public.increment_downloads() from public;
grant execute on function public.increment_downloads() to service_role;

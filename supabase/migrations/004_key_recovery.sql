create table if not exists public.key_recovery (
  user_id uuid primary key,
  provider text not null check (provider = 'google-cloud-kms'),
  kms_key_name text not null,
  wrapped_data_key text not null,
  key_fingerprint text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
alter table public.key_recovery enable row level security;
alter table public.key_recovery force row level security;
revoke all on table public.key_recovery from anon, authenticated;
grant all on table public.key_recovery to service_role;
create policy "key_recovery_deny_clients" on public.key_recovery
  for all to anon, authenticated using (false) with check (false);

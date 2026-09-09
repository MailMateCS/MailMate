create table if not exists public.encrypted_state (
  record_key text primary key,
  user_id uuid not null,
  record_type text not null check (record_type in ('mail_context', 'work_state', 'sync_state')),
  payload_ciphertext text not null,
  payload_nonce text not null,
  encryption_version integer not null default 1,
  updated_at timestamptz not null default now(),
  expires_at timestamptz
);

create index if not exists encrypted_state_owner_idx
  on public.encrypted_state (user_id, record_type, expires_at, updated_at desc);

alter table public.encrypted_state enable row level security;
alter table public.encrypted_state force row level security;

revoke all on table public.encrypted_state from anon, authenticated;
grant all on table public.encrypted_state to service_role;

create policy "encrypted_state_deny_clients" on public.encrypted_state
  for all to anon, authenticated
  using (false)
  with check (false);

alter table public.mail_accounts force row level security;
alter table public.mail_context force row level security;
alter table public.active_ui_context force row level security;
alter table public.active_work_state force row level security;
revoke all on table public.mail_accounts, public.mail_context,
  public.active_ui_context, public.active_work_state from anon, authenticated;

alter policy "mail_accounts_select_own" on public.mail_accounts using ((select auth.uid()) = user_id);
alter policy "mail_accounts_insert_own" on public.mail_accounts with check ((select auth.uid()) = user_id);
alter policy "mail_accounts_update_own" on public.mail_accounts using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
alter policy "mail_accounts_delete_own" on public.mail_accounts using ((select auth.uid()) = user_id);
alter policy "mail_context_select_own" on public.mail_context using ((select auth.uid()) = user_id);
alter policy "mail_context_insert_own" on public.mail_context with check ((select auth.uid()) = user_id);
alter policy "mail_context_update_own" on public.mail_context using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
alter policy "mail_context_delete_own" on public.mail_context using ((select auth.uid()) = user_id);
alter policy "active_ui_context_select_own" on public.active_ui_context using ((select auth.uid()) = user_id);
alter policy "active_ui_context_insert_own" on public.active_ui_context with check ((select auth.uid()) = user_id);
alter policy "active_ui_context_update_own" on public.active_ui_context using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
alter policy "active_ui_context_delete_own" on public.active_ui_context using ((select auth.uid()) = user_id);
alter policy "active_work_state_select_own" on public.active_work_state using ((select auth.uid()) = user_id);
alter policy "active_work_state_insert_own" on public.active_work_state with check ((select auth.uid()) = user_id);
alter policy "active_work_state_update_own" on public.active_work_state using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
alter policy "active_work_state_delete_own" on public.active_work_state using ((select auth.uid()) = user_id);

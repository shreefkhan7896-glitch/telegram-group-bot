-- Additive, rerunnable schema update for gatekeeping and owner SaaS billing.
-- Run in the production Supabase SQL Editor before deploying the matching bot.

begin;

alter table public.groups
    add column if not exists gatekeeper_enabled boolean not null default true,
    add column if not exists anti_spam_enabled boolean not null default true,
    add column if not exists flood_control_enabled boolean not null default true,
    add column if not exists welcome_message text,
    add column if not exists faq_replies jsonb not null default '{}'::jsonb;

create table if not exists public.admin_accounts (
    user_id bigint primary key,
    trial_started_at timestamptz not null default now(),
    trial_ends_at timestamptz not null default (now() + interval '7 days'),
    subscription_status text not null default 'TRIAL'
        check (subscription_status in ('TRIAL', 'ACTIVE', 'LOCKED')),
    paid_until timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.saas_payments (
    id bigint generated always as identity primary key,
    user_id bigint not null,
    amount numeric(10,2) not null check (amount > 0),
    billing_days integer not null default 30 check (billing_days > 0),
    utr_number text not null,
    screenshot_file_id text not null,
    status text not null default 'PENDING'
        check (status in ('PENDING', 'APPROVED', 'REJECTED')),
    reviewed_by bigint,
    reviewed_at timestamptz,
    created_at timestamptz not null default now()
);

alter table public.admin_accounts enable row level security;
alter table public.saas_payments enable row level security;

create index if not exists admin_accounts_status_until_idx
    on public.admin_accounts (subscription_status, paid_until, trial_ends_at);
create index if not exists saas_payments_status_created_idx
    on public.saas_payments (status, created_at desc);
create index if not exists saas_payments_user_created_idx
    on public.saas_payments (user_id, created_at desc);
create index if not exists members_active_expiry_idx
    on public.members (expiry_date)
    where status = 'ACTIVE';
create index if not exists payments_group_status_created_idx
    on public.payments (group_id, status, created_at desc);

-- Keep all existing customers and give already-registered owners a one-time
-- seven-day launch trial. Re-running this migration will not reset their dates.
insert into public.admin_accounts (user_id, trial_started_at, trial_ends_at, subscription_status)
select distinct owner_user_id, now(), now() + interval '7 days', 'TRIAL'
from public.groups
where owner_user_id is not null
on conflict (user_id) do nothing;

-- SaaS UTR uniqueness is independent from the member payments table.
create unique index if not exists saas_payments_utr_unique
    on public.saas_payments (utr_number);

commit;

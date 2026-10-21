-- Additive, rerunnable schema update #2: read-only (broadcast) mode + invite DM tracking.
-- Run in the production Supabase SQL Editor AFTER
-- 20261021_000001_group_saas_features.sql

begin;

-- Point 1: dashboard toggle. false (default) = read-only/broadcast mode
-- (members ko message box nahi dikhta, sirf Mute/Unmute button).
-- true = members group me chat kar sakte hain (paid members ko access milega).
alter table public.groups
    add column if not exists member_chat_enabled boolean not null default false;

-- Point 4: approve par bheje gaye join-link DM ka message_id,
-- taaki join approve hone par usi message ko "safaltapurvak jud gaye" me edit kiya ja sake.
alter table public.members
    add column if not exists invite_message_id bigint;

commit;

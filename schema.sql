-- Ventorme — схема базы Supabase (уже применена к проекту uumdxurztnvykksrfbim).
-- Файл нужен, чтобы развернуть ту же схему в другом проекте: Supabase → SQL Editor → Run.

-- ============ helpers ============
create or replace function public.touch_updated_at() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.updated_at := now();
  return new;
end $$;

create or replace function public.assert_timezone() returns trigger
language plpgsql set search_path = '' as $$
begin
  if not exists (select 1 from pg_catalog.pg_timezone_names where name = new.timezone) then
    raise exception 'Unknown timezone: %', new.timezone using errcode = '22023';
  end if;
  return new;
end $$;

-- ============ profiles (серия, XP, роль, академический профиль) ============
create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  full_name text check (char_length(full_name) <= 120),
  role text not null default 'student' check (role in ('student','parent')),
  grade text check (char_length(grade) <= 40),
  target_country text check (char_length(target_country) <= 80),
  plan text not null default 'free' check (plan in ('free','plus')),
  xp integer not null default 0,
  current_streak integer not null default 0,
  longest_streak integer not null default 0,
  last_active_date date,
  timezone text not null default 'Asia/Almaty',
  link_code text not null unique default ('VTM-' || upper(substr(md5(gen_random_uuid()::text), 1, 6))),
  gpa numeric(3,2) check (gpa between 0 and 5),
  ielts numeric(2,1) check (ielts between 0 and 9),
  sat integer check (sat between 400 and 1600),
  budget_usd integer check (budget_usd between 0 and 200000),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
alter table public.profiles enable row level security;
create policy "profiles_select_own" on public.profiles for select to authenticated using ((select auth.uid()) = id);
create policy "profiles_update_own" on public.profiles for update to authenticated using ((select auth.uid()) = id) with check ((select auth.uid()) = id);
revoke all on public.profiles from anon, authenticated;
grant select on public.profiles to authenticated;
-- xp, серия и тариф меняются только функциями на сервере
grant update (full_name, role, grade, target_country, timezone, gpa, ielts, sat, budget_usd) on public.profiles to authenticated;
create trigger profiles_touch before update on public.profiles for each row execute function public.touch_updated_at();
create trigger profiles_tz before insert or update of timezone on public.profiles for each row execute function public.assert_timezone();

create or replace function public.handle_new_user() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  insert into public.profiles (id, full_name, role)
  values (
    new.id,
    left(coalesce(nullif(new.raw_user_meta_data->>'full_name', ''), split_part(new.email, '@', 1)), 120),
    case when new.raw_user_meta_data->>'role' = 'parent' then 'parent' else 'student' end
  )
  on conflict (id) do nothing;
  return new;
end $$;
create trigger on_auth_user_created after insert on auth.users for each row execute function public.handle_new_user();

-- ============ анкета ============
create table public.quiz_results (
  id bigint generated always as identity primary key,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  answers jsonb not null,
  directions jsonb not null,
  created_at timestamptz not null default now()
);
create index quiz_results_user_idx on public.quiz_results (user_id, created_at desc);
alter table public.quiz_results enable row level security;
create policy "quiz_select_own" on public.quiz_results for select to authenticated using ((select auth.uid()) = user_id);
create policy "quiz_insert_own" on public.quiz_results for insert to authenticated with check ((select auth.uid()) = user_id);
revoke all on public.quiz_results from anon, authenticated;
grant select, insert on public.quiz_results to authenticated;

-- ============ активность и серия ============
create table public.activity_log (
  user_id uuid not null references auth.users(id) on delete cascade,
  day date not null,
  xp integer not null default 0,
  primary key (user_id, day)
);
alter table public.activity_log enable row level security;
create policy "activity_select_own" on public.activity_log for select to authenticated using ((select auth.uid()) = user_id);
revoke all on public.activity_log from anon, authenticated;
grant select on public.activity_log to authenticated;

-- check_in: засчитывает день (+5 XP за первый визит), продлевает или сбрасывает серию, начисляет XP (максимум 50 за вызов)
create or replace function public.check_in(p_xp integer default 0)
returns public.profiles
language plpgsql security definer set search_path = '' as $$
declare
  v_uid uuid := auth.uid();
  v_prof public.profiles;
  v_today date;
  v_xp integer := least(greatest(coalesce(p_xp, 0), 0), 50);
  v_streak integer;
begin
  if v_uid is null then
    raise exception 'not authenticated' using errcode = '28000';
  end if;
  select * into v_prof from public.profiles where id = v_uid for update;
  if not found then
    insert into public.profiles (id) values (v_uid) returning * into v_prof;
  end if;
  v_today := (now() at time zone v_prof.timezone)::date;
  if v_prof.last_active_date is distinct from v_today then
    v_xp := v_xp + 5;
  end if;
  v_streak := case
    when v_prof.last_active_date = v_today then v_prof.current_streak
    when v_prof.last_active_date = v_today - 1 then v_prof.current_streak + 1
    else 1 end;
  insert into public.activity_log (user_id, day, xp) values (v_uid, v_today, v_xp)
  on conflict (user_id, day) do update set xp = public.activity_log.xp + excluded.xp;
  update public.profiles set
    xp = xp + v_xp,
    current_streak = v_streak,
    longest_streak = greatest(longest_streak, v_streak),
    last_active_date = v_today
  where id = v_uid
  returning * into v_prof;
  return v_prof;
end $$;
revoke execute on function public.check_in(integer) from public, anon;
grant execute on function public.check_in(integer) to authenticated;

-- ============ план (задачи и дедлайны) ============
create table public.tasks (
  id bigint generated always as identity primary key,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  title text not null check (char_length(title) between 1 and 200),
  category text not null default 'Другое' check (char_length(category) <= 40),
  priority text not null default 'medium' check (priority in ('low','medium','high')),
  deadline date,
  done boolean not null default false,
  done_at timestamptz,
  created_at timestamptz not null default now()
);
create index tasks_user_idx on public.tasks (user_id);
alter table public.tasks enable row level security;
create policy "tasks_all_own" on public.tasks for all to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
revoke all on public.tasks from anon;

-- ============ сохранённые вузы и стипендии ============
create table public.saved_items (
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  kind text not null check (kind in ('university','scholarship')),
  item_id text not null check (char_length(item_id) <= 64),
  status text not null default 'saved' check (status in ('saved','applying','applied','accepted','rejected')),
  created_at timestamptz not null default now(),
  primary key (user_id, kind, item_id)
);
alter table public.saved_items enable row level security;
create policy "saved_all_own" on public.saved_items for all to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
revoke all on public.saved_items from anon;

-- ============ эссе ============
create table public.essays (
  id bigint generated always as identity primary key,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  title text not null default 'Personal Statement' check (char_length(title) <= 120),
  body text not null default '' check (char_length(body) <= 20000),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index essays_user_idx on public.essays (user_id);
alter table public.essays enable row level security;
create policy "essays_all_own" on public.essays for all to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
revoke all on public.essays from anon;
create trigger essays_touch before update on public.essays for each row execute function public.touch_updated_at();

-- ============ мини-тесты по экзаменам ============
create table public.exam_attempts (
  id bigint generated always as identity primary key,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  exam text not null check (exam in ('SAT','IELTS','TOEFL','ENT')),
  section text check (char_length(section) <= 40),
  correct integer not null check (correct >= 0),
  total integer not null check (total > 0 and correct <= total),
  created_at timestamptz not null default now()
);
create index exam_attempts_user_idx on public.exam_attempts (user_id, created_at);
alter table public.exam_attempts enable row level security;
create policy "exam_select_own" on public.exam_attempts for select to authenticated using ((select auth.uid()) = user_id);
create policy "exam_insert_own" on public.exam_attempts for insert to authenticated with check ((select auth.uid()) = user_id);
revoke all on public.exam_attempts from anon, authenticated;
grant select, insert on public.exam_attempts to authenticated;

-- ============ Telegram-напоминания ============
create table public.telegram_subscriptions (
  user_id uuid primary key default auth.uid() references auth.users(id) on delete cascade,
  username text not null check (username ~ '^[a-z0-9_]{4,32}$'),
  chat_id bigint unique,
  link_token text not null unique default replace(gen_random_uuid()::text, '-', ''),
  remind_time time not null default '19:00',
  timezone text not null default 'Asia/Almaty',
  enabled boolean not null default true,
  linked_at timestamptz,
  last_sent_on date,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index telegram_username_idx on public.telegram_subscriptions (username);
alter table public.telegram_subscriptions enable row level security;
create policy "tg_select_own" on public.telegram_subscriptions for select to authenticated using ((select auth.uid()) = user_id);
create policy "tg_insert_own" on public.telegram_subscriptions for insert to authenticated with check ((select auth.uid()) = user_id);
create policy "tg_update_own" on public.telegram_subscriptions for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy "tg_delete_own" on public.telegram_subscriptions for delete to authenticated using ((select auth.uid()) = user_id);
revoke all on public.telegram_subscriptions from anon, authenticated;
grant select, delete on public.telegram_subscriptions to authenticated;
-- chat_id, linked_at и last_sent_on пишет только бот (service_role)
grant insert (user_id, username, remind_time, timezone, enabled) on public.telegram_subscriptions to authenticated;
grant update (username, remind_time, timezone, enabled) on public.telegram_subscriptions to authenticated;

create or replace function public.tg_normalize() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.username := lower(btrim(new.username));
  new.username := ltrim(new.username, '@');
  if tg_op = 'UPDATE' then
    if new.username is distinct from old.username then
      new.chat_id := null;
      new.linked_at := null;
    end if;
    new.updated_at := now();
  end if;
  return new;
end $$;
create trigger tg_normalize before insert or update on public.telegram_subscriptions for each row execute function public.tg_normalize();
create trigger tg_tz before insert or update of timezone on public.telegram_subscriptions for each row execute function public.assert_timezone();

-- ============ родитель ↔ ребёнок ============
create table public.parent_links (
  parent_id uuid not null references auth.users(id) on delete cascade,
  child_id uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (parent_id, child_id),
  check (parent_id <> child_id)
);
create index parent_links_child_idx on public.parent_links (child_id);
alter table public.parent_links enable row level security;
create policy "links_select_member" on public.parent_links for select to authenticated using ((select auth.uid()) in (parent_id, child_id));
create policy "links_delete_member" on public.parent_links for delete to authenticated using ((select auth.uid()) in (parent_id, child_id));
revoke all on public.parent_links from anon, authenticated;
grant select, delete on public.parent_links to authenticated;

create or replace function public.link_child(p_code text)
returns json
language plpgsql security definer set search_path = '' as $$
declare
  v_uid uuid := auth.uid();
  v_child public.profiles;
begin
  if v_uid is null then
    raise exception 'not authenticated' using errcode = '28000';
  end if;
  select * into v_child from public.profiles where link_code = upper(btrim(p_code));
  if not found then
    raise exception 'Код не найден' using errcode = 'P0002';
  end if;
  if v_child.id = v_uid then
    raise exception 'Это ваш собственный код' using errcode = '22023';
  end if;
  insert into public.parent_links (parent_id, child_id) values (v_uid, v_child.id) on conflict do nothing;
  return json_build_object('child_id', v_child.id, 'full_name', v_child.full_name);
end $$;
revoke execute on function public.link_child(text) from public, anon;
grant execute on function public.link_child(text) to authenticated;

create or replace function public.get_children()
returns table (
  child_id uuid, full_name text, xp integer, current_streak integer, longest_streak integer,
  last_active_date date, tasks_total integer, tasks_done integer, next_deadline date,
  universities integer, applications integer, exam_attempts integer
)
language sql stable security definer set search_path = '' as $$
  select p.id, p.full_name, p.xp,
         case when p.last_active_date >= (now() at time zone p.timezone)::date - 1 then p.current_streak else 0 end,
         p.longest_streak, p.last_active_date,
         (select count(*)::int from public.tasks t where t.user_id = p.id),
         (select count(*)::int from public.tasks t where t.user_id = p.id and t.done),
         (select min(t.deadline) from public.tasks t where t.user_id = p.id and not t.done and t.deadline >= current_date),
         (select count(*)::int from public.saved_items s where s.user_id = p.id and s.kind = 'university'),
         (select count(*)::int from public.saved_items s where s.user_id = p.id and s.kind = 'university' and s.status in ('applied','accepted','rejected')),
         (select count(*)::int from public.exam_attempts e where e.user_id = p.id)
  from public.parent_links pl
  join public.profiles p on p.id = pl.child_id
  where pl.parent_id = (select auth.uid());
$$;
revoke execute on function public.get_children() from public, anon;
grant execute on function public.get_children() to authenticated;

-- ============ заявки от школ, вузов, профориентологов, на тариф Plus ============
create table public.leads (
  id bigint generated always as identity primary key,
  kind text not null check (kind in ('school','university','counselor','parent','other')),
  name text not null check (char_length(name) between 1 and 120),
  organization text check (char_length(organization) <= 200),
  contact text not null check (char_length(contact) between 3 and 200),
  message text check (char_length(message) <= 2000),
  created_at timestamptz not null default now()
);
alter table public.leads enable row level security;
create policy "leads_insert_public" on public.leads for insert to anon, authenticated with check (char_length(contact) >= 3);
revoke all on public.leads from anon, authenticated;
grant insert on public.leads to anon, authenticated;

-- ============ публичный конфиг (username бота для ссылки на сайте) ============
create table public.app_config (
  key text primary key,
  value text not null,
  updated_at timestamptz not null default now()
);
alter table public.app_config enable row level security;
create policy "config_read_public" on public.app_config for select to anon, authenticated using (true);
revoke all on public.app_config from anon, authenticated;
grant select on public.app_config to anon, authenticated;

revoke execute on function public.handle_new_user() from public, anon, authenticated;
revoke execute on function public.touch_updated_at() from public, anon, authenticated;
revoke execute on function public.assert_timezone() from public, anon, authenticated;
revoke execute on function public.tg_normalize() from public, anon, authenticated;

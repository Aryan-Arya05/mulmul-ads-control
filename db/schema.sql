-- Mulmul dashboards: access and data. Lives in the Mulmul Threads Supabase project (mulmul-workspace).
-- Applied as migration "dashboards_access_and_docs". Kept here for reference; do not run twice.

-- Who may open which dashboard. Threads founders and admins can open every dashboard and manage this table.
create table public.dash_access (
  user_id uuid not null references public.profiles(id) on delete cascade,
  dashboard text not null,
  level text not null default 'view' check (level in ('view','admin')),
  granted_by uuid references public.profiles(id) on delete set null,
  created_at timestamptz not null default now(),
  primary key (user_id, dashboard)
);

-- The data each dashboard reads, one row per document (daily, weekly, periods, products, audience, segments).
create table public.dash_docs (
  dashboard text not null,
  name text not null,
  data jsonb not null,
  updated_at timestamptz not null default now(),
  primary key (dashboard, name)
);

-- Every saved version of a document, so past weeks and days stay available.
create table public.dash_doc_history (
  id bigint generated always as identity primary key,
  dashboard text not null,
  name text not null,
  data jsonb not null,
  saved_at timestamptz not null default now()
);

-- Row level security: a signed-in person reads a dashboard's rows only if private.can_view_dash(dashboard) is true.
-- Nobody writes from the browser; data is saved with private.dash_set(dashboard, name, data).

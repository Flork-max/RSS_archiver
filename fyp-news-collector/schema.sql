-- Run once in the Supabase SQL Editor. Keep this internal schema unexposed.
create schema if not exists ingestion;
revoke all on schema ingestion from public, anon, authenticated;

create table if not exists ingestion.feed_state (
  feed_name text primary key, url text not null, etag text, last_modified text
);
create table if not exists ingestion.fetch_log (
  id bigint generated always as identity primary key,
  feed_name text not null, feed_url text not null, fetched_utc timestamptz not null,
  http_status integer not null, entries integer not null default 0,
  n_new integer not null default 0, n_changed integer not null default 0,
  error text not null default ''
);
create table if not exists ingestion.raw_snapshots (
  id bigint generated always as identity primary key,
  fetch_id bigint not null references ingestion.fetch_log(id),
  final_url text not null, response_headers jsonb not null,
  body bytea not null, sha256 text not null
);
create table if not exists ingestion.items (
  id text primary key, link text not null, guid text not null,
  publisher text not null, domain text not null, title text not null, summary text not null,
  published_utc timestamptz, first_seen_utc timestamptz not null,
  last_changed_utc timestamptz not null, content_hash text not null
);
create table if not exists ingestion.item_versions (
  id bigint generated always as identity primary key,
  item_id text not null references ingestion.items(id),
  snapshot_id bigint not null references ingestion.raw_snapshots(id),
  seen_utc timestamptz not null, title text not null, summary text not null,
  published_utc timestamptz, content_hash text not null
);
create table if not exists ingestion.feed_items (
  feed_name text not null, item_id text not null references ingestion.items(id),
  family_hint text not null, first_seen_utc timestamptz not null,
  last_seen_utc timestamptz not null, primary key(feed_name,item_id)
);
create index if not exists items_seen_idx on ingestion.items(first_seen_utc);
create index if not exists versions_asof_idx on ingestion.item_versions(item_id,seen_utc);
create index if not exists fetch_time_idx on ingestion.fetch_log(fetched_utc);
-- Internal backend tables: no browser access or public policies.
alter table ingestion.feed_state enable row level security;
alter table ingestion.fetch_log enable row level security;
alter table ingestion.raw_snapshots enable row level security;
alter table ingestion.items enable row level security;
alter table ingestion.item_versions enable row level security;
alter table ingestion.feed_items enable row level security;

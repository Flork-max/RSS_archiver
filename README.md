# FYP news collection into Supabase

This collects RSS/Atom metadata and feed summaries in one pass, stores it in Supabase Postgres, then exits. GitHub Actions repeats the pass approximately every 15 minutes. It is the RSS part of the FYP platform layer; it does not yet implement GDELT ingestion, event extraction, corroboration, or risk prediction.

## 1 Create the database tables

Create your Supabase project. Open SQL Editor and execute `schema.sql` once. It creates the internal `ingestion` schema; select that schema in Table Editor to inspect its tables. Do not add it to the Data API's exposed schemas. The frontend should later read separately approved tables/views through your backend.

| Table | Contents |
| --- | --- |
| items | Latest collected headline, summary and publisher date for each article URL |
| item_versions | Initial version and every subsequent observed content change |
| feed_items | Which feeds returned an article and when |
| raw_snapshots | Every HTTP 200 response body, headers, URL and SHA-256 hash, including parse failures |
| fetch_log | Each completed feed attempt, error, new and changed item counts |
| feed_state | Persistent ETag and Last-Modified cache validators |

Responses are stored after HTTP decompression, before parsing or text cleaning. Successful responses and their article writes are saved atomically. HTTP failures/304s are logged without snapshot bodies. The script only inserts snapshots and versions; this is application-level append-only behavior, not protection against an administrator deleting them. A process killed before commit cannot log its own failure: check Actions run history for that case.

## 2 Get the connection string

In Supabase select Connect, then copy the **Session pooler** PostgreSQL URI. It supports IPv4, making it convenient for GitHub-hosted runners. Replace the password placeholder with your database password; percent-encode reserved password characters when constructing the URI. This is a database connection string, not your project HTTPS URL, anon key or API service-role key.

The starter uses the supplied database role (often `postgres`) and needs no Supabase Python SDK. For a production deployment, use a dedicated database role limited to these ingestion tables. Never put the URI into source code or frontend code.

## 3 Put these files in GitHub

Extract this folder and put **its contents at the repository root**. Include `.github/workflows/collect-news.yml` (some file browsers hide `.github`). Keep `collector.py`, `requirements.txt`, `feeds.csv` and `schema.sql` beside the README.

In repository Settings → Secrets and variables → Actions, create a repository secret:

- Name: `DATABASE_URL`
- Value: the complete Supabase PostgreSQL pooler URI with your password

Optionally add the repository variable `NEWS_USER_AGENT`, e.g. `FYP-news-collector/1.0 (academic research; contact: your-email@example.com)`.

Commit to the default branch. Open Actions → Collect news → Run workflow. Confirm that the run succeeds, then inspect the tables. The workflow schedules runs at minutes 7, 22, 37 and 52 of each hour (UTC). These are minutes 12, 27, 42 and 57 in Pakistan time.

GitHub scheduling can be delayed or dropped during high load. Public repository schedules can be disabled after 60 days without repository activity. This is near-real-time polling with no guaranteed delivery time, and is not suitable as the sole scheduler for a safety-critical alert service. Scheduled workflows need the file on the default branch. Check your account's Actions usage allowance, especially for private repositories.

## 4 Check the feeds

The URLs in `feeds.csv` are starter candidates carried over from your script, not live-verified endpoints. With Python 3.12 installed:

```sh
pip install -r requirements.txt
python -m unittest -v
python collector.py --check
```

`--check` requests every enabled feed without connecting to the database. Fix or disable failed feeds in `feeds.csv` (`enabled=0`) before relying on the schedule. Malformed feeds fail visibly instead of silently becoming an empty successful fetch. This strict parsing may require replacing a broken publisher feed.

To run a database pass locally, set `DATABASE_URL` in your terminal environment, then run `python collector.py`. A `.env` file is not automatically loaded. Do not use `--loop` in GitHub Actions: each run must finish.

## 5 Inspect what was collected

Run in the Supabase SQL Editor:

```sql
select title, publisher, published_utc, first_seen_utc, link
from ingestion.items
order by first_seen_utc desc limit 20;

select feed_name, fetched_utc, http_status, entries, n_new, n_changed, error
from ingestion.fetch_log
order by id desc limit 50;
```

To replay the latest known version at a historical cutoff, query versions instead of current items:

```sql
select distinct on (item_id) item_id, title, summary, published_utc, seen_utc
from ingestion.item_versions
where seen_utc <= timestamptz '2026-10-29 00:00:00+05:00'
order by item_id, seen_utc desc, id desc;
```

The cutoff above is an example. `published_utc` is the publisher's claim about publication time. `first_seen_utc` is when this collector received the report. The latter prevents future reports from leaking into earlier model inputs. Neither timestamp proves when the incident happened. Earliest collection also does not prove which outlet first reported the incident.

## Research limitations to retain

- An article URL is deduplicated across feeds; separate URLs reporting the same incident remain separate articles. Event linkage belongs in M1.
- Google News is an aggregator. Its URLs may differ from direct publisher links and are intentionally retained. `domain=news.google.com` is not the publisher's domain. The RSS `source` title is saved as `publisher` when present; it may be blank on direct feeds.
- `family_hint` describes feed configuration only. It is not a verified independent-source group and must not be counted directly for corroboration. Establish syndication and original publishers separately.
- Headlines, summaries and feed responses are collected, not full article pages. Full text extraction is a separate step subject to source permissions.
- Only changes actually observed on a poll are archived; edits between polls may be missed. RSS provides no complete retrospective history.
- Raw snapshots grow every time a feed returns HTTP 200. Monitor Supabase storage; archival/retention changes must preserve the study's provenance requirements.
- No database connection or live news fetch was validated in the preparation environment. Parser/identity tests run locally; validate `--check` and the first database pass in your environment.

Official references: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows and https://supabase.com/docs/guides/database/connecting-to-postgres

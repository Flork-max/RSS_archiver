"""RSS/Atom -> Supabase Postgres. One finite pass per scheduled job."""
import argparse
import calendar
import csv
import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import psycopg
import requests
from psycopg.types.json import Jsonb
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def utcnow():
    return datetime.now(timezone.utc)


def clean(value):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', value or ''))).strip()


def normalize_link(link):
    p = urlsplit(link.strip())
    if p.scheme.lower() not in ('http', 'https') or not p.hostname:
        return ''
    # Keep query order/values: signed and publisher URLs may depend on them.
    pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in {'fbclid', 'gclid'}]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, urlencode(pairs), ''))


def parse_feed(body, feed_name):
    parsed = feedparser.parse(body)
    # Do not overwrite validators on malformed/truncated responses.
    if parsed.get('bozo') or not parsed.get('version'):
        raise ValueError('Invalid or malformed RSS/Atom response')
    out = []
    for e in parsed.entries:
        link = normalize_link(e.get('link', ''))
        guid = str(e.get('id', ''))
        if not link and not guid:
            continue
        key = link or (feed_name + '\n' + guid)  # scope opaque IDs to their feed
        published = e.get('published_parsed')
        published = datetime.fromtimestamp(calendar.timegm(published), timezone.utc) if published else None
        row = dict(id=hashlib.sha256(key.encode()).hexdigest(), link=link, guid=guid,
                   publisher=clean(e.get('source', {}).get('title', '')),
                   domain=urlsplit(link).hostname or '', title=clean(e.get('title', '')),
                   summary=clean(e.get('summary', '')), published_utc=published)
        payload = [row['title'], row['summary'], published.isoformat() if published else None]
        row['content_hash'] = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
        out.append(row)
    return out


def load_feeds(path):
    with open(path, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    feeds = [r for r in rows if r['enabled'].strip() == '1' and r['url'].strip()]
    if not feeds or len({r['name'] for r in feeds}) != len(feeds):
        raise ValueError('Need at least one enabled feed, with unique names')
    return feeds


def session():
    s = requests.Session()
    s.headers['User-Agent'] = os.environ.get('NEWS_USER_AGENT', 'FYP-news-collector/1.0 (academic research)')
    retry = Retry(total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=['GET'], raise_on_status=False)
    s.mount('https://', HTTPAdapter(max_retries=retry))
    s.mount('http://', HTTPAdapter(max_retries=retry))
    return s


def persist(conn, feed, response, seen, entries, error):
    status = response.status_code if response is not None else 0
    with conn.transaction():
        # Transaction lock works with transaction-mode poolers too.
        conn.execute('select pg_advisory_xact_lock(2202001)')
        fetch_id = conn.execute('''insert into ingestion.fetch_log
            (feed_name,feed_url,fetched_utc,http_status,entries,error)
            values (%s,%s,%s,%s,%s,%s) returning id''',
            (feed['name'], feed['url'], seen, status, len(entries), error)).fetchone()[0]
        snapshot_id = None
        if response is not None and status == 200:
            # requests.content preserves the response body after HTTP decompression.
            snapshot_id = conn.execute('''insert into ingestion.raw_snapshots
                (fetch_id,final_url,response_headers,body,sha256) values (%s,%s,%s,%s,%s) returning id''',
                (fetch_id, response.url, Jsonb(dict(response.headers)), response.content,
                 hashlib.sha256(response.content).hexdigest())).fetchone()[0]
        new = changed = 0
        if status == 200 and not error:
            for row in entries:
                old = conn.execute('select content_hash,last_changed_utc from ingestion.items where id=%s',
                                   (row['id'],)).fetchone()
                # An older overlapping fetch must not roll current content backwards.
                if old and old[1] > seen:
                    raise ValueError('Out-of-order observation; rerun collection')
                if old is None:
                    conn.execute('''insert into ingestion.items
                      (id,link,guid,publisher,domain,title,summary,published_utc,first_seen_utc,last_changed_utc,content_hash)
                      values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                      tuple(row[k] for k in ('id','link','guid','publisher','domain','title','summary','published_utc'))
                      + (seen,seen,row['content_hash']))
                    new += 1
                elif old[0] != row['content_hash']:
                    conn.execute('''update ingestion.items set title=%s,summary=%s,published_utc=%s,
                       last_changed_utc=%s,content_hash=%s where id=%s''',
                       (row['title'],row['summary'],row['published_utc'],seen,row['content_hash'],row['id']))
                    changed += 1
                if old is None or old[0] != row['content_hash']:
                    conn.execute('''insert into ingestion.item_versions
                       (item_id,snapshot_id,seen_utc,title,summary,published_utc,content_hash)
                       values (%s,%s,%s,%s,%s,%s,%s)''',
                       (row['id'],snapshot_id,seen,row['title'],row['summary'],row['published_utc'],row['content_hash']))
                conn.execute('''insert into ingestion.feed_items values (%s,%s,%s,%s,%s)
                    on conflict(feed_name,item_id) do update set last_seen_utc=excluded.last_seen_utc''',
                    (feed['name'],row['id'],feed.get('family',''),seen,seen))
            conn.execute('''insert into ingestion.feed_state values (%s,%s,%s,%s)
                on conflict(feed_name) do update set url=excluded.url,etag=excluded.etag,last_modified=excluded.last_modified''',
                (feed['name'],feed['url'],response.headers.get('ETag'),response.headers.get('Last-Modified')))
        conn.execute('update ingestion.fetch_log set n_new=%s,n_changed=%s where id=%s', (new,changed,fetch_id))
    return new, changed


def collect(conn, client, feed):
    headers = {}
    if conn is not None:
        state = conn.execute('select url,etag,last_modified from ingestion.feed_state where feed_name=%s',
                             (feed['name'],)).fetchone()
        if state and state[0] == feed['url']:
            if state[1]: headers['If-None-Match'] = state[1]
            if state[2]: headers['If-Modified-Since'] = state[2]
    response, entries, error = None, [], ''
    try:
        response = client.get(feed['url'].strip(), headers=headers, timeout=(10,30))
        seen = utcnow()  # time received, never the publisher's date
        if response.status_code == 200:
            entries = parse_feed(response.content, feed['name'])
        elif response.status_code != 304:
            error = f'HTTP {response.status_code}'
    except (requests.RequestException, ValueError) as exc:
        seen = utcnow()
        error = type(exc).__name__  # avoid logging secrets or response contents
    new, changed = persist(conn,feed,response,seen,entries,error) if conn is not None else (0,0)
    print(f"{feed['name']}: HTTP {response.status_code if response is not None else 0}; "
          f"{len(entries)} entries; {new} new; {changed} changed; {error or 'OK'}", flush=True)
    return not error


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--feeds', default='feeds.csv')
    ap.add_argument('--check', action='store_true', help='Fetch and parse only; no database required')
    args = ap.parse_args()
    feeds = load_feeds(args.feeds)
    with session() as client:
        if args.check:
            results = [collect(None,client,f) for f in feeds]
        else:
            url = os.environ.get('DATABASE_URL')
            if not url:
                sys.exit('Set DATABASE_URL in your environment or GitHub Actions secrets')
            # autocommit prevents idle transactions while waiting for news sites.
            with psycopg.connect(url, sslmode='require', connect_timeout=15,
                                 autocommit=True, prepare_threshold=None) as conn:
                results = [collect(conn,client,f) for f in feeds]
    return 0 if all(results) else 1  # partial feed failures are visible in Actions


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (psycopg.Error, ValueError) as exc:
        sys.exit(f'Collection failed ({type(exc).__name__}); check configuration and database schema.')

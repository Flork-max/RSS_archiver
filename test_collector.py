import unittest
from collector import parse_feed, normalize_link


def rss(title='Report', guid='abc', link='https://example.org/a', date='Tue, 27 Oct 2026 10:00:00 GMT'):
    return f'''<rss version="2.0"><channel><title>News</title><link>https://example.org</link>
    <description>News</description><item><title>{title}</title><guid>{guid}</guid><link>{link}</link>
    <description>Summary</description><pubDate>{date}</pubDate></item></channel></rss>'''.encode()


class CollectorTests(unittest.TestCase):
    def test_tracking_links_share_identity(self):
        a = parse_feed(rss(link='https://example.org/a?utm_source=x#top'),'a')[0]
        b = parse_feed(rss(),'b')[0]
        self.assertEqual(a['id'],b['id'])

    def test_edits_preserve_identity_change_hash(self):
        a = parse_feed(rss(),'a')[0]
        b = parse_feed(rss(title='Updated report'),'a')[0]
        self.assertEqual(a['id'],b['id'])
        self.assertNotEqual(a['content_hash'],b['content_hash'])

    def test_opaque_guid_is_scoped(self):
        self.assertNotEqual(parse_feed(rss(link=''),'a')[0]['id'],parse_feed(rss(link=''),'b')[0]['id'])

    def test_publication_time_is_utc(self):
        a = parse_feed(rss(date='Tue, 27 Oct 2026 15:00:00 +0500'),'a')[0]
        self.assertEqual(a['published_utc'].isoformat(),'2026-10-27T10:00:00+00:00')

    def test_bad_response_rejected(self):
        for body in (b'<html>Blocked</html>',b'<rss><channel>'):
            with self.assertRaises(ValueError): parse_feed(body,'a')

    def test_atom_alternate_link(self):
        a = parse_feed(b'''<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title>
          <entry><id>one</id><title>Report</title><link rel="self" href="https://example.org/api/1"/>
          <link rel="alternate" href="https://example.org/article/1"/></entry></feed>''','a')[0]
        self.assertEqual(a['link'],'https://example.org/article/1')
        self.assertIsNone(a['published_utc'])

    def test_meaningful_query_kept(self):
        self.assertEqual(normalize_link('https://example.org/?id=5&utm_source=x'),'https://example.org/?id=5')


class FailureIsolationTests(unittest.TestCase):
    def test_refused_feed_then_success(self):
        from unittest.mock import Mock, patch
        from collector import collect, report
        import requests
        blocked = requests.Response()
        blocked.status_code = 403
        blocked.url = 'https://example.org/refused'
        good = requests.Response()
        good.status_code = 200
        good.url = 'https://example.org/rss'
        good._content = rss()
        client = Mock()
        client.get.side_effect = [blocked, good]
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = None
        feed = dict(name='Feed',url=good.url,family='example')
        with patch('collector.persist', return_value=(0,0)) as save:
            first = collect(conn,client,feed)
            second = collect(conn,client,feed)
            self.assertIn('403',first['error'])
            self.assertFalse(second['error'])
            self.assertEqual(second['entries'],1)
            self.assertEqual(save.call_count,2)
            self.assertIn('403',save.call_args_list[0].args[-1])
            self.assertEqual(report([first,second]),0)

    def test_timeout_and_malformed_feed_are_skipped(self):
        from unittest.mock import Mock
        from collector import collect, report
        import requests
        client = Mock()
        client.get.side_effect = requests.Timeout()
        feed = dict(name='Feed',url='https://example.org/rss')
        result = collect(None,client,feed)
        self.assertIn('Timeout',result['error'])
        self.assertEqual(report([result]),0)
        client.get.side_effect = None
        response = requests.Response()
        response.status_code = 200
        response._content = b'<html>Access denied</html>'
        client.get.return_value = response
        self.assertTrue(collect(None,client,feed)['error'])

    def test_database_failure_is_not_swallowed(self):
        from unittest.mock import Mock, patch
        from collector import collect
        import requests
        import psycopg
        client = Mock()
        response = requests.Response()
        response.status_code = 403
        client.get.return_value = response
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = None
        with patch('collector.persist',side_effect=psycopg.OperationalError('Unavailable')):
            with self.assertRaises(psycopg.OperationalError):
                collect(conn,client,dict(name='Feed',url='https://example.org/rss'))

    def test_summary_file_lists_failures(self):
        import os
        import tempfile
        from unittest.mock import patch
        from collector import report
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d,'summary.md')
            with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY':path}):
                self.assertEqual(report([dict(name='Blocked',status=403,new=0,changed=0,error='HTTP 403')]),0)
            with open(path) as f:
                text = f.read()
            self.assertIn('HTTP 403',text)
            self.assertIn('all feeds failed',text)

if __name__ == '__main__': unittest.main()

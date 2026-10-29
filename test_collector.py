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

if __name__ == '__main__': unittest.main()

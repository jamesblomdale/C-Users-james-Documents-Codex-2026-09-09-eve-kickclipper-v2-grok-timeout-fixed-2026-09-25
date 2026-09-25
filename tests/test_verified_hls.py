import unittest
from unittest.mock import patch, MagicMock
from urllib.request import urlopen
from urllib.error import HTTPError
import requests
from verified_hls import verified_hls, _limit_master_variants


class VerifiedHLSTest(unittest.TestCase):
    def test_master_playlist_respects_render_height_ceiling(self):
        lines = ['#EXTM3U',
                 '#EXT-X-STREAM-INF:BANDWIDTH=100,RESOLUTION=284x160', 'low.m3u8',
                 '#EXT-X-STREAM-INF:BANDWIDTH=900,RESOLUTION=1920x1080', 'high.m3u8']
        limited = _limit_master_variants(lines, 720)
        self.assertIn('low.m3u8', limited)
        self.assertNotIn('high.m3u8', limited)

    def test_playlist_rewrites_segments_and_keys(self):
        response=MagicMock()
        response.__enter__.return_value=response
        response.is_redirect=False
        response.headers={'Content-Type':'application/vnd.apple.mpegurl'}
        response.content=b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXTINF:2,\nsegment.ts\n'
        with patch('verified_hls.requests.Session.get',return_value=response) as get:
            with verified_hls('https://example.com/list.m3u8') as url:
                body=urlopen(url).read().decode()
                self.assertIn('URI="http://127.0.0.1:',body)
                self.assertIn('.ts',body)
                self.assertNotIn('segment.ts',body)
                self.assertNotIn('verify',get.call_args.kwargs)  # Requests' verified default

    def test_bad_certificate_fails_closed(self):
        with patch('verified_hls.requests.Session.get',side_effect=requests.exceptions.SSLError('bad certificate')):
            with verified_hls('https://example.com/list.m3u8') as url:
                with self.assertRaises(HTTPError) as exc:urlopen(url)
                self.assertEqual(exc.exception.code,502)

    def test_local_files_are_unchanged(self):
        with verified_hls('clip.mp4') as source:self.assertEqual(source,'clip.mp4')

import json
import tempfile
import uuid
import unittest
from pathlib import Path
from vod_links import kick_vod_id, recording_from_page
from clip_trash import move_library_to_trash


class LinkAndTrashTests(unittest.TestCase):
    def test_selects_recording_not_live_or_other_vod(self):
        data={'live':{'id':'other','source':'https://example.com/live.m3u8'},'vod':{'id':'wanted','recording_url':'https://example.com/vod.m3u8','is_live':False}}
        page='<script>self.__next_f.push('+json.dumps([1,'0:'+json.dumps(data)])+')</script>'
        self.assertEqual(recording_from_page(page,'wanted'),'https://example.com/vod.m3u8')
        with self.assertRaises(RuntimeError): recording_from_page(page,'missing')
        self.assertIsNone(kick_vod_id('https://kick.com.evil.test/a/videos/123'))
        self.assertEqual(kick_vod_id('https://kick.com/a/videos/123'),'123')

    def test_trash_is_scoped_and_rolls_back(self):
        root=Path('tests') / ('trash-' + uuid.uuid4().hex[:8])
        root.mkdir()
        try:
            a=root/'u1/vod/a/clips/one'
            b=root/'u2/vod/a/clips/two'
            for folder in (a,b):
                folder.mkdir(parents=True);(folder/'clip.mp4').write_bytes(b'fixture')
            def fail(_): raise RuntimeError('database unavailable')
            with self.assertRaises(RuntimeError):move_library_to_trash(root/'u1',fail)
            self.assertTrue(a.exists());self.assertTrue(b.exists())
            self.assertEqual(move_library_to_trash(root/'u1',lambda _:None),1)
            self.assertFalse(a.exists());self.assertTrue(b.exists())
            self.assertEqual(len(list((root/'u1/.trash').rglob('clip.mp4'))),1)
        finally:
            for path in sorted(root.rglob('*'), key=lambda p: len(p.parts), reverse=True):
                if path.is_file(): path.unlink()
                elif path.is_dir(): path.rmdir()
            root.rmdir()

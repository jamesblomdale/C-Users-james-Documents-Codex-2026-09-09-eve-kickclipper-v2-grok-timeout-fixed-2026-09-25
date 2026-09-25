import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch
import uuid
from mai_transcribe import AudioChunk, MaiTranscriber, chunks_for, normalize, retry_delay
from transcribe import Word, Transcript, Transcriber
from config import Config


def settings(**overrides):
    names=[n for n in dir(Config) if n.startswith(('MAI_', 'AZURE_'))]
    values={n:getattr(Config,n) for n in names}
    values.update(AZURE_SPEECH_ENDPOINT='https://fixture.cognitiveservices.azure.com',AZURE_SPEECH_KEY='secret',PRIORITY_NAMES='Sneako,N3on',MAI_CHUNK_MINUTES=1,MAI_MAX_RETRIES=2)
    return SimpleNamespace(**{**values,**overrides})


def response(text='Hello.', start=1000):
    return {'phrases':[{'text':text,'words':[{'text':text,'offsetMilliseconds':start,'durationMilliseconds':200}]}]}


class MaiTests(unittest.TestCase):
    def setUp(self):
        self.folder=Path('tests')/('mai-'+uuid.uuid4().hex[:8])
        self.folder.mkdir()
        self.source=self.folder/'source.wav';self.source.write_bytes(b'fixture')

    def tearDown(self):
        for p in sorted(self.folder.rglob('*'),key=lambda p:len(p.parts),reverse=True):
            if p.is_file():p.unlink()
            else:p.rmdir()
        self.folder.rmdir()

    def test_normalize_offset_and_punctuation(self):
        words=normalize(response(start=4000),AudioChunk(1,900,1808,900,1800))
        self.assertEqual(words,[Word('Hello.',904,904.2)])

    def test_core_ownership(self):
        a=normalize(response(start=900000),AudioChunk(0,0,908,0,900))
        b=normalize(response(start=8000),AudioChunk(1,892,1808,900,1800))
        self.assertEqual(a,[]);self.assertEqual(len(b),1)
        self.assertEqual(b[0].start,900)

    def test_silence_and_malformed_responses(self):
        chunk=chunks_for(60,1)[0]
        self.assertEqual(normalize({'phrases':[]},chunk),[])
        for data in ({},{'phrases':[{'text':'spoken'}]},response(start=float('nan')),response(start=-1)):
            with self.assertRaises((ValueError,KeyError)): normalize(data,chunk)

    def test_long_vod_is_many_bounded_requests(self):
        chunks=chunks_for(6*3600,15,8)
        self.assertEqual(len(chunks),24)
        self.assertTrue(all(c.audio_end-c.audio_start<=916 for c in chunks))
        with self.assertRaises(ValueError):chunks_for(60,0)

    def test_retry_429_and_reopens_upload(self):
        client=MaiTranscriber(settings())
        limited=Mock(status_code=429,headers={'Retry-After':'3'})
        good=Mock(status_code=200);good.json.return_value=response()
        with patch('mai_transcribe.requests.post',side_effect=[limited,good]) as call, patch('mai_transcribe.time.sleep') as sleep:
            self.assertEqual(client._request(self.source),response())
            sleep.assert_called_once_with(3)
            self.assertEqual(call.call_count,2)
            self.assertFalse(call.call_args.kwargs['allow_redirects'])

    def test_auth_failure_not_retried(self):
        client=MaiTranscriber(settings())
        with patch('mai_transcribe.requests.post',return_value=Mock(status_code=401)) as call:
            for _ in range(2):
                with self.assertRaises(RuntimeError):client._request(self.source)
            self.assertEqual(call.call_count,1)

    def test_parallel_order_cache_and_single_chunk_fallback(self):
        fallback=Mock(return_value=Transcript(self.source,[Word('local',9,9.2)],'local'))
        client=MaiTranscriber(settings(),fallback)
        def extract(source,chunk,path):path.write_text(str(chunk.index))
        def request(path):
            index=int(path.read_text())
            if index==1:raise RuntimeError('fixture outage')
            if index==0:time.sleep(.03)
            return response(str(index),1000 if index==0 else 9000)
        with patch('mai_transcribe.subprocess.check_output',return_value='180'),patch.object(client,'_extract',side_effect=extract),patch.object(client,'_request',side_effect=request) as call,contextlib.redirect_stdout(io.StringIO()) as log:
            result=client.transcribe(self.source,'transcribe')
            self.assertEqual([w.text for w in result.words],['0','local','2'])
            self.assertEqual([w.start for w in result.words],[1,61,121])
            self.assertEqual(fallback.call_count,1)
            self.assertEqual(call.call_count,3)
            self.assertEqual(client.transcribe(self.source).words,result.words)
            self.assertEqual(call.call_count,3)
            self.assertIn('PROGRESS transcribe 100',log.getvalue())
        self.assertEqual(list(self.folder.glob('mai-*.mp3')),[])

    def test_local_facade_preserves_interface(self):
        with patch.object(Config,'TRANSCRIPTION_PROVIDER','whisper'),patch('transcribe.FasterWhisperTranscriber') as local:
            engine=Transcriber('small',profile='vod')
            local.assert_not_called()
            engine.transcribe_chunk(self.source,'transcribe',60)
            local.assert_called_once_with('small',profile='vod')
            local.return_value.transcribe_chunk.assert_called_once_with(self.source,'transcribe',60)

    def test_cloud_facade_does_not_load_local_model(self):
        with patch.object(Config,'TRANSCRIPTION_PROVIDER','mai'),patch('mai_transcribe.MaiTranscriber') as cloud,patch('transcribe.FasterWhisperTranscriber') as local:
            engine=Transcriber(profile='vod');engine.transcribe_chunk(self.source)
            cloud.return_value.transcribe.assert_called_once_with(self.source,None)
            local.assert_not_called()

    def test_missing_key_fails_clearly(self):
        with self.assertRaisesRegex(ValueError,'AZURE_SPEECH_KEY'):
            MaiTranscriber(settings(AZURE_SPEECH_KEY=''))

    def test_retry_after_date(self):
        with patch('mai_transcribe.time.time',return_value=0):
            self.assertEqual(retry_delay('Thu, 01 Jan 1970 00:00:05 GMT',0),5)

    def test_ingest_resume_uses_completed_audio_only(self):
        from audio_ingest_cache import cached_audio
        def pull(folder):
            p=folder/'audio.wav';p.write_bytes(b'0'*100);return p
        with patch(__name__+'.time.sleep'):
            callback=Mock(side_effect=pull)
            a=cached_audio(self.folder,'https://kick.com/a/videos/id',0,120,callback)
            self.assertEqual(cached_audio(self.folder,'https://kick.com/a/videos/id',0,120,callback),a)
            self.assertEqual(callback.call_count,1)
            cached_audio(self.folder,'https://kick.com/a/videos/id',120,240,callback)
            self.assertEqual(callback.call_count,2)
            a.write_bytes(b'partial')
            cached_audio(self.folder,'https://kick.com/a/videos/id',0,120,callback)
            self.assertEqual(callback.call_count,3)

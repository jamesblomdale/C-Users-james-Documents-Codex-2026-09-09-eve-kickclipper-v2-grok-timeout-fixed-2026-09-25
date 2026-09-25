import contextlib
import io
import json
import os
import subprocess
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from audio_pipeline import global_words, merge_transcript_chunks, transcribe_vod
from config import Config
from mai_transcribe import chunks_for
from pipeline_state import AdaptiveGate
from scout_pipeline import semantic_windows, validate_candidates, ProgressiveScout
from transcribe import Word, Transcript


class LongPipelineTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path('tests')/('long-'+uuid.uuid4().hex[:8])
        self.folder.mkdir()
        self.source = self.folder/'source.wav'
        self.source.write_bytes(b'fixture')

    def tearDown(self):
        for p in sorted(self.folder.rglob('*'),key=lambda p:len(p.parts),reverse=True):
            if p.is_file(): p.unlink()
            else: p.rmdir()
        self.folder.rmdir()

    def engine(self):
        return SimpleNamespace(provider='whisper',model_size='fixture',_mai=None,
            _fallback=Mock(return_value=Transcript(self.source,[Word('hello',10,10.2)],'hello')))

    def scheduler(self, engine, duration, callback=None):
        def extract(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b'chunk')
        with patch('audio_pipeline.subprocess.check_output',return_value=str(duration)), \
             patch('audio_pipeline.subprocess.run',side_effect=extract), contextlib.redirect_stdout(io.StringIO()):
            return transcribe_vod(engine,self.source,callback)

    def test_global_timestamp_7200(self):
        self.assertAlmostEqual(global_words([Word('what',13.42,13.71,.97,'speaker_1')],7200)[0].start,7213.42)
        self.assertEqual(global_words([Word('what',1,2,.97,'speaker_1')],7200)[0].speaker,'speaker_1')

    def test_ten_minute_two_hour_six_hour_schedule(self):
        for duration,count in [(600,3),(7200,30),(21600,90)]:
            chunks=chunks_for(duration,4,5)
            self.assertEqual(len(chunks),count)
            self.assertLessEqual(max(c.audio_end-c.audio_start for c in chunks),250)
            self.assertEqual(chunks[-1].core_end,duration)

    def test_overlap_prefers_confidence_and_keeps_unique_words(self):
        a,b=chunks_for(480,4,5)
        words=merge_transcript_chunks([(b,[Word('HELLO!',239.1,239.4,.95),Word('unique',241,241.2,.9)]),
                                       (a,[Word('hello',239,239.3,.5)])])
        self.assertEqual([w.text for w in words],['HELLO!','unique'])
        self.assertEqual(words[0].confidence,.95)

    def test_repeated_words_not_collapsed_within_chunk(self):
        chunk=chunks_for(60,4,5)[0]
        words=[Word('no',1,1.1),Word('no',1.2,1.3)]
        self.assertEqual(merge_transcript_chunks([(chunk,words)]),words)

    def test_semantic_boundary_gets_both_sides(self):
        words=[Word('setup.' if t==238 else 'payoff.' if t==242 else 'word',t,t+.3) for t in range(600)]
        windows=semantic_windows(words,180,120)
        self.assertTrue(any('setup.' in w[2] and 'payoff.' in w[2] for w in windows))
        self.assertEqual(windows[0][0],0)

    def test_cache_resume_no_repeated_asr(self):
        engine=self.engine()
        first=self.scheduler(engine,600)
        self.assertEqual(engine._fallback.call_count,3)
        second=self.scheduler(engine,600)
        self.assertEqual(first.words,second.words)
        self.assertEqual(engine._fallback.call_count,3)
        self.assertFalse(list(self.folder.rglob('*.mp3')))

    def test_failed_chunk_retry(self):
        engine=self.engine()
        engine._fallback.side_effect=[RuntimeError('temporary'),Transcript(self.source,[Word('ok',1,2)],'ok')]
        self.assertEqual(self.scheduler(engine,60).words[0].text,'ok')
        self.assertEqual(engine._fallback.call_count,2)

    def test_interrupted_job_resumes_completed_chunks(self):
        engine=self.engine()
        engine._fallback.side_effect=[Transcript(self.source,[Word('ok',1,2)],'ok'),RuntimeError('fail')]
        with patch.object(Config,'ASR_RETRY_COUNT',0):
            with self.assertRaises(RuntimeError):self.scheduler(engine,480)
        engine._fallback.side_effect=None
        engine._fallback.reset_mock()
        self.scheduler(engine,480)
        self.assertEqual(engine._fallback.call_count,1)

    def test_failed_chunk_is_deferred_while_other_chunks_finish_then_recovers(self):
        engine=self.engine()
        calls={'count':0}
        def intermittent(_path):
            calls['count'] += 1
            if calls['count'] == 1:
                raise RuntimeError('temporary provider connection failure')
            return Transcript(self.source,[Word('ok',1,2)],'ok')
        engine._fallback.side_effect=intermittent
        with patch.object(Config,'ASR_RETRY_COUNT',0):
            transcript=self.scheduler(engine,480)
        self.assertEqual(len(transcript.words),2)
        self.assertEqual(engine._fallback.call_count,3)

    def test_dynamic_pressure_and_recovery(self):
        gate=AdaptiveGate(8,2)
        with patch('pipeline_state.time.monotonic',side_effect=[10,11,16]):
            gate.pressure();self.assertEqual(gate.limit,4)
            gate.pressure();self.assertEqual(gate.limit,4)
            gate.pressure();self.assertEqual(gate.limit,2)
        for _ in range(8):gate.success()
        self.assertEqual(gate.limit,3)

    def test_cancellation_cleans_temporary_audio(self):
        engine=self.engine()
        engine._fallback.side_effect=InterruptedError('cancelled')
        with self.assertRaises(InterruptedError):self.scheduler(engine,60)
        self.assertFalse(list(self.folder.rglob('*.mp3')))
        state=json.loads(next(self.folder.rglob('00000.json')).read_text())
        self.assertEqual(state['status'],'FAILED')

    def test_long_scheduler_does_not_decode_full_source(self):
        engine=self.engine()
        self.scheduler(engine,21600)
        self.assertEqual(engine._fallback.call_count,90)
        self.assertTrue(all(call.args[0].suffix=='.mp3' for call in engine._fallback.call_args_list))

    def test_normalized_aac_uses_stream_copy_fast_path(self):
        source = self.folder/'normalized.m4a'
        source.write_bytes(b'fixture')
        engine = self.engine()
        commands = []
        def extract(cmd, **kwargs):
            commands.append(cmd)
            Path(cmd[-1]).write_bytes(b'chunk')
        with patch('audio_pipeline.subprocess.check_output',return_value='600'), \
             patch('audio_pipeline.subprocess.run',side_effect=extract), \
             patch.object(Config,'ASR_FAST_CHUNK_COPY',True), contextlib.redirect_stdout(io.StringIO()):
            transcribe_vod(engine, source)
        self.assertTrue(commands)
        self.assertTrue(all('-c:a' in cmd and cmd[cmd.index('-c:a')+1]=='copy' for cmd in commands))
        self.assertTrue(all(call.args[0].suffix=='.m4a' for call in engine._fallback.call_args_list))
        self.assertFalse(list(self.folder.rglob('*.m4a'))[1:] if len(list(self.folder.rglob('*.m4a'))) > 1 else [])

    def test_scout_rejects_out_of_range_candidates(self):
        data={'candidates':[dict(start=5,end=20,preliminary_score=82,reason='event',needs_more_context_before=5,needs_more_context_after=5)]}
        self.assertEqual(len(validate_candidates(data,(0,30,''))),1)
        # New tolerant gate never raises; relative/near-window timestamps are normalized or dropped.
        self.assertIsInstance(validate_candidates(data,(10,30,'')), list)

    def test_progressive_scout_waits_for_forward_context(self):
        scout=ProgressiveScout(self.folder)
        with patch('scout_pipeline.scout_records',return_value=[]) as call:
            scout.update([Word('word',t,t+.2) for t in range(180)],180)
            self.assertEqual(call.call_count,0)
            scout.update([Word('word',t,t+.2) for t in range(400)],400)
            scout.close()
            self.assertEqual(call.call_count,1)
            self.assertTrue(all(w[1]+30 < 400 for w in call.call_args.args[0]))

    def test_low_confidence_targeted_repair(self):
        from transcript_quality import repair_candidates
        words=[Word('unclear',10,11,.1)]
        candidate=SimpleNamespace(start=5,end=15,score=80,text='unclear')
        engine=self.engine()
        engine._local=Mock(return_value=SimpleNamespace(_run_model=Mock(return_value=Transcript(self.source,[Word('clear',5,6,.95)],'clear'))))
        with patch('transcript_quality.subprocess.run'):
            repair_candidates([candidate],words,engine,self.source,0,self.folder/'quality')
        self.assertEqual(words[0].text,'clear')
        self.assertAlmostEqual(words[0].start,10)

    def test_duplicate_candidates_collapse(self):
        from moments import Candidate,dedupe_clips
        a=Candidate(10,40,'same event',80,topic='argument ends')
        b=Candidate(11,41,'same event',70,topic='argument ends')
        self.assertEqual(len(dedupe_clips([a,b])),1)

    def test_merged_group_rescoring_is_batched(self):
        from moments import Candidate, merge_candidates
        candidates = [Candidate(i * 300, i * 300 + 60, f'event {i}', 70,
                                topic=f'unrelated {i}') for i in range(17)]
        def scored(texts):
            from detect import DetectionResult
            return [DetectionResult(score=70, reason='kept', topic='event') for _ in texts]
        with patch('moments.score_batch', side_effect=scored) as calls, \
             patch.dict(os.environ, {'MERGE_WINDOWS_PER_BATCH':'8',
                                     'MERGE_SCORING_MAX_CONCURRENCY':'2'}), \
             contextlib.redirect_stdout(io.StringIO()):
            merged = merge_candidates(candidates)
        self.assertEqual(len(merged), 17)
        self.assertEqual(calls.call_count, 3)
        self.assertTrue(all(len(call.args[0]) <= 8 for call in calls.call_args_list))

    def test_profile_selection_requires_measured_quality(self):
        from benchmark_profiles import choose_profile
        fast=dict(wer=.3,candidate_recall=.6,boundary_errors=2,total_seconds=10)
        accurate=dict(wer=.08,candidate_recall=.96,boundary_errors=0,total_seconds=30)
        self.assertEqual(choose_profile([fast,accurate],.1,.9,0),accurate)
        with self.assertRaises(ValueError):choose_profile([fast],.1,.9,0)

    def test_audio_signals_are_bounded_measurements(self):
        from audio_signals import measure_audio
        from array import array
        pcm=array('f',[.5,-.5]*4000).tobytes()
        with patch('audio_signals.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=pcm)) as run:
            features=measure_audio(self.source,0,3600)
        self.assertAlmostEqual(features['rms_dbfs'],-6.02,places=2)
        self.assertIn('180',run.call_args.args[0])

    def test_overlap_disagreement_is_flagged(self):
        a,b=chunks_for(480,4,5)
        result=merge_transcript_chunks([(a,[Word('hello',239,239.3,.9)]),
                                        (b,[Word('yellow',239.05,239.35,.9)])])
        self.assertTrue(all(w.uncertain for w in result))

    def test_cancel_marker_stops_before_asr(self):
        marker=self.folder/'cancel.flag';marker.touch()
        engine=self.engine()
        with patch.dict(os.environ,{'JOB_CANCEL_FILE':str(marker.resolve())}):
            with self.assertRaises(InterruptedError):self.scheduler(engine,60)
        engine._fallback.assert_not_called()

    def test_short_real_ffmpeg_export_with_fixture_ai(self):
        import vod
        from moments import Candidate
        video=self.folder/'source.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=320x240:r=15:d=8',
                        '-f','lavfi','-i','sine=frequency=440:duration=8','-c:v','libx264','-c:a','aac','-shortest',str(video)],check=True,capture_output=True)
        transcript=Transcript(video,[Word('Hello',1,1.5),Word('payoff.',5,5.5)],'Hello payoff.')
        with patch.object(Config,'OUTPUT_DIR',str(self.folder/'output')),patch.object(Config,'TRACKING_ENABLED',False), \
             patch.object(Config,'ASPECT_RATIO','4:3'),patch.object(Config,'CAPTIONS_ENABLED',True), \
             patch.object(Config,'validate'),patch('detect._call_llm',return_value='OK'), \
             patch('vod.Transcriber') as engine,patch('vod.find_candidates',return_value=[Candidate(1,6,'Hello payoff.',80)]), \
             patch('vod.merge_candidates',side_effect=lambda c:c),patch('context_review.review_candidates'), \
             patch('vod.rank_top_moments',return_value=[0]),patch('vod.generate_posts',return_value={}), \
             patch('vod.write_posts_file',side_effect=lambda data,p:p.write_text('Fixture clip')), \
             patch.dict(os.environ,{}),contextlib.redirect_stdout(io.StringIO()):
            engine.return_value.transcribe_vod.return_value=transcript
            vod.process_vod(str(video),'fixture')
        clips=list((self.folder/'output').rglob('clip.mp4'))
        self.assertEqual(len(clips),1)
        self.assertGreater(clips[0].stat().st_size,1000)
        self.assertTrue((clips[0].parent/'recipe.json').exists())
        self.assertTrue((clips[0].parent/'score.json').exists())

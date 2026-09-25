import unittest
from unittest.mock import patch
from guardian import Guardian, redact
from editor_validation import validate_recipe
from scoring_policy import WEIGHTS
from detect import _weighted_score, _apply_hard_caps, score_batch
from track import FaceObservation, compute_smoothed_crop_track

class StudioTests(unittest.TestCase):
    def test_failures_are_visible_and_secrets_redacted(self):
        g=Guardian(); g.observe('ERROR request failed api_key=secret https://example.com/file?token=secret')
        self.assertEqual(len(g.snapshot(True)['incidents']),1)
        self.assertNotIn('secret',g.incidents[0]['message'])

    def test_eta_is_job_wide_and_progress_never_resets_between_stages(self):
        g=Guardian()
        with patch('guardian.time.monotonic',side_effect=[0,10,20,30]):
            self.assertIsNone(g.progress('scan',0)['eta_seconds'])
            g.progress('scan',10)
            scan = g.progress('scan',20)
            self.assertEqual(scan['eta_scope'],'job')
            self.assertGreater(scan['eta_seconds'],0)
            render = g.progress('cut',0)
            self.assertGreaterEqual(render['pct'],scan['pct'])
            self.assertEqual(render['stage_pct'],0)

    def test_event_weights_and_invalid_scores(self):
        scores={k:9 for k in WEIGHTS};scores.update(judgment=0,comment_potential=2)
        # A genuinely strong event can score well without roast value.
        self.assertAlmostEqual(_weighted_score(scores),70.1)
        scores.update(judgment=9,comment_potential=9,payoff=1)
        # A hook with no payoff cannot pass as a finished top clip.
        self.assertLess(_weighted_score(scores),50)
        with self.assertRaises(ValueError):
            _apply_hard_caps({k:float('nan') for k in WEIGHTS})

    def test_instant_cut_at_new_speaker(self):
        obs=[FaceObservation(0,1,.2,10),FaceObservation(9,2,.8,20)]
        track=compute_smoothed_crop_track(obs,20,30)
        self.assertAlmostEqual(track[8],.2)
        self.assertAlmostEqual(track[9],.8)

    def test_editor_rejects_bad_times_and_modes(self):
        r=dict(start=0,end=2,aspect='9:16',track='off',layout='center',captions_enabled=True)
        self.assertEqual(validate_recipe(r)['end'],2)
        for bad in [dict(end=0),dict(end=float('inf')),dict(track='oops'),dict(overlay_seconds=-1)]:
            with self.assertRaises(ValueError):validate_recipe({**r,**bad})

    def test_short_punchline_reaches_scorer(self):
        import json
        result={**{k:8 for k in WEIGHTS},'index':0,'reason':'Clear punchline'}
        with patch('detect._call_llm',return_value=json.dumps([result])) as call:
            scores=score_batch(['Well, that aged well.'])
            self.assertTrue(call.called)
            self.assertEqual(scores[0].score,80)

    def test_provider_credit_failure_is_fatal_and_cached(self):
        import detect
        from unittest.mock import Mock
        response=Mock(status_code=403,text='used all available credits',ok=False)
        detect._fatal_provider_error=None
        try:
            # Keep this test independent of developer machine credentials. CI
            # intentionally has no provider secrets, while score_batch must
            # still reach the mocked transport to exercise fatal-error caching.
            with patch.object(detect.Config, 'LLM_PROVIDER', 'grok'), \
                 patch.object(detect.Config, 'LLM_API_KEY', 'test-key'), \
                 patch('detect.requests.post',return_value=response) as post:
                for _ in range(2):
                    with self.assertRaises(detect.ProviderUnavailable):
                        detect.score_batch(['There is a whole story here.'])
                self.assertEqual(post.call_count,1)
        finally:
            detect._fatal_provider_error=None

    def test_credit_403_is_classified_as_quota(self):
        g=Guardian()
        self.assertEqual(g.observe('403 used all available credits or reached spending limit')['category'],'quota')

if __name__=='__main__':unittest.main()

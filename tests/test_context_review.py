import copy
import json
import os
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from context_review import validate_review, review_candidates
from scoring_policy import WEIGHTS


class ReviewTests(unittest.TestCase):
    def data(self):
        return dict(literal_event='A greeting', social_interpretation='Possibly friendly',
                    skeptic='No clear payoff', uncertainties='Tone unknown', confidence=.9,
                    evidence=[dict(time=10, quote='hello')], scores={k:10 for k in WEIGHTS})

    def test_rejects_nan_and_outside_evidence(self):
        for field, value in [('confidence', float('nan')), ('evidence', [dict(time=200, quote='hello')])]:
            data=self.data();data[field]=value
            with self.assertRaises(ValueError):validate_review(data,0,30)
        data=self.data();data['scores']['hook']=True
        with self.assertRaises(ValueError):validate_review(data,0,30)

    def test_cap_limit_and_context(self):
        folder=Path('tests')/('review-'+uuid.uuid4().hex)
        candidates=[SimpleNamespace(start=10,end=20,score=60-i,text='hello',reason='') for i in range(3)]
        words=[SimpleNamespace(start=10,text='hello')]
        try:
            with patch.dict(os.environ,{'CONTEXT_REVIEW_LIMIT':'1','VISION_MODEL':'','VIDEO_REVIEW_PROVIDER':'frames'}), patch('context_review._call_llm',return_value=json.dumps(self.data())) as call:
                review_candidates(candidates,words,folder,None)
                self.assertEqual(call.call_count,1)
                self.assertEqual(candidates[0].score,72)
                self.assertEqual(candidates[1].score,59)
                self.assertIn('CANDIDATE: 10.00..20.00',call.call_args.args[0])
        finally:
            for f in folder.rglob('*.json'):f.unlink()
            for p in sorted(folder.rglob('*'),reverse=True):
                if p.is_dir():p.rmdir()
            folder.rmdir()

    def test_missing_key_does_not_upload(self):
        from gemini_video import review
        with patch.dict(os.environ,{'GEMINI_API_KEY':''}), patch('gemini_video.requests.post') as post:
            with self.assertRaises(ValueError):review('source.mp4',0,10,'hello',Path('.'))
            post.assert_not_called()

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from parakeet_transcribe import ParakeetTranscriber


class ParakeetTests(unittest.TestCase):
    def settings(self):
        return SimpleNamespace(
            TOGETHER_API_KEY='test-key', PARAKEET_MODEL='nvidia/parakeet-tdt-0.6b-v3',
            PARAKEET_LANGUAGE='en', PARAKEET_CONCURRENCY=4, PARAKEET_TIMEOUT_SECONDS=180,
            PARAKEET_MAX_RETRIES=2, PARAKEET_DIARIZE=False,
        )

    def test_word_timestamps_are_made_absolute(self):
        t = ParakeetTranscriber(self.settings())
        payload = {'text':'hello world','language':'en','words':[
            {'word':'hello','start':1.0,'end':1.4},
            {'word':'world','start':1.5,'end':2.0,'speaker_id':'SPEAKER_01'},
        ]}
        with patch.object(t, '_request', return_value=payload):
            chunk = SimpleNamespace(audio_start=120.0,audio_end=180.0)
            words = t.request_words(Path('fake.mp3'), chunk)
        self.assertEqual(words[0].start, 121.0)
        self.assertEqual(words[1].end, 122.0)
        self.assertEqual(words[1].speaker, 'SPEAKER_01')

    def test_rejects_text_without_word_timestamps(self):
        t = ParakeetTranscriber(self.settings())
        with patch.object(t, '_request', return_value={'text':'speech','words':None}):
            with self.assertRaises(ValueError):
                t.request_words(Path('fake.mp3'), SimpleNamespace(audio_start=0.0,audio_end=60.0))

    def test_probe_rejects_payment_error_clearly(self):
        settings = self.settings(); settings.TOGETHER_API_KEY = 'payment-key'
        t = ParakeetTranscriber(settings)
        response = Mock(status_code=402, text='payment required')
        response.json.return_value = {'error': {'message': 'Add credits'}}
        with patch.object(t, '_post', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'billing/credits|requires billing/credits'):
                t.probe()

    def test_probe_accepts_together_model(self):
        settings = self.settings(); settings.TOGETHER_API_KEY = 'success-key'
        t = ParakeetTranscriber(settings)
        response = Mock(status_code=200, text='')
        response.json.return_value = {'text': ''}
        with patch.object(t, '_post', return_value=response):
            self.assertTrue(t.probe())


if __name__ == '__main__':
    unittest.main()

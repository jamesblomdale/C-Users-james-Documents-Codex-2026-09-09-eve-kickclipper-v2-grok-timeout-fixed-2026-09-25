import unittest
from unittest.mock import patch, Mock
from pathlib import Path
from types import SimpleNamespace

from parakeet_transcribe import ParakeetTranscriber

class ProviderRoleSeparationTests(unittest.TestCase):
    def parakeet_settings(self):
        return SimpleNamespace(
            TOGETHER_API_KEY='together-only', PARAKEET_MODEL='nvidia/parakeet-tdt-0.6b-v3',
            PARAKEET_LANGUAGE='en', PARAKEET_CONCURRENCY=4, PARAKEET_TIMEOUT_SECONDS=180,
            PARAKEET_MAX_RETRIES=2, PARAKEET_DIARIZE=False,
        )

    def test_parakeet_authorization_uses_together_key_only(self):
        t = ParakeetTranscriber(self.parakeet_settings())
        response = Mock(status_code=200)
        session = Mock()
        session.post.return_value = response
        with patch.object(t, '_session', return_value=session):
            t._post(file_tuple=('x.wav', b'x', 'audio/wav'), data={'model':'x'})
        self.assertEqual(session.post.call_args.kwargs['headers']['Authorization'], 'Bearer together-only')

    def test_transcriber_parakeet_path_does_not_construct_whisper(self):
        from config import Config
        from transcribe import Transcriber
        with patch.object(Config, 'TRANSCRIPTION_PROVIDER', 'parakeet'), \
             patch.object(Config, 'PARAKEET_VERIFY_CREDENTIALS', False), \
             patch.object(Config, 'PARAKEET_LOCAL_FALLBACK', False), \
             patch.object(Config, 'TRANSCRIPTION_FALLBACK', 'none'), \
             patch.object(Config, 'TOGETHER_API_KEY', 'together-only'), \
             patch('parakeet_transcribe.ParakeetTranscriber') as pq, \
             patch('transcribe.FasterWhisperTranscriber') as whisper:
            pq.return_value.concurrency = 8
            Transcriber(profile='vod')
            pq.assert_called_once()
            whisper.assert_not_called()

    def test_grok_scoring_uses_llm_key_not_together_key(self):
        import detect
        from config import Config
        good = Mock(status_code=200, ok=True)
        good.json.return_value={'choices':[{'message':{'content':'ok'}}]}
        with patch.object(Config, 'LLM_PROVIDER', 'grok'), \
             patch.object(Config, 'LLM_API_KEY', 'grok-only'), \
             patch.object(Config, 'TOGETHER_API_KEY', 'together-only'), \
             patch('detect.requests.post', return_value=good) as post:
            detect._fatal_provider_error=None; detect._consecutive_failures=0
            self.assertEqual(detect._call_llm('score this','grok-test',1),'ok')
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer grok-only')
        self.assertEqual(post.call_args.args[0], 'https://api.x.ai/v1/chat/completions')

    def test_scoring_rejects_parakeet_as_llm_provider(self):
        import detect
        from config import Config
        with patch.object(Config, 'LLM_PROVIDER', 'parakeet'), patch.object(Config, 'LLM_API_KEY', 'x'):
            detect._fatal_provider_error=None; detect._consecutive_failures=0
            with self.assertRaisesRegex(detect.ProviderUnavailable, 'transcription-only'):
                detect._call_llm('x','x',1)

    def test_openai_scoring_uses_openai_endpoint_and_configured_models(self):
        import detect
        from config import Config
        good = Mock(status_code=200, ok=True)
        good.json.return_value = {'choices': [{'message': {'content': 'ok'}}]}
        with patch.object(Config, 'LLM_PROVIDER', 'openai'), \
             patch.object(Config, 'LLM_API_KEY', 'openai-only'), \
             patch.object(Config, 'OPENAI_SCORING_MODEL', 'scout-model'), \
             patch.object(Config, 'OPENAI_TITLE_MODEL', 'title-model'), \
             patch('detect.requests.post', return_value=good) as post:
            detect._fatal_provider_error = None
            detect._consecutive_failures = 0
            self.assertEqual(detect._scoring_model(), 'scout-model')
            self.assertEqual(detect._title_model(), 'title-model')
            self.assertEqual(detect._call_llm('score this', detect._scoring_model(), 1), 'ok')
        self.assertEqual(post.call_args.args[0], 'https://api.openai.com/v1/chat/completions')
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer openai-only')

    def test_worker_environment_replaces_stale_provider_keys(self):
        import app
        from config import Config
        user = SimpleNamespace(id=42, is_owner=True)
        with patch.dict(app.os.environ, {
            'OPENAI_API_KEY': 'stale-openai', 'GROK_API_KEY': 'stale-grok'
        }), patch.object(Config, 'LLM_PROVIDER', 'openai'), \
             patch.object(Config, 'LLM_API_KEY', 'new-openai'), \
             patch.object(Config, 'OPENAI_API_KEY', 'new-openai'), \
             patch.object(Config, 'GROK_API_KEY', 'saved-grok'):
            env = app.build_subprocess_env(user, 'test', 60, '9:16', True)
        self.assertEqual(env['LLM_API_KEY'], 'new-openai')
        self.assertEqual(env['OPENAI_API_KEY'], 'new-openai')
        self.assertEqual(env['GROK_API_KEY'], 'saved-grok')

    def test_openai_rate_limit_retries_instead_of_dropping_batch(self):
        import detect
        from config import Config
        limited = Mock(status_code=429, ok=False, text='Please try again in 10ms.')
        limited.headers = {}
        good = Mock(status_code=200, ok=True)
        good.json.return_value = {'choices': [{'message': {'content': 'ok'}}]}
        with patch.object(Config, 'LLM_PROVIDER', 'openai'), \
             patch.object(Config, 'LLM_API_KEY', 'openai-only'), \
             patch('detect._reserve_openai_capacity'), \
             patch('detect.time.sleep'), \
             patch('detect.requests.post', side_effect=[limited, good]) as post:
            detect._fatal_provider_error = None
            detect._consecutive_failures = 0
            self.assertEqual(detect._uncached_call_llm('score this', 'gpt-4o-mini', 1), 'ok')
        self.assertEqual(post.call_count, 2)

if __name__ == '__main__': unittest.main()

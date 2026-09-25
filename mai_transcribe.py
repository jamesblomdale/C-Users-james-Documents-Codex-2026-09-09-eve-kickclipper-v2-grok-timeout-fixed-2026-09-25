"""Bounded Azure MAI uploads with timestamp ownership and durable chunk results."""
import hashlib
import json
import math
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, replace
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
import requests
from transcribe import Word, Transcript


@dataclass(frozen=True)
class AudioChunk:
    index: int
    audio_start: float
    audio_end: float
    core_start: float
    core_end: float


def chunks_for(duration, minutes=15, overlap=8):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Source duration must be positive and finite')
    if not math.isfinite(minutes) or not 1 <= minutes <= 60 or not math.isfinite(overlap) or not 0 <= overlap <= 30:
        raise ValueError('MAI chunks must be 1–60 minutes with 0–30 seconds padding')
    seconds = minutes * 60
    return [AudioChunk(i, max(0, i*seconds-overlap), min(duration, (i+1)*seconds+overlap),
                       i*seconds, min(duration, (i+1)*seconds)) for i in range(math.ceil(duration/seconds))]


def own_words(words, chunk):
    result = []
    for word in words:
        if not isinstance(word.text, str) or not word.text.strip():
            raise ValueError('Invalid empty word')
        if not all(math.isfinite(t) for t in (word.start, word.end)) or not 0 <= word.start <= word.end <= chunk.audio_end-chunk.audio_start+1:
            raise ValueError('Invalid word timestamps')
        start, end = word.start+chunk.audio_start, word.end+chunk.audio_start
        if chunk.core_start <= (start+end)/2 < chunk.core_end:
            result.append(replace(word, text=word.text.strip(), start=start, end=end))
    return sorted(result, key=lambda w: (w.start,w.end))


def normalize(data, chunk, retain_overlap=False):
    if not isinstance(data, dict) or not isinstance(data.get('phrases'), list):
        raise ValueError('MAI response has no phrase list')
    words = []
    for phrase in data['phrases']:
        if not isinstance(phrase, dict) or not isinstance(phrase.get('words', []), list):
            raise ValueError('Invalid MAI phrase')
        if phrase.get('text', '').strip() and not phrase.get('words'):
            raise ValueError('Speech returned without word timestamps')
        for item in phrase.get('words', []):
            # Do not fabricate timings when the provider omits a required field.
            start = float(item['offsetMilliseconds'])/1000
            end = start + float(item['durationMilliseconds'])/1000
            words.append(Word(item['text'], start, end, item.get('confidence', phrase.get('confidence')),
                              str(phrase['speaker']) if phrase.get('speaker') is not None else None, phrase.get('locale')))
    if not words and any(p.get('text','').strip() for p in data.get('combinedPhrases', [])):
        raise ValueError('Transcript text returned without timed words')
    if retain_overlap:
        from audio_pipeline import global_words
        if any(w.end > chunk.audio_end-chunk.audio_start+1 for w in words):
            raise ValueError("Word exceeds audio chunk")
        return global_words(words, chunk.audio_start)
    return own_words(words, chunk)


def retry_delay(value, attempt):
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(value).timestamp()-time.time()
            except (TypeError, ValueError, OverflowError):
                seconds = 2**attempt
        if math.isfinite(seconds):
            return max(0, seconds)
    return min(30, 2**attempt)


class MaiTranscriber:
    def __init__(self, config, fallback=None, fallback_identity=None):
        self.config, self.fallback = config, fallback
        self.concurrency = config.MAI_CONCURRENCY
        if not 1 <= self.concurrency <= 8 or not 1 <= config.MAI_MAX_RETRIES <= 6 or not 10 <= config.MAI_TIMEOUT_SECONDS <= 600:
            raise ValueError('MAI concurrency 1–8, attempts 1–6 and timeout 10–600 seconds are required')
        chunks_for(1, config.MAI_CHUNK_MINUTES, config.MAI_CHUNK_OVERLAP_SECONDS)
        endpoint = urlparse(config.AZURE_SPEECH_ENDPOINT)
        if endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in ('','/'):
            raise ValueError('Set AZURE_SPEECH_ENDPOINT to the HTTPS resource origin')
        if not config.AZURE_SPEECH_KEY:
            raise ValueError('Set AZURE_SPEECH_KEY in Admin or choose local Whisper')
        if config.MAI_DIARIZATION:
            raise ValueError('Leave MAI_DIARIZATION=false for long-VOD transcription')
        if config.MAI_TRANSCRIBE_STYLE not in ('verbatim', 'clean'):
            raise ValueError('MAI_TRANSCRIBE_STYLE must be verbatim or clean')
        self.url = config.AZURE_SPEECH_ENDPOINT.rstrip('/')+'/speechtotext/transcriptions:transcribe'
        self.definition = {'enhancedMode':{'enabled':True,'model':config.MAI_MODEL,
                           'modelOptions':{'timestamps':'word','transcribeStyle':config.MAI_TRANSCRIBE_STYLE}},
                           'diarization':{'enabled':False}}
        if config.MAI_LANGUAGE:
            self.definition['locales'] = [config.MAI_LANGUAGE]
        if config.MAI_USE_PRIORITY_NAMES:
            self.definition['phraseList'] = {'phrases':list(dict.fromkeys(n.strip() for n in config.PRIORITY_NAMES.split(',') if n.strip()))}
        self.identity = {'version':1,'endpoint':config.AZURE_SPEECH_ENDPOINT,'api':config.MAI_API_VERSION,
                         'definition':self.definition,'minutes':config.MAI_CHUNK_MINUTES,
                         'overlap':config.MAI_CHUNK_OVERLAP_SECONDS,'fallback':fallback_identity,'fallback_enabled':fallback is not None}
        self.extract_lock = threading.Lock()
        self.auth_failed = threading.Event()
        from pipeline_state import AdaptiveGate
        self.gate = AdaptiveGate(self.concurrency)

    def _request(self, path):
        if path.stat().st_size >= 250_000_000:
            raise RuntimeError('MAI upload exceeds size limit')
        for attempt in range(self.config.MAI_MAX_RETRIES):
            if self.auth_failed.is_set():
                raise RuntimeError('MAI credentials rejected; remaining cloud calls stopped')
            delay = min(30, 2**attempt)
            try:
                with self.gate, path.open('rb') as audio:
                    response = requests.post(self.url, params={'api-version':self.config.MAI_API_VERSION},
                        headers={'Ocp-Apim-Subscription-Key':self.config.AZURE_SPEECH_KEY},
                        files={'audio':(path.name,audio,'audio/mpeg'),
                               'definition':(None,json.dumps(self.definition),'application/json')},
                        timeout=(10,self.config.MAI_TIMEOUT_SECONDS), allow_redirects=False)
                try:
                    if response.status_code == 200:
                        self.gate.success()
                        return response.json()
                    if response.status_code in (401,403):
                        self.auth_failed.set()
                    if response.status_code not in (408,429,500,502,503,504):
                        raise RuntimeError(f'MAI HTTP {response.status_code}; check resource settings')
                    self.gate.pressure()
                    delay = retry_delay(response.headers.get('Retry-After'), attempt)
                    print(f'[mai] retryable HTTP {response.status_code}; attempt {attempt+1}', flush=True)
                finally:
                    response.close()
            except (requests.Timeout, requests.ConnectionError):
                self.gate.pressure()
                print(f'[mai] network request failed; attempt {attempt+1}', flush=True)
            if attempt+1 < self.config.MAI_MAX_RETRIES:
                if delay > self.config.MAI_TIMEOUT_SECONDS:
                    raise RuntimeError('MAI requested a long cooldown; using configured fallback')
                time.sleep(delay)
        raise RuntimeError('MAI request failed after configured attempts')

    def _extract(self, source, chunk, path):
        # Uploads run in parallel; audio encodes share one CPU slot on this PC.
        with self.extract_lock:
            result = subprocess.run(['ffmpeg','-v','error','-y','-ss',str(chunk.audio_start),'-i',str(source),
                '-t',str(chunk.audio_end-chunk.audio_start),'-vn','-ac','1','-ar','16000',
                '-c:a','libmp3lame','-b:a','64k',str(path)],capture_output=True,timeout=600)
            if result.returncode:
                raise RuntimeError('FFmpeg could not extract transcription audio')

    def _one(self, source, chunk, cache_dir):
        cache = cache_dir/f'{chunk.index:04d}.json'
        if self.config.MAI_CACHE_ENABLED and cache.exists():
            try:
                data = json.loads(cache.read_text(encoding='utf-8'))
                if data['chunk'] != asdict(chunk):
                    raise ValueError('Chunk identity mismatch')
                words = [Word(**w) for w in data['words']]
                # Validate persisted absolute coordinates using this chunk's core.
                for w in words:
                    if not w.text or not all(math.isfinite(t) for t in (w.start,w.end)) or not 0 <= w.start <= w.end or not chunk.core_start <= (w.start+w.end)/2 < chunk.core_end:
                        raise ValueError('Invalid cache timestamps')
                print(f'[mai] cache hit chunk {chunk.index+1}', flush=True)
                return words
            except (ValueError,KeyError,TypeError):
                print(f'[mai] ignoring invalid cache for chunk {chunk.index+1}', flush=True)
        path = source.parent/f'mai-{uuid.uuid4().hex[:8]}.mp3'
        try:
            self._extract(source,chunk,path)
            provider = 'mai'
            try:
                words = normalize(self._request(path),chunk)
            except Exception as exc:
                if not self.fallback:
                    raise RuntimeError(f'MAI chunk {chunk.index+1} failed; no fallback enabled') from exc
                print(f'[mai] chunk {chunk.index+1} unavailable ({type(exc).__name__}); falling back to Whisper for this chunk only', flush=True)
                words = own_words(self.fallback(path).words,chunk)
                provider = 'whisper'
                print(f'[mai] fallback recovered chunk {chunk.index+1}', flush=True)
            if self.config.MAI_CACHE_ENABLED:
                temp = cache.with_suffix('.'+uuid.uuid4().hex[:8]+'.tmp')
                try:
                    temp.write_text(json.dumps({'chunk':asdict(chunk),'provider':provider,'words':[asdict(w) for w in words]},ensure_ascii=False),encoding='utf-8')
                    temp.replace(cache)
                finally:
                    temp.unlink(missing_ok=True)
            return words
        finally:
            path.unlink(missing_ok=True)

    def transcribe(self, source, progress_label=None):
        source = source.resolve()
        duration = float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration',
            '-of','default=nw=1:nk=1',str(source)],text=True,timeout=30).strip())
        chunks = chunks_for(duration,self.config.MAI_CHUNK_MINUTES,self.config.MAI_CHUNK_OVERLAP_SECONDS)
        stat = source.stat()
        identity = {**self.identity,'source':str(source),'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
        key = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:20]
        cache_dir = source.parent/'.mai_transcript_cache'/key
        cache_dir.mkdir(parents=True,exist_ok=True)
        print(f'[mai] source={duration/60:.1f} minutes; chunks={len(chunks)}; concurrency={self.concurrency}',flush=True)
        if progress_label:
            print(f'PROGRESS {progress_label} 0',flush=True)
        results = {}
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(self._one,source,c,cache_dir):c.index for c in chunks}
            try:
                for future in as_completed(futures):
                    index = futures[future]
                    results[index] = future.result()
                    print(f'[mai] chunk {index+1}/{len(chunks)} complete ({len(results)} finished)',flush=True)
                    if progress_label:
                        print(f'PROGRESS {progress_label} {int(100*len(results)/len(chunks))}',flush=True)
            except Exception:
                for future in futures:
                    future.cancel()
                raise
        words = sorted((w for index in sorted(results) for w in results[index]),key=lambda w:(w.start,w.end))
        return Transcript(source,words,' '.join(w.text for w in words))

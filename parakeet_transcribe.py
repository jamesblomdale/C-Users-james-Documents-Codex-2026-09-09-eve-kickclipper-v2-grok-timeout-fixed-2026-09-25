"""Together AI NVIDIA Parakeet transcription adapter.

The long-VOD scheduler owns extraction, parallelism, cache/checkpoints and overlap
merging. This module owns Together API I/O, credential/model verification,
provider-side retry/backoff and conversion to the project's normalized word schema.
"""
from __future__ import annotations

import os

import hashlib
import io
import math
import mimetypes
import random
import threading
import time
import wave
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from transcribe import Word, Transcript

_PROBE_CACHE = {}
_PROBE_CACHE_LOCK = threading.Lock()


class ParakeetTranscriber:
    API_URL = "https://api.together.xyz/v1/audio/transcriptions"

    def __init__(self, config, fallback=None, fallback_identity=None):
        self.config = config
        self.fallback = fallback
        self.concurrency = int(config.PARAKEET_CONCURRENCY)
        if not config.TOGETHER_API_KEY:
            raise ValueError("Set TOGETHER_API_KEY in Admin/.env or choose another transcription provider")
        if not 1 <= self.concurrency <= 16:
            raise ValueError("PARAKEET_CONCURRENCY must be 1–16")
        if not 10 <= int(config.PARAKEET_TIMEOUT_SECONDS) <= 600:
            raise ValueError("PARAKEET_TIMEOUT_SECONDS must be 10–600")
        if not 1 <= int(config.PARAKEET_MAX_RETRIES) <= 6:
            raise ValueError("PARAKEET_MAX_RETRIES must be 1–6")
        self.url = self.API_URL
        self.identity = {
            "version": 2,
            "provider": "together",
            "model": config.PARAKEET_MODEL,
            "language": config.PARAKEET_LANGUAGE,
            "diarize": bool(config.PARAKEET_DIARIZE),
            "chunk_seconds": float(getattr(config, "PARAKEET_CHUNK_SECONDS", 1800)),
            "chunk_overlap_seconds": float(getattr(config, "PARAKEET_CHUNK_OVERLAP_SECONDS", 8)),
            "fallback": fallback_identity,
            "fallback_enabled": fallback is not None,
        }
        from pipeline_state import AdaptiveGate
        self.gate = AdaptiveGate(self.concurrency, int(getattr(config, "ASR_MIN_CONCURRENCY", 1)), initial=min(self.concurrency, int(os.getenv("ASR_INITIAL_CONCURRENCY", "8"))))
        self.auth_failed = threading.Event()
        self._probe_ok = False
        self._sessions = threading.local()

    def _session(self):
        """Reuse TLS per worker and ignore accidental shell proxy settings."""
        session = getattr(self._sessions, "value", None)
        if session is None:
            session = requests.Session()
            proxy = str(getattr(self.config, "NETWORK_PROXY", "") or "").strip()
            session.trust_env = not bool(getattr(self.config, "DISABLE_ENV_PROXY", True))
            if proxy:
                session.proxies.update({"http": proxy, "https": proxy})
            adapter = HTTPAdapter(pool_connections=self.concurrency, pool_maxsize=self.concurrency,
                                  max_retries=0, pool_block=True)
            session.mount("https://", adapter)
            self._sessions.value = session
        return session

    @staticmethod
    def _retry_delay(value, attempt):
        if value:
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                try:
                    seconds = parsedate_to_datetime(value).timestamp() - time.time()
                except (TypeError, ValueError, OverflowError):
                    seconds = 2 ** attempt
            if math.isfinite(seconds):
                return max(0.0, min(60.0, seconds))
        # Full jitter prevents concurrent ASR workers from retrying in lockstep.
        return random.uniform(0.0, min(30.0, 2 ** attempt))

    @staticmethod
    def _response_detail(response):
        try:
            payload = response.json()
            if isinstance(payload, dict):
                err = payload.get("error", payload)
                if isinstance(err, dict):
                    return str(err.get("message") or err.get("detail") or err.get("code") or err)[:500]
                return str(err)[:500]
        except Exception:
            pass
        return (response.text or "").strip()[:500]

    def _post(self, *, file_tuple, data, timeout=None):
        return self._session().post(
            self.url,
            headers={"Authorization": f"Bearer {self.config.TOGETHER_API_KEY}"},
            data=data,
            files={"file": file_tuple},
            timeout=(10, timeout or int(self.config.PARAKEET_TIMEOUT_SECONDS)),
            allow_redirects=False,
        )

    def probe(self):
        """Verify API key + exact Parakeet model before a long VOD starts."""
        if self._probe_ok:
            return True
        cache_key = hashlib.sha256((self.config.TOGETHER_API_KEY + "|" + self.config.PARAKEET_MODEL).encode()).hexdigest()
        with _PROBE_CACHE_LOCK:
            checked_at = _PROBE_CACHE.get(cache_key, 0)
            if checked_at and time.monotonic() - checked_at < 600:
                self._probe_ok = True
                return True
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x00\x00" * 16000)
        data = {
            "model": self.config.PARAKEET_MODEL,
            "language": self.config.PARAKEET_LANGUAGE or "en",
            "response_format": "json",
        }
        started = time.monotonic()
        response = self._post(
            file_tuple=("kickclipper-parakeet-probe.wav", buf.getvalue(), "audio/wav"),
            data=data,
            timeout=45,
        )
        try:
            if response.status_code != 200:
                detail = self._response_detail(response)
                if response.status_code in (401, 403):
                    self.auth_failed.set()
                    raise RuntimeError(f"Together rejected the API key (HTTP {response.status_code}): {detail}")
                if response.status_code == 402:
                    raise RuntimeError(f"Together requires billing/credits (HTTP 402): {detail}")
                if response.status_code == 429:
                    raise RuntimeError(f"Together rate limit prevented the Parakeet check (HTTP 429): {detail}")
                raise RuntimeError(f"Together Parakeet preflight failed (HTTP {response.status_code}): {detail}")
            body = response.json()
            if not isinstance(body, dict) or "text" not in body:
                raise RuntimeError("Together Parakeet preflight returned an unexpected response")
            self._probe_ok = True
            with _PROBE_CACHE_LOCK:
                _PROBE_CACHE[cache_key] = time.monotonic()
            print(
                f"[parakeet] VERIFIED model={self.config.PARAKEET_MODEL} "
                f"latency={time.monotonic()-started:.2f}s concurrency={self.concurrency}",
                flush=True,
            )
            return True
        finally:
            response.close()

    def _request(self, path: Path) -> dict:
        if path.stat().st_size >= 450_000_000:
            raise RuntimeError("Parakeet upload is too large; reduce PARAKEET_CHUNK_SECONDS")
        last_error = None
        for attempt in range(int(self.config.PARAKEET_MAX_RETRIES)):
            if self.auth_failed.is_set():
                raise RuntimeError("Together API credentials were rejected; cloud calls stopped")
            started = time.monotonic()
            delay = min(30.0, 2 ** attempt)
            try:
                with self.gate, path.open("rb") as audio:
                    data = {
                        "model": self.config.PARAKEET_MODEL,
                        "language": self.config.PARAKEET_LANGUAGE or "en",
                        "response_format": "verbose_json",
                        "timestamp_granularities": "word",
                    }
                    if self.config.PARAKEET_DIARIZE:
                        data["diarize"] = "true"
                    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    response = self._post(file_tuple=(path.name, audio, mime), data=data)
                try:
                    elapsed = time.monotonic() - started
                    if response.status_code == 200:
                        self.gate.success()
                        payload = response.json()
                        if not isinstance(payload, dict):
                            raise RuntimeError("Together returned a non-object response")
                        print(
                            f"[parakeet] OK {path.name} {path.stat().st_size/1024/1024:.1f}MB in {elapsed:.2f}s",
                            flush=True,
                        )
                        from pipeline_metrics import api_call
                        api_call(kind="asr", provider="together", model=self.config.PARAKEET_MODEL,
                                 elapsed=elapsed, ok=True)
                        return payload

                    detail = self._response_detail(response)
                    last_error = f"HTTP {response.status_code}: {detail}"
                    if response.status_code in (401, 403):
                        self.auth_failed.set()
                        raise RuntimeError(f"Together rejected the API key ({last_error})")
                    if response.status_code == 402:
                        raise RuntimeError(f"Together billing/credits are required ({last_error})")
                    if response.status_code == 413:
                        raise RuntimeError("Together rejected the audio as too large; reduce PARAKEET_CHUNK_SECONDS")
                    if response.status_code == 400 and "audio_too_long" in detail.lower():
                        raise RuntimeError("Together rejected the audio duration; reduce PARAKEET_CHUNK_SECONDS")
                    if response.status_code not in (408, 429, 500, 502, 503, 504):
                        from pipeline_metrics import api_call
                        api_call(kind="asr", provider="together", model=self.config.PARAKEET_MODEL,
                                 elapsed=elapsed, ok=False)
                        raise RuntimeError(f"Together/Parakeet request failed ({last_error})")

                    self.gate.pressure()
                    from pipeline_metrics import api_call, retry
                    api_call(kind="asr", provider="together", model=self.config.PARAKEET_MODEL,
                             elapsed=elapsed, ok=False)
                    retry("asr")
                    delay = self._retry_delay(response.headers.get("Retry-After"), attempt)
                    print(
                        f"[parakeet] retryable {last_error}; attempt {attempt+1}; waiting {delay:.1f}s",
                        flush=True,
                    )
                finally:
                    response.close()
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = type(exc).__name__
                # Network failures are often a dropped keep-alive/TLS socket rather than
                # provider overload. Recreate the session and retry quickly; rate-limit
                # responses still use Retry-After/full backoff above.
                delay = (0.25, 0.75, 1.5, 3.0, 6.0, 10.0)[min(attempt, 5)]
                self.gate.pressure()
                from pipeline_metrics import api_call, retry
                api_call(kind="asr", provider="together", model=self.config.PARAKEET_MODEL,
                         elapsed=time.monotonic()-started, ok=False)
                retry("asr")
                session = getattr(self._sessions, "value", None)
                if session is not None:
                    session.close()
                    self._sessions.value = None
                print(
                    f"[parakeet] network {last_error}; attempt {attempt+1}; waiting {delay:.1f}s",
                    flush=True,
                )
            if attempt + 1 < int(self.config.PARAKEET_MAX_RETRIES):
                time.sleep(delay)
        raise RuntimeError(f"Parakeet request failed after configured attempts ({last_error or 'unknown error'})")

    def request_words(self, path: Path, chunk) -> list[Word]:
        data = self._request(path)
        raw_words = data.get("words")
        if not isinstance(raw_words, list):
            if str(data.get("text", "")).strip():
                raise ValueError("Parakeet returned transcript text without word timestamps")
            return []
        words = []
        local_duration = chunk.audio_end - chunk.audio_start
        for item in raw_words:
            if not isinstance(item, dict):
                raise ValueError("Invalid Parakeet word entry")
            text = str(item.get("word", item.get("text", ""))).strip()
            if not text:
                continue
            start = float(item["start"])
            end = float(item["end"])
            if not all(math.isfinite(v) for v in (start, end)) or not 0 <= start <= end <= local_duration + 2:
                raise ValueError("Invalid Parakeet word timestamp")
            confidence = item.get("confidence")
            if confidence is not None:
                confidence = float(confidence)
                if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                    confidence = None
            speaker = item.get("speaker_id", item.get("speaker"))
            words.append(
                Word(
                    text,
                    start + chunk.audio_start,
                    end + chunk.audio_start,
                    confidence,
                    str(speaker) if speaker is not None else None,
                    data.get("language"),
                )
            )
        return words

    def transcribe(self, source: Path, progress_label=None) -> Transcript:
        import subprocess
        from types import SimpleNamespace

        duration = float(
            subprocess.check_output(
                [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=nw=1:nk=1", str(source),
                ],
                text=True,
                timeout=30,
            ).strip()
        )
        chunk = SimpleNamespace(audio_start=0.0, audio_end=duration)
        words = self.request_words(source, chunk)
        if progress_label:
            print(f"PROGRESS {progress_label} 100", flush=True)
        return Transcript(source, words, " ".join(w.text for w in words))

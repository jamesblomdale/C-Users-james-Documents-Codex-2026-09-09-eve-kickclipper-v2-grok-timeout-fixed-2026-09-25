"""
Runs faster-whisper on each captured chunk to produce a word-level
timestamped transcript. This is local and free — no API cost. A "small"
model is a good speed/accuracy tradeoff for near-real-time use; bump to
"medium" if you have GPU headroom and want cleaner transcripts.
"""

import subprocess
import os
import sys
from dataclasses import dataclass
from pathlib import Path
# A verified upstream wheel is bundled so an older environment can load the
# batch implementation. Native dependencies remain managed by requirements.txt.
_vendor = Path(__file__).resolve().parent / "vendor_python"
if _vendor.is_dir() and str(_vendor) not in sys.path:
    sys.path.insert(0, str(_vendor))


@dataclass
class Word:
    text: str
    start: float
    end: float
    confidence: float | None = None
    speaker: str | None = None
    language: str | None = None
    uncertain: bool = False


@dataclass
class Transcript:
    chunk_path: Path
    words: list[Word]
    full_text: str


def _reencode_audio(chunk_path: Path) -> Path:
    """
    Re-encodes just the audio track to a clean, simple mono WAV via
    ffmpeg. This is the actual fix for chunks that fail to transcribe:
    the underlying decoder faster-whisper uses internally occasionally
    trips on a specific chunk's exact codec/container quirk, but running
    that same audio back through ffmpeg's own (much more robust) decoder
    first almost always produces a file that transcribes cleanly --
    recovering the content instead of losing it.
    """
    wav_path = chunk_path.with_suffix(".reencoded.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(chunk_path), "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav_path)],
        check=True, capture_output=True, timeout=60,
    )
    return wav_path


class FasterWhisperTranscriber:
    def __init__(self, model_size: str = "small", profile: str = "balanced"):
        from faster_whisper import WhisperModel
        self.profile = (profile or "balanced").lower()
        # Auto-detects and uses a GPU if one is available, since that's
        # by far the single biggest speed lever for transcription --
        # typically 5-10x faster than CPU. Falls back to CPU cleanly if
        # no CUDA-capable GPU is found, so this is always safe to leave
        # on regardless of what machine this runs on.
        device, compute_type = self._pick_device()
        print(f"[transcribe] using device={device} compute_type={compute_type}")
        self.device = device
        model_kwargs = {}
        if device == "cpu":
            # Use most of the CPU without starving ffmpeg / the web app.
            # i5-1334U -> 8 threads; i5-8500 -> 6 threads automatically.
            default_threads = str(min(8, os.cpu_count() or 4))
            configured_threads = os.getenv("WHISPER_CPU_THREADS") or default_threads
            model_kwargs["cpu_threads"] = max(1, int(configured_threads))
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type, **model_kwargs)
        self.batch_size = max(1, int(os.getenv("WHISPER_BATCH_SIZE", "1")))
        self.engine = self.model
        if self.batch_size > 1:
            try:
                from faster_whisper import BatchedInferencePipeline
            except ImportError as exc:
                raise RuntimeError("Batch transcription requires faster-whisper 1.2.1. Run Setup.ps1, or use batch size 1 with the existing runtime.") from exc
            self.engine = BatchedInferencePipeline(model=self.model)
        # Tracks how often a chunk genuinely comes back with no usable
        # audio at all (both the direct attempt AND the re-encode
        # fallback failed) -- an occasional one is normal (real silence,
        # a corrupted segment), but a high rate means something systemic
        # is wrong with the HLS source itself, worth surfacing clearly
        # rather than letting it silently eat minutes of content one
        # quiet log line at a time.
        self._chunks_seen = 0
        self._chunks_failed = 0

    @staticmethod
    def _pick_device() -> tuple[str, str]:
        try:
            import ctranslate2
            if os.getenv("WHISPER_DEVICE", "auto") == "cpu":
                return "cpu", "int8"
            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda", "float16"
            return "cpu", "int8"
        except Exception:
            return "cpu", "int8"

    def _run_model(self, source_path: Path, progress_label: str, total_duration_hint: float, beam_override=None) -> Transcript:
        """Raw transcribe pass on a given file -- shared by both the
        first attempt and the re-encoded-fallback attempt below."""
        from config import Config
        initial_prompt = Config.PRIORITY_NAMES.replace(",", " ") if Config.PRIORITY_NAMES else None

        options = {"batch_size": self.batch_size} if self.batch_size > 1 else {}
        segments, info = self.engine.transcribe(
            str(source_path),
            **options,
            language=os.getenv("WHISPER_LANGUAGE") or None,
            condition_on_previous_text=False,
            word_timestamps=True,
            vad_filter=True,  # skips silence, saves time on dead air
            initial_prompt=initial_prompt,  # primes recognition of streamer/celebrity
                                             # names so they come through correctly
                                             # instead of getting garbled into
                                             # something unrecognizable
            beam_size=beam_override or int(os.getenv(
                "WHISPER_VOD_BEAM_SIZE" if self.profile == "vod" else "WHISPER_LIVE_BEAM_SIZE",
                os.getenv("WHISPER_BEAM_SIZE", "1")
            )),  # VOD keeps a modest search beam for accuracy; live prioritises latency. (the library default) is noticeably slower for
                           # modest accuracy gain on clear speech -- beam 1 is the
                           # standard fast setting and is plenty for clip-finding
        )

        total = total_duration_hint or getattr(info, "duration", None)
        words = []
        full_text_parts = []
        last_reported = -1
        for segment in segments:
            full_text_parts.append(segment.text)
            if getattr(segment, "avg_logprob", 0) < -1.0:
                print(f"[transcribe] WARNING low-confidence speech at {segment.start:.1f}-{segment.end:.1f}s; review the audio")
            for w in segment.words or []:
                words.append(Word(text=w.word, start=w.start, end=w.end,
                                  confidence=getattr(w, 'probability', None), language=getattr(info, 'language', None)))

            if progress_label and total:
                pct = min(100, int((segment.end / total) * 100))
                if pct != last_reported:
                    print(f"PROGRESS {progress_label} {pct}")
                    last_reported = pct

        return Transcript(chunk_path=source_path, words=words, full_text="".join(full_text_parts).strip())

    def transcribe_chunk(self, chunk_path: Path, progress_label: str = None, total_duration_hint: float = None) -> Transcript:
        """
        progress_label: if set, prints "PROGRESS {label} {pct}" lines as
        transcription advances (parsed by app.py for the UI progress bar).
        total_duration_hint: known clip duration, used to compute % complete
        as segments stream in -- faster-whisper yields segments lazily, so
        this doesn't require transcribing twice.

        If the chunk fails to transcribe outright (codec edge case), this
        does NOT just give up and lose that content -- it re-encodes the
        audio through ffmpeg and retries once before finally giving up.
        A chunk only comes back empty if BOTH attempts genuinely find no
        usable speech (real silence/dead air), not on the first failure.
        """
        self._chunks_seen += 1
        try:
            result = self._run_model(chunk_path, progress_label, total_duration_hint)
            if progress_label:
                print(f"PROGRESS {progress_label} 100")
            return result
        except Exception as e:
            print(f"[transcribe] {chunk_path.name} failed to transcribe directly ({e}), "
                  f"re-encoding audio and retrying...")

        # Fallback attempt: re-encode through ffmpeg first, then retry.
        reencoded = None
        try:
            reencoded = _reencode_audio(chunk_path)
            result = self._run_model(reencoded, progress_label, total_duration_hint)
            print(f"[transcribe] recovered {chunk_path.name} via re-encode fallback")
            if progress_label:
                print(f"PROGRESS {progress_label} 100")
            return Transcript(chunk_path=chunk_path, words=result.words, full_text=result.full_text)
        except Exception as e2:
            self._chunks_failed += 1
            print(f"[transcribe] re-encode fallback also failed for {chunk_path.name} ({e2}), "
                  f"ERROR this chunk could not be decoded -- content is missing")
            if progress_label:
                print(f"PROGRESS {progress_label} 100")

            # Check every 10 chunks (not every single one) so this warns
            # clearly without spamming the log once the rate is already
            # known to be high.
            if self._chunks_seen >= 10 and self._chunks_seen % 10 == 0:
                fail_rate = self._chunks_failed / self._chunks_seen
                if fail_rate > 0.15:
                    print(f"[transcribe] WARNING: {self._chunks_failed}/{self._chunks_seen} chunks "
                          f"({fail_rate:.0%}) have failed to transcribe entirely -- this is high enough "
                          f"that it's likely a systemic issue with the source (an unstable connection, "
                          f"an expired/degraded stream URL), not isolated bad segments. Real content is "
                          f"being lost, not just skipped noise.")

            raise RuntimeError(f"Transcription failed for {chunk_path.name}; content was NOT confirmed silent") from e2
        finally:
            if reencoded:
                reencoded.unlink(missing_ok=True)


class Transcriber:
    """Stable interface: cloud mode never loads Whisper unless fallback is needed."""
    def __init__(self, model_size='small', profile='balanced'):
        from config import Config
        import threading
        self.model_size, self.profile = model_size, profile
        self.provider = Config.TRANSCRIPTION_PROVIDER
        self._whisper = None
        self._fallback_lock = threading.Lock()
        self._mai = None
        self._parakeet = None
        self._cloud = None
        if self.provider == 'mai':
            from mai_transcribe import MaiTranscriber
            # Validate explicitly; misspelled providers or missing keys must not
            # silently start a many-hour local transcription.
            self._mai = MaiTranscriber(Config, self._fallback if Config.TRANSCRIPTION_FALLBACK in ('whisper', 'faster-whisper') else None,
                                       fallback_identity={'model':model_size,'profile':profile,
                                                          'beam':os.getenv('WHISPER_BEAM_SIZE','1'),
                                                          'vod_beam':os.getenv('WHISPER_VOD_BEAM_SIZE','1'),
                                                          'live_beam':os.getenv('WHISPER_LIVE_BEAM_SIZE','1')})
            self._cloud = self._mai
        elif self.provider == 'parakeet':
            from parakeet_transcribe import ParakeetTranscriber
            allow_local_fallback = (Config.PARAKEET_LOCAL_FALLBACK and
                                    Config.TRANSCRIPTION_FALLBACK in ('whisper', 'faster-whisper'))
            self._parakeet = ParakeetTranscriber(Config, self._fallback if allow_local_fallback else None,
                                       fallback_identity={'model':model_size,'profile':profile,
                                                          'beam':os.getenv('WHISPER_BEAM_SIZE','1'),
                                                          'vod_beam':os.getenv('WHISPER_VOD_BEAM_SIZE','1'),
                                                          'live_beam':os.getenv('WHISPER_LIVE_BEAM_SIZE','1')})
            if Config.PARAKEET_VERIFY_CREDENTIALS:
                self._parakeet.probe()
            self._cloud = self._parakeet
        elif self.provider != 'whisper':
            raise ValueError('TRANSCRIPTION_PROVIDER must be mai, parakeet, or whisper')
        print(f'[role] TRANSCRIPTION provider={self.provider} key=TOGETHER_API_KEY' if self.provider == 'parakeet' else f'[role] TRANSCRIPTION provider={self.provider}', flush=True)

    def _local(self):
        if self._whisper is None:
            self._whisper = FasterWhisperTranscriber(self.model_size, profile=self.profile)
        return self._whisper

    def _fallback(self, path):
        # A single model, serialized: four failing uploads must not load four
        # Whisper models or oversubscribe a small laptop's memory.
        with self._fallback_lock:
            return self._local().transcribe_chunk(path)

    @property
    def device(self):
        return 'cloud' if self._cloud else self._local().device

    @property
    def batch_size(self):
        return self._cloud.concurrency if self._cloud else self._local().batch_size

    def transcribe_chunk(self, chunk_path, progress_label=None, total_duration_hint=None):
        if self._cloud:
            return self._cloud.transcribe(Path(chunk_path), progress_label)
        return self._local().transcribe_chunk(Path(chunk_path), progress_label, total_duration_hint)

    def transcribe_vod(self, source, on_progress=None):
        from audio_pipeline import transcribe_vod
        return transcribe_vod(self, Path(source), on_progress)

    def transcribe_vod_url(self, url, cache_root, duration, on_progress=None):
        """Rolling remote-HLS ASR: range-pull ASR windows concurrently instead of
        waiting for a complete multi-hour audio download first."""
        from audio_pipeline import transcribe_vod
        return transcribe_vod(self, Path(cache_root) / 'remote-hls', on_progress,
                              remote_url=str(url), duration_override=float(duration), cache_root=Path(cache_root))

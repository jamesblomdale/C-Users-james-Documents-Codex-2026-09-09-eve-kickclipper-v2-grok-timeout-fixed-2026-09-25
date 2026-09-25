"""Reuse completed audio ingestion for cloud-ASR resume of the same VOD range."""
import hashlib
import json
from pathlib import Path


def cached_audio(base, reference, start, end, pull):
    # v2 includes the compact-audio ingest format so an old PCM-WAV cache cannot
    # silently pin a job to the pre-speed-update path.
    identity = json.dumps(['v2-compact-audio', reference, start, end], sort_keys=True)
    folder = base/'.audio_cache'/hashlib.sha256(identity.encode()).hexdigest()[:16]
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder/'complete.json'
    if marker.is_file():
        try:
            saved = json.loads(marker.read_text(encoding='utf-8'))
            filename = saved['filename']
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise ValueError('Invalid cached audio filename')
            audio = folder/filename
            stat = audio.stat()
            if saved.get('size') == stat.st_size and saved.get('mtime_ns') == stat.st_mtime_ns and stat.st_size > 44:
                print('[vod] reusing completed compact audio ingest for transcription resume', flush=True)
                return audio
        except (ValueError, OSError, KeyError, TypeError):
            pass
    audio = Path(pull(folder))
    stat = audio.stat()
    from pipeline_state import atomic_json
    atomic_json(marker, {'filename':audio.name,'size':stat.st_size,'mtime_ns':stat.st_mtime_ns})
    return audio

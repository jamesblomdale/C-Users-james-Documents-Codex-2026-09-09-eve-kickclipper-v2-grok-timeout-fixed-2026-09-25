"""One bounded, resumable VOD audio scheduler shared by cloud and local ASR."""
import json
import math
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import asdict, replace
from pathlib import Path
from config import Config
from pipeline_state import atomic_json, fingerprint, check_cancelled
from transcribe import Word, Transcript


def normalize_audio(source, folder, start=None, end=None):
    """Extract a local source's audio once; never transcode its full video."""
    check_cancelled()
    target = folder/'audio.m4a'
    cmd = ['ffmpeg','-v','error','-y']
    if start: cmd += ['-ss',str(start)]
    cmd += ['-i',str(source)]
    if end is not None: cmd += ['-t',str(end-(start or 0))]
    cmd += ['-vn','-ac','1','-ar','16000','-c:a','aac','-b:a',__import__('os').getenv('AUDIO_INGEST_BITRATE','32k'),'-movflags','+faststart',str(target)]
    try:
        subprocess.run(cmd,capture_output=True,check=True,timeout=1800)
        check_cancelled()
        if target.stat().st_size > Config.MAX_TEMP_GB*1024**3:
            raise RuntimeError('Normalized audio exceeds MAX_TEMP_GB')
        return target
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def global_words(words, offset):
    result = []
    for word in words:
        if not word.text.strip() or not all(math.isfinite(t) for t in (word.start, word.end)) or not 0 <= word.start <= word.end:
            raise ValueError('Invalid ASR word or timestamp')
        if word.confidence is not None and (not math.isfinite(word.confidence) or not 0 <= word.confidence <= 1):
            raise ValueError('Invalid ASR confidence')
        result.append(replace(word, start=word.start+offset, end=word.end+offset))
    return result


def merge_transcript_chunks(chunks):
    """Match only across overlapping chunks; retain unique words and confident alternatives."""
    merged = []
    for chunk, words in sorted(chunks, key=lambda item: item[0].audio_start):
        recent = {}
        for i in range(len(merged)-1, -1, -1):
            if merged[i].end < chunk.audio_start-1:
                break
            key = re.sub(r'[^\w]', '', merged[i].text.casefold())
            recent.setdefault(key, []).append(i)
        used = set()
        additions = []
        for word in sorted(words, key=lambda w: (w.start,w.end)):
            key = re.sub(r'[^\w]', '', word.text.casefold())
            options = [i for i in recent.get(key, []) if i not in used and
                       abs(merged[i].start-word.start) <= .55 and abs(merged[i].end-word.end) <= .75]
            if options:
                i = min(options, key=lambda i: abs(merged[i].start-word.start))
                used.add(i)
                if (word.confidence if word.confidence is not None else -1) > (merged[i].confidence if merged[i].confidence is not None else -1):
                    merged[i] = word
            else:
                # Disagreeing words at nearly identical times need targeted review.
                conflicts = [i for indices in recent.values() for i in indices if
                             abs(merged[i].start-word.start) < .15 and abs(merged[i].end-word.end) < .2]
                if conflicts:
                    word = replace(word, uncertain=True)
                    for i in conflicts:
                        merged[i] = replace(merged[i], uncertain=True)
                additions.append(word)
        merged.extend(additions)
        merged.sort(key=lambda w:(w.start,w.end))
    return merged


def transcribe_vod(engine, source, on_progress=None, *, remote_url=None, duration_override=None, cache_root=None):
    from mai_transcribe import chunks_for, normalize
    Config.validate_pipeline()
    source = Path(source)
    if remote_url:
        duration = float(duration_override or subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration',
            '-of','default=nw=1:nk=1',str(remote_url)], text=True, timeout=30))
        source_label = str(remote_url)
        source_size = 0
        source_mtime = 0
    else:
        source = source.resolve()
        duration = float(duration_override or subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration',
            '-of','default=nw=1:nk=1',str(source)], text=True, timeout=30))
        stat = source.stat()
        source_label = str(source)
        source_size = stat.st_size
        source_mtime = stat.st_mtime_ns
    if getattr(engine, '_parakeet', None):
        chunk_seconds = Config.PARAKEET_CHUNK_SECONDS
        chunk_overlap = Config.PARAKEET_CHUNK_OVERLAP_SECONDS
    else:
        chunk_seconds = Config.ASR_CHUNK_SECONDS
        chunk_overlap = Config.ASR_CHUNK_OVERLAP_SECONDS
    chunks = chunks_for(duration, chunk_seconds/60, chunk_overlap)
    identity = {'version':3,'source':source_label,'size':source_size,'mtime':source_mtime,'rolling_remote':bool(remote_url),
        'provider':engine.provider,'model':engine.model_size,'seconds':chunk_seconds,
        'overlap':chunk_overlap,'cloud':engine._cloud.identity if getattr(engine,'_cloud',None) else None,
        'language':__import__('os').getenv('WHISPER_LANGUAGE'),
        'beam':__import__('os').getenv('WHISPER_VOD_BEAM_SIZE','1'),
        'chunk_format':'aac-copy-v1' if (Config.ASR_FAST_CHUNK_COPY and source.suffix.lower() in ('.m4a','.aac')) else 'mp3-v1',
        'chunk_bitrate':Config.ASR_AUDIO_BITRATE,
        'names':Config.PRIORITY_NAMES,'quality':Config.ASR_QUALITY_MIN_CONFIDENCE}
    cache_base = Path(cache_root) if cache_root else source.parent
    cache = cache_base/'.asr_cache'/fingerprint(identity)
    cache.mkdir(parents=True, exist_ok=True)
    # One local model avoids RAM multiplication on low-spec computers.
    workers = Config.ASR_MAX_CONCURRENCY if getattr(engine,'_cloud',None) else 1
    if getattr(engine, '_parakeet', None):
        workers = min(workers, engine._parakeet.concurrency)
    if engine._mai:
        from pipeline_state import AdaptiveGate
        from types import SimpleNamespace
        # Per-engine copy: never mutate the process-wide Config or stored secrets.
        settings = {name:getattr(engine._mai.config,name) for name in dir(engine._mai.config) if name.isupper()}
        settings.update(MAI_TIMEOUT_SECONDS=Config.ASR_TIMEOUT_SECONDS, MAI_MAX_RETRIES=Config.ASR_RETRY_COUNT+1)
        engine._mai.config = SimpleNamespace(**settings)
        engine._mai.gate = AdaptiveGate(workers,Config.ASR_MIN_CONCURRENCY)
    atomic_json(cache/'manifest.json', {'identity':identity,'chunks':[asdict(c) for c in chunks], 'workers':workers})
    provider_label = (f'Together/Parakeet {engine._parakeet.config.PARAKEET_MODEL}'
                      if getattr(engine, '_parakeet', None) else engine.provider)
    print(f'[asr] ACTIVE provider={provider_label}; {len(chunks)} chunks; '
          f'chunk={chunk_seconds:.0f}s; {workers} workers; overlap={chunk_overlap:.0f}s', flush=True)
    results = {}
    failures = {}
    started = time.monotonic()

    def one(chunk):
        check_cancelled()
        result_path = cache/f'{chunk.index:05d}.json'
        if result_path.exists():
            try:
                data = json.loads(result_path.read_text(encoding='utf-8'))
                if data['status'] == 'COMPLETED' and data['chunk'] == asdict(chunk):
                    words = global_words([Word(**w) for w in data['words']], 0)
                    if any(w.start < chunk.audio_start or w.end > chunk.audio_end+1 for w in words):
                        raise ValueError('Cache word outside chunk')
                    from pipeline_metrics import cache_hit
                    cache_hit('asr')
                    return words
            except (ValueError,KeyError,TypeError):
                pass
        state = {'status':'PROCESSING','chunk':asdict(chunk)}
        atomic_json(result_path,state)
        # Remote/local VOD preparation normalizes analysis audio to mono 16 kHz AAC/M4A.
        # For that common path, cut AAC frames directly instead of decoding + re-encoding
        # every 10-minute chunk to MP3. This removes duplicate CPU work while keeping
        # bounded, resumable per-chunk uploads. Fall back to MP3 for arbitrary sources.
        fast_copy = bool((not remote_url) and Config.ASR_FAST_CHUNK_COPY and source.suffix.lower() in ('.m4a', '.aac'))
        audio_ext = 'm4a' if (fast_copy or remote_url) else 'mp3'
        audio = cache/f"{chunk.index:05d}.{audio_ext}"
        try:
            check_cancelled()
            expected = (chunk.audio_end-chunk.audio_start)*8000
            if expected*workers > Config.MAX_TEMP_GB*1024**3 or shutil.disk_usage(cache).free < expected*workers+64*1024**2:
                raise RuntimeError('Insufficient temporary disk budget for ASR chunks')
            if fast_copy:
                ffmpeg_cmd = ['ffmpeg','-v','error','-y','-ss',str(chunk.audio_start),'-i',str(source),
                    '-t',str(chunk.audio_end-chunk.audio_start),'-vn','-c:a','copy','-movflags','+faststart',str(audio)]
            else:
                input_value = str(remote_url) if remote_url else str(source)
                ffmpeg_cmd = ['ffmpeg','-v','error','-y','-ss',str(chunk.audio_start)]
                if remote_url:
                    # HLS window extraction: keep connections alive, allow the HLS demuxer
                    # to use multiple HTTP connections, and recover transient CDN failures
                    # without restarting the entire VOD job. These are input options.
                    ffmpeg_cmd += [
                        '-reconnect','1', '-reconnect_streamed','1',
                        '-reconnect_on_network_error','1',
                        '-reconnect_on_http_error','429,5xx',
                        '-respect_retry_after','1',
                        '-reconnect_delay_max','3',
                        '-http_persistent','1', '-http_multiple','1',
                    ]
                ffmpeg_cmd += ['-i',input_value, '-t',str(chunk.audio_end-chunk.audio_start),'-vn','-ac','1','-ar','16000',
                    '-c:a','aac','-b:a',Config.ASR_AUDIO_BITRATE,'-threads','1',str(audio)]
            try:
                completed = subprocess.run(ffmpeg_cmd, capture_output=True, check=True, timeout=600)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f'FFmpeg timed out extracting ASR chunk {chunk.index+1}') from exc
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or b'').decode('utf-8','replace').strip()[-1000:]
                raise RuntimeError(f'FFmpeg failed extracting ASR chunk {chunk.index+1}: {detail or "unknown error"}') from exc
            for attempt in range(Config.ASR_RETRY_COUNT+1):
                check_cancelled()
                try:
                    if engine._mai:
                        words = normalize(engine._mai._request(audio),chunk, retain_overlap=True)
                    elif getattr(engine, '_parakeet', None):
                        words = engine._parakeet.request_words(audio, chunk)
                    else:
                        words = global_words(engine._fallback(audio).words,chunk.audio_start)
                    break
                except InterruptedError:
                    raise
                except Exception:
                    if getattr(engine, '_cloud', None) and engine._cloud.fallback:
                        words = global_words(engine._fallback(audio).words,chunk.audio_start)
                        break
                    if getattr(engine, '_cloud', None) or attempt == Config.ASR_RETRY_COUNT:
                        raise
                    print(f'[asr] retry chunk {chunk.index+1}, attempt {attempt+2}',flush=True)
            state.update(status='COMPLETED',words=[asdict(w) for w in words])
            atomic_json(result_path,state)
            return words
        except BaseException as exc:
            # Persist the actionable provider/ffmpeg message in the checkpoint;
            # storing only ``RuntimeError`` made deferred-chunk failures
            # impossible to diagnose after the worker had moved on.
            detail = str(exc).strip()
            state.update(status='FAILED', error=type(exc).__name__,
                         error_detail=detail[-1000:] if detail else type(exc).__name__)
            atomic_json(result_path,state)
            raise
        finally:
            audio.unlink(missing_ok=True)

    # Only worker-count futures are submitted. Queue size never scales with duration.
    last_contiguous = 0
    iterator = iter(chunks)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        def submit():
            chunk = next(iterator,None)
            if chunk is not None:
                path = cache/f'{chunk.index:05d}.json'
                if not path.exists():
                    atomic_json(path,{'status':'PENDING','chunk':asdict(chunk)})
                pending[pool.submit(one,chunk)] = chunk
        for _ in range(workers): submit()
        try:
            while pending:
                check_cancelled()
                ready, _ = wait(pending,timeout=.5,return_when=FIRST_COMPLETED)
                for future in ready:
                    chunk = pending.pop(future)
                    try:
                        results[chunk.index] = future.result()
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        # Finish/checkpoint the other in-flight chunks before a
                        # conservative retry. One unstable upload must not cancel
                        # successful work from the rest of a multi-hour VOD.
                        failures[chunk.index] = (chunk, exc)
                        print(f'[asr] chunk {chunk.index+1} deferred after {type(exc).__name__}; continuing other chunks', flush=True)
                    contiguous = 0
                    while contiguous in results: contiguous += 1
                    if on_progress and contiguous > last_contiguous:
                        # Hold trailing overlap until the next chunk confirms it.
                        safe_end = duration if contiguous == len(chunks) else chunks[contiguous-1].core_end-chunk_overlap
                        merged = merge_transcript_chunks([(c,results[c.index]) for c in chunks[:contiguous]])
                        on_progress([w for w in merged if w.end <= safe_end], safe_end)
                        last_contiguous = contiguous
                    print(f'[asr] Transcribing {len(results)} / {len(chunks)}',flush=True)
                    print(f'PROGRESS transcribe {int(100*len(results)/len(chunks))}',flush=True)
                    submit()
        except BaseException:
            for future in pending: future.cancel()
            raise
    if failures:
        print(f'[asr] retrying {len(failures)} deferred chunk(s) at conservative concurrency', flush=True)
        unresolved = []
        for index, (chunk, first_error) in sorted(failures.items()):
            try:
                results[index] = one(chunk)
                print(f'[asr] recovered chunk {index+1}', flush=True)
            except Exception as exc:
                unresolved.append((index, exc))
        if unresolved:
            indices = ', '.join(str(index+1) for index, _ in unresolved)
            raise RuntimeError(
                f'ASR could not recover chunk(s) {indices}. All successful chunks are checkpointed; '
                'restart the same VOD to resume only the missing chunks.'
            ) from unresolved[0][1]
    print('PROGRESS transcribe 100', flush=True)
    merge_start = time.monotonic()
    words = merge_transcript_chunks([(c,results[c.index]) for c in chunks])
    atomic_json(cache/'timings.json',{'source_duration_seconds':duration,'asr_chunks':len(chunks),
        'asr_concurrency':workers,'asr_seconds':time.monotonic()-started,
        'merge_seconds':time.monotonic()-merge_start,'real_time_factor':(time.monotonic()-started)/duration})
    from pipeline_metrics import update
    update(values={'audio_duration_seconds': duration, 'asr_chunks': len(chunks),
                   'asr_concurrency': workers, 'transcription_time': time.monotonic()-started,
                   'transcript_merge_time': time.monotonic()-merge_start,
                   'number_of_transcript_words': len(words)})
    return Transcript(source if not remote_url else Path('remote-hls'),words,' '.join(w.text for w in words))

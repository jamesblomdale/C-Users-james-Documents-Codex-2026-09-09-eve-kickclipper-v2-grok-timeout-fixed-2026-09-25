"""Retry only uncertain intervals inside promising candidates."""
import json
import subprocess
from dataclasses import asdict
from config import Config
from pipeline_state import atomic_json, fingerprint, check_cancelled
from audio_pipeline import global_words
from transcribe import Word


def suspicious(words):
    return [w for w in words if w.uncertain or
            (w.confidence is not None and w.confidence < Config.ASR_QUALITY_MIN_CONFIDENCE)]


def repair_candidates(candidates, words, engine, source, offset, folder):
    if not source or not Config.ASR_QUALITY_RETRY_LIMIT:
        return
    folder.mkdir(parents=True,exist_ok=True)
    ranges = []
    for candidate in sorted(candidates,key=lambda c:c.score,reverse=True):
        uncertain = suspicious([w for w in words if candidate.start <= w.start < candidate.end])
        if not uncertain: continue
        start, end = max(offset,uncertain[0].start-5), min(words[-1].end,uncertain[-1].end+5)
        end = min(end,start+60)
        if any(a <= start <= b for a,b in ranges): continue
        ranges.append((start,end))
        if len(ranges) >= Config.ASR_QUALITY_RETRY_LIMIT: break
    for start,end in ranges:
        check_cancelled()
        original = [w for w in words if start <= w.start < end]
        key = fingerprint({'start':start,'end':end,'words':[asdict(w) for w in original], 'model':engine.model_size})
        cache, audio = folder/(key+'.json'), folder/(key+'.mp3')
        try:
            if cache.exists():
                improved = [Word(**w) for w in json.loads(cache.read_text(encoding='utf-8'))]
            else:
                subprocess.run(['ffmpeg','-v','error','-y','-ss',str(start-offset),'-i',str(source),
                    '-t',str(end-start),'-vn','-ac','1','-ar','16000','-b:a','64k',str(audio)],
                    capture_output=True,check=True,timeout=90)
                # Small targeted local retry with a wider beam, never the whole VOD.
                improved = global_words(engine._local()._run_model(audio,None,end-start,beam_override=3).words,start)
                atomic_json(cache,[asdict(w) for w in improved])
            old_conf = sum(w.confidence or 0 for w in original)/max(1,len(original))
            new_conf = sum(w.confidence or 0 for w in improved)/max(1,len(improved))
            if improved and new_conf > old_conf and len(improved) >= len(original)*.5 and all(start <= w.start <= w.end <= end+1 for w in improved):
                words[:] = sorted([w for w in words if not start <= w.start < end]+improved,key=lambda w:w.start)
                print(f'[quality] repaired uncertain speech {start:.2f}..{end:.2f}',flush=True)
            else:
                print(f'[quality] kept original speech {start:.2f}..{end:.2f}; retry not demonstrably clearer',flush=True)
        except InterruptedError:
            raise
        except Exception as exc:
            print(f'[quality] WARNING targeted retry unavailable ({type(exc).__name__}); original retained',flush=True)
        finally:
            audio.unlink(missing_ok=True)
    for candidate in candidates:
        candidate.text = ' '.join(w.text for w in words if candidate.start <= w.start < candidate.end)

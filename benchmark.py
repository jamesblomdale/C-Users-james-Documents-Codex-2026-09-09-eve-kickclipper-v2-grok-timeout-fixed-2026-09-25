"""Measure actual ASR throughput on a representative local audio/video sample."""
import argparse,json,time,subprocess
from pathlib import Path
from transcribe import Transcriber

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('source',type=Path)
    parser.add_argument('--model',default='small')
    args=parser.parse_args()
    duration=float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(args.source)],text=True).strip())
    load_started=time.perf_counter();asr=Transcriber(args.model,profile='vod')
    if asr.provider == 'whisper':asr._local()
    load_time=time.perf_counter()-load_started
    started=time.perf_counter();transcript=asr.transcribe_vod(args.source)
    elapsed=time.perf_counter()-started
    result=dict(provider=asr.provider,model=args.model,device=asr.device,batch_size=asr.batch_size,source_seconds=duration,
                model_load_seconds=round(load_time,2),transcription_seconds=round(elapsed,2),
                realtime_multiple=round(duration/elapsed,2),
                projected_8h_transcription_minutes=round(elapsed/duration*480,1),
                caveat='ASR-only extrapolation, not a VOD completion promise. Cached chunks can make repeated measurements artificially fast. Excludes downloads, scoring, review and exports.',
                transcript=transcript.full_text,words=[vars(w) for w in transcript.words])
    target=Path('benchmark-result.json');target.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('words','transcript')},indent=2))
    print('Review benchmark-result.json against the actual audio before selecting a faster profile.')

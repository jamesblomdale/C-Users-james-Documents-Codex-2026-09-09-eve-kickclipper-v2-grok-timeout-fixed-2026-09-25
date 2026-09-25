"""Retry saved FFmpeg export failures from a job log, without ASR or API calls.

Usage: python retry_failed_exports.py failed-log.txt [--apply]
Run inside the project whose output folder contains the failed job's media.
Dry-run is the default; existing playable clips are never overwritten.
"""
import argparse,ast,json,subprocess
from pathlib import Path

def inside(root, value):
    path=(root/value).resolve()
    if root not in path.parents:
        raise ValueError('Export path escapes this project')
    return path

def playable(path):
    if not path.is_file():return False
    p=subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height','-of','json',str(path)],capture_output=True,text=True)
    try:return p.returncode==0 and bool(json.loads(p.stdout).get('streams'))
    except ValueError:return False

def recover(log, apply=False):
    root=Path.cwd().resolve(); successes=0;failed=0;seen=set()
    for line in log.splitlines():
        if 'failed to finish clip' not in line or "Command '" not in line:continue
        try:
            raw=line.split("Command '",1)[1].rsplit("' returned",1)[0]
            args=ast.literal_eval(raw)
            if not isinstance(args,list) or not all(isinstance(a,str) for a in args):raise ValueError('Invalid saved arguments')
            source=inside(root,args[args.index('-i')+1]);target=inside(root,args[-1])
            if target in seen:continue
            seen.add(target)
            if playable(target):
                print(f'SKIP already playable: {target}');continue
            captions=target.with_suffix('.ass')
            if not source.is_file() or not captions.is_file():raise ValueError(f'Missing source/captions for {target}')
            # Only reconstruct the fixed center-crop export shown in the log.
            vf=args[args.index('-vf')+1]
            crop=vf.split(',ass=',1)[0]
            import re
            if not re.fullmatch(r'crop=ih\*[0-9]+/[0-9]+:ih:\(iw-ih\*[0-9]+/[0-9]+\)/2:0',crop):
                raise ValueError('Unsupported saved crop; use the editor for this clip')
            start=float(args[args.index('-ss')+1]);duration=float(args[args.index('-t')+1])
            import math
            if not math.isfinite(start+duration) or start<0 or not 0<duration<=600:raise ValueError('Invalid trim')
            print(f'{"RETRY" if apply else "READY"}: {target.name} in {target.parent.name}, {duration:.1f}s')
            if not apply:continue
            # Controlled ASCII caption filename in cwd avoids every filter-path
            # escape issue, including apostrophes and Windows drive colons.
            import uuid,shutil
            temp=root/('retry-'+uuid.uuid4().hex)
            temp.mkdir()
            try:
                shutil.copy2(captions,temp/'captions.ass')
                temporary=temp/'render.mp4'
                command=['ffmpeg','-v','error','-y','-ss',str(start),'-i',str(source),'-t',str(duration),
                         '-vf',crop+',ass=filename=captions.ass','-c:v','libx264','-preset','veryfast','-crf','22',
                         '-af','loudnorm=I=-16:TP=-1.5:LRA=11','-c:a','aac',str(temporary)]
                subprocess.run(command,cwd=temp,check=True)
                if not playable(temporary):raise ValueError('Recovered file has no video')
                temporary.replace(target)
            finally:
                for name in ('captions.ass','render.mp4'):
                    (temp/name).unlink(missing_ok=True)
                temp.rmdir()
            posts=target.parent/'posts.txt'
            if not posts.exists():posts.write_text('Recovered clip — review and write platform titles before posting.\n',encoding='utf-8')
            successes+=1
        except Exception as exc:
            failed+=1;print(f'ERROR: {exc}')
    print(f'{len(seen)} distinct exports found; {successes} recovered; {failed} errors.')
    return failed

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log',type=Path);parser.add_argument('--apply',action='store_true')
    a=parser.parse_args();raise SystemExit(bool(recover(a.log.read_text(encoding='utf-8'),a.apply)))

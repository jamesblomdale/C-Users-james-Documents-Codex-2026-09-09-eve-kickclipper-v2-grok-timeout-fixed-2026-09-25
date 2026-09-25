"""Bounded acoustic measurements; loudness is not evidence of clip quality."""
import math
import subprocess
from array import array


def measure_audio(source, start, end):
    if not source or end <= start:
        return {'available': False}
    result = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(max(0, start)),
        '-i', str(source), '-t', str(min(180, end-start)), '-vn', '-ac', '1',
        '-ar', '8000', '-f', 'f32le', 'pipe:1'], capture_output=True, timeout=45)
    if result.returncode or not result.stdout:
        return {'available': False}
    samples = array('f')
    samples.frombytes(result.stdout)
    import sys
    if sys.byteorder != 'little':
        samples.byteswap()
    rms = math.sqrt(sum(x*x for x in samples)/len(samples))
    peak = max(abs(x) for x in samples)
    return {'available': True, 'rms_dbfs': round(20*math.log10(max(rms, 1e-9)), 2),
            'peak_dbfs': round(20*math.log10(max(peak, 1e-9)), 2),
            'note': 'Amplitude only; cannot identify laughter, sarcasm, speakers or emotions. Do not reward volume alone.'}

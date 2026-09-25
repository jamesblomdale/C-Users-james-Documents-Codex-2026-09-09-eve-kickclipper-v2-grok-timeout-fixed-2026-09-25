"""Local log-monitor bot: deterministic, private, and independent of paid APIs."""
import re
import time
from collections import deque

RULES = [
    (r'ProxyError|Unable to connect to proxy|127\.0\.0\.1.*port=9', 'network', 'The app cannot reach the network through its configured proxy. Restart using Open-Kick-Clipper.cmd in a normal Windows session; check proxy settings if it persists.'),
    (r"ass_read_file|libass track|fopen failed", "captions", "Caption file could not be loaded. Check the subtitle path and retry the saved export."),
    (r'no space left|disk full|not enough space', 'disk', 'Free disk space, then retry from saved results.'),
    (r'429|rate.limit|quota|insufficient.credit|available credits|spending limit|provider credits', 'quota', 'Check provider credits and spending limits; restart after they are resolved.'),
    (r'401|403|invalid.api.key|authenticationerror', 'credentials', 'Check the provider key or expired source URL in owner settings.'),
    (r'cuda|cudnn|cublas|out of memory', 'compute', 'Check CUDA libraries and GPU memory; lower the batch size or use CPU.'),
    (r'failed|error|exception|traceback|dropping|could not be parsed', 'processing', 'Inspect the surrounding log; this may mean missing content, not silence.'),
    (r'warning|retry|skipping|fallback', 'warning', 'A recovery or skipped operation occurred. Check the affected stage.'),
]

# Each subprocess reports progress inside its current stage. Convert that to
# one job-wide bar. Scout and transcription overlap, so they share the same
# band; rendering gets the largest band because it dominated measured jobs.
STAGE_BANDS = {
    'audio_pull': (0, 12),
    'transcribe': (12, 30),
    'scout': (12, 30),
    'scan': (30, 50),
    'review': (50, 55),
    'ranking': (55, 60),
    'cut': (60, 80),
    'layout': (80, 99),
    'encode': (60, 99),
    'done': (100, 100),
}

def redact(line):
    line = re.sub(r'(?i)(bearer\s+|api[_-]?key[=: ]+)[^\s,]+', r'\1[redacted]', line)
    return re.sub(r'(https?://[^\s?]+)\?[^\s]+', r'\1?[redacted]', line)

class Guardian:
    def __init__(self):
        self.incidents = deque(maxlen=100)
        self.last_activity = time.monotonic()
        self.stage = None
        self.samples = deque(maxlen=16)
        self.overall_pct = 0
        self.overall_samples = deque(maxlen=24)

    def observe(self, line):
        self.last_activity = time.monotonic()
        if re.search(r'\[scout\].*(invalid_candidate|local fallback|local_heat_fallback|ValueError)', line, re.I):
            incident=dict(category='warning',message=redact(line)[-1200:],action='Scout recovered a candidate parse/span issue; this is not a provider quota incident.',time=time.time(),severity='warning')
            self.incidents.append(incident); return incident
        for pattern, category, action in RULES:
            if re.search(pattern, line, re.I):
                if category == 'credentials':
                    lower = line.lower()
                    if 'api.openai.com' in lower or 'openai' in lower:
                        action = 'OpenAI rejected the key or model permission. Re-save the OpenAI key and verify gpt-5.4-mini is enabled for the project.'
                    elif 'api.x.ai' in lower or 'xai' in lower or 'grok' in lower:
                        action = 'Grok/xAI rejected the key or model permission. Re-save the Grok key and verify grok-4.6 is enabled.'
                    elif 'together' in lower or 'parakeet' in lower:
                        action = 'Together rejected the key or Parakeet model. Re-save the Together key and verify nvidia/parakeet-tdt-0.6b-v3 is available.'
                incident = dict(category=category, message=redact(line)[-1200:], action=action,
                                time=time.time(), severity='warning' if category == 'warning' else 'error')
                if self.incidents and self.incidents[-1]['message'] == incident['message']:
                    self.incidents[-1]['count'] = self.incidents[-1].get('count', 1) + 1
                else:
                    self.incidents.append(incident)
                return incident

    def progress(self, stage, pct):
        """Map stage progress to one monotonic job-wide bar and one TOTAL ETA.

        Never expose a stage ETA (audio pull, fragment fetch, transcription,
        scan, etc.) as though it were the whole job. ETA is estimated only from
        measured movement of the job-wide percentage.
        """
        now = time.monotonic()
        self.last_activity = now
        if stage != self.stage or (self.samples and pct < self.samples[-1][1]):
            self.stage = stage
            self.samples.clear()
        if not self.samples or pct != self.samples[-1][1]:
            self.samples.append((now, pct))
        stage_pct = max(0, min(100, pct))
        band_start, band_end = STAGE_BANDS.get(stage, (self.overall_pct, self.overall_pct))
        mapped = round(band_start + (band_end-band_start) * stage_pct / 100)
        self.overall_pct = max(self.overall_pct, min(100, mapped))
        if not self.overall_samples or self.overall_pct != self.overall_samples[-1][1]:
            self.overall_samples.append((now, self.overall_pct))

        eta = None
        # TOTAL ETA only. Wait for enough job-wide movement so the first fast
        # stage transition cannot produce a wildly optimistic/slow estimate.
        if len(self.overall_samples) >= 3:
            dt = now - self.overall_samples[0][0]
            dp = self.overall_pct - self.overall_samples[0][1]
            if dt >= 5 and dp >= 2 and self.overall_pct < 100:
                eta = round((100-self.overall_pct) * dt / dp)
        return dict(stage=stage, stage_pct=stage_pct, pct=self.overall_pct,
                    eta_seconds=eta, eta_scope='job',
                    eta_confidence='measured_total' if eta is not None else 'calibrating')

    def snapshot(self, running):
        idle = round(time.monotonic() - self.last_activity)
        return dict(name='Log Guardian', incidents=list(self.incidents),
                    state='quiet' if running and idle > 180 else ('watching' if running else 'finished'),
                    idle_seconds=idle, note='No new output; this can happen during model loading or encoding.' if running and idle > 180 else '')

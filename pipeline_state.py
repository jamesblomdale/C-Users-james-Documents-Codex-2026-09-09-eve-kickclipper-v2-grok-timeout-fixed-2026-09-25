"""Disk checkpoints and cooperative cancellation for existing VOD jobs."""
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.'+uuid.uuid4().hex[:8]+'.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def check_cancelled():
    marker = os.getenv('JOB_CANCEL_FILE')
    if marker and Path(marker).exists():
        raise InterruptedError('Job cancelled; completed checkpoints preserved')


class Timings:
    def __init__(self, path):
        self.path, self.start = path, time.monotonic()
        self.values = {}

    def record(self, **values):
        self.values.update(values)
        self.values['total_processing_seconds'] = time.monotonic()-self.start
        duration = self.values.get('source_duration_seconds', 0)
        if duration:
            self.values['real_time_factor'] = self.values['total_processing_seconds']/duration
            self.values['speed_multiple'] = duration/self.values['total_processing_seconds']
        # Use the telemetry writer's lock/merge path so concurrent API workers
        # cannot have their counters overwritten by a stage update.
        from pipeline_metrics import update
        update(values=self.values, path=self.path)


class AdaptiveGate:
    def __init__(self, maximum, minimum=1, initial=None):
        self.maximum, self.minimum = maximum, minimum
        self.limit = max(minimum, min(maximum, initial if initial is not None else maximum))
        self.active = self.stable = 0
        self.last_pressure = 0.0
        self.cv = threading.Condition()

    def __enter__(self):
        with self.cv:
            while self.active >= self.limit:
                check_cancelled()
                self.cv.wait(.25)
            check_cancelled()
            self.active += 1
        return self

    def __exit__(self, *args):
        with self.cv:
            self.active -= 1
            self.cv.notify_all()

    def pressure(self):
        with self.cv:
            now = time.monotonic()
            # Concurrent requests often report the same short network incident.
            # Count that burst once instead of collapsing 4 -> 2 -> 1.
            if now - self.last_pressure < 5:
                return
            self.last_pressure = now
            self.limit = max(self.minimum, self.limit//2)
            self.stable = 0
            print(f'[asr] reducing active request limit to {self.limit}', flush=True)

    def success(self):
        with self.cv:
            self.stable += 1
            if self.stable >= 8:
                self.limit = min(self.maximum, self.limit+1)
                self.stable = 0
                self.cv.notify_all()

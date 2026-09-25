"""Low-overhead, process-safe-enough telemetry for one local VOD worker.

The profiler deliberately contains no provider keys, prompts, transcripts or
media paths.  It records durations, counts and token totals only.  Workers use
the path supplied in ``JOB_METRICS_PATH`` so cached/recovery runs contribute to
the same source-scoped report without coupling media code to Flask or SQLite.
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from pipeline_state import atomic_json


_LOCK = threading.RLock()

# USD per one million tokens. Environment overrides allow prices to be updated
# without a code release. Unknown models retain usage but report no fake cost.
_TOKEN_PRICES = {
    "grok-4.6": (1.25, 2.50),
    "gpt-5-mini": (0.15, 0.60),
}


def _path() -> Path | None:
    value = os.getenv("JOB_METRICS_PATH", "").strip()
    return Path(value) if value else None


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def update(values: dict | None = None, increments: dict | None = None, *, path=None) -> None:
    path = Path(path) if path is not None else _path()
    if path is None:
        return
    with _LOCK:
        data = _load(path)
        data.update(values or {})
        for key, amount in (increments or {}).items():
            data[key] = data.get(key, 0) + amount
        atomic_json(path, data)


@contextmanager
def stage(name: str):
    started = time.monotonic()
    try:
        yield
    finally:
        update({f"{name}_seconds": time.monotonic() - started})


def cache_hit(kind: str) -> None:
    update(increments={"cache_hits": 1, f"{kind}_cache_hits": 1})


def retry(kind: str) -> None:
    update(increments={"failed_retried_requests": 1, f"{kind}_retries": 1})


def api_call(*, kind: str, provider: str, model: str, elapsed: float,
             usage: dict | None = None, ok: bool = True) -> None:
    usage = usage or {}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    increments = {
        f"{kind}_requests": 1,
        "api_requests": 1,
        f"{kind}_seconds": float(elapsed),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if not ok:
        increments["failed_api_requests"] = 1
    price = _TOKEN_PRICES.get(model)
    if price:
        input_rate = float(os.getenv(f"PRICE_{model.upper().replace('-', '_')}_INPUT", price[0]))
        output_rate = float(os.getenv(f"PRICE_{model.upper().replace('-', '_')}_OUTPUT", price[1]))
        increments["estimated_api_cost_usd"] = (
            input_tokens * input_rate + output_tokens * output_rate
        ) / 1_000_000
    update(
        values={"last_api_provider": provider, "last_api_model": model},
        increments=increments,
    )


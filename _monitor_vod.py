import time
import requests

API = "http://127.0.0.1:5000/api/vod/status"
seen_lines = {}
last_running = {}

print("[monitor] watching VOD jobs...", flush=True)

while True:
    try:
        jobs = requests.get(API, timeout=10).json()
    except Exception as e:
        print(f"[monitor] could not reach app: {e}", flush=True)
        time.sleep(10)
        continue

    for job_id, job in jobs.items():
        log = job.get("log", [])
        prev_count = seen_lines.get(job_id, 0)
        new_lines = log[prev_count:] if len(log) >= prev_count else log
        seen_lines[job_id] = len(log)

        for line in new_lines:
            low = line.lower()
            if ("traceback" in low or "error" in low or "exception" in low
                    or line.startswith("[vod] producing") or "checkpointed" in low
                    or "ranking pass" in low or "clip" in low and "cutting" in low):
                print(f"[monitor:{job_id}] {line}", flush=True)

        running = job.get("running")
        if last_running.get(job_id) is None:
            last_running[job_id] = running
        elif last_running[job_id] and not running:
            tail = "\n".join(log[-15:])
            print(f"[monitor:{job_id}] job stopped running. Last lines:\n{tail}", flush=True)
        last_running[job_id] = running

    time.sleep(15)

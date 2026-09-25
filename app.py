"""
Web control panel + the SaaS layer around it: accounts, a real credit
ledger, and DB-backed job tracking so "how many clips did this job
produce" is always a SQL COUNT against the `Clip` table, never a log
line someone has to trust.

Run it with:
    python app.py
Then open http://localhost:5000

Multi-tenancy: each customer's output lives under output/u<user_id>/...
(a subprocess env override, not a code change to vod.py/main.py, which
still just read Config.OUTPUT_DIR). The owner account is the one
exception -- it keeps writing to the original unprefixed output/ root,
so every clip made before this SaaS layer existed is still exactly
where it was, still visible, no migration needed.

Platform API keys (Grok, etc.) are read from server-side .env ONLY.
There is no code path anywhere in this file that reads an API key out
of a request body -- customers configure everything else (threshold,
aspect, tracking, priority names...) but never see or set the key.
"""

import os
import sys
import json
import shutil
import subprocess
import threading
from guardian import Guardian, redact
from editor_validation import validate_recipe
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_login import login_required, current_user

from config import Config
from models import db, init_db, User, Job, Clip, JobEvent, JobKind, JobStatus, utcnow
from auth import init_auth, owner_required, local_demo_request
import billing
from product_policy import normalize_live_source, validate_customer_vod_input, SUPPORTED_UPLOAD_EXTENSIONS


def demo_unlimited():
    """Local demo accounts show unlimited usage without changing paid billing."""
    return os.getenv('DEMO_MODE', '1') == '1' and local_demo_request()

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SECRET_KEY"] = Config.SECRET_KEY
app.config["SQLALCHEMY_DATABASE_URI"] = Config.DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
init_db(app)
init_auth(app)

from functools import wraps
editor_lock = threading.Lock()
def serialized_edit(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not editor_lock.acquire(blocking=False):
            return jsonify(ok=False, error="Another clip edit is still running. Try again when it finishes."), 409
        try:
            return fn(*args, **kwargs)
        finally:
            editor_lock.release()
    return wrapped

jobs_lock = threading.Lock()
live_jobs = {}   # channel -> {"process": Popen, "log_lines": [...], "job_id": int, ...}
vod_jobs = {}    # job_key -> {"process": Popen, "log_lines": [...], "channel": str, "job_id": int, ...}
_vod_job_counter = 0  # monotonic, so removed jobs can never cause an id collision

ENV_PATH = Path(".env")

# A live session's length isn't known up front (it ends whenever the
# streamer or the user stops it), so -- unlike VOD, which holds exactly
# the source's real duration -- live jobs hold this many minutes'
# worth up front and settle actual elapsed time on stop. Deliberately
# simple for v1; the doc this app is built from explicitly says VOD +
# credits + refunds is the launch bar and live can follow.
LIVE_DEFAULT_HOLD_MINUTES = 60
MIN_BALANCE_TO_START = 1.0  # fast reject before we even know a real estimate
# Matches the Growth plan's advertised concurrency on the Pricing page.
# There's no per-plan enforcement yet (that needs a real subscription/
# plan field on User, which doesn't exist until Stripe is wired up) --
# this is one blanket cap for every account for now, owner included.
MAX_CONCURRENT_VOD_JOBS = Config.HARD_LOCAL_JOB_CAP
# A VOD download can be several GB (a multi-hour 1080p60 stream has hit
# 9-10GB in testing) -- refuse to start a new job below this much free
# space rather than risk another mid-download "No space left on
# device" failure, which gets worse the more jobs run at once.
MIN_FREE_DISK_GB_TO_START = 15


def read_env() -> dict:
    from dotenv import dotenv_values
    return {key: value or '' for key, value in dotenv_values(ENV_PATH).items()}


def write_env_value(key: str, value: str):
    """
    Updates one KEY=value line in .env in place (preserving every other
    line, including comments), or appends it if it isn't there yet.
    This is the only place platform credentials get written -- it's
    reached exclusively from an owner-only route, never from a
    customer-facing one, and the value itself is never echoed back to
    any template or log line.
    """
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    prefix = f"{key}="
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _free_disk_gb() -> float:
    total, used, free = shutil.disk_usage(Path(Config.OUTPUT_DIR).resolve().anchor or ".")
    return free / (1024 ** 3)


def _running_vod_jobs_for(user_id: int):
    """Live view of this user's currently-running VOD subprocesses --
    vod_jobs is keyed by a monotonic job_key, not by user, so this scans
    rather than doing a dict lookup."""
    prefix = f"{user_id}_"
    return [
        (key, job) for key, job in vod_jobs.items()
        if key.startswith(prefix) and job["process"].poll() is None
    ]


def user_namespace(user: User) -> str:
    """Owner keeps the original, unprefixed output/ root (so every clip
    made before accounts existed is still exactly where it was); every
    other user gets their own subtree so customers can never see or
    collide with each other's files."""
    return "" if user.is_owner else f"u{user.id}"


def user_output_dir(user: User) -> Path:
    ns = user_namespace(user)
    return Path(Config.OUTPUT_DIR) / ns if ns else Path(Config.OUTPUT_DIR)


def _owns_subpath(user: User, subpath: str) -> bool:
    root = Path(Config.OUTPUT_DIR).resolve()
    target = (root / subpath).resolve()
    if root not in target.parents:
        return False
    if user.is_owner:
        return True
    return (root / f"u{user.id}").resolve() in target.parents



def stream_output(proc, job: dict, db_job_id: int, app_ctx):
    """
    Reads the subprocess's stdout line by line and does three things
    with it, per line:
      1. "PROGRESS estimate <minutes>"  -> places the real credit hold
         now that source duration is actually known (VOD only).
      2. "PROGRESS <stage> <pct> ..."   -> updates the DB job's stage,
         for the human-readable status the UI shows.
      3. anything else                  -> persisted as a JobEvent row,
         so a job's history survives an app restart, and appended to
         the in-memory tail the frontend polls for the live terminal.
    """
    with app_ctx:
        job_row = db.session.get(Job, db_job_id)
        guardian = job.setdefault("guardian", Guardian())
        for line in proc.stdout:
            line = redact(line.rstrip())
            guardian.observe(line)
            if any(token in line for token in ("skipping undecodable chunk", "dropping this window", "dropping all", "failed to finish clip", "failed to cut clip", "ERROR missing batch", "ERROR invalid scoring")):
                job["content_gaps"] = True
            if line.startswith("PROGRESS "):
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "estimate":
                    try:
                        minutes = float(parts[2])
                        job_row = db.session.get(Job, db_job_id)
                        billing.estimate_and_hold(job_row, minutes)
                    except billing.InsufficientCredits as e:
                        job["log_lines"].append(
                            f"[billing] insufficient credits: need {e.needed:.1f}, have {e.available:.1f} -- stopping job"
                        )
                        job_row.error_category = "insufficient_credits"
                        db.session.commit()
                        proc.terminate()
                    except (IndexError, ValueError):
                        pass
                    continue
                try:
                    stage, pct = parts[1], int(parts[2])
                    eta = int(parts[4]) if len(parts) >= 5 and parts[3] == "eta_seconds" else None
                    job["progress"] = guardian.progress(stage, pct)
                    # Guardian owns the single job-wide ETA. Worker ETAs are
                    # stage-local and must never replace the total completion ETA.
                    job_row = db.session.get(Job, db_job_id)
                    if job_row:
                        job_row.stage = stage
                        job_row.stage_pct = job["progress"]["pct"]
                        job_row.status = JobStatus.RUNNING.value
                        db.session.commit()
                except (IndexError, ValueError):
                    pass
                job["log_lines"].append(line)
                if len(job["log_lines"]) > 300:
                    job["log_lines"].pop(0)
                db.session.add(JobEvent(job_id=db_job_id, line=line))
                db.session.commit()
                continue

            job["log_lines"].append(line)
            if len(job["log_lines"]) > 300:
                job["log_lines"].pop(0)
            db.session.add(JobEvent(job_id=db_job_id, line=line))
            db.session.commit()

        proc.wait()
        # terminate() on Windows reports a non-zero exit code even for a
        # deliberate, successful stop -- don't let that read as a crash.
        crashed = proc.returncode != 0 and not job.get("user_stopped")
        if crashed:
            guardian.observe(f"ERROR process exited with code {proc.returncode}; inspect the preceding log.")
        _finalize(db_job_id, crashed or job.get("content_gaps", False), job)


def _classify_error(log_tail: str) -> str:
    """
    A deliberately simple, keyword-based first pass at error
    categorization -- NOT the AI incident classifier from the spec
    (that's a separate, deferred piece). This just makes sure every
    failed job gets SOME category instead of an empty string, so the
    admin view isn't useless in the meantime.
    """
    text = log_tail.lower()
    if "insufficient credits" in text:
        return "insufficient_credits"
    if "no speech detected" in text:
        return "transcript_empty"
    if any(k in text for k in ("ffmpeg", "ffprobe")):
        return "ffmpeg"
    if any(k in text for k in ("connectionerror", "timeout", "timed out")):
        return "timeout"
    if any(k in text for k in ("kick", "streamlink", "m3u8", "playback url")):
        return "kick_source"
    if any(k in text for k in ("429", "rate limit", "quota")):
        return "quota"
    if "traceback" in text:
        return "unknown"
    return ""


def _index_new_clips(job_row: Job, namespace: str):
    """
    Scans this job's own output folder and inserts a Clip row for
    anything on disk that isn't indexed yet. This -- not a log line
    someone typed once -- is what "how many clips did this job
    produce" means from here on.

    Checks ownership GLOBALLY (every Clip row for this channel/mode,
    not just this job's own) before claiming a folder. Without this, a
    channel re-run after an old, never-indexed batch of clips (or after
    ANY earlier job for the same channel) would get every pre-existing
    clip re-attributed to it too -- which is exactly what happened the
    first time this shipped: two jobs that failed before ever
    downloading anything each showed 31 "kept" clips, because both
    found the same 31 already-real clip folders and neither knew the
    other (or history before either of them) had already claimed them.
    """
    channel = job_row.channel
    mode = job_row.kind  # "vod" or "live"
    base = Path(Config.OUTPUT_DIR) / namespace if namespace else Path(Config.OUTPUT_DIR)
    clips_dir = base / mode / channel / "clips"
    if not clips_dir.exists():
        return

    prefix = f"{namespace}/{mode}/{channel}/clips" if namespace else f"{mode}/{channel}/clips"
    claimed_anywhere = {
        c.subpath for c in Clip.query.filter(Clip.subpath.like(f"{prefix}/%")).all()
    }
    for folder in sorted(clips_dir.iterdir()):
        clip_file = folder / "clip.mp4"
        posts_file = folder / "posts.txt"
        score_file = folder / "score.json"
        subpath = f"{prefix}/{folder.name}"
        if subpath in claimed_anywhere or not clip_file.exists() or not posts_file.exists():
            continue
        score, title = 0.0, ""
        if score_file.exists():
            try:
                score_data = json.loads(score_file.read_text(encoding="utf-8"))
                score = float(score_data.get("score") or 0)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        try:
            first_line = next(
                (l for l in posts_file.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("===")),
                "",
            )
            title = first_line[:500]
        except OSError:
            pass
        db.session.add(Clip(job_id=job_row.id, subpath=subpath, score=score, title=title))
    db.session.commit()


def _finalize(db_job_id: int, crashed: bool, job_dict: dict):
    job_row = db.session.get(Job, db_job_id)
    if not job_row:
        return
    user = db.session.get(User, job_row.user_id)
    namespace = user_namespace(user)
    _index_new_clips(job_row, namespace)

    error_category = job_row.error_category or (
        _classify_error("\n".join(job_dict["log_lines"][-200:])) if crashed else ""
    )
    if not job_row.error_message and crashed:
        job_row.error_message = "\n".join(job_dict["log_lines"][-30:])

    capture_amount = None
    if job_row.kind == JobKind.LIVE.value:
        # Live has no known length up front, so its hold is a fixed
        # block sized above a typical session (see LIVE_DEFAULT_HOLD_
        # MINUTES) -- capture only the wall-clock minutes actually run,
        # not the whole block, or a 5-minute test session would get
        # charged for the full pre-authorized hour.
        # SQLite round-trips datetimes as naive (drops tzinfo), so
        # utcnow()'s timezone-aware value can't be subtracted from
        # job_row.created_at directly -- strip both to naive UTC.
        elapsed = (utcnow().replace(tzinfo=None) - job_row.created_at).total_seconds() / 60.0
        capture_amount = elapsed * job_row.credit_rate

    billing.finalize_job(job_row, crashed=crashed, error_category=error_category, capture_amount=capture_amount)
    try:
        from notifications import send_job_email
        final_job = db.session.get(Job, db_job_id)
        if final_job and final_job.user:
            send_job_email(Config, final_job.user.email, "Your Kick Clipper job is ready",
                           f"Job #{final_job.id} finished with status: {final_job.status}. Clips kept: {final_job.kept_clips}.")
    except Exception as exc:
        print(f"[notify] completion notification skipped: {type(exc).__name__}", flush=True)


def build_subprocess_env(user: User, channel, threshold, aspect_ratio, track_faces,
                          captions_enabled=True, priority_names=None, safety_flags=True, max_clips=0,
                          chunk_seconds=30, whisper_model=None) -> dict:
    """
    Builds this subprocess's own environment. Platform credentials
    (LLM_PROVIDER/LLM_API_KEY) ALWAYS come from this server process's
    own environment (Config), never from the request -- there is no
    parameter here that accepts a client-supplied key. OUTPUT_DIR is
    overridden per-user so customers physically cannot see each
    other's files.
    """
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if Config.DISABLE_ENV_PROXY and not Config.NETWORK_PROXY:
        for proxy_name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(proxy_name, None)
        env["NO_PROXY"] = "*"
    for name in dir(Config):
        if name.startswith((
            'TRANSCRIPTION_', 'AZURE_SPEECH_', 'MAI_', 'TOGETHER_', 'PARAKEET_',
            'ASR_', 'WHISPER_', 'SEMANTIC_', 'SCOUT_', 'DEEP_', 'VISUAL_VERIFY_',
            'MAX_RENDER_', 'MAX_TEMP_'
        )):
            # Explicitly forward cloud-ASR settings from the live Config object.
            # This matters after an owner changes Parakeet/Together settings in
            # Admin: Config is updated immediately, while os.environ may still
            # contain the values from process startup.
            env[name] = str(getattr(Config,name))
    env["KICK_CHANNEL"] = channel
    env["LLM_PROVIDER"] = Config.LLM_PROVIDER
    env["LLM_API_KEY"] = Config.LLM_API_KEY
    # Admin settings update Config immediately, but os.environ still contains
    # values inherited when Flask started. Explicitly replace both provider
    # keys so a worker cannot prefer a stale OPENAI_API_KEY/GROK_API_KEY over
    # the newly validated key.
    env["OPENAI_API_KEY"] = Config.OPENAI_API_KEY
    env["GROK_API_KEY"] = Config.GROK_API_KEY
    env["XAI_API_KEY"] = Config.XAI_API_KEY
    env["XAI_BASE_URL"] = Config.XAI_BASE_URL
    env["GROK_SCORING_MODEL"] = Config.GROK_SCORING_MODEL
    env["GROK_TITLE_MODEL"] = Config.GROK_TITLE_MODEL
    env["OPENAI_SCORING_MODEL"] = Config.OPENAI_SCORING_MODEL
    env["OPENAI_TITLE_MODEL"] = Config.OPENAI_TITLE_MODEL
    # Explicit role markers make subprocess logs/config auditable and prevent
    # accidental provider crossover during future refactors.
    env["KICKCLIPPER_TRANSCRIPTION_ROLE"] = Config.TRANSCRIPTION_PROVIDER
    env["KICKCLIPPER_ANALYSIS_ROLE"] = Config.LLM_PROVIDER
    ns = user_namespace(user)
    env["OUTPUT_DIR"] = str(Path(Config.OUTPUT_DIR) / ns) if ns else Config.OUTPUT_DIR
    env["CLIP_SCORE_THRESHOLD"] = str(threshold)
    env["ASPECT_RATIO"] = aspect_ratio
    env["TRACKING_ENABLED"] = "true" if track_faces else "false"
    env["CAPTIONS_ENABLED"] = "true" if captions_enabled else "false"
    env["SAFETY_FLAGS_ENABLED"] = "true" if safety_flags else "false"
    requested_max = max(0, int(max_clips or 0))
    if user.is_owner:
        effective_max = requested_max
    else:
        customer_cap = Config.CUSTOMER_MAX_CLIPS_PER_JOB
        effective_max = min(requested_max, customer_cap) if requested_max else customer_cap
    env["MAX_CLIPS"] = str(effective_max)
    env["CHUNK_SECONDS"] = str(chunk_seconds or 30)
    env["MAX_SOURCE_HOURS"] = str(0 if user.is_owner else Config.MAX_SOURCE_HOURS)
    if whisper_model:
        env["WHISPER_MODEL_SIZE"] = whisper_model
    if priority_names is not None:
        env["PRIORITY_NAMES"] = priority_names
    return env


# ---------- public marketing site ----------

@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/how-it-works")
def how_it_works_page():
    return render_template("how-it-works.html")


@app.route("/pricing")
def pricing_page():
    return render_template("pricing.html", stripe_enabled=Config.STRIPE_ENABLED)


@app.route("/faq")
def faq_page():
    return render_template("faq.html")


@app.route("/terms")
def terms_page():
    return render_template("terms.html")


@app.route("/privacy")
def privacy_page():
    return render_template("privacy.html")


# ---------- app ----------

@app.route("/dashboard")
@login_required
def index():
    return render_template("index.html", env=read_env(), is_owner=current_user.is_owner)


@app.route("/api/account/summary")
@login_required
def account_summary():
    summary = billing.account_summary(current_user.id)
    if demo_unlimited():
        summary.update(balance='∞', available='∞', held='0', unlimited=True)
    return jsonify(summary)


def _mask_key(key: str) -> str:
    if not key:
        return ""
    tail = key[-4:] if len(key) > 4 else key
    return f"{'•' * 12}{tail}"


def _verify_moment_key(provider: str, api_key: str) -> tuple[bool, str]:
    """Validate the provider-specific key without mixing provider/model roles."""
    import requests
    provider = "grok" if provider == "xai" else provider
    session = requests.Session()
    if Config.DISABLE_ENV_PROXY and not Config.NETWORK_PROXY:
        session.trust_env = False
    proxies = {"http": Config.NETWORK_PROXY, "https": Config.NETWORK_PROXY} if Config.NETWORK_PROXY else None
    model = (Config.OPENAI_SCORING_MODEL or "gpt-5.4-mini") if provider == "openai" else (Config.GROK_SCORING_MODEL or "grok-4.6")
    try:
        if provider == "openai":
            response = session.get(f"https://api.openai.com/v1/models/{model}", headers={"Authorization": f"Bearer {api_key}"}, timeout=30, proxies=proxies)
        else:
            response = session.post("https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 3},
                timeout=30, proxies=proxies)
    except requests.RequestException as exc:
        return False, f"Could not reach {provider}: {type(exc).__name__}. Check the connection and try again."
    if response.ok:
        return True, ""
    try:
        error = response.json().get("error") or {}
        message = error.get("message", "") if isinstance(error, dict) else str(error)
    except (ValueError, AttributeError):
        message = response.text or ""
    return False, f"{provider.title()} validation failed for '{model}' (HTTP {response.status_code}): {message[:300]}"


@app.route("/admin")
@owner_required
def admin_page():
    jobs = Job.query.order_by(Job.created_at.desc()).limit(200).all()
    return render_template(
        "admin.html", jobs=jobs,
        llm_provider=Config.LLM_PROVIDER,
        llm_key_masked=_mask_key(Config.LLM_API_KEY),
        llm_key_set=bool(Config.LLM_API_KEY),
        pipeline_settings=read_env(),
        transcription_controls=True,
    )


@app.route("/api/admin/platform_key", methods=["POST"])
@owner_required
def set_platform_key():
    """
    The ONE place a platform LLM API key can be set -- owner-only,
    never reachable from a customer session (owner_required enforces
    this same as every other admin route). Writes to .env so it
    survives a restart, and updates Config in memory immediately so it
    takes effect without one.
    """
    data = request.get_json(force=True)
    provider = data.get("provider", "grok").strip()
    api_key = data.get("api_key", "").strip()
    if not api_key:
        return jsonify({"ok": False, "error": "Enter an API key."})

    if provider not in ("grok", "xai", "openai"):
        return jsonify({"ok": False, "error": "Choose OpenAI or Grok/xAI."}), 400
    valid, validation_error = _verify_moment_key(provider, api_key)
    if not valid:
        return jsonify({"ok": False, "error": validation_error}), 400
    provider = "grok" if provider == "xai" else provider
    provider_key_name = "OPENAI_API_KEY" if provider == "openai" else "GROK_API_KEY"
    write_env_value(provider_key_name, api_key)
    setattr(Config, provider_key_name, api_key)
    if provider == "openai":
        # OpenAI GPT-5.4-mini is the primary scout/scoring provider.
        write_env_value("LLM_PROVIDER", "openai")
        write_env_value("LLM_API_KEY", api_key)
        write_env_value("OPENAI_SCORING_MODEL", "gpt-5.4-mini")
        write_env_value("OPENAI_TITLE_MODEL", "gpt-5.4-mini")
        Config.LLM_PROVIDER = "openai"
        Config.LLM_API_KEY = api_key
        Config.OPENAI_API_KEY = api_key
        Config.OPENAI_SCORING_MODEL = "gpt-5.4-mini"
        Config.OPENAI_TITLE_MODEL = "gpt-5.4-mini"
    else:
        # Grok 4.6 is independent judge/title provider; saving it must never
        # redirect GPT-5.4-mini scout/scoring calls to xAI.
        write_env_value("XAI_API_KEY", api_key)
        write_env_value("GROK_SCORING_MODEL", "grok-4.6")
        write_env_value("GROK_TITLE_MODEL", "grok-4.6")
        write_env_value("JUDGE_MODEL", "grok-4.6")
        write_env_value("JUDGE_ENABLED", "true")
        Config.GROK_API_KEY = api_key
        Config.XAI_API_KEY = api_key
        Config.GROK_SCORING_MODEL = "grok-4.6"
        Config.GROK_TITLE_MODEL = "grok-4.6"
        Config.JUDGE_MODEL = "grok-4.6"
        Config.JUDGE_ENABLED = True
    return jsonify({"ok": True, "masked": _mask_key(api_key)})


@app.route('/api/admin/transcription', methods=['POST'])
@owner_required
def save_transcription_provider():
    from dotenv import set_key
    from types import SimpleNamespace
    data = request.get_json(force=True)
    try:
        provider = str(data.get('provider','whisper')).strip()
        if provider not in ('whisper','mai','parakeet'):
            raise ValueError('Choose Whisper, MAI, or Parakeet')
        endpoint = str(data.get('endpoint','')).strip().rstrip('/')
        key = str(data.get('key','')).strip() or Config.AZURE_SPEECH_KEY
        concurrency = int(data.get('concurrency',4))
        if not 1 <= concurrency <= 16 or '\n' in key or '\r' in key:
            raise ValueError('Use 1–16 concurrent requests and a single-line key')
        together_key = str(data.get('together_key','')).strip() or Config.TOGETHER_API_KEY
        if '\n' in together_key or '\r' in together_key:
            raise ValueError('Use a single-line Together API key')
        values = dict(TRANSCRIPTION_PROVIDER=provider,AZURE_SPEECH_ENDPOINT=endpoint,
                      AZURE_SPEECH_KEY=key,TOGETHER_API_KEY=together_key,MAI_CONCURRENCY=concurrency,
                      PARAKEET_CONCURRENCY=concurrency,ASR_MAX_CONCURRENCY=concurrency)
        if provider == 'mai':
            from mai_transcribe import MaiTranscriber
            settings = {n:getattr(Config,n) for n in dir(Config) if n.startswith(('MAI_','AZURE_'))}
            MaiTranscriber(SimpleNamespace(**{**settings,**values,'PRIORITY_NAMES':Config.PRIORITY_NAMES}))
        elif provider == 'parakeet':
            from parakeet_transcribe import ParakeetTranscriber
            settings = {n:getattr(Config,n) for n in dir(Config) if n.startswith(('PARAKEET_','TOGETHER_','ASR_'))}
            candidate = ParakeetTranscriber(SimpleNamespace(**{**settings,**values}))
            candidate.probe()
        for name, value in values.items():
            set_key(str(ENV_PATH),name,str(value))
            setattr(Config,name,value)
        return jsonify(ok=True)
    except (ValueError,TypeError,RuntimeError) as exc:
        return jsonify(ok=False,error=str(exc)),400


# ---------- Live streamer jobs (multi-streamer) ----------

@app.route("/api/live/start", methods=["POST"])
@login_required
def live_start():
    data = request.get_json(force=True)
    raw_source = (data.get("source") or data.get("channel") or "").strip()
    try:
        live_platform, channel, live_source = normalize_live_source(raw_source)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    if not Config.LLM_API_KEY:
        transcription_name = "Together Parakeet" if Config.TRANSCRIPTION_PROVIDER == "parakeet" else Config.TRANSCRIPTION_PROVIDER.upper()
        return jsonify({
            "ok": False,
            "error": (
                f"{transcription_name} transcription is selected, but clip finding also needs a separate Moment AI key. "
                "Owner: open Admin → Moment analysis AI and add a Grok/xAI or OpenAI API key. "
                "Parakeet handles transcription; the Moment AI reads that transcript and finds/scores the clips."
            ),
        })

    account = billing.get_or_create_account(current_user.id)
    if account.balance < MIN_BALANCE_TO_START and not demo_unlimited():
        return jsonify({"ok": False, "error": "Not enough credits to start a job. Top up on the Pricing page."})

    with jobs_lock:
        existing = live_jobs.get((current_user.id, channel))
        if existing and existing["process"].poll() is None:
            return jsonify({"ok": False, "error": f"{channel} is already running."})

        env = build_subprocess_env(
            current_user, channel, data.get("threshold", 60),
            data.get("aspect_ratio", "9:16"), data.get("track_faces", True),
            captions_enabled=data.get("captions_enabled", True),
            priority_names=data.get("priority_names"),
            safety_flags=data.get("safety_flags", True),
            max_clips=data.get("max_clips", 0),
            chunk_seconds=data.get("chunk_seconds", 30),
            whisper_model=data.get("whisper_model"),
        )
        env["LIVE_SOURCE"] = live_source
        env["LIVE_PLATFORM"] = live_platform

        job_row = Job(
            user_id=current_user.id, kind=JobKind.LIVE.value, channel=channel,
            input_raw=live_source, preset_json=json.dumps({**data, "platform": live_platform, "source": live_source}), credit_rate=0 if demo_unlimited() else Config.CREDIT_RATE_LIVE,
            status=JobStatus.RUNNING.value, stage="starting",
        )
        db.session.add(job_row)
        db.session.commit()

        try:
            billing.estimate_and_hold(job_row, LIVE_DEFAULT_HOLD_MINUTES)
        except billing.InsufficientCredits as e:
            job_row.status = JobStatus.FAILED.value
            job_row.error_category = "insufficient_credits"
            db.session.commit()
            return jsonify({"ok": False, "error": f"Need {e.needed:.0f} credits to start a live session, you have {e.available:.0f}."})

        proc = subprocess.Popen(
            [sys.executable, "main.py"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
        )
        job = {"process": proc, "log_lines": [], "progress": None, "job_id": job_row.id}
        live_jobs[(current_user.id, channel)] = job
        threading.Thread(
            target=stream_output, args=(proc, job, job_row.id, app.app_context()),
            daemon=True,
        ).start()

    return jsonify({"ok": True})


@app.route("/api/live/stop", methods=["POST"])
@login_required
def live_stop():
    channel = request.get_json(force=True).get("channel", "").strip()
    with jobs_lock:
        job = live_jobs.get((current_user.id, channel))
        if job and job["process"].poll() is None:
            job["user_stopped"] = True
            job["process"].terminate()
            return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Not running."})


@app.route("/api/live/remove", methods=["POST"])
@login_required
def live_remove():
    channel = request.get_json(force=True).get("channel", "").strip()
    with jobs_lock:
        job = live_jobs.pop((current_user.id, channel), None)
        if job and job["process"].poll() is None:
            job["user_stopped"] = True
            if job.get('cancel_file'):
                Path(job['cancel_file']).touch()
            else:
                job["process"].terminate()
    return jsonify({"ok": True})


@app.route("/api/live/status")
@login_required
def live_status():
    with jobs_lock:
        result = {}
        for (uid, channel), job in live_jobs.items():
            if uid != current_user.id:
                continue
            running = job["process"].poll() is None
            result[channel] = {"running": running, "log": job["log_lines"][-80:] if current_user.is_owner else [], "guardian": job.get("guardian", Guardian()).snapshot(running) if current_user.is_owner else None, "progress": job.get("progress")}
    return jsonify(result)


@app.route("/api/live/clips")
@login_required
def live_clips():
    channel = request.args.get("channel", "")
    ns = user_namespace(current_user)
    base = user_output_dir(current_user)
    prefix = f"{ns}/live/{channel}/clips" if ns else f"live/{channel}/clips"
    return jsonify(_list_clips(base / "live" / channel / "clips", prefix))


# ---------- Uploads ----------

@app.route("/api/upload", methods=["POST"])
@login_required
def upload_video():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(ok=False, error="Choose a video file."), 400
    from werkzeug.utils import secure_filename
    from uuid import uuid4
    name = secure_filename(file.filename)
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_UPLOAD_EXTENSIONS:
        return jsonify(ok=False, error="Supported uploads: MP4, MOV, MKV, WebM, M4V and TS."), 400
    max_bytes = int(Config.MAX_UPLOAD_GB * 1024**3)
    if request.content_length and request.content_length > max_bytes:
        return jsonify(ok=False, error=f"Upload exceeds the {Config.MAX_UPLOAD_GB:g} GB launch limit."), 413
    folder = user_output_dir(current_user) / "_uploads"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{uuid4().hex}{suffix}"
    file.save(target)
    if target.stat().st_size > max_bytes:
        target.unlink(missing_ok=True)
        return jsonify(ok=False, error=f"Upload exceeds the {Config.MAX_UPLOAD_GB:g} GB launch limit."), 413
    return jsonify(ok=True, input=str(target.resolve()), display_name=name)


# ---------- VOD jobs ----------

@app.route("/api/vod/start", methods=["POST"])
@login_required
def vod_start():
    data = request.get_json(force=True)
    vod_input = data.get("input", "").strip()
    stream_url = data.get("stream_url", "") if current_user.is_owner else ""  # raw HLS: owner-only debug path
    channel = data.get("channel", "").strip()

    import re
    if channel and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", channel):
        return jsonify(ok=False, error="Use letters, numbers, underscores or hyphens for the channel."), 400
    if not channel or (not vod_input and not stream_url):
        return jsonify({"ok": False, "error": "Enter a channel/project name and a Kick, Twitch or YouTube URL, or upload a video."})
    if vod_input and not current_user.is_owner:
        try:
            vod_input = validate_customer_vod_input(vod_input, user_output_dir(current_user) / "_uploads")
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
    if not Config.LLM_API_KEY:
        transcription_name = "Together Parakeet" if Config.TRANSCRIPTION_PROVIDER == "parakeet" else Config.TRANSCRIPTION_PROVIDER.upper()
        return jsonify({
            "ok": False,
            "error": (
                f"{transcription_name} transcription is selected, but clip finding also needs a separate Moment AI key. "
                "Owner: open Admin → Moment analysis AI and add a Grok/xAI or OpenAI API key. "
                "Parakeet handles transcription; the Moment AI reads that transcript and finds/scores the clips."
            ),
        })

    account = billing.get_or_create_account(current_user.id)
    if account.balance < MIN_BALANCE_TO_START and not demo_unlimited():
        return jsonify({"ok": False, "error": "Not enough credits to start a job. Top up on the Pricing page."})

    with jobs_lock:
        running = _running_vod_jobs_for(current_user.id)
        if any(job["channel"] == channel for _, job in running):
            return jsonify({"ok": False, "error": f"A VOD job for {channel} is already running -- wait for it to finish before starting another for the same channel (they'd overwrite each other's work files)."})
        if len(running) >= MAX_CONCURRENT_VOD_JOBS:
            return jsonify({"ok": False, "error": f"This local worker has reached its safety cap of {MAX_CONCURRENT_VOD_JOBS} simultaneous VOD jobs. In production these jobs should queue onto cloud workers instead of starting more local processes."}), 429
        overload_warning = None
        if len(running) >= Config.SOFT_CONCURRENT_JOB_WARNING:
            overload_warning = f"You already have {len(running)} jobs running. This job can start, but heavy simultaneous processing can increase completion time until cloud autoscaling is enabled."

    free_gb = _free_disk_gb()
    if free_gb < MIN_FREE_DISK_GB_TO_START:
        return jsonify({"ok": False, "error": f"Only {free_gb:.1f}GB free on disk -- need at least {MIN_FREE_DISK_GB_TO_START}GB to safely start a VOD download. Free up space first."})

    env = build_subprocess_env(
        current_user, channel, data.get("threshold", 60),
        data.get("aspect_ratio", "9:16"), data.get("track_faces", True),
        captions_enabled=data.get("captions_enabled", True),
        priority_names=data.get("priority_names"),
        safety_flags=data.get("safety_flags", True),
        max_clips=data.get("max_clips", 0),
        chunk_seconds=data.get("chunk_seconds", 30),
        whisper_model=data.get("whisper_model"),
    )

    global _vod_job_counter
    with jobs_lock:
        _vod_job_counter += 1
        job_key = f"{current_user.id}_{channel}_{_vod_job_counter}"

    range_args = []
    start_time = data.get("start_time")
    end_time = data.get("end_time")
    if start_time:
        range_args += ["--start", str(start_time)]
    if end_time:
        range_args += ["--end", str(end_time)]

    the_input = stream_url or vod_input
    cmd = [sys.executable, "vod.py", "--input", the_input, "--channel", channel] + range_args
    if data.get("resume_transcript"):
        # Explicit recovery only: never silently reuse another VOD's transcript.
        previous = Job.query.filter_by(user_id=current_user.id, channel=channel, input_raw=the_input).order_by(Job.id.desc()).first()
        saved = user_output_dir(current_user) / "vod" / channel / "transcript.json"
        if not current_user.is_owner or not previous or not saved.is_file():
            return jsonify(ok=False, error="No matching saved VOD transcript available for recovery."), 400
        old = json.loads(previous.preset_json or "{}")
        if any(old.get(k) != data.get(k) for k in ("start_time", "end_time")):
            return jsonify(ok=False, error="Recovery must use the original time range."), 400
        cmd += ["--transcript", str(saved.resolve())]
        if data.get("resume_checkpoint"):
            checkpoint = saved.parent / "found_moments_checkpoint.json"
            if not checkpoint.is_file():
                return jsonify(ok=False, error="No saved moments checkpoint available."), 400
            cmd += ["--checkpoint", str(checkpoint.resolve())]

    job_row = Job(
        user_id=current_user.id, kind=JobKind.VOD.value, channel=channel,
        input_raw=the_input, preset_json=json.dumps(data), credit_rate=0 if demo_unlimited() else Config.CREDIT_RATE_VOD,
        status=JobStatus.QUEUED.value, stage="resolving_source",
    )
    db.session.add(job_row)
    db.session.commit()

    cancel_file = user_output_dir(current_user) / '.cancel' / f'{job_row.id}.flag'
    cancel_file.parent.mkdir(parents=True,exist_ok=True)
    env['JOB_CANCEL_FILE'] = str(cancel_file.resolve())
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
    )
    job = {"process": proc, "log_lines": [], "channel": channel, "progress": None, "job_id": job_row.id, "cancel_file": str(cancel_file)}
    with jobs_lock:
        vod_jobs[job_key] = job
    threading.Thread(
        target=stream_output, args=(proc, job, job_row.id, app.app_context()),
        daemon=True,
    ).start()

    return jsonify({"ok": True, "job_id": job_key, "warning": overload_warning})


@app.route("/api/vod/remove", methods=["POST"])
@login_required
def vod_remove():
    job_id = request.get_json(force=True).get("job_id", "").strip()
    if not job_id.startswith(f"{current_user.id}_"):
        return jsonify({"ok": False, "error": "Not your job."}), 403
    with jobs_lock:
        job = vod_jobs.get(job_id)
        if job and job["process"].poll() is not None:
            vod_jobs.pop(job_id, None)
        if job and job["process"].poll() is None:
            job["user_stopped"] = True
            if job.get('cancel_file'):
                Path(job['cancel_file']).touch()
            else:
                job["process"].terminate()
    return jsonify({"ok": True})


@app.route("/api/vod/status")
@login_required
def vod_status():
    with jobs_lock:
        result = {}
        for job_id, job in vod_jobs.items():
            if not job_id.startswith(f"{current_user.id}_"):
                continue
            running = job["process"].poll() is None
            result[job_id] = {
                "channel": job["channel"], "running": running,
                "log": job["log_lines"][-80:] if current_user.is_owner else [], "guardian": job.get("guardian", Guardian()).snapshot(running) if current_user.is_owner else None, "progress": job.get("progress"),
            }
    return jsonify(result)


@app.route("/api/vod/clips")
@login_required
def vod_clips():
    channel = request.args.get("channel", "")
    ns = user_namespace(current_user)
    base = user_output_dir(current_user)
    prefix = f"{ns}/vod/{channel}/clips" if ns else f"vod/{channel}/clips"
    clips = _list_clips(base / "vod" / channel / "clips", prefix)
    prefix_s = f"{ns}/vod_stream/{channel}/clips" if ns else f"vod_stream/{channel}/clips"
    clips += _list_clips(base / "vod_stream" / channel / "clips", prefix_s)
    return jsonify(clips)


@app.route("/api/library/clips")
@login_required
def library_clips():
    """
    Every clip this user has ever produced. Scoped by DB Job ownership
    (via user_output_dir's per-user folder) rather than a global
    filesystem scan -- customers can only ever see their own subtree.
    The owner keeps the original unscoped view of everything under
    output/ (all their pre-SaaS work, still there, untouched).
    """
    base = user_output_dir(current_user)
    ns = user_namespace(current_user)
    results = []

    for mode, prefix in (("live", "live"), ("vod", "vod"), ("vod_stream", "vod_stream")):
        mode_dir = base / mode
        if not mode_dir.exists():
            continue
        for channel_dir in mode_dir.iterdir():
            if not channel_dir.is_dir():
                continue
            clips_dir = channel_dir / "clips"
            channel = channel_dir.name
            url_prefix = f"{ns}/{prefix}/{channel}/clips" if ns else f"{prefix}/{channel}/clips"
            for clip in _list_clips(clips_dir, url_prefix):
                clip["channel"] = channel
                clip["mode"] = mode
                results.append(clip)

    results.sort(key=lambda c: c["id"], reverse=True)
    return jsonify(results)


# ---------- shared helpers ----------

def _read_optional_json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _list_clips(clips_dir: Path, url_prefix: str):
    results = []
    if clips_dir.exists():
        for folder in sorted(clips_dir.iterdir(), reverse=True):
            clip_file = folder / "clip.mp4"
            posts_file = folder / "posts.txt"
            score_file = folder / "score.json"
            notes_file = folder / "notes.txt"
            if clip_file.exists() and clip_file.stat().st_size > 0:
                score = None
                if score_file.exists():
                    try:
                        score = json.loads(score_file.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        score = None
                results.append({
                    "id": folder.name,
                    "visual_review": _read_optional_json(folder / "visual_review.json"),
                    "editable": (folder / "recipe.json").exists() and (folder / "words.json").exists(),
                    "subpath": f"{url_prefix}/{folder.name}",
                    "video_url": f"/media/{url_prefix}/{folder.name}/clip.mp4",
                    "posts": posts_file.read_text(encoding="utf-8") if posts_file.exists() else "Titles unavailable. The video is still ready to review.",
                    "score": score,
                    "note": notes_file.read_text(encoding="utf-8") if notes_file.exists() else "",
                })
    return results


@app.route("/api/clip/note", methods=["POST"])
@login_required
def save_clip_note():
    data = request.get_json(force=True)
    subpath = data.get("subpath", "").strip()
    note = data.get("note", "")
    if not subpath or not _owns_subpath(current_user, subpath):
        return jsonify({"ok": False, "error": "Invalid clip reference."}), 403

    target_dir = _resolve_clip_dir(subpath)
    if not target_dir:
        return jsonify({"ok": False, "error": "Clip folder not found."})

    (target_dir / "notes.txt").write_text(note, encoding="utf-8")
    return jsonify({"ok": True})


def _resolve_clip_dir(subpath: str) -> Path | None:
    """Shared safety check: resolves a clip's subpath to a real directory
    inside OUTPUT_DIR, refusing anything that would escape it."""
    target_dir = (Path(Config.OUTPUT_DIR) / subpath).resolve()
    output_root = Path(Config.OUTPUT_DIR).resolve()
    if output_root not in target_dir.parents and target_dir != output_root:
        return None
    if not target_dir.is_dir() or target_dir.parent.name != "clips":
        return None
    return target_dir


@app.route("/api/clip/update", methods=["POST"])
@login_required
@serialized_edit
def update_clip():
    from transcribe import Word
    from clip import re_render_from_recipe, snap_to_words

    data = request.get_json(force=True)
    subpath = data.get("subpath", "").strip()
    if not subpath or not _owns_subpath(current_user, subpath):
        return jsonify({"ok": False, "error": "Invalid clip reference."}), 403
    clip_dir = _resolve_clip_dir(subpath)
    if not clip_dir:
        return jsonify({"ok": False, "error": "Clip folder not found."})

    recipe_path = clip_dir / "recipe.json"
    words_path = clip_dir / "words.json"
    if not recipe_path.exists() or not words_path.exists():
        return jsonify({"ok": False, "error": "This clip has no saved recipe (made before the editor was added?)."})

    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    words_raw = json.loads(words_path.read_text(encoding="utf-8"))
    words = [Word(text=w["text"], start=w["start"], end=w["end"]) for w in words_raw]

    for field in ("start", "end", "aspect", "track", "track_face_id", "layout",
                  "captions_enabled", "caption_style", "overlay_text", "overlay_seconds", "zoom"):
        if field in data:
            recipe[field] = data[field]

    try:
        validate_recipe(recipe)
        source = Path(recipe['source_video'])
        if not source.is_file():
            return jsonify(ok=False, error="The original source for this clip is missing. Restore it before re-rendering."), 400
        probe = subprocess.check_output([
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=nw=1:nk=1', str(source),
        ], text=True, timeout=15)
        source_duration = float(probe.strip())
        if recipe['start'] >= source_duration or recipe['end'] > source_duration + .1:
            return jsonify(ok=False, error=f"Trim must stay within the original source ({source_duration:.1f} seconds)."), 400
    except (ValueError, TypeError) as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except (KeyError, OSError, subprocess.SubprocessError):
        return jsonify(ok=False, error="Could not read the saved source video. Check its path and FFmpeg installation."), 400

    snapped_start, snapped_end = snap_to_words(
        words, float(recipe["start"]), float(recipe["end"]), pad_pre=0.0, pad_post=0.0,
    )
    recipe["start"], recipe["end"] = snapped_start, snapped_end

    out_path = clip_dir / "clip.mp4"
    try:
        temporary = clip_dir / "clip.pending.mp4"
        re_render_from_recipe(recipe, words, temporary)
        probe = subprocess.check_output([
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height', '-of', 'json', str(temporary),
        ], text=True, timeout=15)
        if not json.loads(probe).get('streams'):
            raise ValueError('The render contained no video frames; the previous clip has been preserved.')
        temporary.replace(out_path)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Re-render failed: {e}"})

    recipe_path.write_text(json.dumps(recipe, indent=2), encoding="utf-8")

    return jsonify({
        "ok": True,
        "video_url": f"/media/{subpath}/clip.mp4",
        "recipe": recipe,
    })


@app.route("/api/clip/regenerate_posts", methods=["POST"])
@login_required
def regenerate_clip_posts():
    from transcribe import Word
    from generate_posts import generate_posts, write_posts_file

    data = request.get_json(force=True)
    subpath = data.get("subpath", "").strip()
    if not subpath or not _owns_subpath(current_user, subpath):
        return jsonify({"ok": False, "error": "Invalid clip reference."}), 403
    clip_dir = _resolve_clip_dir(subpath)
    if not clip_dir:
        return jsonify({"ok": False, "error": "Clip folder not found."})

    recipe_path = clip_dir / "recipe.json"
    words_path = clip_dir / "words.json"
    if not recipe_path.exists() or not words_path.exists():
        return jsonify({"ok": False, "error": "This clip has no saved recipe."})

    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    words_raw = json.loads(words_path.read_text(encoding="utf-8"))
    words = [Word(text=w["text"], start=w["start"], end=w["end"]) for w in words_raw]

    clip_words = [w for w in words if recipe["start"] <= w.start <= recipe["end"]]
    transcript_text = " ".join(w.text.strip() for w in clip_words)

    # No live Candidate object here (this is a manual re-run against an
    # already-cut clip) -- recover the same standalone score / safety_flag
    # the original job computed from the saved score.json instead, so a
    # regenerate gets the same long-form/tone-override behavior as the
    # original generation did.
    standalone_score = None
    safety_flag = None
    score_path = clip_dir / "score.json"
    if score_path.exists():
        try:
            score_data = json.loads(score_path.read_text(encoding="utf-8"))
            standalone_score = (score_data.get("breakdown") or {}).get("standalone")
            if Config.SAFETY_FLAGS_ENABLED:
                safety_flag = score_data.get("safety_flag")
        except Exception:
            pass

    posts = generate_posts(transcript_text, standalone_score=standalone_score, safety_flag=safety_flag)
    write_posts_file(posts, clip_dir / "posts.txt")
    return jsonify({"ok": True, "posts": (clip_dir / "posts.txt").read_text(encoding="utf-8")})


@app.route("/media/<path:subpath>/clip.mp4")
@login_required
def serve_clip(subpath):
    if not _owns_subpath(current_user, subpath):
        return "Forbidden", 403
    full_dir = _resolve_clip_dir(subpath)
    if full_dir is None:
        return "Not found", 404
    return send_from_directory(full_dir, "clip.mp4")




@app.route('/api/admin/pipeline', methods=['POST'])
@owner_required
def set_pipeline():
    data=request.get_json() or {}
    try:
        batch=int(data.get('batch_size',1));beam=int(data.get('beam_size',1))
        if not 1 <= batch <= 32 or not 1 <= beam <= 5:
            raise ValueError('Batch must be 1–32 and beam must be 1–5.')
        model=str(data.get('vision_model','')).strip()
        if len(model)>100 or any(c in model for c in '\r\n'):
            raise ValueError('Invalid vision model.')
        limit=int(data.get('review_limit',10))
        if not 0 <= limit <= 20: raise ValueError('Review limit must be 0–20.')
        provider=str(data.get('video_provider','frames'))
        if provider not in ('frames','gemini'): raise ValueError('Invalid video review provider.')
        if provider == 'gemini' and not os.getenv('GEMINI_API_KEY'): raise ValueError('Add GEMINI_API_KEY to the server .env and restart before selecting Gemini.')
        values={'VIDEO_REVIEW_PROVIDER':provider,'CONTEXT_REVIEW_LIMIT':str(limit),'WHISPER_BATCH_SIZE':str(batch),'WHISPER_BEAM_SIZE':str(beam),'WHISPER_VOD_BEAM_SIZE':str(beam),'WHISPER_LIVE_BEAM_SIZE':str(beam),'VISION_MODEL':model}
        for key,value in values.items():
            write_env_value(key,value);os.environ[key]=value
        return jsonify(ok=True)
    except (TypeError,ValueError) as exc:
        return jsonify(ok=False,error=str(exc)),400

@app.route("/api/clip/recipe", methods=["POST"])
@login_required
def clip_recipe():
    subpath = (request.get_json() or {}).get("subpath", "")
    folder = _resolve_clip_dir(subpath) if _owns_subpath(current_user, subpath) else None
    if folder is None or not (folder / "recipe.json").exists():
        return jsonify(ok=False, error="No editable recipe for this clip."), 404
    recipe = json.loads((folder / "recipe.json").read_text(encoding="utf-8"))
    return jsonify(ok=True, recipe={k: v for k, v in recipe.items() if k not in ('source_video','words_path')})

@app.route("/api/clip/delete", methods=["POST"])
@login_required
@serialized_edit
def delete_clip():
    import uuid
    subpath = (request.get_json() or {}).get("subpath", "")
    folder = _resolve_clip_dir(subpath) if _owns_subpath(current_user, subpath) else None
    if folder is None:
        return jsonify(ok=False, error="Clip not found."), 404
    # Reversible removal, outside the library scan. Never delete original media.
    trash = user_output_dir(current_user) / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    target = trash / (uuid.uuid4().hex + "_" + folder.name)
    folder.rename(target)
    try:
        Clip.query.filter_by(subpath=subpath).delete()
        db.session.commit()
    except Exception:
        db.session.rollback()
        target.rename(folder)
        raise
    return jsonify(ok=True)


@app.route("/api/clips/delete-all", methods=["POST"])
@login_required
@serialized_edit
def delete_all_clips():
    if (request.get_json(silent=True) or {}).get('confirmation') != 'DELETE ALL':
        return jsonify(ok=False, error='Confirm deletion of all clips.'), 400
    with jobs_lock:
        if _running_vod_jobs_for(current_user.id) or any(uid == current_user.id and job['process'].poll() is None for (uid, _), job in live_jobs.items()):
            return jsonify(ok=False, error='Stop your processing jobs before deleting all clips.'), 409
    from clip_trash import move_library_to_trash
    def commit(folders):
        try:
            for folder in folders:
                subpath = folder.relative_to(Path(Config.OUTPUT_DIR).resolve()).as_posix()
                Clip.query.filter_by(subpath=subpath).delete()
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
    count = move_library_to_trash(user_output_dir(current_user), commit)
    return jsonify(ok=True, deleted=count)


if __name__ == "__main__":
    # Use a production-grade threaded WSGI server locally. VOD/Live work already
    # runs in child processes; these threads keep status/UI requests from
    # serialising behind each other without multiplying heavy workers.
    from waitress import serve
    serve(app, host="127.0.0.1", port=int(os.getenv("PORT", "5000")),
          threads=max(4, int(os.getenv("HTTP_THREADS", "8"))))

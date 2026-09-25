# Current upgrade (September 10, 2026)

Read UPGRADE-NOTES.md for the current implementation. The historical notes below describe earlier revisions; scoring_policy.py is now the active cross-platform rubric. guardian.py monitors processing logs. Local ASR 1.2.1 is bundled in vendor_python, with provenance and upstream licensing. Three.js is bundled locally.

# AGENTS.md — Project Context for kick-clipper

Read this first before making changes. It saves re-explaining the project
from scratch every time.

## What this is

A personal, locally-run tool that watches Kick/YouTube streams and VODs,
transcribes them, scores moments for clip-worthiness with an LLM, cuts
the good ones (captioned, cropped/tracked, audio-normalized), and writes
platform-specific titles. Runs on one person's native Windows machine via
a Flask web app at localhost:5000. Not a hosted service, not multi-tenant.

## Tech stack

- Python 3.12 (via `venv` at `venv/`, native Windows — NOT conda, NOT WSL. See "The user's setup" below.)
- Flask (`app.py`) — the web UI and job orchestration
- `faster-whisper` (needs `ctranslate2`, which needs the MSVC redistributable installed on Windows) — local transcription, no API cost
- Grok (`grok-4.5`) or OpenAI (`gpt-4o-mini`) — scoring + title generation
- `ffmpeg` — all video/audio work (capture, cutting, cropping, captions)
- `playwright` (chromium) — browser-automation fallback for Kick VOD downloads; needs `playwright install chromium` run once, separately from `pip install`
- OpenCV (DNN detector, falls back to Haar cascade) — face tracking
- Vanilla JS + HTML/CSS, no frontend framework — `templates/index.html` (the working dashboard, at `/dashboard`) and `templates/landing.html` (marketing/demo page, at `/` — same design tokens as the dashboard for brand continuity)

## Key files

- `main.py` — live streamer pipeline (streamlink → ffmpeg chunks → transcribe → score → clip)
- `vod.py` — full-download VOD pipeline (download whole file → transcribe once → scan windows → clip)
- `vod_stream.py` — pipelined VOD pipeline (direct `.m3u8` URL → ffmpeg chunks as they arrive → same per-chunk flow as live)
- `capture.py` — all ffmpeg/streamlink capture logic
- `transcribe.py` — faster-whisper wrapper, with re-encode fallback for corrupted chunks
- `detect.py` — LLM scoring (6-category weighted breakdown), retry logic on transient API errors
- `clip.py` — cutting, captioning (karaoke ASS), layout/tracking, audio normalization
- `track.py` — face detection + smoothed crop tracking
- `generate_posts.py` — title/post generation matching the user's documented style system
- `app.py` — Flask routes, job management, the web UI backend
- `config.py` — all settings, read from `.env` (never commit this file — it holds the API key)
- `templates/landing.html` — marketing/demo page at `/` (the front door now), describes the real pipeline (not fabricated features) and links into the real dashboard at `/dashboard`

## Conventions worth knowing

- Every clip folder gets `clip.mp4`, `posts.txt`, `score.json`, and optionally `notes.txt`
- Output is namespaced: `output/{live,vod,vod_stream}/<channel>/clips/<clip_id>/`
- Raw capture chunks are `.ts` (MPEG-TS), NOT `.mp4` — this was a deliberate fix; `.mp4` segmenting requires an AAC bitstream filter conversion at every chunk boundary that's a known source of audio corruption
- Any LLM call (`detect.py`'s `_call_llm`) retries on timeout/connection errors, but NOT on real API errors (bad key, billing limit) since those fail identically every time
- A failed transcription chunk gets a re-encode-then-retry attempt before being treated as genuinely empty — never silently dropped on the first failure
- URLs for serving clips MUST include the `/clips/` path segment (physical folder structure is `.../channel/clips/clip_id/`, not `.../channel/clip_id/`) — this was a real bug once, easy to reintroduce
- Job subprocesses (`main.py`/`vod.py`) MUST be launched with `sys.executable`, never a literal `"python"` string — see session log below, this was a real bug that silently ran jobs under the wrong interpreter
- Never run a second `python app.py` in a separate terminal while one is already up. Windows will silently let both bind port 5000 and requests land on whichever one at random — looks exactly like flaky, unrelated bugs
- `vod.py`'s whole-VOD ranking pass writes `found_moments_checkpoint.json` before doing anything risky, and is wrapped in try/except — a bug there degrades to "export everything that already passed" instead of losing a multi-hour job's results

## Known upstream issues (not our bugs)

- Kick's own VOD metadata API is broken as of mid-2026 — breaks `yt-dlp` and `streamlink` for Kick VODs specifically. Workarounds in `vod.py`: browser automation → Apify (paid) → yt-dlp (last resort). YouTube is unaffected, routes straight to yt-dlp.
- xAI retired the `grok-4` model slug in May 2026 — current model string is `grok-4.5`.

## Deliberately not built

- Automated posting to social accounts (ban risk against platform ToS)
- Celebrity/streamer facial recognition identity database (different scale/sensitivity of project — would need real biometric infrastructure)
- Full asyncio rearchitecture, SQLite crash-resume, watchdog supervisor (disproportionate complexity for a single-user local tool)

## The user's setup

Native Windows + PowerShell — NOT WSL, NOT conda anymore. The project
lives at `C:\Users\james\kick-clipper` (a short path, deliberately — see
session log) with its own `venv/` at `venv/`. Originally the user ran
this via WSL2 + Miniconda; that's now history, not the current setup.
ffmpeg, Python 3.12, the MSVC redistributable, and Playwright's chromium
were all missing from this machine and had to be installed from scratch
— if a "command not found" or DLL-load error shows up again, check
whether it's actually still missing before assuming it's already solved.

## Recent session log (carry this context forward)

- **Fixed: `TypeError: unhashable type: 'Candidate'` in `vod.py`.**
  `vod.py`'s whole-VOD ranking pass (around the `rank_top_moments` call)
  puts `Candidate` objects into sets (`{merged[i] for i in
  chosen_indices}`, `locked = {m for m in merged if ...}`, then a set
  union and `m not in locked`). `Candidate` in `moments.py` is a plain
  `@dataclass`, which auto-generates `__eq__` but sets `__hash__` to
  `None` — that's what broke it. Couldn't make it `frozen=True` instead
  since it has a `breakdown: dict` field (dicts aren't hashable either).
  Fix: added `__hash__ = object.__hash__` directly on `Candidate` in
  `moments.py`, restoring identity-based hashing. That's the correct
  semantics here — the ranking pass is tracking "which specific
  instances," not comparing values — not just a workaround.
- **Redesigned `templates/index.html`** (the Flask dashboard UI) to a
  colorful, higher-contrast theme, based on a moodboard image the user
  shared. Kept every class name, id, and the JS logic completely
  untouched — this was a CSS/typography-only pass (new color tokens,
  Space Grotesk + Inter fonts, a lime/cyan/violet/pink spectrum accent,
  visible focus states, `prefers-reduced-motion` support). Safe to keep
  iterating on the visual design without needing to touch `app.py` or
  any of the JS.
- User is moving from copy-pasting code/errors into chat to running
  Claude Code directly in this project folder (native Windows
  PowerShell, not WSL, per their preference) — so from here on, prefer
  actually running `vod.py`/`main.py` and reading tracebacks directly
  over asking the user to paste logs.
- **Machine-level setup, done from scratch this session:** C: drive was
  at 0 bytes free (a 188GB WSL virtual disk that had never been
  compacted — `diskpart compact vdisk` after `wsl --shutdown` got it to
  8GB). Installed Python 3.12 (`py install 3.12` — only 3.14 existed),
  ffmpeg (`winget install Gyan.FFmpeg`), the MSVC redistributable
  (`winget install Microsoft.VCRedist.2015+.x64` — `ctranslate2`/
  faster-whisper can't load without it, fails with a misleading
  "DLL not found" for a file that does exist), and Playwright's
  chromium (`playwright install chromium` — separate from `pip install
  playwright`, easy to forget).
- **Moved the project from a deeply-nested temp/scratch path to
  `C:\Users\james\kick-clipper`.** Windows' 260-char MAX_PATH broke
  compiled Python extensions when run from the original path —
  `streamlink` crashed with `charset_normalizer`'s `.pyd` failing
  "filename or extension is too long," which looked like a version
  conflict but wasn't. Always develop from this short path.
- **Fixed: `app.py` subprocess launches used a literal `"python"`
  instead of `sys.executable`.** If `python` on PATH doesn't happen to
  resolve to the venv, job subprocesses silently run under a totally
  different interpreter missing every dependency — `ModuleNotFoundError:
  requests` was the symptom. Fixed at both `subprocess.Popen` call sites
  in `app.py`. Related: two `app.py` instances (one venv, one system
  Python) ended up bound to port 5000 at once during debugging — Windows
  allowed it silently, and requests landed on whichever one at random.
- **Fixed: subprocess output could crash on Windows' console encoding.**
  `PYTHONIOENCODING=utf-8` added to `build_subprocess_env()` in
  `app.py`. Without it, a `print()` containing anything cp1252 can't
  encode (box-drawing characters in a library's own error banner, for
  instance) raises `UnicodeEncodeError` while writing that very message
  — which kills the subprocess and erases the original error before it
  ever reaches the job log. This is why a job could die with zero
  explanation in the UI.
- **Fixed: `vod.py`'s aria2c check used `subprocess.run(["which",
  "aria2c"])`.** `which` doesn't exist on Windows at all — crashed the
  first time any yt-dlp download path ran. Now uses `shutil.which`,
  which is cross-platform.
- **Fixed: `vod.py`'s `_download_via_browser` used `wait_until=
  "networkidle"`.** Kick's live pages keep the network busy indefinitely
  (chat websocket, analytics), so this could hang the full 60s timeout
  even when the page loaded fine. Switched to `"domcontentloaded"` — the
  `.m3u8` response listener is already independent of the wait
  condition, so it never needed networkidle in the first place.
- **Fixed: `_pull_audio_only` in `vod.py` silently discarded ffmpeg's
  own stderr**, only logging lines that matched the progress `time=`
  regex. A real failure surfaced as nothing but a bare exit code (one
  job died with `3419392776` and no other explanation). Now keeps the
  last 20 lines of ffmpeg's actual output and includes them in the
  raised error.
- **Merged in resilience improvements to `vod.py`** from a separate
  export of this project: retry once on yt-dlp merge/rename transient
  failures (distinct from the existing aria2c retry), checkpoint every
  found moment to `found_moments_checkpoint.json` right before the
  ranking pass runs, and wrap the ranking/capping pass in try/except so
  a bug there degrades to "export everything that already passed"
  instead of losing a multi-hour job's results.
- **`templates/index.html` got a motion pass**: tabs fade+rise on
  switch (`showTab()` already removes then re-adds `.active`, so a CSS
  animation on that class restarts cleanly every time — no JS change
  needed), job/clip cards fade+slide in via `@starting-style` (also no
  JS needed), a slow shimmer on the panel/card spectrum bar, a breathing
  pulse on the "live" status dot, and a faint grain texture overlay.
  Important: the grain overlay MUST use `mix-blend-mode: soft-light`,
  not `overlay` — `overlay` visibly washes out this dark palette's
  contrast across the whole page (confirmed by testing both).
- **Added a marketing/demo page** (`templates/landing.html`) in a dark
  glowing-SaaS style the user referenced from other sites, but grounded
  in the real product: an animated canvas waveform instead of a generic
  hero graphic, the actual 4-step pipeline as numbered cards, a faithful
  mini-recreation of the real dashboard UI (not stock photos), and a
  "Run it your way" CPU/GPU/VPS section instead of invented SaaS pricing
  tiers, since this is self-hosted, not a hosted product. Same color
  tokens as `index.html` for brand continuity. Originally added at
  `/landing`, but the user then wanted it to be the front door — routes
  were swapped so `/` now serves `landing.html` and the working
  dashboard moved to `/dashboard`. If bookmarks/muscle memory expect the
  dashboard at `/`, that's why it's not there anymore.
- **Rewrote the scoring system in `detect.py`/`config.py`/`moments.py`/
  `main.py`** (merged in from an edited copy the user brought back from
  elsewhere) from a general "clip-worthy" hook/payoff/emotion/
  standalone/subject/technical + `moment_type` taxonomy to a judgment-
  centric one: `judgment` (35%, dominant), `payoff` (25%), `conflict`
  (15%), `standalone` (10%), `hook` (10%), `technical` (5%) — tuned
  specifically for ego/status/hypocrisy/callout moments with high
  audience-judgment and comment potential, not generic virality.
  `moment_type` is retired (kept as an always-empty field on
  `DetectionResult`/`Candidate` for structural compatibility, not used
  in any logic anymore). **Scores are now 0-100, not 1-10** — every
  threshold that compares against a score was rescaled by 10x
  (`CLIP_SCORE_THRESHOLD` 6.0→60.0, `CANDIDATE_SCORE_THRESHOLD`
  3.2→32.0, and all of `moments.py`'s merge-compatibility constants).
  Updated to match: the live `.env`'s stale `CLIP_SCORE_THRESHOLD=7`
  (would have made almost everything clear the new 0-100 bar — a
  10x-scale bug, not a real-world 60→7 change of heart), `.env.example`,
  `README.md`, and `templates/index.html`'s threshold inputs/labels and
  preset threshold values (speed 50, tiktok/youtube/quality 60,
  balanced 60 — a straightforward 10x scaling of the old 1-10 presets,
  not independently re-tuned).
  The incoming copy of `vod.py` that came with this edit had silently
  **reverted two Windows-specific fixes** from earlier in this project
  (back to `subprocess.run(["which", "aria2c"])` instead of
  `shutil.which`, and Playwright's `page.goto(..., wait_until=
  "networkidle")` instead of `"domcontentloaded"` — see the two
  "Fixed:" entries above) — those were kept as-is rather than
  reintroduced; only the scoring-related pieces of that `vod.py` (drop
  `moment_type`, add the previously-missing `quote=result.quote` on the
  `Candidate(...)` call, checkpoint dict) were merged in by hand. Also
  reverted a literal `["python", "main.py"]` call, i.e. the exact bug
  documented above — not reintroduced either. **If a future "updated
  copy" of this project ever shows up again, diff it against what's
  here before copying anything wholesale — it may be older than it
  looks in places even where it's genuinely newer in others.**

"""
Processes an already-recorded VOD instead of a live stream: transcribes
the whole thing once, scans it for clip-worthy moments with overlapping
windows (so nothing at a window boundary gets missed), merges adjacent
good moments into single clips, and cuts/captions/posts each one --
using the exact same clip.py and generate_posts.py as the live pipeline.

Usage:
    python vod.py --input <local_file_or_vod_url> --channel <name>

Examples:
    python vod.py --input https://kick.com/deenthegreat/videos/abc123 --channel deenthegreat
    python vod.py --input ./downloads/some_vod.mp4 --channel deenthegreat
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import requests
from verified_hls import verified_source
from pathlib import Path
from datetime import datetime as datetime_module
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import Config
from transcribe import Transcriber, Word, Transcript
from detect import score_batch, rank_top_moments, ProviderUnavailable
from clip import cut_and_caption, apply_layout, snap_to_words, cut_single_pass
from generate_posts import generate_posts, write_posts_file
from moments import Candidate, merge_candidates, dedupe_clips

WINDOW_SECONDS = Config.DETECTION_WINDOW_SECONDS  # 75s default -- long enough to hold a
                                                   # whole hook->progression->climax arc,
                                                   # not just one line of it
STEP_SECONDS = Config.DETECTION_HOP_SECONDS        # 20s default -- heavy overlap, so a
                                                    # story spanning a window boundary
                                                    # still gets judged as a whole


def _download_via_browser(url: str, dest_dir: Path) -> Path:
    """
    Free, no-signup path: opens the VOD page in a real (headless)
    browser via Playwright, watches actual network traffic for the
    .m3u8 stream URL the page itself requests to play the video -- the
    same thing your own browser does when you press play -- then hands
    that URL to ffmpeg to download. This works regardless of whether
    Kick's metadata API (the thing yt-dlp's extractor depends on) is
    currently broken, since it doesn't use that API at all.
    """
    from playwright.sync_api import sync_playwright

    print("[vod] launching browser to find the video stream URL...")
    m3u8_url = None

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        def handle_response(response):
            nonlocal m3u8_url
            if m3u8_url is None and ".m3u8" in response.url:
                m3u8_url = response.url

        page.on("response", handle_response)
        # "networkidle" is the wrong wait condition for a live-streaming
        # site like Kick -- chat websockets, analytics, and ad beacons
        # keep the network busy indefinitely, so it can hang the full
        # 60s timeout even when the page (and its video player) loaded
        # fine. The .m3u8 response listener above is already independent
        # of this wait, and the polling loop below gives the player time
        # to start requesting the stream after the page is interactive.
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # give the player a moment to start requesting the stream if it
        # hasn't already
        for _ in range(20):
            if m3u8_url:
                break
            page.wait_for_timeout(1000)

        browser.close()

    if not m3u8_url:
        raise RuntimeError("No .m3u8 stream URL was observed loading this page.")

    print(f"[vod] found stream URL, downloading via ffmpeg...")
    dest = dest_dir / "vod_source.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-i", m3u8_url, "-c", "copy", str(dest)],
        check=True,
    )
    return dest


def _download_via_apify(url: str, dest_dir: Path) -> Path:
    """
    Downloads a Kick VOD via the parsebird/kick-video-downloader Apify
    actor -- a paid third-party service (a few cents per download,
    scaled by file size). Used as the primary path since yt-dlp's own
    Kick VOD support is currently broken upstream (Kick changed how VOD
    URLs work; see https://github.com/yt-dlp/yt-dlp/issues/17284).
    """
    from apify_client import ApifyClient

    client = ApifyClient(Config.APIFY_API_TOKEN)
    print("[vod] requesting VOD via Apify Kick downloader (this incurs a small Apify cost)...")
    run = client.actor("parsebird/kick-video-downloader").call(
        run_input={"startUrls": [{"url": url}], "quality": "best"}
    )
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    if not items or items[0].get("status") != "success":
        raise RuntimeError(f"Apify actor did not return a successful download: {items}")

    download_url = items[0]["download_url"]
    dest = dest_dir / "vod_source.mp4"
    print("[vod] downloading video from Apify result...")
    resp = requests.get(download_url, stream=True, timeout=600)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
    return dest


def _run_yt_dlp(input_path: str, dest_dir: Path, use_aria2c: bool = True, _retried_after_merge_failure: bool = False) -> Path:
    """
    Runs yt-dlp with speed flags: -N enables downloading multiple video
    fragments concurrently instead of one at a time (the single biggest
    yt-dlp speed lever for fragmented HLS/DASH sources like YouTube),
    and uses aria2c as the actual downloader if it's installed on the
    system, since it's generally faster than yt-dlp's built-in
    downloader for this kind of multi-fragment pull.

    Known gotcha #1: yt-dlp + aria2c can occasionally fail right at the
    end of a download that needs post-merging (e.g. YouTube's separate
    video/audio streams) -- aria2c's own temp-file handling sometimes
    doesn't line up with what yt-dlp expects to find afterward. If that
    happens, this automatically retries once WITHOUT aria2c.

    Known gotcha #2: even without aria2c, yt-dlp's own final merge/
    rename step can fail with "No such file or directory" on a temp
    file that was just written seconds earlier -- almost always a
    transient filesystem timing issue (antivirus briefly scanning/
    locking the file, a second job sharing the same work folder, etc.),
    not a real problem with the download itself. Retrying the whole
    download once after cleaning up leftovers usually just works, and
    is far cheaper than losing the download entirely and making the
    user start over by hand.
    """
    out_template = str(dest_dir / "vod_source.%(ext)s")
    cmd = [
        __import__('sys').executable, "-m", "yt_dlp", input_path,
        "-o", out_template,
        "--merge-output-format", "mp4",
        "-N", "8",  # 8 concurrent fragment downloads
    ]

    aria2c_available = use_aria2c and shutil.which("aria2c") is not None
    if aria2c_available:
        cmd += ["--downloader", "aria2c", "--downloader-args", "aria2c:-x8 -s8"]
        print("[vod] using aria2c for faster fragment downloading")
    else:
        print("[vod] using yt-dlp's built-in downloader"
              + ("" if use_aria2c else " (retry without aria2c)"))

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        if use_aria2c and aria2c_available:
            # The aria2c+merge interop issue -- retry once without it
            # rather than losing the whole download over a quirk in how
            # the two tools hand off the finished fragments.
            print("[vod] download failed with aria2c (known merge-handoff quirk), "
                  "retrying without it...")
            for leftover in dest_dir.glob("vod_source.*"):
                leftover.unlink(missing_ok=True)
            return _run_yt_dlp(input_path, dest_dir, use_aria2c=False)

        if not _retried_after_merge_failure:
            # A plain (non-aria2c) merge/rename failure -- almost always
            # a transient filesystem timing issue, not a real download
            # problem. One clean retry, not an infinite loop.
            print("[vod] download failed during yt-dlp's final merge/rename step "
                  "(likely a transient filesystem timing issue -- antivirus scanning "
                  "the file, another job sharing this folder, etc.). Cleaning up and "
                  "retrying the download once...")
            for leftover in dest_dir.glob("vod_source.*"):
                leftover.unlink(missing_ok=True)
            return _run_yt_dlp(input_path, dest_dir, use_aria2c=use_aria2c, _retried_after_merge_failure=True)

        raise

    matches = list(dest_dir.glob("vod_source.*"))
    if not matches:
        raise RuntimeError("yt-dlp did not produce an output file.")
    return matches[0]


@verified_source
def _probe_duration(stream_url: str) -> float | None:
    """Asks ffprobe for the source's total duration, used to compute
    percentage progress during the audio pull below."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", stream_url],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def _resolve_analysis_audio_url(stream_url: str) -> str:
    """Resolve the lightest audio-capable HLS rendition for rolling ASR.

    A master HLS URL may expose video-heavy variants. Rolling ASR only needs
    speech, so ask yt-dlp for its best audio-only rendition once and let every
    ASR window read that smaller playlist. Fail open to the original URL.
    """
    if not stream_url.startswith("http"):
        return stream_url
    if os.getenv("ROLLING_AUDIO_ONLY_RESOLUTION", "true").lower() not in ("1", "true", "yes", "on"):
        return stream_url
    try:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist",
             "-f", "bestaudio[protocol^=m3u8]/worst[protocol^=m3u8]/bestaudio/worst",
             "-g", stream_url],
            capture_output=True, text=True, timeout=30,
        )
        urls = [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("http")]
        if result.returncode == 0 and urls:
            chosen = urls[0]
            if chosen != stream_url:
                print("[vod] rolling ASR resolved an audio-only HLS rendition", flush=True)
            return chosen
    except Exception as exc:
        print(f"[vod] audio-only HLS resolution unavailable ({type(exc).__name__}); using original stream", flush=True)
    return stream_url


def _parallel_hls_fetch(stream_url: str, dest_dir: Path) -> Path | None:
    """Parallel HLS ingest with a fast first attempt and conservative fallback.

    HLS fragment concurrency is intentionally separate from Together/Parakeet
    request concurrency. We try 8 fragments by default (bounded to 16), then
    retry at 6 if the CDN/network path is unstable. This avoids saturating
    consumer connections where higher parallelism causes retransmits.
    without raising ASR API pressure or turning one transient fragment failure
    into a failed VOD.
    """
    import re
    if os.getenv("PARALLEL_HLS_INGEST", "true").lower() not in ("1", "true", "yes", "on"):
        return None
    requested = int(os.getenv("HLS_CONCURRENT_FRAGMENTS", "16"))
    hard_max = max(1, min(16, int(os.getenv("HLS_CONCURRENT_FRAGMENTS_MAX", "16"))))
    primary = max(1, min(hard_max, requested))
    fallback = max(1, min(primary, int(os.getenv("HLS_CONCURRENT_FRAGMENTS_FALLBACK", "12"))))
    attempts = []
    for n in (primary, fallback):
        if n not in attempts: attempts.append(n)
    if primary <= 1 or not stream_url.startswith("http"):
        return None

    for attempt_no, fragments in enumerate(attempts, 1):
        for leftover in dest_dir.glob("hls_ingest.*"):
            leftover.unlink(missing_ok=True)
        template = dest_dir / "hls_ingest.%(ext)s"
        cmd = [
            sys.executable, "-m", "yt_dlp", stream_url,
            "--no-warnings", "--newline", "--hls-prefer-native",
            "--concurrent-fragments", str(fragments),
            "--retries", os.getenv("HLS_RETRIES", "5"),
            "--fragment-retries", os.getenv("HLS_FRAGMENT_RETRIES", "8"),
            "--socket-timeout", os.getenv("HLS_SOCKET_TIMEOUT_SECONDS", "20"),
            "--buffer-size", os.getenv("HLS_BUFFER_SIZE", "4M"),
            "-f", "bestaudio[protocol^=m3u8]/worst[protocol^=m3u8]/bestaudio/worst",
            "-o", str(template), "--max-filesize", f"{Config.MAX_TEMP_GB:g}G",
        ]
        print(f"[vod] fast HLS ingest: {fragments} concurrent fragments"
              + ("" if attempt_no == 1 else " (stability fallback)"), flush=True)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        tail=[]; last_bucket=-1
        try:
            for line in proc.stdout:
                from pipeline_state import check_cancelled
                check_cancelled(); tail.append(line.rstrip()); tail=tail[-20:]
                m=re.search(r"\[download\]\s+([0-9]+(?:\.[0-9]+)?)%", line)
                if m:
                    # Report one-percent buckets so the UI reflects real
                    # movement instead of appearing frozen for a long time.
                    raw=min(100, int(float(m.group(1))))
                    bucket=min(95, raw)
                    if bucket > last_bucket:
                        print(f"[vod] parallel audio source pull: {bucket}%", flush=True)
                        print(f"PROGRESS audio_pull {bucket}", flush=True); last_bucket=bucket
            proc.wait()
        except BaseException:
            if proc.poll() is None: proc.kill()
            proc.wait(); raise
        finally:
            if proc.stdout: proc.stdout.close()
        matches=[q for q in dest_dir.glob("hls_ingest.*") if not q.name.endswith((".part",".ytdl")) and q.is_file()]
        if proc.returncode == 0 and matches:
            chosen=max(matches,key=lambda q:q.stat().st_size)
            print(f"[vod] parallel HLS fetch complete ({chosen.stat().st_size/1024**2:.1f} MB)",flush=True)
            return chosen
        print(f"[vod] HLS attempt at {fragments} fragments failed; "
              + ("retrying conservatively" if attempt_no < len(attempts) else "using sequential fallback")
              + (f"; {tail[-1]}" if tail else ""), flush=True)
    for leftover in dest_dir.glob("hls_ingest.*"): leftover.unlink(missing_ok=True)
    return None


@verified_source
def _pull_audio_only(stream_url: str, dest_dir: Path,
                      start_seconds: float = None, end_seconds: float = None) -> Path:
    """
    Pulls JUST the audio track from a direct stream URL -- this is the
    real ingest speed fix. Whisper only ever looks at audio, never
    pixels, so remuxing a full 1080p60 video (which can genuinely take
    longer than the stream's own runtime, with no progress feedback the
    whole time) before transcription even starts was solving the wrong
    problem. Video only gets pulled later, and only for the specific
    clips that actually get kept.

    Reports real progress by parsing ffmpeg's own "time=" output as it
    runs (so the UI shows real percentage, not a dead spinner), and
    detects a genuine stall (no progress for 90s) instead of hanging
    forever with no explanation.
    """
    import re
    import time as _time
    from collections import deque

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "audio.m4a"

    total_duration = None
    if start_seconds or end_seconds:
        total_duration = (end_seconds or _probe_duration(stream_url) or 0) - (start_seconds or 0)
    else:
        total_duration = _probe_duration(stream_url)

    parallel_source = _parallel_hls_fetch(stream_url, dest_dir)
    ingest_source = str(parallel_source) if parallel_source else stream_url

    # If the parallel HLS result already contains AAC audio, remux it directly.
    # Re-encoding an entire multi-hour VOD before ASR is pure serial latency;
    # Parakeet accepts the native AAC and the chunk scheduler can stream-copy it.
    if parallel_source:
        try:
            codec = subprocess.check_output(["ffprobe","-v","error","-select_streams","a:0",
                "-show_entries","stream=codec_name","-of","default=nw=1:nk=1",str(parallel_source)],
                text=True, timeout=20).strip().lower()
        except Exception:
            codec = ""
        if codec == "aac" and not start_seconds and not end_seconds:
            remux = ["ffmpeg","-v","error","-y","-i",str(parallel_source),"-vn","-c:a","copy",
                     "-movflags","+faststart",str(dest)]
            try:
                subprocess.run(remux, capture_output=True, check=True, timeout=300)
                if dest.exists() and dest.stat().st_size > 0:
                    parallel_source.unlink(missing_ok=True)
                    print("[vod] AAC source detected: skipped full-VOD audio re-encode", flush=True)
                    print("PROGRESS audio_pull 100", flush=True)
                    return dest
            except Exception as exc:
                print(f"[vod] AAC remux fast path unavailable ({type(exc).__name__}); using compact re-encode", flush=True)

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info"]
    if not parallel_source:
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-http_persistent", "1"]
    if start_seconds:
        cmd += ["-ss", str(start_seconds)]
    cmd += ["-i", ingest_source]
    if end_seconds and start_seconds is not None:
        cmd += ["-t", str(max(1, end_seconds - start_seconds))]
    elif end_seconds:
        cmd += ["-t", str(end_seconds)]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", __import__("os").getenv("AUDIO_INGEST_BITRATE", "32k"), "-movflags", "+faststart", str(dest)]

    print("[vod] extracting compact ASR audio from the fetched low-bandwidth source...")
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)

    time_pattern = re.compile(r"time=(\d+):(\d+):(\d+)\.\d+")
    last_progress_time = _time.time()
    last_reported_pct = -1
    STALL_TIMEOUT_SECONDS = 90
    # ffmpeg's own diagnostic lines (bad URL, connection refused, invalid
    # data, etc.) don't match the progress pattern below and were
    # previously just dropped -- so a real failure surfaced as nothing
    # but a bare exit code, with the actual reason gone. Keep the tail so
    # a failure can report what ffmpeg actually said.
    recent_stderr = deque(maxlen=20)

    import threading
    from pipeline_state import check_cancelled
    stopped = threading.Event()
    watchdog_error = []
    def monitor():
        while not stopped.wait(.5):
            try:
                check_cancelled()
                if _time.time()-last_progress_time > STALL_TIMEOUT_SECONDS:
                    raise RuntimeError('Audio ingest stalled; completed ASR cache remains available')
                if dest.exists() and dest.stat().st_size > Config.MAX_TEMP_GB*1024**3:
                    raise RuntimeError('Audio ingest exceeds MAX_TEMP_GB disk budget')
            except Exception as exc:
                watchdog_error.append(exc)
                proc.kill()
                return
    watcher = threading.Thread(target=monitor,daemon=True)
    watcher.start()
    try:
        for line in proc.stderr:
            recent_stderr.append(line.rstrip("\n"))
            match = time_pattern.search(line)
            if match:
                last_progress_time = _time.time()
                h, m, s = map(int, match.groups())
                elapsed_seconds = h * 3600 + m * 60 + s
                if total_duration:
                    pct = min(100, int((elapsed_seconds / total_duration) * 100))
                    if pct != last_reported_pct:
                        print(f"[vod] audio pull: {pct}% ({elapsed_seconds // 60}/{int(total_duration // 60)} min)")
                        print(f"PROGRESS audio_pull {pct}")
                        last_reported_pct = pct
            elif _time.time() - last_progress_time > STALL_TIMEOUT_SECONDS:
                print(f"[vod] no progress for {STALL_TIMEOUT_SECONDS}s -- audio pull appears stalled "
                      f"(likely an expired or unstable stream URL), killing and failing")
                proc.kill()
                raise RuntimeError("audio pull stalled -- the .m3u8 URL may have expired, try grabbing a fresh one")
    
        proc.wait()
        if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
            tail = "\n".join(recent_stderr) or "(ffmpeg produced no output at all)"
            raise RuntimeError(
                f"audio pull failed (ffmpeg exit code {proc.returncode}). "
                f"ffmpeg's last output:\n{tail}"
            )
    
        if watchdog_error:
            raise watchdog_error[0]
    except BaseException:
        if proc.poll() is None: proc.kill()
        proc.wait()
        dest.unlink(missing_ok=True)
        raise
    finally:
        stopped.set()
        watcher.join(timeout=1)
        if proc.stderr: proc.stderr.close()
        if parallel_source:
            parallel_source.unlink(missing_ok=True)

    print(f"[vod] audio pull complete -> {dest}")
    print("PROGRESS audio_pull 100")
    return dest


@verified_source
def _cut_range_from_url(stream_url: str, start: float, end: float, out_path: Path):
    """
    Cuts just ONE clip's time range directly from the source stream URL
    -- no full video download needed. Only called for clips that
    actually made it through scoring, so total video data pulled is
    "a few minutes per keeper," not "the whole VOD's video track."
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start
    cmd = [
        "ffmpeg", "-y",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-http_persistent", "0",
        "-ss", str(start),
        "-i", stream_url,
        "-t", str(duration),
        "-c", "copy", "-bsf:a", "aac_adtstoasc",
        "-avoid_negative_ts", "make_zero",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        # Same AAC-header fallback as the full remux path.
        cmd2 = [
            "ffmpeg", "-y",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-http_persistent", "0",
            "-ss", str(start),
            "-i", stream_url,
            "-t", str(duration),
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            str(out_path),
        ]
        subprocess.run(cmd2, check=True)


@verified_source
def _remux_stream_url(stream_url: str, dest_dir: Path) -> Path:
    """
    Downloads a direct .m3u8/stream URL into ONE stable file instead of
    segmenting it into many small chunks. This is the real fix for the
    recurring "Packet corrupt" / missing-audio-stream errors seen with
    the old chunked pipelined mode: those came from splitting a live
    HLS stream into artificially separate pieces that don't each carry
    a fully self-contained container/AAC header on their own. Remuxing
    the whole thing into one continuous file avoids that entirely --
    and for an already-finished VOD, there's no real benefit to the old
    chunked approach's "start before the download finishes" trick that
    would be worth trading reliability for.

    Tries a lossless stream copy first (fast, no quality loss). If that
    specifically fails on the AAC bitstream conversion, falls back to a
    full audio re-encode, which is slower but far more robust.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "vod_source.mkv"

    print("[vod] remuxing full stream into one stable file (replaces the old per-chunk approach)...")
    cmd = [
        "ffmpeg", "-y",
        "-fflags", "+genpts+discardcorrupt",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-rw_timeout", "15000000",
        "-http_persistent", "0",  # avoids the "keepalive request failed" errors seen on Kick's IVS CDN
        "-i", stream_url,
        "-map", "0:v:0", "-map", "0:a:0",
        "-c", "copy", "-bsf:a", "aac_adtstoasc",
        "-avoid_negative_ts", "make_zero",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        print("[vod] stream-copy remux failed (likely an AAC header issue), "
              "retrying with a full audio re-encode instead...")
        dest2 = dest_dir / "vod_source.mp4"
        cmd2 = [
            "ffmpeg", "-y",
            "-fflags", "+genpts+discardcorrupt",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-rw_timeout", "15000000",
            "-http_persistent", "0",
            "-i", stream_url,
            "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            str(dest2),
        ]
        subprocess.run(cmd2, check=True)
        return dest2

    return dest


def download_if_url(input_path: str, dest_dir: Path) -> Path:
    """
    If given a URL, downloads the video. YouTube URLs go straight to
    yt-dlp, which has excellent, fully-working native YouTube support --
    no need for the Kick-specific fallback chain below. For Kick URLs,
    tries methods in order:
      1. Browser automation (free, no signup -- finds the real stream
         URL the same way a human watching in DevTools would, then
         downloads it with ffmpeg).
      2. Apify's Kick downloader, if APIFY_API_TOKEN is set (paid, but
         doesn't need a browser environment).
      3. yt-dlp (last resort -- currently broken for Kick VODs upstream,
         but kept as a fallback in case that gets fixed).
    If given a local path, just returns it as-is.
    """
    if not input_path.startswith("http"):
        return Path(input_path)

    dest_dir.mkdir(parents=True, exist_ok=True)

    # A direct stream/manifest URL (e.g. grabbed from browser DevTools)
    # gets remuxed into one stable file directly -- more reliable than
    # the webpage-URL chain below, since there's no page to scrape a
    # stream URL out of; it already IS the stream URL.
    if ".m3u8" in input_path:
        return _remux_stream_url(input_path, dest_dir)

    is_youtube = "youtube.com" in input_path or "youtu.be" in input_path
    if is_youtube:
        print("[vod] YouTube URL detected -- using yt-dlp directly (no Kick-specific workaround needed)")
        return _run_yt_dlp(input_path, dest_dir)

    try:
        return _download_via_browser(input_path, dest_dir)
    except Exception as e:
        print(f"[vod] browser-based download failed ({e}), trying next method")

    if Config.APIFY_API_TOKEN:
        try:
            return _download_via_apify(input_path, dest_dir)
        except Exception as e:
            print(f"[vod] Apify download failed ({e}), falling back to yt-dlp")

    return _run_yt_dlp(input_path, dest_dir)


def _snap_to_pause(words: list[Word], target_time: float, search_window: float = 5.0, min_gap: float = 0.4, gap_index=None) -> float:
    """
    Adjusts a window boundary to land on a natural pause in speech (a gap
    between consecutive words) instead of a rigid clock-time cutoff.

    Cutting a scoring window at an arbitrary fixed second can slice a
    sentence in half, handing the LLM a torn, confusing fragment instead
    of a complete thought -- the same problem naive fixed-size text
    chunking has in RAG pipelines. Snapping to the nearest real pause
    within a small search radius fixes this without changing the overall
    windowing scheme.
    """
    from bisect import bisect_left, bisect_right
    if gap_index is None:
        gap_index = sorted((words[i].end, words[i+1].start-words[i].end) for i in range(len(words)-1))
    left = bisect_left(gap_index, (target_time-search_window, float('-inf')))
    right = bisect_right(gap_index, (target_time+search_window, float('inf')))
    candidates = []
    for gap_start, gap_size in gap_index[left:right]:
        if gap_size >= min_gap and abs(gap_start - target_time) <= search_window:
            candidates.append((abs(gap_start - target_time), gap_start))

    if not candidates:
        return target_time  # no pause nearby, fall back to the original cutoff

    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


def _local_priority_score(text: str) -> int:
    """
    Cheap, local, zero-cost heuristic used ONLY to decide which windows
    get scored first -- never to skip or discard anything. Counts
    priority-name mentions plus a small set of generic high-signal
    keywords. This matters when MAX_CLIPS causes early cancellation of
    remaining batches: processing the more-promising windows first means
    a genuinely good moment is more likely to be found before the job
    stops, instead of being wherever it happened to fall in scan order.
    """
    text_lower = text.lower()
    score = 0
    for name in (Config.PRIORITY_NAMES or "").split(","):
        name = name.strip().lower()
        if name and name in text_lower:
            score += 3
    for keyword in ("gift", "surprise", "crazy", "insane", "wow", "oh my god",
                     "fight", "arrested", "banned", "slapped", "cheat", "shocked"):
        if keyword in text_lower:
            score += 1
    return score


def find_candidates(words: list[Word], total_duration: float, audit_path=None) -> list[Candidate]:
    """
    Slides a window across the whole VOD transcript and scores every one
    -- in BATCHES of several windows per API call, not one call per
    window. Per-window calls to a full model were the actual reason a
    60-minute VOD could take longer to scan than to watch: dozens of
    slow round-trips add up fast. Batching keeps the exact same 6-category
    scoring depth, just stops paying for a separate HTTP request every
    20-75 seconds of content.

    Windows are also ordered by a cheap local heuristic (name/keyword
    mentions) before batching -- not to skip anything, only so the more
    promising stretches get scored first. That matters if MAX_CLIPS
    causes the job to stop early: you want the strong candidates found
    before that happens, not left unscored because of scan order.

    Window boundaries are snapped to natural speech pauses where
    possible, so the LLM is judging a complete thought rather than a
    sentence cut off mid-word by a rigid fixed-second cutoff.
    """
    import time as _time

    WINDOWS_PER_BATCH = max(1, min(12, int(__import__('os').getenv('DEEP_WINDOWS_PER_BATCH', '8'))))
    MAX_CONCURRENT_BATCHES = Config.DEEP_ANALYSIS_MAX_CONCURRENCY

    from scout_pipeline import semantic_windows, shortlist, context_text, cached_deep_score
    import os
    all_windows = semantic_windows(words, Config.SEMANTIC_WINDOW_SECONDS,
                                   Config.SEMANTIC_WINDOW_SECONDS-Config.SEMANTIC_WINDOW_OVERLAP_SECONDS)
    all_windows = shortlist(all_windows, words, audit_path)
    deep_model = Config.DEEP_SCORING_MODEL or None

    batches = [all_windows[i:i + WINDOWS_PER_BATCH] for i in range(0, len(all_windows), WINDOWS_PER_BATCH)]
    total_windows = len(all_windows)
    candidates = []
    windows_done = 0
    windows_failed = 0
    recent_samples = []  # (wall_clock_time, windows_done), rolling window
    started_at = _time.time()

    print(f"[vod] scoring {total_windows} windows in {len(batches)} batches of up to "
          f"{WINDOWS_PER_BATCH} (up to {MAX_CONCURRENT_BATCHES} batches running at once)...")

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_BATCHES) as executor:
        future_to_batch = {
            executor.submit(cached_deep_score, [context_text(w, words) for w in batch], deep_model, audit_path.parent if audit_path else Path(Config.OUTPUT_DIR)): batch
            for batch in batches
        }

        stop_early = False
        for future in as_completed(future_to_batch):
            if stop_early:
                future.cancel()
                continue

            batch = future_to_batch[future]
            try:
                results = future.result()
            except ProviderUnavailable:
                for pending in future_to_batch:
                    pending.cancel()
                raise
            except Exception as e:
                print(f"[vod] batch scoring failed ({e})")
                results = [None] * len(batch)

            for (window_start, window_end, window_text), result in zip(batch, results):
                if result is None:
                    windows_failed += 1
                    windows_done += 1
                    continue

                print(f"[vod] {window_start:.0f}s-{window_end:.0f}s score={result.score} "
                      f"topic={result.topic!r} reason={result.reason}")

                # safety_flag no longer gates whether a window is kept --
                # rating is purely score-based now. It's still carried
                # through on the Candidate so generate_posts.py can use
                # it to pick a plain, factual tone instead of the usual
                # clickbait formula (a real judgment/legal-situation clip
                # used to get thrown away entirely just for being
                # flagged, which lost genuinely strong moments).
                if result.score >= Config.CANDIDATE_SCORE_THRESHOLD:
                    # Deliberately loose gate here -- a candidate is
                    # allowed to be an incomplete fragment (pure setup,
                    # or a payoff whose setup was in the previous
                    # window). The real quality gate happens AFTER
                    # merging, against CLIP_SCORE_THRESHOLD.
                    candidates.append(Candidate(
                        window_start, window_end, window_text, result.score, result.reason,
                        topic=result.topic, quote=result.quote,
                        breakdown=result.breakdown, safety_flag=result.safety_flag,
                        moment_type=getattr(result, 'event_class', ''),
                        tightness=getattr(result, 'tightness', None),
                        payoff_at_seconds=getattr(result, 'payoff_at_seconds', None),
                        payoff_type=getattr(result, 'payoff_type', 'none'),
                    ))

                windows_done += 1

            pct = min(100, int((windows_done / total_windows) * 100)) if total_windows else 100

            now = _time.time()
            recent_samples.append((now, windows_done))
            if len(recent_samples) > 12:
                recent_samples.pop(0)

            if len(recent_samples) >= 2:
                window_start_time, window_start_count = recent_samples[0]
                real_elapsed_in_window = now - window_start_time
                windows_in_sample = windows_done - window_start_count
                remaining_windows = total_windows - windows_done

                if windows_in_sample > 0 and real_elapsed_in_window > 0:
                    seconds_per_window = real_elapsed_in_window / windows_in_sample
                    eta_seconds = int(seconds_per_window * remaining_windows)
                    print(f"PROGRESS scan {pct}")

            if False:  # Never stop scanning early: MAX_CLIPS caps exports, not discovery.
                print(f"[vod] reached max clips ({Config.MAX_CLIPS}), cancelling remaining batches")
                stop_early = True

    if windows_failed:
        print(f'[vod] WARNING incomplete scan: {windows_failed}/{total_windows} windows could not be scored')
        if not candidates:
            raise RuntimeError('Scoring failed for some windows; an empty result is not a confirmed complete scan')
    return candidates


def _save_recipe(clip_dir: Path, channel: str, source_video, start: float, end: float,
                  aspect: str, track: str, layout: str, captions_enabled: bool,
                  clip_words, mode: str = "vod"):
    """
    Saves recipe.json + words.json alongside a freshly-cut clip -- this
    is what lets /api/clip/update later re-render just THIS clip (trim,
    aspect, tracking, overlay text, etc.) without re-running Whisper or
    the LLM scorer at all. words.json is the exact Whisper words used,
    so a later re-trim can still snap to real word boundaries.

    source_video is always a real local file by the time this is called
    (see _prepare_clip_source) -- for a URL-sourced VOD, that's the
    small per-clip segment already cut from the stream, not the original
    URL itself. Re-rendering later reuses that segment file directly
    rather than re-fetching from the source URL, which may have expired
    by then.
    """
    source_str = str(source_video)
    if not source_str.startswith("http"):
        source_str = str(Path(source_video).resolve())

    recipe = {
        "clip_id": clip_dir.name,
        "channel": channel,
        "mode": mode,
        "source_video": source_str,
        "start": start,
        "end": end,
        "aspect": aspect,
        "track": "auto" if track else "off",
        "track_face_id": None,
        "layout": layout,
        "captions_enabled": captions_enabled,
        "caption_style": "default",
        "overlay_text": "",
        "overlay_seconds": 3.0,
        "zoom": 1.0,
        "words_path": "words.json",
    }
    (clip_dir / "recipe.json").write_text(json.dumps(recipe, indent=2), encoding="utf-8")

    words_data = [{"text": w.text, "start": w.start, "end": w.end} for w in clip_words]
    (clip_dir / "words.json").write_text(json.dumps(words_data), encoding="utf-8")


def _prepare_clip_source(c, all_words, work_dir: Path, source_is_url: bool, source):
    """
    For a direct-stream-URL source, cuts just THIS clip's small time
    range into a local temp file first -- so the existing captioning/
    cropping pipeline (which expects a normal local file) works exactly
    as it always has, without ever pulling the whole VOD's video track.
    Only the clips that actually get kept ever touch the video stream at
    all. For a normal local-file source, this just returns the existing
    absolute timestamps unchanged -- no re-basing needed.

    Returns (source_for_cutting, local_start, local_end, local_words).
    """
    snapped_start, snapped_end = snap_to_words(all_words, c.start, c.end, max_length=max(45, c.end-c.start+3))
    clip_words = [w for w in all_words if snapped_start <= w.start <= snapped_end]

    if not source_is_url:
        return source, snapped_start, snapped_end, clip_words

    local_path = work_dir / f"segment_{snapped_start:.3f}_{snapped_end:.3f}.mp4"
    from retry_failed_exports import playable
    if not playable(local_path):
        _cut_range_from_url(source, snapped_start, snapped_end, local_path)
    local_words = [
        __import__("dataclasses").replace(w, start=w.start - snapped_start, end=w.end - snapped_start)
        for w in clip_words
    ]
    return local_path, 0.0, snapped_end - snapped_start, local_words


def trim_video(source_video: Path, start_seconds: float, end_seconds: float, work_dir: Path) -> Path:
    """
    Cuts the source down to just the requested range BEFORE transcription
    even starts -- this is what actually makes "just process 10 minutes"
    fast: the rest of the video is never transcribed or scanned at all,
    not just filtered out afterward.
    """
    duration = max(1, end_seconds - start_seconds) if end_seconds else None
    trimmed = work_dir / "trimmed_range.mp4"
    cmd = ["ffmpeg", "-y", "-ss", str(start_seconds or 0)]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-i", str(source_video), "-c", "copy", str(trimmed)]
    subprocess.run(cmd, check=True)
    print(f"[vod] trimmed to requested range -> {trimmed}")
    return trimmed


def _generate_posts_with_fallback(transcript_text: str, streamer: str, clip_dir: Path,
                                   standalone_score: float = None, safety_flag: str = None):
    """
    Guarantees posts.txt always exists once a clip is cut, even if
    generate_posts() somehow raises instead of returning its normal
    error-placeholder dict, or write_posts_file() itself fails. Library
    listings now require posts.txt to exist before showing a card
    (app.py's _list_clips) -- this is the other half of that fix: making
    sure the file always gets written, one way or another, instead of a
    clip silently sitting there with a thumbnail nobody can see.
    """
    try:
        posts = generate_posts(transcript_text, standalone_score=standalone_score, safety_flag=safety_flag)
        write_posts_file(posts, clip_dir / "posts.txt")
    except Exception as e:
        print(f"[vod] title generation failed unexpectedly ({e}), writing fallback posts.txt")
        ts = datetime_module.now().strftime("%H%M%S")
        fallback = f"{streamer} clip {ts} [title generation failed, write manually]\n"
        (clip_dir / "posts.txt").write_text(fallback, encoding="utf-8")


def process_vod(input_path: str, channel: str, start_seconds: float = None, end_seconds: float = None, transcript_path: str = None, checkpoint: str = None):
    Config.validate()
    from pipeline_state import Timings, fingerprint, atomic_json, check_cancelled
    import time as pipeline_time
    pipeline_started = pipeline_time.monotonic()
    base = Path(Config.OUTPUT_DIR) / "vod" / channel
    import uuid
    run_id = uuid.uuid4().hex[:12]
    work_dir = base / "work" / run_id
    work_dir.mkdir(parents=True, exist_ok=True)
    state_dir = base / ".pipeline" / fingerprint({"input":input_path,"start":start_seconds,"end":end_seconds})
    state_dir.mkdir(parents=True, exist_ok=True)
    timings = Timings(state_dir / "timings.json")
    __import__("os").environ["JOB_METRICS_PATH"] = str(state_dir / "timings.json")
    __import__("os").environ["JOB_ANALYSIS_CACHE_DIR"] = str(state_dir/"llm_cache")
    from detect import _call_llm, _scoring_model
    preflight_started = pipeline_time.monotonic()
    print("[vod] checking scoring provider before downloading or transcribing...")
    _call_llm("Reply with OK.", model=_scoring_model())
    preflight_seconds = pipeline_time.monotonic() - preflight_started
    # This job's channel is who this job is actually for -- overriding
    # here (rather than relying on whatever STREAMER_NAME/KICK_CHANNEL
    # happens to be sitting in .env) is what fixes wrong-name titles
    # ("Broner/Deen on an N3ON job"). .env's KICK_CHANNEL is just a
    # convenience default for a single-job setup; every job actually
    # run through the UI passes its own --channel, and that's what
    # should drive scoring/title name-filtering, always.
    Config.STREAMER_NAME = channel
    Config.log_active_thresholds()
    timings.record(provider_preflight_seconds=preflight_seconds)
    clips_dir = base / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    # For a direct .m3u8 URL, pull audio ONLY for detection -- video is
    # never downloaded up front. This is the real speed fix: Whisper
    # never looks at pixels, so remuxing a full 1080p60 video before
    # transcription even starts was solving the wrong problem (and could
    # genuinely take longer than the stream's own runtime). Video only
    # gets pulled later, per-clip, and only for clips that get kept.
    from vod_links import kick_vod_id, resolve_kick_vod
    vod_reference = input_path
    if kick_vod_id(input_path):
        print("[vod] resolving recording from the Kick VOD page...", flush=True)
        input_path = resolve_kick_vod(input_path)
        print("[vod] recording resolved; using audio-first processing", flush=True)
    analysis_stream_url = input_path
    is_direct_stream = input_path.startswith("http") and ".m3u8" in input_path

    if transcript_path:
        saved_transcript = json.loads(Path(transcript_path).read_text(encoding="utf-8"))
        transcript = Transcript(Path(transcript_path), [Word(**w) for w in saved_transcript["words"]], saved_transcript["text"])
        if not transcript.words:
            raise ValueError("Saved transcript has no words; cannot resume")
        transcribe_source = Path(transcript_path)
        source_is_url = input_path.startswith("http")
        if source_is_url and not is_direct_stream:
            raise ValueError("Transcript recovery requires a direct HLS URL or the original local video")
        cut_source = input_path
        print("[vod] recovered saved transcript; skipping audio download and transcription", flush=True)
    elif is_direct_stream:
        rolling_remote_asr = os.getenv('ROLLING_REMOTE_ASR', 'false').lower() in ('1','true','yes','on') and not start_seconds and not end_seconds
        if rolling_remote_asr:
            # Do not serially download 100% of a multi-hour audio track first.
            # The ASR scheduler range-pulls independent windows from HLS concurrently,
            # checkpoints each result, and feeds ProgressiveScout as contiguous windows finish.
            transcribe_source = None
            analysis_stream_url = _resolve_analysis_audio_url(input_path)
            print('[vod] ROLLING REMOTE ASR enabled: skipping full audio-source pull', flush=True)
            print('PROGRESS audio_pull 100', flush=True)
        elif Config.MAI_CACHE_ENABLED:
            from audio_ingest_cache import cached_audio
            transcribe_source = cached_audio(base, vod_reference, start_seconds, end_seconds,
                lambda folder: _pull_audio_only(input_path, folder, start_seconds, end_seconds))
        else:
            transcribe_source = _pull_audio_only(input_path, work_dir, start_seconds, end_seconds)
        cut_source = input_path  # cut winners directly from the URL later
        source_is_url = True
        print(f"[vod] using audio-first ingest, cutting winners directly from the stream URL")
    else:
        source_video = download_if_url(input_path, work_dir)
        print(f"[vod] using source file: {source_video}")
        from audio_pipeline import normalize_audio
        from audio_ingest_cache import cached_audio
        source_stat = source_video.stat()
        local_identity = str(source_video.resolve())+f':{source_stat.st_size}:{source_stat.st_mtime_ns}'
        transcribe_source = cached_audio(base,local_identity,start_seconds,end_seconds,
            lambda folder: normalize_audio(source_video,folder,start_seconds,end_seconds))
        cut_source = source_video
        source_is_url = False

    # Billing hook: app.py holds credits sized to the source's actual
    # duration, but only once it's cheaply knowable -- right here, after
    # resolving the source but BEFORE the expensive transcribe/score/
    # render work starts. This print is the entire contract between this
    # pipeline and the credit ledger; vod.py itself knows nothing about
    # billing beyond "announce how many minutes you're about to process."
    source_seconds = saved_transcript.get("source_seconds") if transcript_path else (_probe_duration(input_path) if is_direct_stream and transcribe_source is None else _probe_duration(str(transcribe_source)))
    if source_seconds:
        max_hours = float(getattr(Config, "MAX_SOURCE_HOURS", 24) or 0)
        if max_hours and source_seconds > max_hours * 3600:
            raise RuntimeError(f"Source is {source_seconds/3600:.1f}h; this plan/deployment is limited to {max_hours:g}h per job.")
        print(f"PROGRESS estimate {source_seconds / 60:.2f}")

    timings.record(source_duration_seconds=source_seconds or 0, media_preparation_seconds=pipeline_time.monotonic()-pipeline_started)
    if not transcript_path:
        print("[vod] transcribing (this can take a while for long streams)...")
        transcriber = Transcriber(model_size=Config.WHISPER_MODEL_SIZE, profile="vod")
        import time as benchmark_time
        transcription_started = benchmark_time.monotonic()
        from scout_pipeline import ProgressiveScout
        scout = ProgressiveScout(state_dir, (start_seconds or 0))
        try:
            if is_direct_stream and transcribe_source is None:
                if not source_seconds:
                    raise RuntimeError('Could not determine remote HLS duration for rolling ASR')
                transcript = transcriber.transcribe_vod_url(analysis_stream_url, work_dir, source_seconds, scout.update)
            else:
                transcript = transcriber.transcribe_vod(transcribe_source, scout.update)
        finally:
            scout.close()
        timings.record(asr_and_progressive_scout_seconds=benchmark_time.monotonic()-transcription_started)
        (base / "transcript.json").write_text(json.dumps({"text": transcript.full_text, "words": [vars(w) for w in transcript.words], "elapsed_seconds": benchmark_time.monotonic()-transcription_started, "source_seconds": source_seconds}, ensure_ascii=False), encoding="utf-8")

    # Remote clips are cut from the original VOD. A range-limited audio
    # transcript starts at zero, so restore its source offset exactly once.
    # The saved transcript above stays relative to the ingested audio.
    if start_seconds:
        transcript = Transcript(transcript.chunk_path,
            [__import__("dataclasses").replace(w,start=w.start+start_seconds,end=w.end+start_seconds) for w in transcript.words],
            transcript.full_text)
    if not transcript.words:
        print("[vod] no speech detected, nothing to do.")
        return

    total_duration = transcript.words[-1].end
    print(f"[vod] transcript covers {total_duration:.0f}s, scanning for moments...")

    atomic_json(state_dir / "transcript-global.json", {"words":[vars(w) for w in transcript.words],"source_offset":start_seconds or 0})
    analysis_started = pipeline_time.monotonic()
    if checkpoint:
        saved_moments = json.loads(Path(checkpoint).read_text(encoding="utf-8"))
        candidates = [Candidate(text=" ".join(w.text for w in transcript.words if m["start"] <= w.start < m["end"]), **m) for m in saved_moments]
        print(f"[vod] recovered {len(candidates)} saved moments; skipping scoring and merging", flush=True)
    else:
        candidates = find_candidates(transcript.words, total_duration, state_dir / "scout_shortlist.json")
    timings.record(scout_and_deep_seconds=pipeline_time.monotonic()-analysis_started, initial_candidates=len(candidates))
    print(f"[vod] found {len(candidates)} raw candidate windows, merging nearby ones into full moments...")
    repair_started = pipeline_time.monotonic()
    if not transcript_path:
        from transcript_quality import repair_candidates
        repair_candidates(candidates, transcript.words, transcriber, transcribe_source,
                          (start_seconds or 0), state_dir/'quality')
        atomic_json(state_dir/'transcript-global.json', {'words':[vars(w) for w in transcript.words]})
    timings.record(transcript_repair_seconds=pipeline_time.monotonic()-repair_started)
    merge_started = pipeline_time.monotonic()
    merged_all = candidates if checkpoint else merge_candidates(candidates)
    timings.record(moment_merge_rescore_seconds=pipeline_time.monotonic()-merge_started)

    # merge_candidates() ALREADY makes the full keep/drop decision
    # internally -- including the CLIP_SCORE_THRESHOLD bar, the peak-
    # preservation fallback, and the lower 5.5 bar for high-value
    # moment types (REVEAL/QUOTE_BOMB/CRASHOUT). Only entries that
    # cleared one of those are ever returned in merged_all. Re-filtering
    # here with a flat score >= CLIP_SCORE_THRESHOLD check was a real
    # bug: it silently discarded anything that only cleared via the
    # type-floor exception (e.g. a 5.6 CRASHOUT peak that moments.py
    # had already correctly decided to keep) -- undoing that decision
    # right after making it. Trust what merge_candidates() returns.
    passed = merged_all
    print(f"[vod] {len(candidates)} raw candidates -> {len(passed)} moments cleared for export")

    # Safety net: even with topic/gap merging, two of these can still be
    # the same underlying moment. Drop the weaker duplicate before
    # spending a render + title-generation call on both.
    merged = dedupe_clips(passed)
    if len(merged) < len(passed):
        print(f"[vod] removed {len(passed) - len(merged)} duplicate/overlapping clip(s)")

    # Checkpoint: write every found moment to disk RIGHT NOW, before the
    # ranking pass or any cutting happens. This is the actual lesson
    # from a real incident -- a bug in the ranking pass once crashed a
    # multi-hour job after it had already found 12 real moments, and
    # NONE of them were recoverable except by manually re-reading the
    # terminal scrollback and re-typing ffmpeg commands by hand. If
    # anything below this point ever fails for any reason, this file
    # means the moments are still recoverable from a clean, structured
    # record -- not a hope that the terminal log is still visible.
    checkpoint_path = base / "found_moments_checkpoint.json"
    checkpoint_data = [
        {"start": m.start, "end": m.end, "score": m.score,
         "topic": m.topic, "reason": m.reason, "quote": m.quote,
         "event_class": getattr(m, "moment_type", ""), "tightness": getattr(m, "tightness", None),
         "payoff_at_seconds": getattr(m, "payoff_at_seconds", None), "payoff_type": getattr(m, "payoff_type", "none"), "moment_score": getattr(m,"moment_score",None), "cut_score": getattr(m,"cut_score",None), "suggested_trim": getattr(m,"suggested_trim","") }
        for m in merged
    ]
    checkpoint_path.write_text(json.dumps(checkpoint_data, indent=2), encoding="utf-8")
    print(f"[vod] checkpointed {len(merged)} found moments -> {checkpoint_path} "
          f"(recoverable even if anything below this point fails)")

    review_started = pipeline_time.monotonic()
    from context_review import review_candidates
    try:
        review_candidates(merged, transcript.words, state_dir,
                          lambda c: _prepare_clip_source(c, transcript.words, work_dir, source_is_url, cut_source),
                          audio_source=None if transcript_path else transcribe_source,
                          audio_offset=(start_seconds or 0))
    except ProviderUnavailable:
        raise
    except Exception as exc:
        print(f"[vod] WARNING optional context review failed ({exc}); continuing with saved scores", flush=True)

    merged = [c for c in merged if (c.breakdown or {}).get("payoff",3) >= 3]
    timings.record(context_visual_seconds=pipeline_time.monotonic()-review_started)
    print("PROGRESS ranking 0", flush=True)
    ranking_started = pipeline_time.monotonic()
    # Whole-VOD ranking pass: a second, holistic look at everything that
    # already cleared its own bar -- picks the best 3-8 to actually
    # post, the way a human editor reviewing a shortlist would, rather
    # than every individually-passing moment getting auto-exported. A
    # hard peak-lock (raw score >= VERY_HIGH_PEAK_SCORE) always survives
    # regardless of this pass's opinion -- a genuinely excellent moment
    # shouldn't get cut just because the ranking call had a bad take.
    #
    # The whole block is wrapped in try/except: this pass is a quality
    # improvement, not a required step, and it should NEVER be able to
    # take down a job that already successfully found real moments (see
    # the checkpoint above and its reasoning) -- a bug here should
    # degrade to "skip ranking, export everything that already cleared
    # its own bar" instead of losing hours of work.
    if merged:
        try:
            from moments import VERY_HIGH_PEAK_SCORE
            chosen_indices_set = set(rank_top_moments(merged))
            locked_indices_set = {i for i, m in enumerate(merged) if m.score >= VERY_HIGH_PEAK_SCORE}
            keep_indices = sorted(chosen_indices_set | locked_indices_set)
            ranked_result = [merged[i] for i in keep_indices]
            locked = [merged[i] for i in locked_indices_set]

            if len(ranked_result) < len(merged):
                print(f"[vod] ranking pass: {len(merged)} candidates -> {len(ranked_result)} chosen to post "
                      f"({len(locked)} peak-locked regardless of the ranking call)")

            # Cap total exports so a long VOD with many decent-but-not-great
            # moments doesn't dump dozens of clips -- peak-locks still always
            # survive the cap, since those are guaranteed regardless.
            hours = max(1.0, total_duration / 3600)
            cap = max(len(locked), int(8 * hours))
            if len(ranked_result) > cap:
                locked_ids = {id(m) for m in locked}
                non_locked = [m for m in ranked_result if id(m) not in locked_ids]
                non_locked_sorted = sorted(non_locked, key=lambda m: m.score, reverse=True)
                keep_non_locked = non_locked_sorted[:max(0, cap - len(locked))]
                ranked_result = sorted(locked + keep_non_locked, key=lambda m: m.start)
                print(f"[vod] capped to {cap} clips for a {hours:.1f}-hour VOD (peak-locks always kept)")

            merged = ranked_result
        except Exception as e:
            print(f"[vod] ranking pass failed ({e}) -- skipping it and exporting every moment "
                  f"that already cleared its own bar instead of losing the whole job over it")

    merged = dedupe_clips(merged)  # Compare final reviewed scores before export.
    if Config.MAX_CLIPS:
        merged = sorted(merged, key=lambda c: c.score, reverse=True)[:Config.MAX_CLIPS]
    atomic_json(state_dir / "final_candidates.json", [vars(c) for c in merged])
    timings.record(final_ranking_seconds=pipeline_time.monotonic()-ranking_started,
                   final_candidates=len(merged), transcript_words=len(transcript.words))
    print("PROGRESS ranking 100", flush=True)
    render_started = pipeline_time.monotonic()
    from render_checkpoint import RenderCheckpoint
    render_cache = RenderCheckpoint(state_dir/'renders',
        {'aspect':Config.ASPECT_RATIO,'captions':Config.CAPTIONS_ENABLED,'tracking':Config.TRACKING_ENABLED}, transcript.words)
    print(f"[vod] producing {len(merged)} clips")

    if not Config.TRACKING_ENABLED:
        # Fast path: no face tracking needed, so skip the two-phase
        # cut-then-layout flow entirely and do a single ffmpeg pass per
        # clip (cut + static crop + captions + audio norm all at once).
        print(f"[vod] tracking is off -- single-pass encoding {len(merged)} clips...")
        print("PROGRESS encode 0", flush=True)
        done = [0]
        succeeded = 0

        def do_single_pass(i, c):
            check_cancelled()
            existing = render_cache.find(c)
            if existing: return existing
            local_source, local_start, local_end, local_words = _prepare_clip_source(
                c, transcript.words, work_dir, source_is_url, cut_source
            )
            final_path = clips_dir / f"{run_id}_clip_{i:03d}" / "clip.mp4"
            cut_single_pass(
                source_video=local_source, words=local_words,
                start=local_start, end=local_end, out_path=final_path,
                aspect_ratio=Config.ASPECT_RATIO, captions_enabled=Config.CAPTIONS_ENABLED,
            )
            window_text = " ".join(w.text.strip() for w in local_words)
            _generate_posts_with_fallback(
                window_text, channel, final_path.parent,
                standalone_score=(c.breakdown or {}).get("standalone"),
                safety_flag=c.safety_flag if Config.SAFETY_FLAGS_ENABLED else None,
            )
            score_data = {
                "score": c.score, "reason": c.reason,
                "breakdown": c.breakdown or {}, "safety_flag": c.safety_flag,
                "moment_score": getattr(c, "moment_score", None), "cut_score": getattr(c, "cut_score", None),
                "event_class": getattr(c, "moment_type", ""), "tightness": getattr(c, "tightness", None),
                "hook_at_seconds": getattr(c, "hook_at_seconds", None), "payoff_at_seconds": getattr(c, "payoff_at_seconds", None),
                "payoff_type": getattr(c, "payoff_type", "none"), "suggested_trim": getattr(c, "suggested_trim", ""),
            }
            score_data['editorial_review'] = getattr(c, 'editorial_review', None)
            if getattr(c, 'visual_review', ''):
                (final_path.parent/'visual_review.json').write_text(c.visual_review, encoding='utf-8')
            (final_path.parent / "score.json").write_text(json.dumps(score_data, indent=2))
            _save_recipe(
                final_path.parent, channel, local_source, local_start, local_end,
                aspect=Config.ASPECT_RATIO, track=False, layout="center",
                captions_enabled=Config.CAPTIONS_ENABLED, clip_words=local_words,
            )
            render_cache.save(c,final_path.parent)
            return final_path.parent

        with ThreadPoolExecutor(max_workers=Config.MAX_RENDER_CONCURRENCY) as executor:
            futures = {executor.submit(do_single_pass, i, c): i for i, c in enumerate(merged)}
            for future in as_completed(futures):
                i = futures[future]
                try:
                    clip_dir = future.result()
                    succeeded += 1
                    print(f"[vod] clip {i} ready: {clip_dir}")
                except Exception as e:
                    print(f"[vod] failed to finish clip {i}: {e}")
                done[0] += 1
                print(f"PROGRESS encode {int((done[0] / max(1, len(merged))) * 100)}")

        from export_status import finish_exports
        timings.record(render_seconds=pipeline_time.monotonic()-render_started, clips_produced=succeeded)
        finish_exports(succeeded, len(merged))
        return

    # Phase 1: cut + caption EVERY found clip first (original aspect
    # ratio, no cropping yet) -- this is "all clips found" before any
    # tracking work starts. Runs a few clips at once (ffmpeg subprocess
    # calls overlap fine) instead of one at a time.
    print(f"[vod] phase 1: cutting + captioning {len(merged)} clips (captions={Config.CAPTIONS_ENABLED})...")
    cut_clips = []  # (index, captioned_path, transcript_text, candidate)
    cut_done = [0]

    def do_cut(i, c):
        local_source, local_start, local_end, local_words = _prepare_clip_source(
            c, transcript.words, work_dir, source_is_url, cut_source
        )
        captioned_path = clips_dir / f"{run_id}_clip_{i:03d}" / "captioned.mp4"
        cut_and_caption(
            source_video=local_source, words=local_words,
            start=local_start, end=local_end, out_path=captioned_path,
            captions_enabled=False,
        )
        window_text = " ".join(w.text.strip() for w in local_words)
        return (i, captioned_path, window_text, c, local_source, local_start, local_end, local_words)

    with ThreadPoolExecutor(max_workers=Config.MAX_RENDER_CONCURRENCY) as executor:
        futures = {executor.submit(do_cut, i, c): i for i, c in enumerate(merged)}
        for future in as_completed(futures):
            i = futures[future]
            try:
                cut_clips.append(future.result())
            except Exception as e:
                print(f"[vod] failed to cut clip {i}: {e}")
            cut_done[0] += 1
            print(f"PROGRESS cut {int((cut_done[0] / max(1, len(merged))) * 100)}")

    cut_clips.sort(key=lambda x: x[0])

    # Phase 2: NOW apply layout (aspect ratio + face tracking, if
    # enabled) as its own pass over every clip that was found in phase 1.
    # Also run a few at once -- tracking/cropping and the title-generation
    # API call both benefit from overlapping instead of going one by one.
    print(f"[vod] phase 2: applying layout to {len(cut_clips)} clips (tracking={Config.TRACKING_ENABLED})...")
    layout_done = [0]
    succeeded = 0

    def do_layout(i, captioned_path, window_text, c, local_source, local_start, local_end, local_words):
        check_cancelled()
        existing = render_cache.find(c)
        if existing: return existing
        final_path = clips_dir / f"{run_id}_clip_{i:03d}" / "clip.mp4"
        from clip import re_render_from_recipe
        re_render_from_recipe(dict(source_video=str(local_source),start=local_start,end=local_end,
            aspect=Config.ASPECT_RATIO,track='auto',layout='follow',
            captions_enabled=Config.CAPTIONS_ENABLED),local_words,final_path)
        captioned_path.unlink(missing_ok=True)
        _generate_posts_with_fallback(
            window_text, channel, final_path.parent,
            standalone_score=(c.breakdown or {}).get("standalone"),
            safety_flag=c.safety_flag if Config.SAFETY_FLAGS_ENABLED else None,
        )

        score_data = {
            "score": c.score, "reason": c.reason,
            "breakdown": c.breakdown or {}, "safety_flag": c.safety_flag,
        }
        score_data['editorial_review'] = getattr(c, 'editorial_review', None)
        if getattr(c, 'visual_review', ''):
            (final_path.parent/'visual_review.json').write_text(c.visual_review, encoding='utf-8')
        (final_path.parent / "score.json").write_text(json.dumps(score_data, indent=2))
        _save_recipe(
            final_path.parent, channel, local_source, local_start, local_end,
            aspect=Config.ASPECT_RATIO, track=Config.TRACKING_ENABLED, layout="follow",
            captions_enabled=Config.CAPTIONS_ENABLED, clip_words=local_words,
        )
        render_cache.save(c,final_path.parent)
        return final_path.parent

    with ThreadPoolExecutor(max_workers=Config.MAX_RENDER_CONCURRENCY) as executor:
        futures = {
            executor.submit(do_layout, i, captioned_path, window_text, c, local_source, snapped_start, snapped_end, clip_words): i
            for i, captioned_path, window_text, c, local_source, snapped_start, snapped_end, clip_words in cut_clips
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                clip_dir = future.result()
                succeeded += 1
                print(f"[vod] clip {i} ready: {clip_dir}")
            except Exception as e:
                print(f"[vod] failed to finish clip {i}: {e}")
            layout_done[0] += 1
            print(f"PROGRESS layout {int((layout_done[0] / max(1, len(cut_clips))) * 100)}")

    from export_status import finish_exports
    timings.record(render_seconds=pipeline_time.monotonic()-render_started, clips_produced=succeeded)
    finish_exports(succeeded, len(merged))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process a Kick VOD into clips.")
    parser.add_argument("--input", required=True, help="Local video file path or VOD URL")
    parser.add_argument("--channel", required=True, help="Streamer name, used to namespace output")
    parser.add_argument("--start", type=float, default=None, help="Only process from this many seconds into the VOD")
    parser.add_argument("--end", type=float, default=None, help="Only process up to this many seconds into the VOD")
    parser.add_argument("--transcript", help="Explicit recovery: saved transcript for this exact source and time range")
    parser.add_argument("--checkpoint", help="Explicit recovery: saved found moments for this transcript")
    args = parser.parse_args()

    process_vod(args.input, args.channel, start_seconds=args.start, end_seconds=args.end, transcript_path=args.transcript, checkpoint=args.checkpoint)

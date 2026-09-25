"""
Processes a VOD via its direct stream URL (a .m3u8 link) instead of
downloading the whole file first. ffmpeg segments the stream into chunks
in the background while transcription, scoring, and clipping run on each
chunk as it arrives -- so clips start appearing within minutes, not after
a multi-hour download completes.

Get the .m3u8 URL from your browser: open the VOD, press F12, go to the
Network tab, filter by "Media", reload the page, let the video start
playing, and find the request ending in .m3u8 -- right-click it -> Copy
-> Copy link address. That's the URL this script wants.

This bypasses yt-dlp and Apify entirely: it reads the exact same direct
stream URL your browser itself uses to play the video, so it doesn't
depend on Kick's (currently broken) VOD metadata API at all.

Usage:
    python vod_stream.py --url "<m3u8 url>" --channel <name>
"""

import argparse
import time
import threading
import subprocess
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from config import Config
from capture import start_url_capture
from transcribe import Transcriber, Word
from detect import score_window
from clip import (
    cut_and_format_clip,
    concat_chunks,
    select_chunks_for_range,
    parse_chunk_start_time,
)
from generate_posts import generate_posts, write_posts_file
import json

POLL_SECONDS = 1
CHUNK_HISTORY = 3
# How long to wait with no new chunks before deciding the VOD is
# actually done (vs. just momentarily between chunks)
IDLE_SECONDS_BEFORE_DONE = 20


def probe_duration(stream_url: str) -> float | None:
    """
    Asks ffprobe for the VOD's total duration up front, so progress and
    ETA can be reported as processing goes -- without this we'd have no
    way to know what fraction of the video has been handled yet.
    Returns None if the probe fails (progress just won't show an ETA).
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", stream_url],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except Exception as e:
        print(f"[vod_stream] could not determine VOD duration ({e}) -- progress bar won't show an ETA")
        return None


def write_score_file(result, out_path):
    data = {
        "score": result.score, "tier": result.tier, "reason": result.reason,
        "breakdown": result.breakdown, "safety_flag": result.safety_flag,
    }
    out_path.write_text(json.dumps(data, indent=2))


def process_vod_stream(stream_url: str, channel: str, start_seconds: float = None, end_seconds: float = None):
    Config.validate()
    # Same fix as vod.py/main.py: the channel this specific job was
    # given always wins over whatever STREAMER_NAME/KICK_CHANNEL might
    # be sitting in .env.
    Config.STREAMER_NAME = channel
    Config.log_active_thresholds()
    channel_base = Path(Config.OUTPUT_DIR) / "vod_stream" / channel
    raw_dir = channel_base / "raw_chunks"
    clips_dir = channel_base / "clips"
    tmp_dir = channel_base / "tmp"
    raw_dir.mkdir(parents=True, exist_ok=True)
    clips_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    if start_seconds or end_seconds:
        # A specific range was requested -- that range length IS the
        # known duration for progress/ETA purposes, no need to probe the
        # full VOD (and this way progress reflects the requested clip,
        # not the whole multi-hour source).
        total_duration = (end_seconds or probe_duration(stream_url) or 0) - (start_seconds or 0)
        print(f"[vod_stream] processing requested range only: {start_seconds or 0:.0f}s-{end_seconds or '(end)'}s "
              f"({total_duration/60:.1f} min)")
    else:
        total_duration = probe_duration(stream_url)
        if total_duration:
            print(f"[vod_stream] VOD duration: {total_duration/60:.0f} min")

    # Run the one-shot segmenter in the background -- it exits on its own
    # once it reaches the end of the VOD (or the requested end time).
    capture_proc = start_url_capture(stream_url, Config.CHUNK_SECONDS, raw_dir, start_seconds, end_seconds)

    transcriber = Transcriber(model_size=Config.WHISPER_MODEL_SIZE, profile="vod")
    processed_chunks = set()
    word_buffer: list[Word] = []
    chunk_history: list[tuple[Path, datetime]] = []
    clips_produced = 0
    last_new_chunk_time = time.time()
    started_at = time.time()
    recent_samples = []  # (wall_clock_time, video_seconds_processed_so_far), rolling window

    def mark_chunk_done(chunk_path):
        """Marks a chunk processed and reports progress -- called from
        every exit path in the loop below (quiet chunk, safety-flagged
        chunk, or a fully scored/clipped chunk) so the progress bar and
        ETA stay accurate even during long quiet stretches, not just
        when a clip is actually produced.

        ETA is based on the RECENT processing rate (last ~8 chunks), not
        the lifetime average -- using the lifetime average makes the ETA
        drift upward if things slow down partway through (e.g. another
        job starts competing for CPU) instead of reflecting current pace.
        """
        processed_chunks.add(chunk_path)
        nonlocal_seconds[0] += Config.CHUNK_SECONDS

        now = time.time()
        recent_samples.append((now, nonlocal_seconds[0]))
        if len(recent_samples) > 8:
            recent_samples.pop(0)

        if total_duration and len(recent_samples) >= 2:
            pct = min(100, int((nonlocal_seconds[0] / total_duration) * 100))
            window_start_time, window_start_seconds = recent_samples[0]
            real_elapsed_in_window = now - window_start_time
            video_seconds_in_window = nonlocal_seconds[0] - window_start_seconds
            remaining_video_seconds = max(0.0, total_duration - nonlocal_seconds[0])

            if video_seconds_in_window > 0:
                current_rate = video_seconds_in_window / real_elapsed_in_window  # video-seconds processed per real second
                eta_seconds = int(remaining_video_seconds / current_rate) if current_rate > 0 else 0
                print(f"PROGRESS transcribe {pct} eta_seconds {eta_seconds}")

    nonlocal_seconds = [0.0]  # mutable box so the nested function above can update it
    clips_lock = threading.Lock()

    def process_window(chunk_path, window_text, window_words, chunk_history_snapshot, window_start_epoch, newest_epoch):
        """
        Scores a window and, if it qualifies, cuts the clip -- run on a
        background thread so the main loop can move straight on to
        transcribing the NEXT chunk instead of blocking on this window's
        API call and clip-cutting work. This is what actually closes most
        of the speed gap with real-time: transcription (CPU) and
        scoring+clipping (mostly network + some CPU) now overlap instead
        of happening strictly one after another.
        """
        nonlocal clips_produced
        try:
            result = score_window(window_text)

            if result is None:
                print("[vod_stream] window scoring failed, dropping this window")
                return

            print(f"[vod_stream] window score={result.score} reason={result.reason}")

            # Preserve sensitive moments; factual packaging is handled by the title writer.

            if result.score >= Config.CLIP_SCORE_THRESHOLD:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                clip_out = clips_dir / ts / "clip.mp4"
                concat_out = tmp_dir / f"{ts}_concat.mp4"

                clip_abs_start = datetime.fromtimestamp(window_start_epoch)
                clip_abs_end = datetime.fromtimestamp(newest_epoch)
                needed_chunks = select_chunks_for_range(
                    chunk_history_snapshot, clip_abs_start, clip_abs_end, Config.CHUNK_SECONDS
                )

                if needed_chunks:
                    try:
                        if len(needed_chunks) == 1:
                            source_video = needed_chunks[0]
                            concat_base_epoch = parse_chunk_start_time(needed_chunks[0]).timestamp()
                        else:
                            concat_chunks(needed_chunks, concat_out)
                            source_video = concat_out
                            concat_base_epoch = parse_chunk_start_time(needed_chunks[0]).timestamp()

                        clip_start = window_start_epoch - concat_base_epoch
                        clip_end = newest_epoch - concat_base_epoch
                        local_words = [
                            Word(text=w.text, start=w.start - concat_base_epoch, end=w.end - concat_base_epoch)
                            for w in window_words
                        ]

                        cut_and_format_clip(
                            source_video=source_video, words=local_words,
                            start=clip_start, end=clip_end, out_path=clip_out,
                        )
                        from vod import _save_recipe
                        import shutil
                        retained = clip_out.parent / "source.mp4"
                        shutil.copy2(source_video, retained)
                        _save_recipe(clip_out.parent, Config.KICK_CHANNEL, retained, clip_start, clip_end,
                                     Config.ASPECT_RATIO, Config.TRACKING_ENABLED, "follow", Config.CAPTIONS_ENABLED,
                                     local_words, mode="vod_stream")
                        posts = generate_posts(window_text, safety_flag=result.safety_flag if Config.SAFETY_FLAGS_ENABLED else None)
                        write_posts_file(posts, clip_out.parent / "posts.txt")
                        write_score_file(result, clip_out.parent / "score.json")
                        print(f"[vod_stream] clip ready: {clip_out.parent} (tier: {result.tier})")
                        with clips_lock:
                            clips_produced += 1
                    except Exception as e:
                        print(f"[vod_stream] failed to produce clip: {e}")
                    finally:
                        concat_out.unlink(missing_ok=True)
        except Exception as e:
            print(f"[vod_stream] window processing failed: {e}")

    print("[vod_stream] processing chunks as they arrive...")

    with ThreadPoolExecutor(max_workers=4) as scoring_executor:
        while True:
            if Config.MAX_CLIPS and clips_produced >= Config.MAX_CLIPS:
                print(f"[vod_stream] reached max clips ({Config.MAX_CLIPS}), stopping.")
                capture_proc.terminate()
                return

            chunks = sorted(raw_dir.glob("chunk_*.ts"))
            new_chunks = [c for c in chunks if c not in processed_chunks]
            capture_done = capture_proc.poll() is not None

            if new_chunks:
                last_new_chunk_time = time.time()

            for chunk_path in new_chunks:
                # Skip the newest chunk unless capture has fully finished --
                # it may still be mid-write.
                is_last_chunk_overall = chunk_path == chunks[-1]
                if is_last_chunk_overall and not capture_done:
                    continue

                chunk_start = parse_chunk_start_time(chunk_path)
                chunk_history.append((chunk_path, chunk_start))
                chunk_history[:] = chunk_history[-CHUNK_HISTORY:]

                print(f"[vod_stream] transcribing {chunk_path.name}")
                try:
                    transcript = transcriber.transcribe_chunk(chunk_path)
                except RuntimeError as e:
                    print(f"[vod_stream] WARNING skipping undecodable chunk {chunk_path.name}: {e}")
                    mark_chunk_done(chunk_path)
                    continue

                base_epoch = chunk_start.timestamp()
                for w in transcript.words:
                    word_buffer.append(Word(text=w.text, start=w.start + base_epoch, end=w.end + base_epoch))

                newest_epoch = base_epoch + Config.CHUNK_SECONDS
                cutoff = newest_epoch - (Config.DETECTION_WINDOW_SECONDS * 2)
                word_buffer[:] = [w for w in word_buffer if w.start >= cutoff]

                window_start_epoch = newest_epoch - Config.DETECTION_WINDOW_SECONDS
                window_words = [w for w in word_buffer if w.start >= window_start_epoch]
                window_text = " ".join(w.text.strip() for w in window_words)

                mark_chunk_done(chunk_path)

                if not window_text.strip():
                    continue

                # Snapshot chunk_history NOW -- it keeps mutating as more
                # chunks arrive, but this window's clip (if any) only
                # ever needs the chunks that existed at this moment.
                chunk_history_snapshot = list(chunk_history)
                scoring_executor.submit(
                    process_window, chunk_path, window_text, list(window_words),
                    chunk_history_snapshot, window_start_epoch, newest_epoch,
                )

            # Done when: capture process has exited AND nothing new has shown
            # up for a while (gives the last chunk time to finish writing).
            if capture_done and (time.time() - last_new_chunk_time) > IDLE_SECONDS_BEFORE_DONE:
                print("[vod_stream] waiting for remaining background clip work to finish...")
                scoring_executor.shutdown(wait=True)
                print(f"[vod_stream] VOD fully processed -- {clips_produced} clips produced")
                print("PROGRESS done 100")
                return

            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process a Kick VOD via its direct .m3u8 stream URL.")
    parser.add_argument("--url", required=True, help="Direct .m3u8 (or other ffmpeg-readable) stream URL")
    parser.add_argument("--channel", required=True, help="Streamer name, used to namespace output")
    parser.add_argument("--start", type=float, default=None, help="Only process from this many seconds into the VOD")
    parser.add_argument("--end", type=float, default=None, help="Only process up to this many seconds into the VOD")
    args = parser.parse_args()

    process_vod_stream(args.url, args.channel, start_seconds=args.start, end_seconds=args.end)

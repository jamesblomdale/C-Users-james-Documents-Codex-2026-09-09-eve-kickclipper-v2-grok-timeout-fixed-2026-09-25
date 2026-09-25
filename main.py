"""
Orchestrates the full pipeline:

  capture (separate process) -> transcribe each new chunk -> maintain a
  rolling transcript buffer (in real wall-clock time) -> score windows as
  CANDIDATES, buffer them, merge nearby ones into full moments once the
  story goes quiet -> cut + caption + crop only the merged, re-scored
  clips that clear the real threshold -> generate posts

Chunk timestamps come from the filename (capture.py writes them with
wall-clock start times), so a clip that crosses a chunk boundary gets
built from however many source chunks it actually needs, instead of
being cut wrong or dropped.

This brings live mode to the same standard vod.py already has: a single
window immediately hitting the threshold used to trigger an instant cut,
which is exactly why a real moment split across 3-4 chunks (each scoring
2-3 on its own) never became a clip -- nothing ever assembled the full
scene. Now windows only need to clear the much looser CANDIDATE
threshold to get buffered; the real CLIP_SCORE_THRESHOLD gate is applied
to the MERGED, re-scored span once the story appears to have wrapped up.

Flush timing is measured in STREAM time (the timestamps on the actual
candidates), not wall-clock time. Wall-clock timing would drift under
processing lag -- if a chunk takes 30 real seconds to transcribe under
CPU load, a wall-clock gap check could flush mid-story even though the
stream itself only advanced a few seconds, or fail to flush a genuine
20-second stream-silence because processing caught up quickly right
after. Stream time is anchored to `newest_epoch`, updated every chunk
whether or not it produces a candidate.
"""

import time
import multiprocessing
from pathlib import Path
from datetime import datetime

from config import Config
from capture import run_forever as run_capture
from transcribe import Transcriber, Word
from detect import score_window
from moments import Candidate, merge_candidates, dedupe_clips
from clip import (
    cut_and_format_clip,
    concat_chunks,
    select_chunks_for_range,
    parse_chunk_start_time,
)
from generate_posts import generate_posts, write_posts_file

import json


def write_score_file(result_score, result_tier, result_reason, result_breakdown, result_safety_flag, out_path):
    """Writes the full weighted score breakdown alongside the clip, so
    the UI can show why a clip scored what it did, not just a number."""
    data = {
        "score": result_score,
        "tier": result_tier,
        "reason": result_reason,
        "breakdown": result_breakdown,
        "safety_flag": result_safety_flag,
    }
    out_path.write_text(json.dumps(data, indent=2))


POLL_SECONDS = 1
# Keep enough recent chunk history in memory to cover a merged clip that
# spans several chunks -- generous on purpose, since a slow-building
# story can span more chunks than a single detection window ever would.
# These are just (path, datetime) references, not file contents, so
# keeping more of them costs almost nothing.
CHUNK_HISTORY = 20
# Three rules together decide when a pending story has wrapped up and
# should be merged + exported. All measured in STREAM time (see module
# docstring), not wall-clock.
FLUSH_GAP_SECONDS = 18        # no new candidate for this long (in stream time) -> probably over
FLUSH_MAX_WAIT_SECONDS = 90   # never hold a candidate group open longer than this,
                               # measured from the FIRST candidate's start to the latest stream position


def _tier_for_score(score: float) -> str:
    # 0-100 scale, matching detect.py's DetectionResult.tier property.
    if score >= 90:
        return "Post immediately"
    if score >= 75:
        return "Strong, post today"
    if score >= 60:
        return "Decent, good for volume"
    if score >= 40:
        return "Only if you need filler"
    return "Skip"


def _cut_and_export(candidate: Candidate, all_words: list[Word], chunk_history, clips_dir, tmp_dir) -> bool:
    """
    Cuts, captions, crops, and writes posts for ONE merged candidate
    (epoch-second start/end). Returns True if a clip was actually
    produced. This is the same cutting logic that used to run inline
    per-window -- pulled out so it can be called on a MERGED span
    instead of a single raw window.

    all_words is the full live word buffer -- words for this candidate's
    exact span are filtered from it directly (the same approach vod.py
    uses), rather than trying to carry a word list through the merge
    step, since merge_candidates() legitimately builds brand-new
    Candidate objects that don't retain arbitrary extra attributes.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    clip_out = clips_dir / ts / "clip.mp4"
    concat_out = tmp_dir / f"{ts}_concat.mp4"

    clip_abs_start = datetime.fromtimestamp(candidate.start)
    clip_abs_end = datetime.fromtimestamp(candidate.end)

    needed_chunks = select_chunks_for_range(
        chunk_history, clip_abs_start, clip_abs_end, Config.CHUNK_SECONDS
    )
    if not needed_chunks:
        print(f"[main] no chunks on disk cover the merged span "
              f"{clip_abs_start.strftime('%H:%M:%S')}-{clip_abs_end.strftime('%H:%M:%S')}, skipping")
        return False

    span_words = [w for w in all_words if candidate.start <= w.start <= candidate.end]
    if not span_words:
        # No words survive for this exact span -- the word buffer trim
        # or a transcription gap ate them. Cutting a clip with no words
        # means no captions and a broken posts.txt -- skip rather than
        # produce a silently-broken clip.
        print(f"[main] no words available for the merged span "
              f"{clip_abs_start.strftime('%H:%M:%S')}-{clip_abs_end.strftime('%H:%M:%S')}, skipping")
        return False

    try:
        if len(needed_chunks) == 1:
            source_video = needed_chunks[0]
            concat_base_epoch = parse_chunk_start_time(needed_chunks[0]).timestamp()
        else:
            concat_chunks(needed_chunks, concat_out)
            source_video = concat_out
            concat_base_epoch = parse_chunk_start_time(needed_chunks[0]).timestamp()

        clip_start = candidate.start - concat_base_epoch
        clip_end = candidate.end - concat_base_epoch

        local_words = [
            Word(text=w.text, start=w.start - concat_base_epoch, end=w.end - concat_base_epoch)
            for w in span_words
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
                     local_words, mode="live")
        posts = generate_posts(
            candidate.text,
            standalone_score=candidate.breakdown.get("standalone"),
            safety_flag=candidate.safety_flag if Config.SAFETY_FLAGS_ENABLED else None,
        )
        write_posts_file(posts, clip_out.parent / "posts.txt")
        write_score_file(
            candidate.score, _tier_for_score(candidate.score), candidate.reason,
            candidate.breakdown, candidate.safety_flag, clip_out.parent / "score.json",
        )
        print(f"[main] clip ready: {clip_out.parent} (tier: {_tier_for_score(candidate.score)})")
        return True
    except Exception as e:
        print(f"[main] failed to produce clip: {e}")
        return False
    finally:
        concat_out.unlink(missing_ok=True)


def watch_and_process():
    Config.validate()
    # Same fix as vod.py: make the actual channel this job is watching
    # win over whatever STREAMER_NAME might be sitting in .env, so a
    # stale/explicit .env value can't silently produce wrong-name
    # titles ("Broner/Deen on an N3ON job") on a job it wasn't meant for.
    Config.STREAMER_NAME = Config.KICK_CHANNEL
    Config.log_active_thresholds()
    channel_base = Path(Config.OUTPUT_DIR) / "live" / Config.KICK_CHANNEL
    raw_dir = channel_base / "raw_chunks"
    clips_dir = channel_base / "clips"
    tmp_dir = channel_base / "tmp"
    raw_dir.mkdir(parents=True, exist_ok=True)
    clips_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    transcriber = Transcriber(model_size=Config.WHISPER_MODEL_SIZE, profile="live")

    processed_chunks = set()
    word_buffer: list[Word] = []  # absolute epoch-second timestamps
    chunk_history: list[tuple[Path, datetime]] = []  # (path, wall-clock start)
    clips_produced = 0

    pending_candidates: list[Candidate] = []
    latest_stream_time = None  # newest_epoch as of the most recently processed chunk,
                                 # whether or not that chunk produced a candidate

    def flush_pending(reason: str):
        """Merges everything currently buffered into full moments and
        exports whatever merge_candidates() already decided clears the
        bar -- that function handles the full keep/drop decision
        internally (normal threshold, peak-preservation, and the lower
        bar for high-value moment types), so re-filtering its output
        here with a flat score check would silently undo exactly those
        exceptions. Clears the buffer either way -- anything genuinely
        below every bar is correctly dropped by merge_candidates() itself."""
        nonlocal clips_produced
        if not pending_candidates:
            return

        print(f"[main] {len(pending_candidates)} pending candidate(s) -- merging (reason: {reason})...")
        merged_all = merge_candidates(pending_candidates)
        merged = dedupe_clips(merged_all)

        for m in merged:
            if Config.MAX_CLIPS and clips_produced >= Config.MAX_CLIPS:
                break
            if _cut_and_export(m, word_buffer, chunk_history, clips_dir, tmp_dir):
                clips_produced += 1

        pending_candidates.clear()

    print("[main] watching for new chunks...")

    while True:
        if Config.MAX_CLIPS and clips_produced >= Config.MAX_CLIPS:
            print(f"[main] reached max clips ({Config.MAX_CLIPS}), stopping.")
            return

        chunks = sorted(raw_dir.glob("chunk_*.ts"))
        new_chunks = [c for c in chunks if c not in processed_chunks]

        for chunk_path in new_chunks:
            # Skip the most recent chunk -- it may still be being written to
            if chunk_path == chunks[-1]:
                continue

            chunk_start = parse_chunk_start_time(chunk_path)
            chunk_history.append((chunk_path, chunk_start))
            if len(chunk_history) > CHUNK_HISTORY:
                # Anything falling out of chunk_history is provably
                # unreachable by _cut_and_export from this point on --
                # it's the ONLY lookup table cutting ever uses (see
                # select_chunks_for_range above), and CHUNK_HISTORY's
                # window (600s at default settings) is already far
                # bigger than the longest a candidate can wait before
                # being flushed (FLUSH_MAX_WAIT_SECONDS + a detection
                # window, ~165s) -- so a chunk aging out here can never
                # still be needed by a pending clip. Deleting it here is
                # what keeps a live job's disk usage bounded instead of
                # growing for as long as the stream runs; before this,
                # every chunk was kept forever (a multi-day session
                # could -- and did -- fill the whole disk).
                evicted = chunk_history[:-CHUNK_HISTORY]
                chunk_history[:] = chunk_history[-CHUNK_HISTORY:]
                for old_path, _ in evicted:
                    old_path.unlink(missing_ok=True)

            print(f"[main] transcribing {chunk_path.name}")
            try:
                transcript = transcriber.transcribe_chunk(chunk_path)
            except RuntimeError as e:
                print(f"[main] WARNING skipping undecodable chunk {chunk_path.name}: {e}")
                processed_chunks.add(chunk_path)
                continue

            # Shift word timestamps to absolute epoch seconds
            base_epoch = chunk_start.timestamp()
            for w in transcript.words:
                word_buffer.append(
                    Word(text=w.text, start=w.start + base_epoch, end=w.end + base_epoch)
                )

            # Trim buffer to keep only the last ~2x detection window
            newest_epoch = base_epoch + Config.CHUNK_SECONDS
            latest_stream_time = newest_epoch  # advances every chunk, candidate or not
            cutoff = newest_epoch - (Config.DETECTION_WINDOW_SECONDS * 2)
            word_buffer[:] = [w for w in word_buffer if w.start >= cutoff]

            window_start_epoch = newest_epoch - Config.DETECTION_WINDOW_SECONDS
            window_words = [w for w in word_buffer if w.start >= window_start_epoch]
            window_text = " ".join(w.text.strip() for w in window_words)

            if not window_text.strip():
                print(f"[main] {chunk_path.name} had no usable speech, skipping")
                processed_chunks.add(chunk_path)
                continue

            result = score_window(window_text)

            if result is None:
                # API failure or unparseable response -- drop this window
                # rather than guessing a score for it.
                print("[main] window scoring failed, dropping this window")
                processed_chunks.add(chunk_path)
                continue

            breakdown_str = ", ".join(f"{k}={v}" for k, v in result.breakdown.items())
            print(f"[main] window score={result.score} ({breakdown_str}) reason={result.reason}")

            # safety_flag is carried through on the Candidate below but no
            # longer affects whether this window gets kept -- it used to
            # skip the window outright here, which meant a genuinely
            # strong, high-judgment moment (a legal situation, a real
            # incident) could get thrown away entirely just for being
            # flagged. Rating is now purely score-based; safety_flag's
            # only remaining job is telling generate_posts.py to use a
            # plain, factual tone instead of the usual clickbait formula.

            # Deliberately loose gate here -- a candidate is allowed to
            # be an incomplete fragment (pure setup, or a payoff whose
            # setup was in an earlier window). The real quality gate is
            # applied to the MERGED span in flush_pending() above.
            #
            # NOTE: an earlier version of this also force-flushed on a
            # detected topic change, but testing topic_overlap() directly
            # showed it returns 0.0 even for clearly-continuous stories
            # ("car stuck" -> "still no keys") purely because short topic
            # labels get worded differently window to window. Using that
            # as an immediate trigger risked recreating the exact
            # fragmentation bug this whole buffer/merge system was built
            # to fix. The two stream-time rules below (gap + max-wait)
            # are reliable and deterministic; merge_candidates() still
            # applies topic+gap grouping correctly at flush time using
            # the same signal, just combined with the time-gap check
            # rather than trusted alone.
            if result.score >= Config.CANDIDATE_SCORE_THRESHOLD:
                candidate = Candidate(
                    start=window_start_epoch, end=newest_epoch, text=window_text,
                    score=result.score, reason=result.reason, topic=result.topic,
                    safety_flag=result.safety_flag,
                    breakdown=result.breakdown, quote=result.quote,
                )
                pending_candidates.append(candidate)

            processed_chunks.add(chunk_path)

        if pending_candidates and latest_stream_time is not None:
            last_candidate_end = pending_candidates[-1].end
            first_candidate_start = pending_candidates[0].start

            gone_quiet = (latest_stream_time - last_candidate_end) > FLUSH_GAP_SECONDS
            waited_too_long = (latest_stream_time - first_candidate_start) > FLUSH_MAX_WAIT_SECONDS

            if gone_quiet:
                flush_pending(reason=f"quiet for {FLUSH_GAP_SECONDS}s (stream time)")
            elif waited_too_long:
                flush_pending(reason=f"held open {FLUSH_MAX_WAIT_SECONDS}s (stream time), flushing regardless")

        time.sleep(POLL_SECONDS)


def main():
    Config.validate()
    raw_dir = Path(Config.OUTPUT_DIR) / "live" / Config.KICK_CHANNEL / "raw_chunks"

    capture_proc = multiprocessing.Process(
        target=run_capture,
        args=(Config.LIVE_SOURCE or f"https://kick.com/{Config.KICK_CHANNEL}", Config.CHUNK_SECONDS, raw_dir),
    )
    capture_proc.start()

    try:
        watch_and_process()
    except KeyboardInterrupt:
        print("[main] shutting down")
        capture_proc.terminate()


if __name__ == "__main__":
    main()

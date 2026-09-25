"""
Pulls the live Kick stream into rolling video chunks on disk using streamlink.

Kick channels are watched at https://kick.com/<channel> — streamlink supports
Kick out of the box as of recent versions. Each chunk is a self-contained
.mp4 file of CHUNK_SECONDS length, named by its start timestamp, so
downstream steps (transcribe/clip) can process them independently while
capture keeps running.
"""

import subprocess
import time
import os
from pathlib import Path
from config import Config


def start_capture(source: str, chunk_seconds: int, out_dir: Path):
    """
    Launches streamlink piping into ffmpeg, which segments the live feed
    into fixed-length chunks. Runs until the process is killed or the
    stream ends.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    chunk_pattern = str(out_dir / "chunk_%Y%m%d_%H%M%S.ts")

    # streamlink resolves the live playlist URL for us; we pipe its stdout
    # into ffmpeg which handles the segmenting.
    streamlink_cmd = [
        "streamlink",
        source,
        "best",
        "--stdout",
        "--retry-streams", "10",
        "--retry-max", "0",  # retry indefinitely if the stream drops
    ]

    ffmpeg_cmd = [
        "ffmpeg",
        "-fflags", "+genpts+discardcorrupt",  # regenerates timestamps across the
                                                # discontinuities live HLS reload
                                                # skips create, and discards a
                                                # corrupt packet instead of letting
                                                # it poison the whole chunk
        "-i", "pipe:0",
        "-c", "copy",
        "-f", "segment",
        "-segment_format", "mpegts",  # native AAC support, avoids the ADTS
                                       # bitstream-filter conversion that .mp4
                                       # segmenting requires at every boundary
                                       # -- that conversion step is a known
                                       # source of audio corruption right at
                                       # segment edges.
        "-segment_time", str(chunk_seconds),
        "-reset_timestamps", "1",
        "-avoid_negative_ts", "make_zero",
        "-strftime", "1",
        "-loglevel", "warning",  # suppress the repetitive frame=/fps=/time= progress spam
        chunk_pattern,
    ]

    print(f"[capture] watching {source}, {chunk_seconds}s chunks -> {out_dir}")

    streamlink_proc = subprocess.Popen(streamlink_cmd, stdout=subprocess.PIPE)
    ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdin=streamlink_proc.stdout)

    return streamlink_proc, ffmpeg_proc


def start_url_capture(stream_url: str, chunk_seconds: int, out_dir: Path,
                       start_seconds: float = None, end_seconds: float = None):
    """
    Segments a direct video/HLS stream URL (e.g. a .m3u8 link grabbed from
    browser DevTools) into chunks, the same way start_capture() does for
    live Kick channels -- but reading straight from the URL instead of
    going through streamlink. Used for VOD processing so chunks can start
    being transcribed/scored while the rest of the VOD is still being
    read, instead of waiting for a full download first.

    If start_seconds/end_seconds are given, only that portion of the
    source is read at all -- this is the real speed win for "just
    process this 10-minute part," since the rest of the VOD is never
    downloaded or transcribed in the first place, not just skipped
    afterward.

    Unlike live capture, this is a ONE-SHOT run: ffmpeg naturally exits
    once it reaches the end of the VOD (or the requested end time).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    chunk_pattern = str(out_dir / "chunk_%Y%m%d_%H%M%S.ts")

    ffmpeg_cmd = ["ffmpeg"]
    if start_seconds:
        ffmpeg_cmd += ["-ss", str(start_seconds)]  # before -i: fast input-side seek
    ffmpeg_cmd += ["-i", stream_url]
    if end_seconds and start_seconds is not None:
        duration = max(1, end_seconds - start_seconds)
        ffmpeg_cmd += ["-t", str(duration)]
    elif end_seconds:
        ffmpeg_cmd += ["-t", str(end_seconds)]

    ffmpeg_cmd += [
        "-c", "copy",
        "-f", "segment",
        "-segment_format", "mpegts",  # native AAC support, avoids the ADTS
                                       # bitstream-filter conversion that .mp4
                                       # segmenting requires at every boundary
        "-segment_time", str(chunk_seconds),
        "-reset_timestamps", "1",
        "-strftime", "1",
        "-loglevel", "warning",  # suppress the repetitive frame=/fps=/time= progress spam
        chunk_pattern,
    ]

    range_desc = ""
    if start_seconds or end_seconds:
        range_desc = f" (range: {start_seconds or 0:.0f}s-{end_seconds or '?'}s)"
    print(f"[capture] reading VOD from direct URL{range_desc}, {chunk_seconds}s chunks -> {out_dir}")
    return subprocess.Popen(ffmpeg_cmd)


def run_forever(source: str, chunk_seconds: int, out_dir: Path):
    """
    Supervises the capture process, restarting it if it dies (e.g. the
    streamer goes offline and comes back later).
    """
    while True:
        streamlink_proc, ffmpeg_proc = start_capture(source, chunk_seconds, out_dir)
        ffmpeg_proc.wait()
        streamlink_proc.wait()
        print("[capture] stream ended or dropped, retrying in 30s...")
        time.sleep(30)


if __name__ == "__main__":
    Config.validate()
    out_dir = Path(Config.OUTPUT_DIR) / "raw_chunks"
    run_forever(Config.LIVE_SOURCE or f"https://kick.com/{Config.KICK_CHANNEL}", Config.CHUNK_SECONDS, out_dir)

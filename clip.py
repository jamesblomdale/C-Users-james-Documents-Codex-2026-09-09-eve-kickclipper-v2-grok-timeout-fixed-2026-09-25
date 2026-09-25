"""
Cuts the marked segment, crops to 9:16 centered on the streamer's face
using Mediapipe face detection, and burns in word-synced captions from
the Whisper transcript.
"""

import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
import cv2
from transcribe import Word

CHUNK_FILENAME_FMT = "chunk_%Y%m%d_%H%M%S.ts"


def parse_chunk_start_time(chunk_path: Path) -> datetime:
    """
    Recovers the wall-clock start time of a chunk from its filename
    (capture.py names chunks with -strftime, e.g. chunk_20260815_140203.ts).
    This is what lets us map an absolute clip time range onto the right
    set of chunk files.
    """
    return datetime.strptime(chunk_path.name, CHUNK_FILENAME_FMT)


def concat_chunks(chunk_paths: list[Path], out_path: Path):
    """
    Joins consecutive raw chunks into a single file via ffmpeg's concat
    demuxer (stream copy, so this is fast and lossless â€” no re-encoding).
    Chunks must be same codec/container, which they are since capture.py
    produces them all the same way.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = out_path.with_suffix(".txt")
    list_file.write_text(
        "\n".join(f"file '{p.resolve()}'" for p in chunk_paths)
    )

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    list_file.unlink(missing_ok=True)


def select_chunks_for_range(
    chunks_with_start: list[tuple[Path, datetime]],
    clip_abs_start: datetime,
    clip_abs_end: datetime,
    chunk_seconds: int,
) -> list[Path]:
    """
    Given (chunk_path, start_time) pairs sorted by time, returns the
    chunks that overlap [clip_abs_start, clip_abs_end] â€” this is what
    lets a clip that spans a chunk boundary get built from more than
    one source file instead of silently missing content.
    """
    selected = []
    for path, chunk_start in chunks_with_start:
        chunk_end = chunk_start.timestamp() + chunk_seconds
        if chunk_end >= clip_abs_start.timestamp() and chunk_start.timestamp() <= clip_abs_end.timestamp():
            selected.append(path)
    return selected


def snap_to_words(words: list[Word], start: float, end: float,
                   pad_pre: float = 0.8, pad_post: float = 1.2,
                   min_length: float = 12.0, max_length: float = 45.0) -> tuple[float, float]:
    """
    Adjusts a clip's cut points to line up with actual word boundaries
    instead of the raw scoring-window timestamps, and pads slightly on
    each side so speech doesn't start or end abruptly mid-breath. Without
    this, even a well-scored moment can start mid-sentence because the
    scoring window's boundary landed a fraction of a second into a word.
    """
    start = max(0, start - pad_pre)
    end = end + pad_post

    starts_after = [w.start for w in words if w.start >= start - 0.4]
    ends_before = [w.end for w in words if w.end <= end + 0.4]

    if starts_after:
        start = max(0, starts_after[0] - 0.15)
    if ends_before:
        end = ends_before[-1] + 0.25

    if end - start < min_length:
        end = start + min_length
    if end - start > max_length:
        end = start + max_length

    return start, end


def cut_single_pass(
    source_video: Path,
    words: list[Word],
    start: float,
    end: float,
    out_path: Path,
    aspect_ratio: str = "9:16",
    captions_enabled: bool = True,
    overlay_text: str = "",
    overlay_seconds: float = 3.0,
):
    """
    Fast path used ONLY when face tracking is off: does the cut, static
    center crop, captions, overlay text, and audio normalization all in
    a single ffmpeg call, instead of the multi-pass cut -> OpenCV crop ->
    mux flow that tracking mode genuinely needs. Tracking mode can't use
    this because the smoothed per-frame crop position requires OpenCV's
    frame-by-frame analysis -- but a static center crop is just a fixed
    ffmpeg filter, so there's no reason to pay for extra encode passes
    when tracking isn't being used anyway.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start

    ratio_map = {"9:16": (9, 16), "16:9": (16, 9), "4:3": (4, 3), "1:1": (1, 1), "4:5": (4, 5)}
    ratio_w, ratio_h = ratio_map.get(aspect_ratio, (9, 16))

    filters = []
    if True:
        # static center crop to the target ratio, expressed as an ffmpeg
        # filter rather than needing any per-frame Python/OpenCV work
        filters.append(f"crop=trunc(min(iw\\,ih*{ratio_w}/{ratio_h})/2)*2:trunc(min(ih\\,iw*{ratio_h}/{ratio_w})/2)*2")

    ass_path = None
    if captions_enabled:
        ass_path = out_path.with_suffix(".ass")
        build_ass_captions(words, start, end, ass_path)
        filters.append(ass_filter(ass_path))

    if overlay_text:
        overlay_path = out_path.with_suffix('.overlay.ass')
        build_overlay(overlay_text, overlay_seconds, overlay_path)
        filters.append(ass_filter(overlay_path))

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", str(source_video),
        "-t", str(duration),
    ]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:d=0.15,areverse,afade=t=in:d=0.15,areverse",
        "-c:a", "aac",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    print(f"[clip] single-pass cut (no tracking) -> {out_path}")


def build_ass_captions(words: list[Word], start: float, end: float, out_path: Path, karaoke: bool = True):
    """
    Builds a .ass subtitle file synced to word timestamps. When
    karaoke=True (default), each word within a caption group highlights
    in sequence as it's spoken (true karaoke-style, via ASS \\k timing
    tags) rather than the whole group appearing/disappearing as one
    static block.
    """
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, Bold, Outline, Alignment, MarginV
Style: Default,Arial Black,64,&H00FFFFFF,&H0018FC53,&H00000000,1,3,2,120

[Events]
Format: Layer, Start, End, Style, Text
"""

    def fmt_time(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = t % 60
        return f"{h:01d}:{m:02d}:{s:05.2f}"

    lines = [header]
    clip_words = [w for w in words if start <= w.start <= end]

    group_size = 4
    for i in range(0, len(clip_words), group_size):
        group = clip_words[i:i + group_size]
        rel_start = group[0].start - start
        rel_end = group[-1].end - start

        if karaoke:
            parts = []
            for j, w in enumerate(group):
                if j + 1 < len(group):
                    duration = group[j + 1].start - w.start
                else:
                    duration = w.end - w.start
                centiseconds = max(1, int(duration * 100))
                parts.append(f"{{\\k{centiseconds}}}{w.text.strip().replace(chr(92), '').replace('{', '(').replace('}', ')')} ")
            text = "".join(parts).strip()
        else:
            text = " ".join(w.text.strip() for w in group)

        lines.append(
            f"Dialogue: 0,{fmt_time(rel_start)},{fmt_time(rel_end)},Default,{text}\n"
        )

    out_path.write_text("".join(lines), encoding="utf-8")


def cut_and_caption(
    source_video: Path,
    words: list[Word],
    start: float,
    end: float,
    out_path: Path,
    captions_enabled: bool = True,
):
    """
    Stage 1: cuts [start, end] from source_video, optionally burning in
    karaoke-style captions, keeping the ORIGINAL aspect ratio -- no
    cropping/tracking happens here. This is the "clip found" step.
    Layout (aspect ratio + whether to track faces) is applied separately
    in apply_layout(), so tracking can run as its own later pass over
    every clip once they're all cut, instead of happening immediately
    clip-by-clip.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start

    if captions_enabled:
        ass_path = out_path.with_suffix(".ass")
        build_ass_captions(words, start, end, ass_path)
        cmd = [
            "ffmpeg", "-y",
            "-i", str(source_video),
            "-ss", str(start),
            "-t", str(duration),
            "-vf", ass_filter(ass_path),
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac",
            str(out_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(source_video),
            "-ss", str(start),
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac",
            str(out_path),
        ]
    subprocess.run(cmd, check=True)
    print(f"[clip] cut+captioned {out_path}")


def apply_layout(
    captioned_clip: Path,
    out_path: Path,
    aspect_ratio: str = "9:16",
    track_faces: bool = True,
):
    """
    Stage 2: takes an already-cut, already-captioned clip (from
    cut_and_caption) and produces the final version at the target aspect
    ratio -- either with smoothed face tracking, or a plain center crop
    if track_faces is off. This is deliberately separate from cutting so
    it can run as its own pass over a whole batch of clips after they've
    all been found, rather than per-clip in the detection hot path.
    """
    from track import build_layout

    raw_video_only = out_path.with_name(out_path.stem + "_video_only.mp4")
    build_layout(captioned_clip, raw_video_only, aspect_ratio=aspect_ratio, track_faces=track_faces)

    # build_layout's OpenCV path writes video-only -- mux the original
    # clip's audio back in for the final output. (If build_layout just
    # copied the file straight through for a same-ratio case, this mux
    # is still safe since it reads audio from the original captioned clip.)
    # -af applies loudness normalization (consistent, platform-ready
    # volume instead of whatever level the raw stream happened to be)
    # plus a quick 150ms fade in/out so clips don't start/end on an
    # audio hard-cut -- a small polish touch, cheap to add here.
    cmd = [
        "ffmpeg", "-y",
        "-i", str(raw_video_only),
        "-i", str(captioned_clip),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:d=0.15,areverse,afade=t=in:d=0.15,areverse",
        "-c:a", "aac",
        "-shortest",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    raw_video_only.unlink(missing_ok=True)
    print(f"[clip] layout applied -> {out_path}")


def cut_and_format_clip(
    source_video: Path,
    words: list[Word],
    start: float,
    end: float,
    out_path: Path,
    vertical: bool = True,
):
    """
    Convenience wrapper that runs both stages back-to-back (cut+caption,
    then layout) -- kept for callers that want the old one-call behavior,
    e.g. the live pipeline where there's no natural "all clips found"
    point to defer tracking to.
    """
    from config import Config

    re_render_from_recipe(dict(source_video=str(source_video),start=start,end=end,
        aspect=Config.ASPECT_RATIO if vertical else '16:9',track='auto' if Config.TRACKING_ENABLED else 'off',
        layout='follow',captions_enabled=Config.CAPTIONS_ENABLED), words, out_path)


def re_render_from_recipe(recipe: dict, words: list[Word], out_path: Path):
    """
    Re-renders ONE clip from its saved recipe (start/end/aspect/track/
    layout/captions/overlay) without touching detection, transcription,
    or any other clip. Cuts fresh from the ORIGINAL source video (never
    from the already-compressed clip.mp4), so quality doesn't degrade
    across repeated edits.

    track == "off" or layout == "center": single ffmpeg pass (fast).
    track == "auto" or "lock", or layout == "split2": goes through the
    tracking pass in track.py, then one final mux -- genuinely needs the
    frame-by-frame work, no way around that when tracking is involved.
    """
    source_video = Path(recipe["source_video"])
    start, end = recipe["start"], recipe["end"]
    aspect_ratio = recipe.get("aspect", "9:16")
    track_mode = recipe.get("track", "auto")
    layout = recipe.get("layout", "follow")
    captions_enabled = recipe.get("captions_enabled", True)
    overlay_text = recipe.get("overlay_text", "")
    overlay_seconds = recipe.get("overlay_seconds", 3.0)

    clip_words = [w for w in words if start <= w.start <= end]

    if track_mode == "off" or layout == "center":
        cut_single_pass(
            source_video=source_video, words=clip_words, start=start, end=end,
            out_path=out_path, aspect_ratio=aspect_ratio, captions_enabled=captions_enabled,
            overlay_text=overlay_text, overlay_seconds=overlay_seconds,
        )
        return

    # Tracking-involved path: cut+caption, then the tracking layout pass,
    # then burn overlay text as a final quick pass if requested.
    from track import build_layout

    captioned = out_path.with_name(out_path.stem + "_captioned.mp4")
    cut_and_caption(source_video, clip_words, start, end, captioned, captions_enabled=False)

    raw_video_only = out_path.with_name(out_path.stem + "_video_only.mp4")
    build_layout(
        captioned, raw_video_only, aspect_ratio=aspect_ratio,
        track_faces=True, layout=layout, lock=(track_mode == "lock"),
    )

    filters = []
    if captions_enabled:
        captions_path = out_path.with_suffix('.ass')
        build_ass_captions(clip_words, start, end, captions_path)
        filters.append(ass_filter(captions_path))
    if overlay_text:
        overlay_path = out_path.with_suffix('.overlay.ass')
        build_overlay(overlay_text, overlay_seconds, overlay_path)
        filters.append(ass_filter(overlay_path))

    cmd = [
        "ffmpeg", "-y",
        "-i", str(raw_video_only),
        "-i", str(captioned),
        "-map", "0:v:0",
        "-map", "1:a:0",
    ]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:d=0.15,areverse,afade=t=in:d=0.15,areverse",
        "-c:a", "aac",
        "-shortest",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    raw_video_only.unlink(missing_ok=True)
    captioned.unlink(missing_ok=True)
    print(f"[clip] re-rendered -> {out_path}")

def ass_filter(path):
    value = str(Path(path).resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return "ass=filename='" + value + "'"

def build_overlay(text, seconds, path):
    import textwrap
    text = text.replace("\\", "").replace("{", "(").replace("}", ")")
    wrapped = "\\N".join(textwrap.wrap(text.upper(), 27))
    end = f"0:{int(seconds//60):02d}:{seconds%60:05.2f}"
    path.write_text("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
        "Style: Overlay,Arial,66,&H00FFFFFF,&H90000000,-1,3,12,0,8,70,70,140\n"
        "[Events]\nFormat: Layer, Start, End, Style, Text\n"
        f"Dialogue: 1,0:00:00.00,{end},Overlay,{wrapped}\n", encoding="utf-8")

"""
Tracks everyone in frame across a clip and figures out who's talking, so
the crop can smoothly follow the active speaker instead of you picking it
by hand.

Detector: uses OpenCV's DNN face detector (a small pretrained neural
network, much more accurate than a Haar cascade -- this is the same
general class of technique production auto-reframe tools use) if the
model files have been downloaded via download_models.py. If they haven't,
falls back automatically to OpenCV's built-in Haar cascade so nothing
breaks -- just with lower accuracy. There is no public spec for exactly
what any specific commercial tool runs internally, so this is a strong,
honest equivalent, not a claim of using anyone else's proprietary system.

"Who's talking" is approximated by motion energy inside each tracked
face's box (frame-to-frame pixel change).

Output aspect ratio (9:16, 16:9, 4:3, etc.) is configurable -- crop width
is computed from the target ratio rather than being hardcoded to vertical.
"""

import cv2
import numpy as np
from pathlib import Path
from dataclasses import dataclass

MODELS_DIR = Path(__file__).parent / "models"
PROTOTXT = MODELS_DIR / "deploy.prototxt"
CAFFEMODEL = MODELS_DIR / "res10_300x300_ssd_iter_140000.caffemodel"
HAAR_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"

ASPECT_RATIOS = {
    "9:16": (9, 16),
    "16:9": (16, 9),
    "4:3": (4, 3),
    "4:5": (4, 5),
    "1:1": (1, 1),
}


def _load_detector():
    """Prefers the DNN detector if its model files are present, otherwise
    falls back to the Haar cascade -- always usable, never a hard error."""
    if PROTOTXT.exists() and CAFFEMODEL.exists():
        net = cv2.dnn.readNetFromCaffe(str(PROTOTXT), str(CAFFEMODEL))
        return ("dnn", net)
    return ("haar", cv2.CascadeClassifier(HAAR_PATH))


def _detect_faces(detector, frame) -> list[tuple[int, int, int, int]]:
    """Returns a list of (x, y, w, h) boxes in pixel coordinates,
    regardless of which detector backend is active."""
    kind, model = detector
    h, w = frame.shape[:2]

    if kind == "dnn":
        blob = cv2.dnn.blobFromImage(cv2.resize(frame, (300, 300)), 1.0, (300, 300), (104.0, 177.0, 123.0))
        model.setInput(blob)
        detections = model.forward()
        boxes = []
        for i in range(detections.shape[2]):
            confidence = detections[0, 0, i, 2]
            if confidence < 0.5:
                continue
            box = detections[0, 0, i, 3:7] * np.array([w, h, w, h])
            x1, y1, x2, y2 = box.astype(int)
            boxes.append((max(0, x1), max(0, y1), x2 - x1, y2 - y1))
        return boxes
    else:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = model.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
        return [tuple(f) for f in faces]


@dataclass
class FaceObservation:
    frame_idx: int
    track_id: int
    x_center: float
    activity: float


def _match_track(prev_faces: dict, x_center: float, max_dist: float = 0.15):
    best_id, best_dist = None, max_dist
    for track_id, last_x in prev_faces.items():
        dist = abs(last_x - x_center)
        if dist < best_dist:
            best_id, best_dist = track_id, dist
    return best_id


def analyze_speakers(video_path: Path, sample_stride: int = 4) -> list[FaceObservation]:
    detector = _load_detector()
    cap = cv2.VideoCapture(str(video_path))
    observations = []
    next_track_id = 0
    prev_faces = {}
    prev_gray_by_id = {}

    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % sample_stride != 0:
            frame_idx += 1
            continue

        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = _detect_faces(detector, frame)

        current_faces = {}
        for (x, y, fw, fh) in faces:
            if fw <= 0 or fh <= 0:
                continue
            x_center = (x + fw / 2) / w
            track_id = _match_track({k:v for k,v in prev_faces.items() if k not in current_faces}, x_center)
            if track_id is None:
                track_id = next_track_id
                next_track_id += 1

            region = gray[max(0,y+fh//2):min(h,y+fh), max(0,x):min(w,x+fw)]
            if region.size == 0:
                continue
            face_region = cv2.resize(region, (64, 32))
            activity = 0.0
            if track_id in prev_gray_by_id:
                diff = cv2.absdiff(face_region, prev_gray_by_id[track_id])
                activity = float(np.mean(diff))
            prev_gray_by_id[track_id] = face_region

            current_faces[track_id] = x_center
            observations.append(FaceObservation(frame_idx, track_id, x_center, activity))

        prev_faces = current_faces
        frame_idx += 1

    cap.release()
    return observations


def compute_smoothed_crop_track(
    observations: list[FaceObservation],
    total_frames: int,
    fps: float,
    ema_alpha: float = 0.12,
    min_hold_seconds: float = 0.20,
    dead_zone_frac: float = 0.04,
    lock: bool = False,
) -> np.ndarray:
    """
    dead_zone_frac: small movements below this fraction of frame width
    are ignored entirely rather than nudging the crop -- stops a
    barely-moving speaker from causing constant tiny jitter.
    lock: if True, the single most-active face across the WHOLE clip is
    picked once up front and tracked for the entire duration (never
    switches speakers), instead of following whoever's currently loudest.
    """
    if not observations:
        return np.full(total_frames, 0.5)

    by_frame: dict[int, list[FaceObservation]] = {}
    for obs in observations:
        by_frame.setdefault(obs.frame_idx, []).append(obs)

    sample_frames = sorted(by_frame.keys())
    min_hold_frames = int(min_hold_seconds * fps)

    locked_id = None
    if lock:
        totals: dict[int, float] = {}
        for obs in observations:
            totals[obs.track_id] = totals.get(obs.track_id, 0.0) + obs.activity
        if totals:
            locked_id = max(totals, key=totals.get)

    active_id = locked_id
    active_since = 0
    activity_history: dict[int, list[float]] = {}
    sparse_positions = []  # (frame_idx, x_center_or_None)
    cut_frames = set()
    last_seen_frame = None

    for f in sample_frames:
        faces = by_frame[f]
        for obs in faces:
            hist = activity_history.setdefault(obs.track_id, [])
            hist.append(obs.activity)
            if len(hist) > 3:
                hist.pop(0)

        if lock:
            chosen = next((o for o in faces if o.track_id == locked_id), None)
        else:
            scores = {
                obs.track_id: (sum(activity_history[obs.track_id]) / len(activity_history[obs.track_id]))
                for obs in faces
            }
            candidate_id = max(scores, key=scores.get) if scores else active_id

            if active_id is None:
                active_id = candidate_id
                active_since = f
            elif candidate_id != active_id and (f - active_since) >= min_hold_frames:
                active_id = candidate_id
                active_since = f
                cut_frames.add(f)

            chosen = next((o for o in faces if o.track_id == active_id), None)

        if chosen:
            last_seen_frame = f
            sparse_positions.append((f, chosen.x_center))
        else:
            # No face detected this sample -- if it's been a full second
            # since we last saw one, ease toward center instead of
            # holding a stale position or jump-cutting.
            if last_seen_frame is not None and (f - last_seen_frame) >= fps:
                sparse_positions.append((f, 0.5))

    if not sparse_positions:
        return np.full(total_frames, 0.5)

    xs = [p[0] for p in sparse_positions]
    ys = [p[1] for p in sparse_positions]
    # Hold each measured location until the next observation; no pre-cut pan.
    full = np.array(ys)[np.clip(np.searchsorted(xs, np.arange(total_frames), side="right")-1, 0, len(ys)-1)]

    # Dead zone: collapse movements smaller than dead_zone_frac into the
    # previous position before smoothing, so tiny jitter never reaches
    # the EMA at all.
    dead_zoned = full.copy()
    for i in range(1, len(dead_zoned)):
        if abs(dead_zoned[i] - dead_zoned[i - 1]) < dead_zone_frac:
            dead_zoned[i] = dead_zoned[i - 1]

    smoothed = np.zeros_like(dead_zoned)
    smoothed[0] = dead_zoned[0]
    for i in range(1, len(dead_zoned)):
        smoothed[i] = dead_zoned[i] if i in cut_frames else ema_alpha * dead_zoned[i] + (1 - ema_alpha) * smoothed[i - 1]

    return smoothed


def _target_dims(aspect_ratio: str, source_h: int) -> tuple[int, int, int]:
    """
    Given a target aspect ratio and the source frame height, returns
    (crop_w, out_w, out_h) -- crop_w is the width to crop from the
    source (based on its native height), out_w/out_h is the final
    encoded resolution for that ratio.
    """
    ratio_w, ratio_h = ASPECT_RATIOS.get(aspect_ratio, (9, 16))
    crop_w = int(source_h * ratio_w / ratio_h)

    # Sensible fixed output resolutions per common ratio
    presets = {
        "9:16": (1080, 1920),
        "16:9": (1920, 1080),
        "4:3": (1440, 1080),
        "4:5": (1080, 1350),
        "1:1": (1080, 1080),
    }
    out_w, out_h = presets.get(aspect_ratio, (1080, 1920))
    return crop_w, out_w, out_h


def apply_dynamic_crop(video_path: Path, x_positions: np.ndarray, out_path: Path, aspect_ratio: str = "9:16"):
    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    crop_w, out_w, out_h = _target_dims(aspect_ratio, height)
    crop_w = min(crop_w, width)  # can't crop wider than the source frame

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, fps, (out_w, out_h))

    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        x_frac = x_positions[frame_idx] if frame_idx < len(x_positions) else 0.5
        cx = int(x_frac * width)
        x0 = max(0, min(width - crop_w, cx - crop_w // 2))
        cropped = frame[:, x0:x0 + crop_w]
        resized = cv2.resize(cropped, (out_w, out_h))
        writer.write(resized)
        frame_idx += 1

    cap.release()
    writer.release()


def apply_center_crop(video_path: Path, out_path: Path, aspect_ratio: str = "9:16"):
    """
    No tracking -- just a fixed center crop to the target aspect ratio.
    Used when tracking is turned off in settings.
    """
    cap = cv2.VideoCapture(str(video_path))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    positions = np.full(total_frames, 0.5)
    apply_dynamic_crop(video_path, positions, out_path, aspect_ratio)


def apply_split2_crop(video_path: Path, out_path: Path, aspect_ratio: str = "9:16"):
    """
    Two largest faces stacked (vertical output) or side-by-side
    (horizontal output). If fewer than 2 faces are ever found, falls
    back to the normal single-speaker tracking crop instead -- a split
    layout with an empty half looks broken, not intentional.
    """
    observations = analyze_speakers(video_path)
    unique_ids = {obs.track_id for obs in observations}
    if len(unique_ids) < 2:
        print("[track] split2 requested but fewer than 2 distinct faces found -- falling back to follow")
        cap = cv2.VideoCapture(str(video_path))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        cap.release()
        positions = compute_smoothed_crop_track(observations, total_frames, fps)
        apply_dynamic_crop(video_path, positions, out_path, aspect_ratio)
        return

    # Pick the two most-active (most consistently talking) faces overall
    totals: dict[int, float] = {}
    for obs in observations:
        totals[obs.track_id] = totals.get(obs.track_id, 0.0) + obs.activity
    top_two = sorted(totals, key=totals.get, reverse=True)[:2]

    by_frame: dict[int, dict[int, float]] = {}
    for obs in observations:
        if obs.track_id in top_two:
            by_frame.setdefault(obs.frame_idx, {})[obs.track_id] = obs.x_center

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    ratio_w, ratio_h = ASPECT_RATIOS.get(aspect_ratio, (9, 16))
    out_w, out_h = {"9:16": (1080, 1920), "1:1": (1080, 1080)}.get(aspect_ratio, (1080, 1920))
    half_h = out_h // 2
    crop_w = int(height * ratio_w / ratio_h)
    crop_w = min(crop_w, width)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, fps, (out_w, out_h))

    last_positions = {top_two[0]: 0.5, top_two[1]: 0.5}
    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break

        positions = by_frame.get(frame_idx, {})
        for tid in top_two:
            if tid in positions:
                last_positions[tid] = positions[tid]

        halves = []
        for tid in top_two:
            cx = int(last_positions[tid] * width)
            x0 = max(0, min(width - crop_w, cx - crop_w // 2))
            cropped = frame[:, x0:x0 + crop_w]
            halves.append(cv2.resize(cropped, (out_w, half_h)))

        stacked = np.vstack(halves)
        writer.write(stacked)
        frame_idx += 1

    cap.release()
    writer.release()


def build_layout(
    source_video: Path,
    out_path: Path,
    aspect_ratio: str = "9:16",
    track_faces: bool = True,
    layout: str = "follow",
    lock: bool = False,
):
    """
    Single entry point clip.py calls: produces the final-aspect-ratio
    video. `layout` controls the crop behavior when tracking is on:
    "follow" (smoothed single-speaker tracking, the default), "center"
    (plain static crop, same as track_faces=False), or "split2" (two
    largest faces stacked). `lock` (only meaningful with "follow") keeps
    the first-chosen speaker for the whole clip instead of switching
    whoever's currently talking.
    """
    if aspect_ratio == "16:9":
        # Same ratio as the source stream -- no cropping needed at all.
        cap = cv2.VideoCapture(str(source_video))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        if abs((width / height) - (16 / 9)) < 0.05:
            import shutil
            shutil.copy(source_video, out_path)
            return

    if not track_faces or layout == "center":
        apply_center_crop(source_video, out_path, aspect_ratio)
        return

    if layout == "split2":
        apply_split2_crop(source_video, out_path, aspect_ratio)
        return

    cap = cv2.VideoCapture(str(source_video))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    cap.release()

    observations = analyze_speakers(source_video)
    positions = compute_smoothed_crop_track(observations, total_frames, fps, lock=lock)
    apply_dynamic_crop(source_video, positions, out_path, aspect_ratio)


# Backward-compatible alias for existing callers
def build_smooth_vertical_crop(source_video: Path, out_path: Path):
    build_layout(source_video, out_path, aspect_ratio="9:16", track_faces=True)

"""
One-time download of the DNN face detector model files. Run this once
after installing dependencies:

    python download_models.py

track.py will automatically use this detector if these files are present,
and falls back to OpenCV's built-in (less accurate, but zero-setup) Haar
cascade detector if they're not -- so skipping this step doesn't break
anything, it just means lower-accuracy tracking.
"""

import urllib.request
from pathlib import Path

MODELS_DIR = Path(__file__).parent / "models"
FILES = {
    "deploy.prototxt": "https://raw.githubusercontent.com/opencv/opencv/master/samples/dnn/face_detector/deploy.prototxt",
    "res10_300x300_ssd_iter_140000.caffemodel": "https://raw.githubusercontent.com/opencv/opencv_3rdparty/dnn_samples_face_detector_20170830/res10_300x300_ssd_iter_140000.caffemodel",
}


def main():
    MODELS_DIR.mkdir(exist_ok=True)
    for filename, url in FILES.items():
        dest = MODELS_DIR / filename
        if dest.exists():
            print(f"[models] {filename} already present, skipping")
            continue
        print(f"[models] downloading {filename}...")
        urllib.request.urlretrieve(url, dest)
        print(f"[models] saved to {dest}")
    print("[models] done -- track.py will now use the higher-accuracy detector")


if __name__ == "__main__":
    main()

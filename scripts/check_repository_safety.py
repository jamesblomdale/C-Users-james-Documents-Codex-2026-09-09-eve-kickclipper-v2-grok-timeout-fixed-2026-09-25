"""Fail CI if secrets, databases, logs, media, or local environments are tracked."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
tracked = subprocess.check_output(
    ["git", "ls-files", "-z"], cwd=ROOT
).decode("utf-8", "replace").split("\0")

forbidden_names = {".env", "kickclipper.db"}
forbidden_suffixes = {".db", ".sqlite", ".log", ".mp4", ".mkv", ".wav", ".pem", ".key"}
forbidden_parts = {"output", "venv", ".venv", "__pycache__"}
secret_patterns = [
    re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(rb"\bxai-[A-Za-z0-9_-]{20,}"),
]

problems = []
for name in filter(None, tracked):
    path = Path(name)
    if path.name in forbidden_names or path.suffix.lower() in forbidden_suffixes or forbidden_parts.intersection(path.parts):
        problems.append(f"forbidden tracked file: {name}")
        continue
    full = ROOT / path
    if not full.is_file() or full.stat().st_size > 2_000_000:
        continue
    data = full.read_bytes()
    if any(pattern.search(data) for pattern in secret_patterns):
        problems.append(f"possible API credential: {name}")

if problems:
    print("Repository safety check failed:", *problems, sep="\n- ")
    sys.exit(1)
print(f"Repository safety check passed for {len([p for p in tracked if p])} tracked files.")


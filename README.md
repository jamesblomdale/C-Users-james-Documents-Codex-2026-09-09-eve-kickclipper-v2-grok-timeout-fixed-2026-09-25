# kick-clipper

A personal live-clipping pipeline for Kick streams: captures the live feed,
transcribes it in near real time, uses an LLM to spot clip-worthy moments,
cuts + capitions + face-crops the clip, and generates your X post / TikTok
title / YouTube title in your existing style.

This is built to run on your own machine or a cheap VPS — nothing here
depends on any paid SaaS except pennies of LLM API usage per stream.

## What it does

1. `capture.py` — uses `streamlink` to pull the live Kick stream into
   rolling video chunks on disk.
2. `transcribe.py` — runs `faster-whisper` (local, free) on each chunk to
   get a word-level timestamped transcript.
3. `detect.py` — feeds rolling transcript windows to an LLM (Grok or
   OpenAI) and asks it to score clip-worthiness. High-scoring windows get
   queued for clipping.
4. `clip.py` — uses `ffmpeg` + a lightweight face detector (Mediapipe) to
   cut the segment, crop to 9:16 centered on the streamer's face, and burn
   in word-synced captions.
5. `generate_posts.py` — sends the clip's transcript to the LLM using your
   existing post structure (hook / context / quote / commentary /
   engagement bait for X, separate hook titles for TikTok and YouTube) and
   writes them next to the clip.
6. `main.py` — orchestrates all of the above in a loop and drops finished
   clips + a `.txt` of generated titles/posts into `output/`.

You post the finished clips manually — this tool does the clipping and
writing for you, not the posting.

## Setup

```bash
# System dependencies
brew install ffmpeg streamlink        # or apt-get install ffmpeg; pip install streamlink

# Python dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

You'll need a GPU for faster-whisper to run comfortably in real time, but
it also works on CPU for a single stream (just slower — a small/medium
Whisper model is fine here, you don't need the largest one). A $20/month
GPU VPS (e.g. a spot instance) is enough for one stream at a time.

## Configuration

Copy `.env.example` to `.env` and fill in:

```
KICK_CHANNEL=streamer_name
LLM_API_KEY=your_key_here
LLM_PROVIDER=grok        # or openai
CLIP_SCORE_THRESHOLD=60  # 0-100, how clip-worthy a moment must be
```

## Running

```bash
python main.py
```

It will run continuously, watching the channel, and drop clips + generated
titles/posts into `output/<timestamp>/` as they're found.

## Cost per stream (rough)

- Transcription: free (local model)
- Moment detection: ~$0.01–0.05/hour of stream (small LLM calls on text only)
- Post/title generation: ~$0.01 per clip
- Compute: whatever your VPS/GPU costs, the rest is free/open-source

## Notes

- `CLIP_SCORE_THRESHOLD` is the main dial — lower it to catch more clips
  (more false positives), raise it to be pickier.
- The detection prompt in `detect.py` and the post-generation prompt in
  `generate_posts.py` are the two files worth tuning to your taste — they
  control what counts as "clippable" and how the copy sounds.

import os
from dotenv import load_dotenv

load_dotenv()

# Real streamers/names worth boosting name-strength scoring on, expanded
# beyond just the account's own niche to include the kind of names that
# come up as guests/topics/claims (NBA legends, other public figures) --
# a mention of these should ADD to a score, never gate it to zero.
DEFAULT_PRIORITY_NAMES = (
    "N3on,Sneako,Clavicular,Clav,Adin Ross,Adin,Blueface,Chrisean,"
    "Delonte West,DeLonte,LeBron,Iverson,Michael Jordan,MJ,Mike Tyson,"
    "Conor McGregor,DDG,Cuffem,Treyliving,Adrien Broner,Deen the Great,Fousey,Blame,BAU House"
)


class Config:
    # Long-VOD controls. MAI_* names remain supported for existing installs.
    ASR_CHUNK_SECONDS = float(os.getenv('ASR_CHUNK_SECONDS', '240'))
    ASR_CHUNK_OVERLAP_SECONDS = float(os.getenv('ASR_CHUNK_OVERLAP_SECONDS', '15'))
    ASR_MIN_CONCURRENCY = int(os.getenv('ASR_MIN_CONCURRENCY', '1'))
    ASR_MAX_CONCURRENCY = int(os.getenv('ASR_MAX_CONCURRENCY', '8'))
    ASR_RETRY_COUNT = int(os.getenv('ASR_RETRY_COUNT', '3'))
    ASR_TIMEOUT_SECONDS = int(os.getenv('ASR_TIMEOUT_SECONDS', '180'))
    ASR_FAST_CHUNK_COPY = os.getenv('ASR_FAST_CHUNK_COPY', 'true').lower() in ('1','true','yes','on')
    ASR_AUDIO_BITRATE = os.getenv('ASR_AUDIO_BITRATE', '32k').strip() or '48k'
    SEMANTIC_WINDOW_SECONDS = float(os.getenv('SEMANTIC_WINDOW_SECONDS', '180'))
    SEMANTIC_WINDOW_OVERLAP_SECONDS = float(os.getenv('SEMANTIC_WINDOW_OVERLAP_SECONDS', '20'))
    MIN_SCOUT_WORDS = int(os.getenv('MIN_SCOUT_WORDS', '8'))
    SCOUT_MAX_CONCURRENCY = int(os.getenv('SCOUT_MAX_CONCURRENCY', '4'))
    SCOUT_MIN_SCORE = float(os.getenv('SCOUT_MIN_SCORE', '36'))
    SCOUT_MODEL = os.getenv('SCOUT_MODEL', 'gpt-5.4-mini').strip()
    SCOUT_REASONING = os.getenv('SCOUT_REASONING', 'off').strip().lower()
    DEEP_SCORING_MODEL = os.getenv('DEEP_SCORING_MODEL', os.getenv('SCOUT_MODEL', 'gpt-5.4-mini')).strip()
    DEEP_ANALYSIS_MAX_CONCURRENCY = int(os.getenv('DEEP_ANALYSIS_MAX_CONCURRENCY', '2'))
    # Project-tier limit used by the shared OpenAI request pacer. Keep a safety
    # margin because OpenAI accounts prompt and completion tokens per minute.
    OPENAI_TPM_LIMIT = int(os.getenv('OPENAI_TPM_LIMIT', '200000'))
    VISUAL_VERIFY_MIN_SCORE = float(os.getenv('VISUAL_VERIFY_MIN_SCORE', '60'))
    MAX_RENDER_CONCURRENCY = int(os.getenv('MAX_RENDER_CONCURRENCY', '2'))
    CUSTOMER_MAX_CLIPS_PER_JOB = int(os.getenv('CUSTOMER_MAX_CLIPS_PER_JOB', '8'))
    MAX_TEMP_GB = float(os.getenv('MAX_TEMP_GB', '8'))
    ASR_QUALITY_MIN_CONFIDENCE = float(os.getenv('ASR_QUALITY_MIN_CONFIDENCE', '.5'))
    ASR_QUALITY_RETRY_LIMIT = int(os.getenv('ASR_QUALITY_RETRY_LIMIT', '0'))
    JUDGE_MODEL = os.getenv('JUDGE_MODEL', 'grok-4.6').strip()
    JUDGE_ENABLED = os.getenv('JUDGE_ENABLED', 'true').lower() in ('1','true','yes','on')
    JUDGE_REASONING_EFFORT = os.getenv('JUDGE_REASONING_EFFORT', 'low').strip().lower()
    JUDGE_MAX_CONCURRENCY = int(os.getenv('JUDGE_MAX_CONCURRENCY', '3'))
    JUDGE_ALWAYS_TOP = int(os.getenv('JUDGE_ALWAYS_TOP', '0'))
    JUDGE_ALSO_EVERYTHING_OVER = float(os.getenv('JUDGE_ALSO_EVERYTHING_OVER', '999'))
    JUDGE_ONLY_AMBIGUOUS = os.getenv('JUDGE_ONLY_AMBIGUOUS', 'true').lower() in ('1','true','yes','on')
    JUDGE_MIN_SCORE = float(os.getenv('JUDGE_MIN_SCORE', '40'))
    JUDGE_MAX_SCORE = float(os.getenv('JUDGE_MAX_SCORE', '74'))
    JUDGE_ALSO_ON_OVERLAP = os.getenv('JUDGE_ALSO_ON_OVERLAP', 'true').lower() in ('1','true','yes','on')
    JUDGE_ALSO_ON_CLOSE_SCORES = os.getenv('JUDGE_ALSO_ON_CLOSE_SCORES', 'true').lower() in ('1','true','yes','on')
    JUDGE_CLOSE_SCORE_DELTA = float(os.getenv('JUDGE_CLOSE_SCORE_DELTA', '5'))
    JUDGE_MAX_CALLS_PER_JOB = int(os.getenv('JUDGE_MAX_CALLS_PER_JOB', '8'))
    JUDGE_MAX_PROMPT_TOKENS = int(os.getenv('JUDGE_MAX_PROMPT_TOKENS', '20000'))
    JUDGE_TIMEOUT_SECONDS = int(os.getenv('JUDGE_TIMEOUT_SECONDS', '45'))
    JUDGE_BATCH_MODE = os.getenv('JUDGE_BATCH_MODE', 'false').lower() in ('1','true','yes','on')

    @classmethod
    def validate_pipeline(cls):
        import math
        ranges = {'ASR_CHUNK_SECONDS': (60,3600), 'ASR_CHUNK_OVERLAP_SECONDS': (1,30),
                  'ASR_MIN_CONCURRENCY': (1,16), 'ASR_MAX_CONCURRENCY': (1,16),
                  'ASR_RETRY_COUNT': (0,5), 'ASR_TIMEOUT_SECONDS': (10,600),
                  'SEMANTIC_WINDOW_SECONDS': (30,600), 'SEMANTIC_WINDOW_OVERLAP_SECONDS': (5,120),
                  'SCOUT_MAX_CONCURRENCY': (1,8), 'SCOUT_MIN_SCORE': (0,100),
                  'MIN_SCOUT_WORDS': (0,1000),
                  'DEEP_ANALYSIS_MAX_CONCURRENCY': (1,8), 'VISUAL_VERIFY_MIN_SCORE': (0,100),
                  'OPENAI_TPM_LIMIT': (1000,100000000),
                  'MAX_RENDER_CONCURRENCY': (1,4), 'MAX_TEMP_GB': (.1,1000),
                  'CUSTOMER_MAX_CLIPS_PER_JOB': (1,100),
                  'ASR_QUALITY_MIN_CONFIDENCE': (0,1), 'ASR_QUALITY_RETRY_LIMIT': (0,100)}
        for key, (low, high) in ranges.items():
            value = getattr(cls, key)
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{key} must be {low}..{high}')
        if cls.ASR_MIN_CONCURRENCY > cls.ASR_MAX_CONCURRENCY:
            raise ValueError('ASR_MIN_CONCURRENCY exceeds maximum')
        if cls.SEMANTIC_WINDOW_OVERLAP_SECONDS >= cls.SEMANTIC_WINDOW_SECONDS:
            raise ValueError('Semantic overlap must be shorter than the window')
    KICK_CHANNEL = os.getenv("KICK_CHANNEL", "")
    # Who this job's clips are actually about -- the highest name-strength
    # boost applies when THIS person is the one doing the action, not just
    # whenever their name gets said. Defaults to the channel being
    # processed (the natural "whose show is this" answer) but can be
    # overridden for edge cases like a guest-hosted stream.
    STREAMER_NAME = os.getenv("STREAMER_NAME", "") or KICK_CHANNEL
    # STRICT ROLE SEPARATION:
    # - TRANSCRIPTION_PROVIDER + TOGETHER_API_KEY own speech-to-text only.
    # - LLM_PROVIDER + LLM_API_KEY own transcript scoring/titles/vision only.
    # Never infer one provider from the other key.
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    # Keep provider credentials separate so changing the analysis provider can
    # never send an xAI key to OpenAI (or overwrite the key needed to roll back).
    _LEGACY_LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
    GROK_API_KEY = os.getenv("GROK_API_KEY", "").strip()
    XAI_API_KEY = os.getenv("XAI_API_KEY", "").strip() or GROK_API_KEY
    XAI_BASE_URL = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1").strip().rstrip('/')
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
    LLM_API_KEY = (
        OPENAI_API_KEY or (_LEGACY_LLM_API_KEY if LLM_PROVIDER == "openai" else "")
        if LLM_PROVIDER == "openai"
        else GROK_API_KEY or _LEGACY_LLM_API_KEY
    )
    # Two different Grok models for two different jobs, same key/account:
    # scoring is a classification task done 10-100+ times per VOD, so it
    # uses a cheap, fast model -- title/post writing happens once per
    # finished clip and benefits from the stronger creative model. This
    # is what actually fixes "80 slow API calls per VOD," not just
    # merging windows after the fact.
    GROK_SCORING_MODEL = os.getenv("GROK_SCORING_MODEL", "grok-4.6")
    GROK_TITLE_MODEL = os.getenv("GROK_TITLE_MODEL", "grok-4.6")
    OPENAI_SCORING_MODEL = os.getenv("OPENAI_SCORING_MODEL", "gpt-5.4-mini")
    OPENAI_TITLE_MODEL = os.getenv("OPENAI_TITLE_MODEL", "gpt-5.4-mini")
    # Final gate for an actually-cut clip, applied AFTER nearby candidate
    # windows have been merged into one full moment -- not applied to a
    # single raw window, which is what was causing real multi-part
    # stories (e.g. a rambling setup that pays off two windows later) to
    # get scored as several separate mediocre fragments instead of one
    # strong clip. Scale is 0-100 (see detect.py's scoring system).
    CLIP_SCORE_THRESHOLD = float(os.getenv("CLIP_SCORE_THRESHOLD", "60.0"))
    # Looser gate for keeping a single raw window as a "candidate" worth
    # holding onto for the merge step -- deliberately low, since a
    # candidate is allowed to be an incomplete fragment of a bigger story.
    CANDIDATE_SCORE_THRESHOLD = float(os.getenv("CANDIDATE_SCORE_THRESHOLD", "32.0"))
    WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "small")
    TRANSCRIPTION_PROVIDER = os.getenv('TRANSCRIPTION_PROVIDER', 'parakeet').strip().lower()
    ASR_PROVIDER = os.getenv('ASR_PROVIDER', TRANSCRIPTION_PROVIDER).strip().lower()
    TRANSCRIPTION_FALLBACK = os.getenv('TRANSCRIPTION_FALLBACK', 'none').strip().lower()
    TOGETHER_API_KEY = os.getenv('TOGETHER_API_KEY', '').strip()
    PARAKEET_MODEL = os.getenv('PARAKEET_MODEL', 'nvidia/parakeet-tdt-0.6b-v3').strip()
    PARAKEET_LANGUAGE = os.getenv('PARAKEET_LANGUAGE', 'en').strip()
    PARAKEET_CONCURRENCY = int(os.getenv('PARAKEET_CONCURRENCY', os.getenv('ASR_MAX_CONCURRENCY', '8')))
    PARAKEET_CHUNK_SECONDS = float(os.getenv('PARAKEET_CHUNK_SECONDS', '240'))
    PARAKEET_CHUNK_OVERLAP_SECONDS = float(os.getenv('PARAKEET_CHUNK_OVERLAP_SECONDS', '15'))
    PARAKEET_VERIFY_CREDENTIALS = os.getenv('PARAKEET_VERIFY_CREDENTIALS', 'true').lower() in ('1','true','yes','on')
    PARAKEET_LOCAL_FALLBACK = os.getenv('PARAKEET_LOCAL_FALLBACK', 'false').lower() in ('1','true','yes','on')
    PARAKEET_TIMEOUT_SECONDS = int(os.getenv('PARAKEET_TIMEOUT_SECONDS', os.getenv('ASR_TIMEOUT_SECONDS', '180')))
    PARAKEET_MAX_RETRIES = int(os.getenv('PARAKEET_MAX_RETRIES', '4'))
    PARAKEET_DIARIZE = os.getenv('PARAKEET_DIARIZE', 'false').lower() in ('1','true','yes','on')
    AZURE_SPEECH_ENDPOINT = os.getenv('AZURE_SPEECH_ENDPOINT', '').strip().rstrip('/')
    AZURE_SPEECH_KEY = os.getenv('AZURE_SPEECH_KEY', '').strip()
    MAI_MODEL = os.getenv('MAI_MODEL', 'MAI-Transcribe-2')
    MAI_API_VERSION = os.getenv('MAI_API_VERSION', '2025-10-15')
    MAI_LANGUAGE = os.getenv('MAI_LANGUAGE', 'en')
    MAI_TRANSCRIBE_STYLE = os.getenv('MAI_TRANSCRIBE_STYLE', 'verbatim')
    MAI_CHUNK_MINUTES = float(os.getenv('MAI_CHUNK_MINUTES', '15'))
    MAI_CHUNK_OVERLAP_SECONDS = float(os.getenv('MAI_CHUNK_OVERLAP_SECONDS', '8'))
    MAI_CONCURRENCY = int(os.getenv('MAI_CONCURRENCY', '4'))
    MAI_TIMEOUT_SECONDS = int(os.getenv('MAI_TIMEOUT_SECONDS', '180'))
    MAI_MAX_RETRIES = int(os.getenv('MAI_MAX_RETRIES', '4'))
    MAI_CACHE_ENABLED = os.getenv('MAI_CACHE_ENABLED', 'true').lower() in ('1','true','yes','on')
    MAI_USE_PRIORITY_NAMES = os.getenv('MAI_USE_PRIORITY_NAMES', 'true').lower() in ('1','true','yes','on')
    MAI_DIARIZATION = os.getenv('MAI_DIARIZATION', 'false').lower() in ('1','true','yes','on')
    CHUNK_SECONDS = int(os.getenv("CHUNK_SECONDS", "30"))
    DETECTION_WINDOW_SECONDS = int(os.getenv("DETECTION_WINDOW_SECONDS", "75"))
    # How far apart (in seconds) each scoring window starts from the last
    # -- smaller than DETECTION_WINDOW_SECONDS so windows overlap, which
    # is what lets a story spanning a window boundary still get judged
    # as a whole rather than getting cut in half.
    DETECTION_HOP_SECONDS = int(os.getenv("DETECTION_HOP_SECONDS", "20"))
    # Two candidate windows this close together (in seconds) get merged
    # into one clip before final scoring, instead of being treated as
    # two separate, weaker moments.
    MERGE_GAP_SECONDS = int(os.getenv("MERGE_GAP_SECONDS", "12"))
    OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")

    # "9:16" (TikTok/Shorts), "16:9" (YouTube/original), "4:3" (classic)
    ASPECT_RATIO = os.getenv("ASPECT_RATIO", "9:16")
    # Whether to run the face-tracking crop pass at all. If false, clips
    # are just center-cropped to the chosen aspect ratio -- no tracking.
    TRACKING_ENABLED = os.getenv("TRACKING_ENABLED", "false").lower() in ("1", "true", "yes")
    # Whether to burn in captions at all.
    CAPTIONS_ENABLED = os.getenv("CAPTIONS_ENABLED", "true").lower() in ("1", "true", "yes")
    # Comma-separated names, used ONLY as a spelling/normalization
    # reference and in generated titles -- NOT a scoring boost list.
    # A name appearing here does not, by itself, make a moment
    # clip-worthy; someone getting name-dropped in passing while the
    # streamer sets up their mic is not a clip. See STREAMER_NAME above
    # for who actually gets a real name-strength boost.
    PRIORITY_NAMES = os.getenv("PRIORITY_NAMES", DEFAULT_PRIORITY_NAMES)
    # Whether flagged moments (minor present, explicit content, an
    # unverified serious claim) get a plain, factual title/caption tone
    # in generate_posts.py instead of the usual clickbait formula. Does
    # NOT affect whether a moment gets scored/kept as a clip -- that
    # used to skip flagged moments outright, which threw away genuinely
    # strong, high-judgment clips (a real legal situation, a real
    # incident) just for being flagged. Rating is purely score-based now.
    SAFETY_FLAGS_ENABLED = os.getenv("SAFETY_FLAGS_ENABLED", "true").lower() in ("1", "true", "yes")
    # Stop automatically after this many clips are produced. 0 = unlimited.
    MAX_CLIPS = int(os.getenv("MAX_CLIPS", "0"))
    # Optional: Apify API token for downloading Kick VODs via the
    # parsebird/kick-video-downloader actor. Only needed if yt-dlp's
    # built-in Kick VOD support is broken (it currently is, as of a
    # recent Kick site change). Get one at https://console.apify.com/account#/integrations
    APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")

    # ---------- SaaS layer: auth, billing, app-level config ----------
    # Flask session-cookie signing key. A random one is fine for local
    # dev, but changing it invalidates every existing session (logs
    # everyone out) -- set a fixed value in .env before real users exist.
    SECRET_KEY = os.getenv("SECRET_KEY", "") or os.urandom(32).hex()
    # An explicit absolute path, not the bare "sqlite:///kickclipper.db"
    # relative form -- Flask-SQLAlchemy resolves relative SQLite URIs
    # against app.instance_path (a hidden instance/ subfolder it
    # creates), not the project root, which is surprising and easy to
    # lose track of (a restart pointed at the "wrong" file looks
    # identical to data loss).
    _DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kickclipper.db")
    DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{_DB_PATH}")
    # Bootstrap: on first run, if no users exist yet, an owner account is
    # created from these two -- there's no other way to get an initial
    # admin without a DB console. Change the password after first login.
    OWNER_EMAIL = os.getenv("OWNER_EMAIL", "")
    OWNER_PASSWORD = os.getenv("OWNER_PASSWORD", "")
    # Credits are denominated in "1 credit = 1 processed source minute."
    # Live is priced higher than VOD because it holds a worker the whole
    # time a stream is live, not just for the length of the content.
    CREDIT_RATE_VOD = float(os.getenv("CREDIT_RATE_VOD", "1.0"))
    CREDIT_RATE_LIVE = float(os.getenv("CREDIT_RATE_LIVE", "1.5"))
    # Stripe is wired up but inert until real keys are set -- billing.py
    # checks for this and disables checkout/webhooks gracefully otherwise.
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_ENABLED = bool(STRIPE_SECRET_KEY)

    # Product/SaaS policy. Keep these separate from the media pipeline so
    # cloud queueing/billing can evolve without changing ASR or ranking code.
    FREE_TRIAL_CREDITS = float(os.getenv("FREE_TRIAL_CREDITS", "180"))  # 3 source-hours
    RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "5"))
    SOFT_CONCURRENT_JOB_WARNING = int(os.getenv("SOFT_CONCURRENT_JOB_WARNING", "3"))
    HARD_LOCAL_JOB_CAP = int(os.getenv("HARD_LOCAL_JOB_CAP", "8"))
    MAX_SOURCE_HOURS = float(os.getenv("MAX_SOURCE_HOURS", "24"))  # 0 = unlimited
    MAX_UPLOAD_GB = float(os.getenv("MAX_UPLOAD_GB", "12"))
    LIVE_SOURCE = os.getenv("LIVE_SOURCE", "").strip()
    SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM = os.getenv("SMTP_FROM", "").strip()
    SMTP_STARTTLS = os.getenv("SMTP_STARTTLS", "true").lower() in ("1","true","yes","on")
    NETWORK_PROXY = os.getenv("NETWORK_PROXY", "").strip()
    DISABLE_ENV_PROXY = os.getenv("DISABLE_ENV_PROXY", "true").lower() in ("1","true","yes","on")

    @classmethod
    def validate(cls):
        cls.validate_pipeline()
        if cls.TRANSCRIPTION_PROVIDER not in ('mai','parakeet','whisper') or cls.TRANSCRIPTION_FALLBACK not in ('whisper','faster-whisper','none'):
            raise ValueError('Invalid transcription provider or fallback')
        if cls.LLM_PROVIDER not in ('grok', 'openai'):
            raise ValueError('LLM_PROVIDER must be grok or openai; transcription providers do not belong in this setting')
        if cls.TRANSCRIPTION_PROVIDER == 'parakeet' and not cls.TOGETHER_API_KEY:
            raise RuntimeError('Parakeet transcription requires TOGETHER_API_KEY. LLM_API_KEY/Grok cannot be used for transcription.')
        if cls.LLM_PROVIDER == 'grok' and not cls.LLM_API_KEY:
            raise RuntimeError('Grok moment analysis requires LLM_API_KEY. TOGETHER_API_KEY/Parakeet cannot be used for scoring.')
        if cls.TRANSCRIPTION_PROVIDER == 'mai':
            from mai_transcribe import MaiTranscriber
            MaiTranscriber(cls)  # Validate settings before downloading a VOD.
        elif cls.TRANSCRIPTION_PROVIDER == 'parakeet':
            from parakeet_transcribe import ParakeetTranscriber
            provider = ParakeetTranscriber(cls)
            if cls.PARAKEET_VERIFY_CREDENTIALS:
                provider.probe()
        missing = []
        if not cls.KICK_CHANNEL:
            missing.append("KICK_CHANNEL")
        if not cls.LLM_API_KEY:
            missing.append("LLM_API_KEY")
        if missing:
            raise RuntimeError(
                f"Missing required config: {', '.join(missing)}. "
                f"Fill these in your .env file."
            )

    @classmethod
    def log_active_thresholds(cls):
        """
        Prints the ACTUAL active scoring settings at the start of every
        job -- so a stale value left over in .env from an earlier point
        in setup (e.g. an old CLIP_SCORE_THRESHOLD=7 from before the
        default was lowered to 6.0) is immediately visible in the log
        instead of silently causing "0 clips produced" with no obvious
        cause. If this line doesn't match what you expect, check .env.
        """
        print(f"[config] CLIP_SCORE_THRESHOLD={cls.CLIP_SCORE_THRESHOLD} "
              f"CANDIDATE_SCORE_THRESHOLD={cls.CANDIDATE_SCORE_THRESHOLD} "
              f"MERGE_GAP_SECONDS={cls.MERGE_GAP_SECONDS} "
              f"STREAMER_NAME={cls.STREAMER_NAME!r}")

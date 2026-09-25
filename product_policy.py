"""Product-level policy kept separate from media processing.

This module centralizes SaaS defaults so billing/account limits do not leak
into transcription, scoring, or rendering code. Production deployments can
replace the local-process limits with a distributed queue without changing the
core media pipeline.
"""
from urllib.parse import urlparse

SUPPORTED_VOD_HOSTS = {
    'kick.com', 'www.kick.com', 'twitch.tv', 'www.twitch.tv',
    'youtube.com', 'www.youtube.com', 'youtu.be', 'm.youtube.com',
}
SUPPORTED_UPLOAD_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.webm', '.m4v', '.ts'}


def normalize_live_source(value: str) -> tuple[str, str, str]:
    """Return (platform, display/channel key, streamlink-compatible source).

    Bare values remain backwards compatible as Kick channel names. URLs are
    limited to the three launch live platforms; arbitrary URLs are not passed
    to streamlink from customer input.
    """
    value = (value or '').strip()
    if not value:
        raise ValueError('Enter a Kick, Twitch or YouTube live link (or a Kick channel name).')
    if '://' not in value:
        import re
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
            raise ValueError('Use a valid Kick channel name or supported live URL.')
        return 'kick', value, f'https://kick.com/{value}'

    parsed = urlparse(value)
    if parsed.scheme != 'https' or parsed.hostname not in SUPPORTED_VOD_HOSTS:
        raise ValueError('Live links currently support Kick, Twitch and YouTube HTTPS URLs.')
    host = parsed.hostname.lower()
    if 'kick.com' in host:
        platform = 'kick'
    elif 'twitch.tv' in host:
        platform = 'twitch'
    else:
        platform = 'youtube'
    slug = parsed.path.strip('/').split('/')[-1] or platform
    slug = ''.join(c if c.isalnum() or c in '_-' else '_' for c in slug)[:80] or platform
    return platform, slug, value


def validate_customer_vod_input(value: str, upload_root) -> str:
    """Allow only supported public platform URLs or this user's own upload path."""
    from pathlib import Path
    value = (value or '').strip()
    if not value:
        raise ValueError('Enter a Kick, Twitch, YouTube URL or upload a video.')
    if '://' in value:
        parsed = urlparse(value)
        if parsed.scheme != 'https' or parsed.hostname not in SUPPORTED_VOD_HOSTS:
            raise ValueError('VOD links currently support Kick, Twitch and YouTube HTTPS URLs.')
        return value
    candidate = Path(value).resolve()
    root = Path(upload_root).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError('Local file paths are not accepted. Upload the video through your account first.')
    if candidate.suffix.lower() not in SUPPORTED_UPLOAD_EXTENSIONS or not candidate.is_file():
        raise ValueError('Uploaded video is missing or uses an unsupported format.')
    return str(candidate)

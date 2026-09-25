"""Resolve a public Kick VOD page to its own recording, never its live player."""
import json
import re
from urllib.parse import urlparse
import requests


def kick_vod_id(url):
    parsed = urlparse(url)
    if parsed.hostname not in ('kick.com', 'www.kick.com'):
        return None
    match = re.fullmatch(r'/(?:[\w-]+/)?videos/([\w-]+)/?', parsed.path)
    return match.group(1) if match else None


def recording_from_page(page, video_id):
    decoder = json.JSONDecoder()
    texts = [page]
    for match in re.finditer(r'self\.__next_f\.push\(', page):
        try:
            payload, _ = decoder.raw_decode(page, match.end())
            texts.extend(v for v in payload if isinstance(v, str))
        except (ValueError, TypeError):
            continue

    def walk(value):
        if isinstance(value, dict):
            if str(value.get('id')) == video_id and value.get('is_live') is not True:
                url = value.get('recording_url') or value.get('source')
                if isinstance(url, str) and url.startswith('https://') and '.m3u8' in url:
                    return url
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found

    for text in texts:
        for match in re.finditer(r'\{', text):
            try:
                value, _ = decoder.raw_decode(text, match.start())
            except ValueError:
                continue
            found = walk(value)
            if found:
                return found
    raise RuntimeError('This Kick page did not expose a public recording for the requested VOD. Check that the recording is still available.')


def resolve_kick_vod(url):
    video_id = kick_vod_id(url)
    if not video_id:
        raise ValueError('Use the full Kick VOD page link, including /videos/ and its ID.')
    response = requests.get(url, timeout=(10, 30))
    response.raise_for_status()
    return recording_from_page(response.text, video_id)

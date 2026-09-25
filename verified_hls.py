"""Loopback HLS transport with Requests TLS verification for every resource."""
from contextlib import contextmanager
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit
import re
import secrets
import threading
import os
import requests
from requests.adapters import HTTPAdapter


def _limit_master_variants(lines, max_height):
    """Drop master-playlist video renditions above the render ceiling."""
    if not max_height:
        return lines
    kept = []
    drop_uri = False
    for line in lines:
        if line.startswith('#EXT-X-STREAM-INF:'):
            match = re.search(r'RESOLUTION=\d+x(\d+)', line, re.I)
            drop_uri = bool(match and int(match.group(1)) > max_height)
            if drop_uri:
                continue
        if drop_uri and line.strip() and not line.startswith('#'):
            drop_uri = False
            continue
        kept.append(line)
    return kept


@contextmanager
def verified_hls(url):
    if urlsplit(str(url)).scheme != 'https':
        yield url
        return
    routes = {}
    lock = threading.Lock()
    upstream = requests.Session()
    proxy = os.getenv('NETWORK_PROXY', '').strip()
    upstream.trust_env = os.getenv('DISABLE_ENV_PROXY', 'true').lower() not in ('1','true','yes','on')
    if proxy:
        upstream.proxies.update({'http': proxy, 'https': proxy})
    upstream.mount('https://', HTTPAdapter(pool_connections=8, pool_maxsize=8,
                                           max_retries=0, pool_block=True))

    def register(remote):
        if urlsplit(remote).scheme != 'https':
            raise ValueError('HLS resource must use HTTPS')
        suffix = PurePosixPath(urlsplit(remote).path).suffix
        if not re.fullmatch(r'\.[a-zA-Z0-9]{1,8}', suffix): suffix = '.ts'
        token = secrets.token_urlsafe(24)+suffix
        with lock: routes['/'+token] = remote
        return f'http://127.0.0.1:{server.server_port}/{token}'

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def log_message(self, *args):
            pass

        def do_GET(self):
            with lock: remote = routes.get(self.path)
            if remote is None:
                self.send_error(404)
                return
            try:
                headers = {'Accept-Encoding':'identity'}
                if self.headers.get('Range'): headers['Range'] = self.headers['Range']
                # Follow redirects explicitly to prevent an HTTPS downgrade.
                for _ in range(6):
                    response = upstream.get(remote, headers=headers, stream=True, timeout=(15,45), allow_redirects=False)
                    if response.is_redirect:
                        target = urljoin(remote,response.headers['Location'])
                        response.close()
                        if urlsplit(target).scheme != 'https': raise ValueError('HTTPS downgrade refused')
                        remote = target
                        continue
                    break
                else: raise ValueError('Too many source redirects')
                with response:
                    response.raise_for_status()
                    is_playlist = urlsplit(remote).path.endswith('.m3u8') or 'mpegurl' in response.headers.get('Content-Type','').lower()
                    if is_playlist:
                        raw = response.content
                        if len(raw)>8_000_000: raise ValueError('Playlist too large')
                        source_lines = raw.decode('utf-8-sig').splitlines()
                        max_height = max(0, int(os.getenv('HLS_MAX_HEIGHT', '720') or 0))
                        source_lines = _limit_master_variants(source_lines, max_height)
                        lines=[]
                        for line in source_lines:
                            if line.startswith('#'):
                                line = re.sub(r'URI="([^"]+)"', lambda m:'URI="'+register(urljoin(remote,m[1]))+'"',line)
                            elif line.strip(): line = register(urljoin(remote,line.strip()))
                            lines.append(line)
                        body=('\n'.join(lines)+'\n').encode()
                        self.send_response(200)
                        self.send_header('Content-Type','application/vnd.apple.mpegurl')
                        self.send_header('Content-Length',str(len(body)))
                        self.end_headers(); self.wfile.write(body)
                    else:
                        self.send_response(response.status_code)
                        for key in ('Content-Type','Content-Length','Content-Range','Accept-Ranges'):
                            if key in response.headers: self.send_header(key,response.headers[key])
                        self.end_headers()
                        for chunk in response.iter_content(256*1024): self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception as exc:
                print(f'[transport] ERROR verified HLS fetch failed ({type(exc).__name__})')
                try: self.send_error(502,'Verified upstream fetch failed')
                except OSError: pass

    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        yield register(url)
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)
        upstream.close()


def verified_source(function):
    @wraps(function)
    def wrapped(stream_url,*args,**kwargs):
        with verified_hls(stream_url) as local:
            return function(local,*args,**kwargs)
    return wrapped

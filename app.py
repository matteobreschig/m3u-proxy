import os
import re
import time
import logging
import requests
from flask import Flask, Response, request, redirect
from urllib.parse import quote

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config from env
SOURCE_URL = os.environ.get("SOURCE_PLAYLIST_URL", "")
MEDIAFLOW_URL = os.environ.get("MEDIAFLOW_URL", "http://mediaflow-proxy-light:8888")
MEDIAFLOW_PASSWORD = os.environ.get("MEDIAFLOW_PASSWORD", "")
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "3600"))
WATCH_CACHE_TTL = int(os.environ.get("WATCH_CACHE_TTL_SECONDS", "300"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")  # es. http://breschi.asuscomm.com:5000

# In-memory cache
_cache = {"content": None, "timestamp": 0}
# Slug -> parsed channel info (rebuilt every time playlist is parsed)
_channel_index = {}


def slugify(name):
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def get_playlist(ttl=None):
    now = time.time()
    effective_ttl = CACHE_TTL if ttl is None else ttl
    if _cache["content"] and (now - _cache["timestamp"]) < effective_ttl:
        logger.info("Serving from cache")
        return _cache["content"]

    logger.info(f"Fetching playlist from {SOURCE_URL}")
    resp = requests.get(SOURCE_URL, timeout=30)
    resp.raise_for_status()
    content = resp.text
    _cache["content"] = content
    _cache["timestamp"] = now
    return content


def detect_stream_type(url, extinf_block):
    """Detect stream type from URL and EXTINF metadata."""
    url_lower = url.lower()
    block_lower = extinf_block.lower()

    if "manifest_type=dash" in block_lower or url_lower.endswith(".mpd"):
        return "dash"
    elif url_lower.endswith(".m3u8") or "manifest_type=hls" in block_lower:
        return "hls"
    else:
        return "stream"


def extract_clearkey(block):
    """Extract ClearKey kid:key from KODIPROP lines."""
    match = re.search(
        r"#KODIPROP:inputstream\.adaptive\.license_key=([a-f0-9]{32}):([a-f0-9]{32})",
        block,
        re.IGNORECASE,
    )
    if match:
        return match.group(1), match.group(2)
    return None, None


def extract_headers(block):
    """Extract custom headers from EXTVLCOPT or similar lines."""
    headers = {}
    # EXTVLCOPT: http-referrer=
    for m in re.finditer(r"#EXTVLCOPT:http-referrer=(.*)", block):
        headers["Referer"] = m.group(1).strip()
    for m in re.finditer(r"#EXTVLCOPT:http-user-agent=(.*)", block):
        headers["User-Agent"] = m.group(1).strip()
    # tvg-style user-agent in EXTINF
    for m in re.finditer(r'user-agent="([^"]+)"', block, re.IGNORECASE):
        headers["User-Agent"] = m.group(1).strip()
    return headers


def build_proxy_url(stream_url, stream_type, key_id=None, key=None, headers=None):
    """Build the MediaFlow proxy URL for a given stream."""
    base = MEDIAFLOW_URL.rstrip("/")
    encoded_url = quote(stream_url, safe="")
    pwd = f"&api_password={MEDIAFLOW_PASSWORD}" if MEDIAFLOW_PASSWORD else ""

    if stream_type == "dash":
        path = "/proxy/mpd/manifest.m3u8"
        extra = "&audio_languages=ita,mul,eng"
        if key_id and key:
            extra += f"&key_id={key_id}&key={key}"
        return f"{base}{path}?d={encoded_url}{extra}{pwd}"

    elif stream_type == "hls":
        path = "/proxy/hls/manifest.m3u8"
        extra = ""
        if headers:
            for k, v in headers.items():
                extra += f"&h_{k}={quote(v, safe='')}"
        return f"{base}{path}?d={encoded_url}{extra}{pwd}"

    else:  # generic stream
        path = "/proxy/stream"
        extra = ""
        if headers:
            for k, v in headers.items():
                extra += f"&h_{k}={quote(v, safe='')}"
        return f"{base}{path}?d={encoded_url}{extra}{pwd}"


def convert_playlist(raw):
    """Parse the M3U playlist, build channel index, and emit stable /watch URLs."""
    lines = raw.splitlines()
    output = []
    i = 0

    new_index = {}
    used_slugs = {}

    base_public = (PUBLIC_URL or "").rstrip("/")

    while i < len(lines):
        line = lines[i]

        if line.startswith("#EXTM3U"):
            output.append(line)
            i += 1
            continue

        if line.startswith("#EXTINF"):
            block_lines = [line]
            i += 1

            while i < len(lines) and lines[i].startswith("#"):
                block_lines.append(lines[i])
                i += 1

            if i < len(lines) and not lines[i].startswith("#"):
                stream_url = lines[i].strip()
                i += 1

                if stream_url:
                    block_text = "\n".join(block_lines)

                    # Extract channel name for slug (tvg-name or trailing name after comma)
                    name_match = re.search(r'tvg-name="([^"]+)"', block_text)
                    if name_match:
                        channel_name = name_match.group(1)
                    else:
                        channel_name = block_lines[0].split(",")[-1].strip()

                    slug = slugify(channel_name) or f"ch{len(new_index) + 1}"
                    # avoid collisions
                    if slug in used_slugs:
                        used_slugs[slug] += 1
                        slug = f"{slug}-{used_slugs[slug]}"
                    else:
                        used_slugs[slug] = 0

                    stream_type = detect_stream_type(stream_url, block_text)
                    key_id, key = extract_clearkey(block_text)
                    headers = extract_headers(block_text)

                    new_index[slug] = {
                        "url": stream_url,
                        "type": stream_type,
                        "key_id": key_id,
                        "key": key,
                        "headers": headers,
                        "name": channel_name,
                    }

                    if base_public:
                        watch_url = f"{base_public}/watch/{slug}"
                    else:
                        watch_url = f"/watch/{slug}"

                    playlist_password = os.environ.get("PLAYLIST_PASSWORD", "")
                    if playlist_password:
                        watch_url += f"?token={quote(playlist_password, safe='')}"

                    output.append(block_lines[0])
                    output.append(watch_url)
                else:
                    output.extend(block_lines)
            else:
                output.extend(block_lines)
            continue

        output.append(line)
        i += 1

    _channel_index.clear()
    _channel_index.update(new_index)

    return "\n".join(output)


def ensure_index(ttl=None):
    """Make sure _channel_index is populated (parses playlist if needed)."""
    raw = get_playlist(ttl=ttl)
    convert_playlist(raw)


@app.route("/watch/<slug>")
def watch(slug):
    """Stable short URL. Resolves to a fresh MediaFlow proxy URL on each request."""
    playlist_password = os.environ.get("PLAYLIST_PASSWORD", "")
    if playlist_password:
        token = request.args.get("token", "")
        if token != playlist_password:
            return "Unauthorized", 401

    ensure_index(ttl=WATCH_CACHE_TTL)

    entry = _channel_index.get(slug)
    if not entry:
        return f"Channel '{slug}' not found", 404

    proxy_url = build_proxy_url(
        entry["url"], entry["type"], entry["key_id"], entry["key"], entry["headers"]
    )
    return redirect(proxy_url, code=302)


@app.route("/playlist.m3u")
def playlist():
    if not SOURCE_URL:
        return "SOURCE_PLAYLIST_URL not configured", 500

    playlist_password = os.environ.get("PLAYLIST_PASSWORD", "")
    if playlist_password:
        token = request.args.get("token", "")
        if token != playlist_password:
            return "Unauthorized", 401

    try:
        raw = get_playlist()
        converted = convert_playlist(raw)
        return Response(converted, mimetype="application/x-mpegurl")
    except Exception as e:
        logger.error(f"Error: {e}")
        return f"Error: {e}", 500


@app.route("/playlist/refresh", methods=["POST"])
def refresh():
    """Force cache invalidation."""
    _cache["timestamp"] = 0
    return "Cache cleared", 200


@app.route("/health")
def health():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

#!/usr/bin/env python3

import asyncio
import json
import logging
import os
import re
import shutil
import threading
from collections import namedtuple
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request as UrllibRequest
from uuid import uuid4

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Update
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL
from yt_dlp.networking import Request as YtDlpRequest


load_dotenv("/etc/xvbot/.env")


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise KeyError(f"{name} is required; set it in /etc/xvbot/.env")
    return value


def _positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _optional_http_url_env(name: str) -> str | None:
    value = os.getenv(name, "").strip().rstrip("/")
    if not value:
        return None
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"{name} must be an absolute HTTP(S) URL")
    return value


TOKEN = _required_env("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = int(_required_env("CHANNEL_ID"))
DOWNLOAD_TIMEOUT_SECONDS = _positive_int_env("DOWNLOAD_TIMEOUT_SECONDS", 60)
MAX_VIDEO_SIZE_MB = _positive_int_env("MAX_VIDEO_SIZE_MB", 50)
PREFERRED_VIDEO_HEIGHT = _positive_int_env("PREFERRED_VIDEO_HEIGHT", 720)
MIN_VIDEO_HEIGHT = _positive_int_env("MIN_VIDEO_HEIGHT", 720)
EPORNER_PREFERRED_VIDEO_HEIGHT = _positive_int_env(
    "EPORNER_PREFERRED_VIDEO_HEIGHT", PREFERRED_VIDEO_HEIGHT
)
EPORNER_MIN_VIDEO_HEIGHT = _positive_int_env(
    "EPORNER_MIN_VIDEO_HEIGHT", MIN_VIDEO_HEIGHT
)
REJECT_UNKNOWN_VIDEO_HEIGHT = _bool_env("REJECT_UNKNOWN_VIDEO_HEIGHT", True)
MAX_CONCURRENT_DOWNLOADS = _positive_int_env("MAX_CONCURRENT_DOWNLOADS", 1)
MIN_FREE_DISK_MB = _positive_int_env("MIN_FREE_DISK_MB", 4096)
DROP_PENDING_UPDATES = _bool_env("DROP_PENDING_UPDATES", False)
TELEGRAM_MEDIA_WRITE_TIMEOUT_SECONDS = _positive_int_env(
    "TELEGRAM_MEDIA_WRITE_TIMEOUT_SECONDS", 300
)
TELEGRAM_BOT_API_BASE_URL = _optional_http_url_env("TELEGRAM_BOT_API_BASE_URL")
TELEGRAM_BOT_API_FILE_URL = _optional_http_url_env("TELEGRAM_BOT_API_FILE_URL")
if bool(TELEGRAM_BOT_API_BASE_URL) != bool(TELEGRAM_BOT_API_FILE_URL):
    raise ValueError(
        "TELEGRAM_BOT_API_BASE_URL and TELEGRAM_BOT_API_FILE_URL must be configured together"
    )
if TELEGRAM_BOT_API_BASE_URL and not TELEGRAM_BOT_API_BASE_URL.endswith("/bot"):
    raise ValueError("TELEGRAM_BOT_API_BASE_URL must end with /bot")
if TELEGRAM_BOT_API_FILE_URL and not TELEGRAM_BOT_API_FILE_URL.endswith("/file/bot"):
    raise ValueError("TELEGRAM_BOT_API_FILE_URL must end with /file/bot")
if PREFERRED_VIDEO_HEIGHT < MIN_VIDEO_HEIGHT:
    raise ValueError("PREFERRED_VIDEO_HEIGHT must be at least MIN_VIDEO_HEIGHT")
if EPORNER_PREFERRED_VIDEO_HEIGHT < EPORNER_MIN_VIDEO_HEIGHT:
    raise ValueError(
        "EPORNER_PREFERRED_VIDEO_HEIGHT must be at least EPORNER_MIN_VIDEO_HEIGHT"
    )
USER_AGENT = os.getenv("REQUEST_USER_AGENT", "Mozilla/5.0 (compatible; XVBOT/1.0)")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = Path(os.getenv("LOG_DIR", "/var/log/xvbot"))
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID") or None
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024
MIN_FREE_DISK_BYTES = MIN_FREE_DISK_MB * 1024 * 1024
PROCESSING_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
TMP_DIR = Path(os.getenv("MEDIA_TMP_DIR", "/tmp"))
CHUNK_SIZE = 32 * 1024


LOG_DIR.mkdir(parents=True, exist_ok=True)
_formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")


class _SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = message.replace(TOKEN, "<redacted>")
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


_secret_filter = _SecretRedactionFilter()
_file_handler = RotatingFileHandler(
    LOG_DIR / "bot.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
)
_file_handler.setFormatter(_formatter)
_file_handler.addFilter(_secret_filter)
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_formatter)
_stream_handler.addFilter(_secret_filter)
_root_logger = logging.getLogger()
_root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
_root_logger.handlers.clear()
_root_logger.addHandler(_file_handler)
_root_logger.addHandler(_stream_handler)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
LOGGER = logging.getLogger("main")


VideoVariant = namedtuple(
    "VideoVariant",
    ["url", "quality_label", "bitrate", "size_bytes"],
    defaults=[None],
)


TWITTER_URL_RE = re.compile(
    r"https?://(www\.)?(twitter\.com|x\.com)/[A-Za-z0-9_]+/status/\d+",
    re.IGNORECASE,
)

REDGIFS_URL_RE = re.compile(
    r"https?://(www\.)?redgifs\.com/watch/[A-Za-z0-9_-]+/?",
    re.IGNORECASE,
)

EPORNER_URL_RE = re.compile(
    r"https?://(www\.)?eporner\.com/"
    r"(?:(?:hd-porn|embed)/[A-Za-z0-9_]+|video-[A-Za-z0-9_]+)"
    r"(?:/[A-Za-z0-9_-]+)?/?",
    re.IGNORECASE,
)


def _https_url(url: str) -> str:
    if url.lower().startswith("http://"):
        return "https://" + url[7:]
    return url


def extract_tweet_url(text: str) -> str | None:
    match = TWITTER_URL_RE.search(text)
    if not match:
        return None
    return _https_url(match.group(0))


def extract_redgifs_url(text: str) -> str | None:
    match = REDGIFS_URL_RE.search(text)
    if not match:
        return None
    return _https_url(match.group(0))


def extract_eporner_url(text: str) -> str | None:
    match = EPORNER_URL_RE.search(text)
    if not match:
        return None
    return _https_url(match.group(0))


TCO_URL_RE = re.compile(r"https?://t\.co/[A-Za-z0-9]+", re.IGNORECASE)


def _extract_tco_url(text: str) -> str | None:
    match = TCO_URL_RE.search(text)
    if not match:
        return None
    return _https_url(match.group(0))


async def resolve_tco_url(text: str, client: httpx.AsyncClient) -> str | None:
    tco_url = _extract_tco_url(text)
    if not tco_url:
        return None

    logger = logging.getLogger("tco_resolver")
    try:
        response = await client.head(tco_url, follow_redirects=True)
    except (httpx.HTTPError, Exception) as exc:
        logger.warning("failed to resolve t.co link: %s", exc)
        return None

    for response_url in [item.url for item in response.history] + [response.url]:
        source_url = extract_supported_url(str(response_url))
        if source_url:
            return source_url
    return None


def extract_supported_url(text: str) -> str | None:
    matches = [
        match
        for match in (
            TWITTER_URL_RE.search(text),
            REDGIFS_URL_RE.search(text),
            EPORNER_URL_RE.search(text),
        )
        if match
    ]
    if not matches:
        return None
    return _https_url(min(matches, key=lambda match: match.start()).group(0))


def _is_redgifs_url(url: str) -> bool:
    return REDGIFS_URL_RE.fullmatch(url) is not None


def _is_eporner_url(url: str) -> bool:
    return EPORNER_URL_RE.fullmatch(url) is not None


async def extract_message_source_url(text: str, client: httpx.AsyncClient) -> str | None:
    matches = [
        match
        for match in (
            TWITTER_URL_RE.search(text),
            REDGIFS_URL_RE.search(text),
            EPORNER_URL_RE.search(text),
            TCO_URL_RE.search(text),
        )
        if match
    ]
    if not matches:
        return None

    first_match = min(matches, key=lambda match: match.start())
    first_url = _https_url(first_match.group(0))
    if TCO_URL_RE.fullmatch(first_url):
        return await resolve_tco_url(first_url, client)
    return first_url


async def extract_message_tweet_url(text: str, client: httpx.AsyncClient) -> str | None:
    source_url = await extract_message_source_url(text, client)
    if source_url and TWITTER_URL_RE.fullmatch(source_url):
        return source_url
    return None


def _provider_headers(referer: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "application/json, text/html, */*",
    }


def _https_variant_url(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.lower().startswith("https://"):
        return None
    return url


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        digits = re.sub(r"\D", "", value)
        if digits:
            return int(digits)
    return None


def _extract_redgifs_id(redgifs_url: str) -> str | None:
    path_parts = [part for part in urlparse(redgifs_url).path.split("/") if part]
    if len(path_parts) < 2 or path_parts[0].lower() != "watch":
        return None
    return path_parts[1]


def _redgifs_variant_label(gif: dict) -> str | None:
    width = _optional_int(gif.get("width"))
    height = _optional_int(gif.get("height"))
    if width and height:
        return f"{width}x{height}"
    return None


async def provider_redgifs(
    redgifs_url: str,
    client: httpx.AsyncClient,
) -> list[VideoVariant] | None:
    logger = logging.getLogger("provider_redgifs")
    redgifs_id = _extract_redgifs_id(redgifs_url)
    if not redgifs_id:
        return None

    try:
        token_response = await client.get(
            "https://api.redgifs.com/v2/auth/temporary",
            headers=_provider_headers("https://www.redgifs.com/"),
        )
        token_response.raise_for_status()
        token = token_response.json().get("token")
        if not isinstance(token, str) or not token:
            return None

        media_response = await client.get(
            f"https://api.redgifs.com/v2/gifs/{redgifs_id}",
            headers={
                **_provider_headers(redgifs_url),
                "Authorization": f"Bearer {token}",
            },
        )
        media_response.raise_for_status()
        payload = media_response.json()
        gif = payload.get("gif")
        if not isinstance(gif, dict):
            return None
        urls = gif.get("urls")
        if not isinstance(urls, dict):
            return None

        variants = []
        quality_label = _redgifs_variant_label(gif)
        for key in ("hd", "sd", "file", "file_url"):
            variant_url = _https_variant_url(urls.get(key))
            if not variant_url:
                continue
            variants.append(
                VideoVariant(
                    url=variant_url,
                    quality_label=quality_label if key in ("hd", "file", "file_url") else None,
                    bitrate=None,
                )
            )
        return variants or None
    except Exception as exc:
        logger.warning("provider failed: %s", exc)
        return None


class _HttpsOnlyYoutubeDL(YoutubeDL):
    def urlopen(self, request):
        if isinstance(request, str):
            request = _https_url(request)
        elif isinstance(request, YtDlpRequest):
            request.update(url=_https_url(request.url))
        elif isinstance(request, UrllibRequest) and request.full_url.lower().startswith("http://"):
            request = UrllibRequest(
                _https_url(request.full_url),
                data=request.data,
                headers=dict(request.headers),
                method=request.get_method(),
            )
        return super().urlopen(request)


def _extract_eporner_variants(eporner_url: str) -> list[VideoVariant]:
    options = {
        "cachedir": False,
        "extractor_retries": 1,
        "fragment_retries": 1,
        "http_headers": {"User-Agent": USER_AGENT},
        "noplaylist": True,
        "no_warnings": True,
        "quiet": True,
        "retries": 1,
        "skip_download": True,
        "socket_timeout": DOWNLOAD_TIMEOUT_SECONDS,
    }
    with _HttpsOnlyYoutubeDL(options) as downloader:
        info = downloader.extract_info(eporner_url, download=False)

    if not isinstance(info, dict):
        return []

    variants = []
    seen_urls = set()
    for item in info.get("formats") or []:
        if not isinstance(item, dict):
            continue
        protocol = item.get("protocol")
        if protocol not in (None, "http", "https"):
            continue
        if item.get("ext") != "mp4":
            continue
        vcodec = item.get("vcodec")
        if vcodec == "none" or item.get("acodec") == "none":
            continue

        raw_url = item.get("url")
        normalized_vcodec = vcodec.lower() if isinstance(vcodec, str) else ""
        raw_path = urlparse(raw_url).path.lower() if isinstance(raw_url, str) else ""
        if normalized_vcodec.startswith(("av1", "av01")) or "-av1." in raw_path:
            continue
        variant_url = _https_variant_url(_https_url(raw_url)) if isinstance(raw_url, str) else None
        if not variant_url or variant_url in seen_urls:
            continue
        seen_urls.add(variant_url)

        width = _optional_int(item.get("width"))
        height = _optional_int(item.get("height"))
        if width and height:
            quality_label = f"{width}x{height}"
        elif height:
            quality_label = f"{height}p"
        else:
            quality_label = item.get("format_note") if isinstance(item.get("format_note"), str) else None

        tbr = item.get("tbr")
        bitrate = int(tbr * 1000) if isinstance(tbr, (int, float)) else None
        size_bytes = _optional_int(item.get("filesize")) or _optional_int(item.get("filesize_approx"))
        variants.append(VideoVariant(variant_url, quality_label, bitrate, size_bytes))
    return variants


async def provider_eporner(
    eporner_url: str,
    client: httpx.AsyncClient,
) -> list[VideoVariant] | None:
    del client
    logger = logging.getLogger("provider_eporner")
    try:
        result = {}

        def extract():
            try:
                result["variants"] = _extract_eporner_variants(eporner_url)
            except Exception as exc:
                result["exception"] = exc

        worker = threading.Thread(target=extract, name="eporner-extractor", daemon=True)
        worker.start()
        while worker.is_alive():
            await asyncio.sleep(0.05)
        if "exception" in result:
            raise result["exception"]
        variants = result.get("variants", [])
        return variants or None
    except Exception as exc:
        logger.warning("provider failed: %s", exc)
        return None


async def provider_savetwt(
    tweet_url: str,
    client: httpx.AsyncClient,
) -> list[VideoVariant] | None:
    logger = logging.getLogger("provider_savetwt")
    try:
        response = await client.post(
            "https://savetwt.com/download",
            data={"url": tweet_url},
            headers=_provider_headers("https://savetwt.com/"),
        )
        response.raise_for_status()
        payload = response.json()
        links = payload.get("links", [])
        variants = []
        for item in links:
            variant_url = _https_variant_url(item.get("url"))
            if not variant_url:
                continue
            variants.append(
                VideoVariant(
                    url=variant_url,
                    quality_label=item.get("quality"),
                    bitrate=None,
                )
            )
        return variants or None
    except Exception as exc:
        logger.warning("provider failed: %s", exc)
        return None


async def provider_ssstwitter(
    tweet_url: str,
    client: httpx.AsyncClient,
) -> list[VideoVariant] | None:
    logger = logging.getLogger("provider_ssstwitter")
    try:
        response = await client.post(
            "https://ssstwitter.com/",
            data={"id": tweet_url},
            headers=_provider_headers("https://ssstwitter.com/"),
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        variants = []
        for link in soup.find_all("a"):
            href = _https_variant_url(link.get("href"))
            if not href:
                continue
            if not urlparse(href).path.lower().endswith(".mp4"):
                continue
            quality_label = link.get_text(" ", strip=True) or None
            variants.append(VideoVariant(url=href, quality_label=quality_label, bitrate=None))
        return variants or None
    except Exception as exc:
        logger.warning("provider failed: %s", exc)
        return None


async def provider_tweeload(
    tweet_url: str,
    client: httpx.AsyncClient,
) -> list[VideoVariant] | None:
    logger = logging.getLogger("provider_tweeload")
    try:
        response = await client.post(
            "https://tweeload.com/en/download",
            json={"url": tweet_url},
            headers=_provider_headers("https://tweeload.com/"),
        )
        response.raise_for_status()
        payload = response.json()
        links = payload.get("data", {}).get("links", [])
        variants = []
        for item in links:
            variant_url = _https_variant_url(item.get("url"))
            if not variant_url:
                continue
            bitrate = item.get("bitrate")
            variants.append(
                VideoVariant(
                    url=variant_url,
                    quality_label=None,
                    bitrate=bitrate if isinstance(bitrate, int) else None,
                )
            )
        return variants or None
    except Exception as exc:
        logger.warning("provider failed: %s", exc)
        return None


async def provider_twittervideodownloader(
    tweet_url: str,
    client: httpx.AsyncClient,
) -> list[VideoVariant] | None:
    logger = logging.getLogger("provider_twittervideodownloader")
    try:
        response = await client.post(
            "https://twittervideodownloader.com/en/",
            data={"tweet": tweet_url},
            headers=_provider_headers("https://twittervideodownloader.com/en/"),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return None
        variants = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            variant_url = _https_variant_url(item.get("url"))
            if not variant_url:
                continue
            variants.append(
                VideoVariant(
                    url=variant_url,
                    quality_label=item.get("resolution"),
                    bitrate=None,
                )
            )
        return variants or None
    except Exception as exc:
        logger.warning("provider failed: %s", exc)
        return None


def _extract_json_blob(text: str, start_index: int) -> str | None:
    opening = text[start_index]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escape = False

    for index in range(start_index, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start_index : index + 1]
    return None


def _extract_twmate_video_data(html: str) -> object | None:
    match = re.search(r"video_data\s*(?:=|:)\s*([\[{])", html, re.DOTALL)
    if not match:
        return None
    blob = _extract_json_blob(html, match.start(1))
    if not blob:
        return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


def _iter_twmate_video_items(video_data: object) -> list[dict]:
    if isinstance(video_data, list):
        return [item for item in video_data if isinstance(item, dict)]
    if not isinstance(video_data, dict):
        return []

    for key in ("variants", "videos", "links"):
        items = video_data.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]

    nested_video = video_data.get("video")
    if isinstance(nested_video, dict):
        for key in ("variants", "videos", "links"):
            items = nested_video.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]

    return [video_data]


def _parse_twmate_html_variants(html: str) -> list[VideoVariant]:
    soup = BeautifulSoup(html, "lxml")
    variants = []
    for link in soup.select("a.btn-dl[href]"):
        variant_url = _https_variant_url(link.get("href"))
        if not variant_url:
            continue
        row = link.find_parent("tr")
        cells = row.find_all("td") if row else []
        quality_label = cells[0].get_text(" ", strip=True) if cells else None
        media_type = cells[1].get_text(" ", strip=True).lower() if len(cells) > 1 else ""
        if media_type and "mp4" not in media_type:
            continue
        variants.append(
            VideoVariant(
                url=variant_url,
                quality_label=quality_label,
                bitrate=None,
            )
        )
    return variants


async def provider_twmate(
    tweet_url: str,
    client: httpx.AsyncClient,
) -> list[VideoVariant] | None:
    logger = logging.getLogger("provider_twmate")
    try:
        response = await client.post(
            "https://twmate.com/en2/",
            data={"page": tweet_url, "ftype": "all"},
            headers=_provider_headers("https://twmate.com/en2/"),
        )
        response.raise_for_status()
        video_data = _extract_twmate_video_data(response.text)
        variants = []
        for item in _iter_twmate_video_items(video_data):
            variant_url = _https_variant_url(item.get("url") or item.get("src"))
            if not variant_url:
                continue
            variants.append(
                VideoVariant(
                    url=variant_url,
                    quality_label=item.get("quality") or item.get("resolution"),
                    bitrate=_optional_int(item.get("bitrate")),
                )
            )
        return variants or _parse_twmate_html_variants(response.text) or None
    except Exception as exc:
        logger.warning("provider failed: %s", exc)
        return None


async def provider_getxbot(
    tweet_url: str,
    client: httpx.AsyncClient,
) -> list[VideoVariant] | None:
    logger = logging.getLogger("provider_getxbot")
    try:
        response = await client.post(
            "https://www.getxbot.com/",
            json={"url": tweet_url},
            headers=_provider_headers("https://www.getxbot.com/"),
        )
        response.raise_for_status()
        payload = response.json()
        videos = payload.get("result", {}).get("videos", [])
        variants = []
        for item in videos:
            if not isinstance(item, dict):
                continue
            variant_url = _https_variant_url(item.get("url"))
            if not variant_url:
                continue
            variants.append(
                VideoVariant(
                    url=variant_url,
                    quality_label=item.get("quality") or item.get("resolution"),
                    bitrate=_optional_int(item.get("bitrate")),
                )
            )
        return variants or None
    except Exception as exc:
        logger.warning("provider failed: %s", exc)
        return None


PROVIDERS = [
    provider_savetwt,
    provider_ssstwitter,
    provider_tweeload,
    provider_twittervideodownloader,
    provider_twmate,
    provider_getxbot,
]

REDGIFS_PROVIDERS = [
    provider_redgifs,
]

EPORNER_PROVIDERS = [
    provider_eporner,
]


RESOLUTION_RE = re.compile(r"(\d{2,5})\s*[xX]\s*(\d{2,5})")
HEIGHT_RE = re.compile(r"(\d{2,5})\s*[pP]")


def _resolution_pixels(quality_label: str | None) -> int | None:
    if not quality_label:
        return None
    match = RESOLUTION_RE.search(quality_label)
    if not match:
        return None
    return int(match.group(1)) * int(match.group(2))


def _resolution_height(quality_label: str | None) -> int | None:
    if not quality_label:
        return None
    match = RESOLUTION_RE.search(quality_label)
    if match:
        width, height = int(match.group(1)), int(match.group(2))
        return min(width, height)
    match = HEIGHT_RE.search(quality_label)
    return int(match.group(1)) if match else None


def _variant_quality_key(variant: VideoVariant) -> tuple[int, int]:
    return (
        variant.bitrate if variant.bitrate is not None else -1,
        _resolution_pixels(variant.quality_label) or -1,
    )


def pick_best_variant(
    variants: list[VideoVariant],
    preferred_height: int | None = None,
    minimum_height: int | None = None,
    reject_unknown_height: bool | None = None,
    max_size_bytes: int | None = None,
) -> VideoVariant | None:
    target_height = PREFERRED_VIDEO_HEIGHT if preferred_height is None else preferred_height
    minimum = MIN_VIDEO_HEIGHT if minimum_height is None else minimum_height
    reject_unknown = (
        REJECT_UNKNOWN_VIDEO_HEIGHT
        if reject_unknown_height is None
        else reject_unknown_height
    )
    size_eligible = [
        variant
        for variant in variants
        if max_size_bytes is None
        or variant.size_bytes is None
        or variant.size_bytes <= max_size_bytes
    ]
    variants_by_height = [
        (variant, _resolution_height(variant.quality_label))
        for variant in size_eligible
        if (_resolution_height(variant.quality_label) or 0) >= minimum
    ]
    exact = [variant for variant, height in variants_by_height if height == target_height]
    if exact:
        return max(exact, key=_variant_quality_key)

    above = [(variant, height) for variant, height in variants_by_height if height > target_height]
    if above:
        best_height = min(height for _, height in above)
        return max(
            (variant for variant, height in above if height == best_height),
            key=_variant_quality_key,
        )

    if reject_unknown:
        return None
    unknown = [
        variant
        for variant in size_eligible
        if _resolution_height(variant.quality_label) is None
    ]
    if unknown:
        logging.getLogger("variant_selector").warning(
            "selecting a final-fallback variant with unknown resolution"
        )
        return max(unknown, key=_variant_quality_key)
    return None


def pick_best_fitting_variant(
    variants: list[VideoVariant],
    max_size_bytes: int,
    preferred_height: int,
    minimum_height: int | None = None,
    reject_unknown_height: bool | None = None,
) -> VideoVariant | None:
    return pick_best_variant(
        variants,
        preferred_height=preferred_height,
        minimum_height=minimum_height,
        reject_unknown_height=reject_unknown_height,
        max_size_bytes=max_size_bytes,
    )


def _providers_for_url(source_url: str):
    if _is_redgifs_url(source_url):
        return REDGIFS_PROVIDERS
    if _is_eporner_url(source_url):
        return EPORNER_PROVIDERS
    if TWITTER_URL_RE.fullmatch(source_url):
        return PROVIDERS
    return []


def _preferred_height_for_url(source_url: str) -> int:
    if _is_eporner_url(source_url):
        return EPORNER_PREFERRED_VIDEO_HEIGHT
    return PREFERRED_VIDEO_HEIGHT


def _minimum_height_for_url(source_url: str) -> int:
    if _is_eporner_url(source_url):
        return EPORNER_MIN_VIDEO_HEIGHT
    return MIN_VIDEO_HEIGHT


CONTENT_RANGE_TOTAL_RE = re.compile(r"/(\d+)$")


async def _probe_eporner_variant_sizes(
    source_url: str,
    variants: list[VideoVariant],
    client: httpx.AsyncClient,
) -> list[VideoVariant]:
    logger = logging.getLogger("provider_eporner")
    measured = []
    for variant in variants:
        if variant.size_bytes is not None:
            measured.append(variant)
            continue
        try:
            async with client.stream(
                "GET",
                variant.url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": source_url,
                    "Accept": "*/*",
                    "Range": "bytes=0-0",
                },
            ) as response:
                response.raise_for_status()
                content_range = response.headers.get("Content-Range", "")
                match = CONTENT_RANGE_TOTAL_RE.search(content_range)
                size_bytes = int(match.group(1)) if match else None
                if size_bytes is None and response.status_code == 200:
                    size_bytes = _optional_int(response.headers.get("Content-Length"))
                measured.append(variant._replace(size_bytes=size_bytes))
        except Exception as exc:
            logger.warning("variant size probe failed for %s: %s", variant.quality_label, exc)
            measured.append(variant)
    return measured


def _has_sufficient_free_disk(required_bytes: int = 0) -> bool:
    try:
        free_bytes = shutil.disk_usage(TMP_DIR).free
    except OSError as exc:
        logging.getLogger("download_best_video").error(
            "cannot determine free media disk space: %s", exc
        )
        return False
    return free_bytes >= MIN_FREE_DISK_BYTES + max(required_bytes, 0)


async def _download_variant(
    client: httpx.AsyncClient,
    variant: VideoVariant,
    source_url: str,
    temp_path: Path,
) -> bool:
    logger = logging.getLogger("download_best_video")
    expected_size = variant.size_bytes or 0
    if not _has_sufficient_free_disk(expected_size):
        logger.error("insufficient free disk space for media download")
        return False

    bytes_written = 0
    completed = False
    try:
        async with client.stream(
            "GET",
            variant.url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": source_url,
                "Accept": "*/*",
            },
        ) as response:
            response.raise_for_status()
            content_length = _optional_int(response.headers.get("Content-Length"))
            if content_length is not None and content_length > MAX_VIDEO_SIZE_BYTES:
                logger.warning("download rejected by Content-Length upload cap")
                return False

            with temp_path.open("wb") as output:
                async for chunk in response.aiter_bytes(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    bytes_written += len(chunk)
                    if bytes_written > MAX_VIDEO_SIZE_BYTES:
                        logger.warning("download stopped after crossing upload cap")
                        return False
                    if not _has_sufficient_free_disk(len(chunk)):
                        logger.error("download stopped to preserve minimum free disk space")
                        return False
                    output.write(chunk)
        if bytes_written <= 0 or not temp_path.exists():
            return False
        final_size = temp_path.stat().st_size
        if final_size <= 0 or final_size > MAX_VIDEO_SIZE_BYTES:
            return False
        completed = True
        return True
    except Exception:
        raise
    finally:
        if not completed:
            temp_path.unlink(missing_ok=True)


async def validate_media(
    video_path: Path,
    minimum_height: int | None = None,
    require_audio: bool = True,
) -> bool:
    logger = logging.getLogger("media_validation")
    minimum = MIN_VIDEO_HEIGHT if minimum_height is None else minimum_height
    try:
        size_bytes = video_path.stat().st_size
    except OSError as exc:
        logger.warning("media file cannot be inspected: %s", exc)
        return False
    if size_bytes <= 0 or size_bytes > MAX_VIDEO_SIZE_BYTES:
        logger.warning("media file size is invalid or exceeds the upload cap")
        return False

    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(video_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
    except (OSError, asyncio.SubprocessError) as exc:
        logger.error("ffprobe could not validate media: %s", exc)
        return False
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        logger.warning("ffprobe rejected media: %s", detail or "unknown probe error")
        return False

    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        logger.warning("ffprobe returned invalid JSON")
        return False

    format_data = payload.get("format")
    streams = payload.get("streams")
    if not isinstance(format_data, dict) or not isinstance(streams, list):
        return False
    format_names = str(format_data.get("format_name", "")).lower().split(",")
    if "mp4" not in format_names:
        logger.warning("media container is not MP4")
        return False
    try:
        duration = float(format_data.get("duration", 0))
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        logger.warning("media duration is missing or invalid")
        return False

    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not video_streams:
        logger.warning("media must contain a video stream")
        return False
    if require_audio and not audio_streams:
        logger.warning("media must contain an audio stream for this source")
        return False
    video_stream = video_streams[0]
    if str(video_stream.get("codec_name", "")).lower() != "h264":
        logger.warning("media video codec is not H.264/AVC")
        return False
    if audio_streams and not any(
        str(stream.get("codec_name", "")).lower() == "aac"
        for stream in audio_streams
    ):
        logger.warning("media audio codec is not AAC")
        return False
    width = _optional_int(video_stream.get("width"))
    height = _optional_int(video_stream.get("height"))
    if not width or not height or min(width, height) < minimum:
        logger.warning("media resolution is below the configured minimum")
        return False
    return True


async def download_best_video(source_url: str) -> Path | None:
    logger = logging.getLogger("download_best_video")
    providers = _providers_for_url(source_url)
    timeout = httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for provider in providers:
            provider_name = provider.__name__
            try:
                variants = await provider(source_url, client)
            except Exception as exc:
                logging.getLogger(provider_name).warning("provider raised: %s", exc)
                variants = None

            if not variants:
                continue

            preferred_height = _preferred_height_for_url(source_url)
            minimum_height = _minimum_height_for_url(source_url)
            if _is_eporner_url(source_url):
                variants = await _probe_eporner_variant_sizes(source_url, variants, client)
                best = pick_best_fitting_variant(
                    variants,
                    max_size_bytes=MAX_VIDEO_SIZE_BYTES,
                    preferred_height=preferred_height,
                    minimum_height=minimum_height,
                )
                if best is None:
                    logger.error("no Eporner variant satisfies size and resolution policy")
                    continue
            else:
                best = pick_best_variant(
                    variants,
                    preferred_height=preferred_height,
                    minimum_height=minimum_height,
                    max_size_bytes=MAX_VIDEO_SIZE_BYTES,
                )
                if best is None:
                    logger.warning(
                        "%s returned no variant satisfying size and resolution policy",
                        provider_name,
                    )
                    continue
            if not best.url.lower().startswith("https://"):
                logger.warning("skipping non-HTTPS video URL from %s", provider_name)
                continue

            try:
                TMP_DIR.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.error("media temporary directory is unavailable: %s", exc)
                return None
            temp_path = TMP_DIR / f"xvbot_{uuid4().hex}.mp4"
            try:
                downloaded = await _download_variant(client, best, source_url, temp_path)
                if downloaded and await validate_media(
                    temp_path,
                    minimum_height,
                    require_audio=not _is_redgifs_url(source_url),
                ):
                    return temp_path
                temp_path.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning("download failed after %s returned variants: %s", provider_name, exc)
                temp_path.unlink(missing_ok=True)
                continue

    return None


def _file_too_large_error(exc: TelegramError) -> bool:
    message = str(exc).lower()
    return "file is too big" in message or "request entity too large" in message


async def _send_video_or_document(ctx: ContextTypes.DEFAULT_TYPE, video_path: Path, source_url: str) -> bool:
    logger = logging.getLogger("handle_message")
    if video_path.stat().st_size > MAX_VIDEO_SIZE_BYTES:
        logger.error("video exceeds Telegram upload cap; skipping upload")
        return False

    with video_path.open("rb") as video:
        for attempt, delay in enumerate([1, 4, 16, None], start=1):
            try:
                await ctx.bot.send_video(
                    chat_id=CHANNEL_ID,
                    video=video,
                    caption=source_url,
                    supports_streaming=True,
                )
                return True
            except BadRequest as exc:
                if _file_too_large_error(exc):
                    logger.error("Telegram rejected video as too large: %s", exc)
                    return False
                logger.error("send_video failed: %s", exc)
                return False
            except NetworkError as exc:
                if _file_too_large_error(exc):
                    logger.error("Telegram rejected video as too large: %s", exc)
                    return False
                if delay is None:
                    logger.error("send_video failed after retries: %s", exc)
                    return False
                logger.warning("send_video network error on attempt %s: %s", attempt, exc)
                video.seek(0)
                await asyncio.sleep(delay)
            except TelegramError as exc:
                if _file_too_large_error(exc):
                    logger.error("Telegram rejected video as too large: %s", exc)
                    return False
                logger.error("send_video failed: %s", exc)
                return False
    return False


async def _alert_admin(ctx: ContextTypes.DEFAULT_TYPE, source_url: str) -> None:
    if not ADMIN_CHAT_ID:
        return
    logger = logging.getLogger("handle_message")
    try:
        admin_chat_id = int(ADMIN_CHAT_ID)
    except ValueError:
        logger.warning("ADMIN_CHAT_ID is not an integer; skipping admin alert")
        return
    if admin_chat_id == CHANNEL_ID:
        logger.warning("ADMIN_CHAT_ID matches CHANNEL_ID; skipping plain-text admin alert")
        return
    try:
        await ctx.bot.send_message(
            chat_id=admin_chat_id,
            text=f"XVBOT failed to download: {source_url}",
        )
    except TelegramError as exc:
        logger.warning("admin alert failed: %s", exc)


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    logger = logging.getLogger("handle_message")
    async with PROCESSING_SEMAPHORE:
        temp_path = None
        try:
            message = update.channel_post
            if not message or not message.text:
                return

            async with httpx.AsyncClient(timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS)) as client:
                source_url = await extract_message_source_url(message.text, client)
            if not source_url:
                return

            temp_path = await download_best_video(source_url)
            if temp_path is None:
                logger.error("all providers failed for URL")
                await _alert_admin(ctx, source_url)
                return

            uploaded = await _send_video_or_document(ctx, temp_path, source_url)
            if not uploaded:
                return

            try:
                await ctx.bot.delete_message(
                    chat_id=CHANNEL_ID,
                    message_id=message.message_id,
                )
            except TelegramError as exc:
                logger.warning("delete_message failed: %s", exc)
        except Exception:
            logger.critical("unhandled handler exception", exc_info=True)
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)


def build_application() -> Application:
    builder = Application.builder().token(TOKEN)
    if TELEGRAM_BOT_API_BASE_URL:
        builder = (
            builder.base_url(TELEGRAM_BOT_API_BASE_URL)
            .base_file_url(TELEGRAM_BOT_API_FILE_URL)
            .media_write_timeout(TELEGRAM_MEDIA_WRITE_TIMEOUT_SECONDS)
        )
    return builder.build()


def main():
    LOGGER.info("starting xvbot")
    app = build_application()
    app.add_handler(MessageHandler(filters.Chat(CHANNEL_ID), handle_message))
    app.run_polling(drop_pending_updates=DROP_PENDING_UPDATES)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3

import asyncio
import json
import logging
import os
import re
from collections import namedtuple
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import InputMediaPhoto, Update
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


load_dotenv("/etc/xvbot/.env")


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise KeyError(f"{name} is required; set it in /etc/xvbot/.env")
    return value


TOKEN = _required_env("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = int(_required_env("CHANNEL_ID"))
DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "60"))
MAX_VIDEO_SIZE_MB = int(os.getenv("MAX_VIDEO_SIZE_MB", "50"))
USER_AGENT = os.getenv("REQUEST_USER_AGENT", "Mozilla/5.0 (compatible; XVBOT/1.0)")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = Path(os.getenv("LOG_DIR", "/var/log/xvbot"))
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID") or None
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024
PROCESSING_SEMAPHORE = asyncio.Semaphore(3)
_fetch_active = False
_fetch_cancelled = False
TMP_DIR = Path("/tmp")
CHUNK_SIZE = 32 * 1024


LOG_DIR.mkdir(parents=True, exist_ok=True)
_formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
_file_handler = RotatingFileHandler(
    LOG_DIR / "bot.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
)
_file_handler.setFormatter(_formatter)
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_formatter)
_root_logger = logging.getLogger()
_root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
_root_logger.handlers.clear()
_root_logger.addHandler(_file_handler)
_root_logger.addHandler(_stream_handler)
LOGGER = logging.getLogger("main")


VideoVariant = namedtuple("VideoVariant", ["url", "quality_label", "bitrate"])


TWITTER_URL_RE = re.compile(
    r"https?://(www\.)?(twitter\.com|x\.com)/[A-Za-z0-9_]+/status/\d+",
    re.IGNORECASE,
)

REDGIFS_URL_RE = re.compile(
    r"https?://(www\.)?redgifs\.com/watch/[A-Za-z0-9_-]+/?",
    re.IGNORECASE,
)

EROME_URL_RE = re.compile(
    r"https?://(?:(?:www|[a-z]{2,3})\.)?erome\.com/a/[A-Za-z0-9]+/?",
    re.IGNORECASE,
)

REDGIFS_USER_URL_RE = re.compile(
    r"https?://(www\.)?redgifs\.com/users/([A-Za-z0-9_-]+)/?",
    re.IGNORECASE,
)

REDDIT_URL_RE = re.compile(
    r"https?://(www\.)?reddit\.com/(r|u|user)/([A-Za-z0-9_]+)/?",
    re.IGNORECASE,
)

CHAN4_URL_RE = re.compile(
    r"https?://boards\.4chan(?:nel)?\.org/([A-Za-z0-9]+)/thread/(\d+)",
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


def extract_erome_url(text: str) -> str | None:
    match = EROME_URL_RE.search(text)
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
            EROME_URL_RE.search(text),
        )
        if match
    ]
    if not matches:
        return None
    return _https_url(min(matches, key=lambda match: match.start()).group(0))


def _is_redgifs_url(url: str) -> bool:
    return REDGIFS_URL_RE.fullmatch(url) is not None


def _is_erome_url(url: str) -> bool:
    return EROME_URL_RE.fullmatch(url) is not None


async def extract_message_source_url(text: str, client: httpx.AsyncClient) -> str | None:
    matches = [
        match
        for match in (
            TWITTER_URL_RE.search(text),
            REDGIFS_URL_RE.search(text),
            EROME_URL_RE.search(text),
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


async def provider_erodown(
    erome_url: str,
    client: httpx.AsyncClient,
) -> list[VideoVariant] | None:
    """Return only the first valid video from an Erome album."""
    logger = logging.getLogger("provider_erodown")
    try:
        response = await client.post(
            "https://erodown.com/download",
            json={"url": erome_url},
            headers=_provider_headers("https://erodown.com/"),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("success") is not True:
            return None

        media = payload.get("media")
        if not isinstance(media, list):
            return None

        for item in media:
            if not isinstance(item, dict) or str(item.get("type", "")).lower() != "video":
                continue
            variant_url = _https_variant_url(item.get("url"))
            if not variant_url:
                continue
            return [
                VideoVariant(
                    url=variant_url,
                    quality_label=item.get("quality") or item.get("resolution"),
                    bitrate=_optional_int(item.get("bitrate")),
                )
            ]
        return None
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

EROME_PROVIDERS = [
    provider_erodown,
]


def _providers_for_url(source_url: str):
    if _is_redgifs_url(source_url):
        return REDGIFS_PROVIDERS
    if _is_erome_url(source_url):
        return EROME_PROVIDERS
    return PROVIDERS


def _extract_tweet_id(tweet_url: str) -> str | None:
    match = re.search(r"/status/(\d+)", tweet_url)
    return match.group(1) if match else None


async def fetch_tweet_images(tweet_url: str, client: httpx.AsyncClient) -> list[str] | None:
    logger = logging.getLogger("fetch_tweet_images")
    tweet_id = _extract_tweet_id(tweet_url)
    if not tweet_id:
        return None
    try:
        response = await client.get(
            f"https://api.fxtwitter.com/i/status/{tweet_id}",
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        payload = response.json()
        photos = payload.get("tweet", {}).get("media", {}).get("photos", [])
        urls = [p["url"] for p in photos if isinstance(p, dict) and isinstance(p.get("url"), str)]
        return urls or None
    except Exception as exc:
        logger.warning("fxtwitter failed: %s", exc)
        return None


RESOLUTION_RE = re.compile(r"(\d{2,5})\s*[xX]\s*(\d{2,5})")


def _resolution_pixels(quality_label: str | None) -> int | None:
    if not quality_label:
        return None
    match = RESOLUTION_RE.search(quality_label)
    if not match:
        return None
    return int(match.group(1)) * int(match.group(2))


def pick_best_variant(variants: list[VideoVariant]) -> VideoVariant:
    for variant in sorted(
        variants,
        key=lambda item: item.bitrate if item.bitrate is not None else -1,
        reverse=True,
    ):
        if variant.bitrate is not None:
            return variant

    for variant in sorted(
        variants,
        key=lambda item: _resolution_pixels(item.quality_label) or -1,
        reverse=True,
    ):
        if _resolution_pixels(variant.quality_label) is not None:
            return variant

    return variants[0]


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

            best = pick_best_variant(variants)
            if not best.url.lower().startswith("https://"):
                logger.warning("skipping non-HTTPS video URL from %s", provider_name)
                continue

            temp_path = TMP_DIR / f"xvbot_{uuid4().hex}.mp4"
            try:
                async with client.stream(
                    "GET",
                    best.url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Referer": source_url,
                        "Accept": "*/*",
                    },
                ) as response:
                    response.raise_for_status()
                    with temp_path.open("wb") as output:
                        async for chunk in response.aiter_bytes(chunk_size=CHUNK_SIZE):
                            if chunk:
                                output.write(chunk)
                if temp_path.exists() and temp_path.stat().st_size > 0:
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
        logger.info("video exceeds send_video size cap; sending as document")
        with video_path.open("rb") as document:
            try:
                await ctx.bot.send_document(
                    chat_id=CHANNEL_ID,
                    document=document,
                    caption=source_url,
                )
                return True
            except TelegramError as exc:
                logger.error("send_document failed: %s", exc)
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
                    break
                logger.error("send_video failed: %s", exc)
                return False
            except NetworkError as exc:
                if delay is None:
                    logger.error("send_video failed after retries: %s", exc)
                    return False
                logger.warning("send_video network error on attempt %s: %s", attempt, exc)
                video.seek(0)
                await asyncio.sleep(delay)
            except TelegramError as exc:
                logger.error("send_video failed: %s", exc)
                return False

    with video_path.open("rb") as document:
        try:
            await ctx.bot.send_document(
                chat_id=CHANNEL_ID,
                document=document,
                caption=source_url,
            )
            return True
        except TelegramError as exc:
            logger.error("send_document failed: %s", exc)
            return False


async def _send_images(ctx: ContextTypes.DEFAULT_TYPE, photo_urls: list[str], source_url: str) -> bool:
    logger = logging.getLogger("handle_message")
    try:
        if len(photo_urls) == 1:
            await ctx.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=photo_urls[0],
                caption=source_url,
            )
        else:
            media = [
                InputMediaPhoto(media=url, caption=source_url if i == 0 else None)
                for i, url in enumerate(photo_urls)
            ]
            await ctx.bot.send_media_group(chat_id=CHANNEL_ID, media=media)
        return True
    except TelegramError as exc:
        logger.error("send_photo/media_group failed: %s", exc)
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


async def fetch_redgifs_user_media(username: str, client: httpx.AsyncClient):
    logger = logging.getLogger("fetch_redgifs_user")
    try:
        token_resp = await client.get(
            "https://api.redgifs.com/v2/auth/temporary",
            headers=_provider_headers("https://www.redgifs.com/"),
        )
        token_resp.raise_for_status()
        token = token_resp.json().get("token")
        if not isinstance(token, str) or not token:
            logger.error("failed to obtain RedGifs token")
            return
    except Exception as exc:
        logger.error("RedGifs auth failed: %s", exc)
        return

    cursor = None
    while True:
        params: dict = {"count": 40}
        if cursor:
            params["pos"] = cursor
        try:
            resp = await client.get(
                f"https://api.redgifs.com/v2/users/{username}/search",
                params=params,
                headers={**_provider_headers("https://www.redgifs.com/"), "Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning("RedGifs user page failed: %s", exc)
            break

        gifs = payload.get("gifs", [])
        if not gifs:
            break

        for gif in gifs:
            if not isinstance(gif, dict):
                continue
            urls = gif.get("urls", {})
            video_url = _https_variant_url(urls.get("hd") or urls.get("sd"))
            if video_url:
                yield video_url

        cursor = payload.get("cursor")
        if not cursor:
            break


def _extract_reddit_post_media(post: dict) -> dict | None:
    if post.get("is_gallery"):
        items = post.get("gallery_data", {}).get("items", [])
        metadata = post.get("media_metadata", {})
        urls = []
        for item in items:
            mid = item.get("media_id")
            if not mid:
                continue
            s = metadata.get(mid, {}).get("s", {})
            url = s.get("u") or s.get("gif")
            if url:
                urls.append(url.replace("&amp;", "&"))
        return {"type": "gallery", "urls": urls} if urls else None

    if post.get("is_video"):
        url = post.get("media", {}).get("reddit_video", {}).get("fallback_url")
        return {"type": "video", "url": url} if url else None

    url = post.get("url", "")
    if post.get("post_hint") == "image" or re.search(r"\.(jpg|jpeg|png|gif|webp)(\?|$)", url, re.IGNORECASE):
        if url.startswith("http"):
            return {"type": "image", "url": url}

    return None


async def fetch_reddit_media(target_url: str, client: httpx.AsyncClient):
    logger = logging.getLogger("fetch_reddit")
    match = REDDIT_URL_RE.match(target_url)
    if not match:
        return
    kind, name = match.group(2).lower(), match.group(3)
    if kind in ("u", "user"):
        api_url = f"https://www.reddit.com/user/{name}/submitted.json"
    else:
        api_url = f"https://www.reddit.com/r/{name}.json"

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    after = None
    while True:
        params: dict = {"limit": 100, "raw_json": 1}
        if after:
            params["after"] = after
        try:
            resp = await client.get(api_url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json().get("data", {})
        except Exception as exc:
            logger.warning("Reddit fetch failed: %s", exc)
            break

        children = data.get("children", [])
        if not children:
            break

        for child in children:
            item = _extract_reddit_post_media(child.get("data", {}))
            if item:
                yield item

        after = data.get("after")
        if not after:
            break


async def _download_to_temp(url: str, client: httpx.AsyncClient) -> Path | None:
    temp_path = TMP_DIR / f"xvbot_{uuid4().hex}.mp4"
    try:
        async with client.stream("GET", url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}) as resp:
            resp.raise_for_status()
            with temp_path.open("wb") as f:
                async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
        if temp_path.exists() and temp_path.stat().st_size > 0:
            return temp_path
    except Exception:
        pass
    temp_path.unlink(missing_ok=True)
    return None


async def fetch_4chan_thread_media(board: str, thread_id: str, client: httpx.AsyncClient):
    logger = logging.getLogger("fetch_4chan")
    try:
        resp = await client.get(
            f"https://a.4cdn.org/{board}/thread/{thread_id}.json",
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        posts = resp.json().get("posts", [])
    except Exception as exc:
        logger.error("4chan API failed: %s", exc)
        return

    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    video_exts = {".webm", ".mp4"}

    for post in posts:
        attachments = [post] + post.get("extra_files", [])
        for att in attachments:
            ext = att.get("ext", "").lower()
            tim = att.get("tim")
            if not tim or not ext:
                continue
            url = f"https://i.4cdn.org/{board}/{tim}{ext}"
            if ext in image_exts:
                yield {"type": "image", "url": url}
            elif ext in video_exts:
                yield {"type": "video", "url": url}


async def handle_fetch_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global _fetch_active, _fetch_cancelled
    logger = logging.getLogger("handle_fetch")
    message = update.channel_post or update.message
    if not message:
        return

    if _fetch_active:
        await ctx.bot.send_message(chat_id=CHANNEL_ID, text="Fetch em andamento. Use /stop para cancelar.")
        return

    args = ctx.args or []
    if not args:
        await ctx.bot.send_message(
            chat_id=CHANNEL_ID,
            text="Uso: /fetch <url>\nSuportado: redgifs.com/users/* · boards.4chan.org/*/thread/*",
        )
        return

    target_url = args[0].strip()
    redgifs_match = REDGIFS_USER_URL_RE.match(target_url)
    chan4_match = CHAN4_URL_RE.match(target_url)

    if not redgifs_match and not chan4_match:
        await ctx.bot.send_message(chat_id=CHANNEL_ID, text=f"URL não suportada: {target_url}")
        return

    _fetch_active = True
    _fetch_cancelled = False
    count = 0
    timeout = httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            if redgifs_match:
                username = redgifs_match.group(2)
                async for video_url in fetch_redgifs_user_media(username, client):
                    if _fetch_cancelled:
                        break
                    temp_path = await _download_to_temp(video_url, client)
                    if temp_path:
                        try:
                            await _send_video_or_document(ctx, temp_path, video_url)
                            count += 1
                        finally:
                            temp_path.unlink(missing_ok=True)
                    await asyncio.sleep(1)

            elif chan4_match:
                board, thread_id = chan4_match.group(1), chan4_match.group(2)
                async for item in fetch_4chan_thread_media(board, thread_id, client):
                    if _fetch_cancelled:
                        break
                    try:
                        if item["type"] == "image":
                            await ctx.bot.send_photo(chat_id=CHANNEL_ID, photo=item["url"])
                            count += 1
                        elif item["type"] == "video":
                            temp_path = await _download_to_temp(item["url"], client)
                            if temp_path:
                                try:
                                    await _send_video_or_document(ctx, temp_path, item["url"])
                                    count += 1
                                finally:
                                    temp_path.unlink(missing_ok=True)
                    except TelegramError as exc:
                        logger.warning("send failed: %s", exc)
                    await asyncio.sleep(0.5)
    finally:
        _fetch_active = False

    status = "Cancelado" if _fetch_cancelled else "Concluído"
    await ctx.bot.send_message(chat_id=CHANNEL_ID, text=f"{status}. {count} itens enviados de {target_url}")


async def handle_stop_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global _fetch_cancelled
    if _fetch_active:
        _fetch_cancelled = True
        await ctx.bot.send_message(chat_id=CHANNEL_ID, text="Parando após o item atual...")
    else:
        await ctx.bot.send_message(chat_id=CHANNEL_ID, text="Nenhum fetch em andamento.")


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
                if TWITTER_URL_RE.fullmatch(source_url):
                    async with httpx.AsyncClient(timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS)) as client:
                        photo_urls = await fetch_tweet_images(source_url, client)
                    if photo_urls:
                        uploaded = await _send_images(ctx, photo_urls, source_url)
                        if uploaded:
                            try:
                                await ctx.bot.delete_message(
                                    chat_id=CHANNEL_ID,
                                    message_id=message.message_id,
                                )
                            except TelegramError as exc:
                                logger.warning("delete_message failed: %s", exc)
                        return
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


def main():
    LOGGER.info("starting xvbot")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("fetch", handle_fetch_command, filters=filters.Chat(CHANNEL_ID)))
    app.add_handler(CommandHandler("stop", handle_stop_command, filters=filters.Chat(CHANNEL_ID)))
    app.add_handler(MessageHandler(filters.Chat(CHANNEL_ID), handle_message))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

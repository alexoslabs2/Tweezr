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
from telegram import Update
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import Application, ContextTypes, MessageHandler, filters


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


def _https_url(url: str) -> str:
    if url.lower().startswith("http://"):
        return "https://" + url[7:]
    return url


def extract_tweet_url(text: str) -> str | None:
    match = TWITTER_URL_RE.search(text)
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
        tweet_url = extract_tweet_url(str(response_url))
        if tweet_url:
            return tweet_url
    return None


async def extract_message_tweet_url(text: str, client: httpx.AsyncClient) -> str | None:
    twitter_match = TWITTER_URL_RE.search(text)
    tco_match = TCO_URL_RE.search(text)

    if twitter_match and (not tco_match or twitter_match.start() < tco_match.start()):
        return _https_url(twitter_match.group(0))
    if tco_match:
        return await resolve_tco_url(_https_url(tco_match.group(0)), client)
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


async def download_best_video(tweet_url: str) -> Path | None:
    logger = logging.getLogger("download_best_video")
    timeout = httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for provider in PROVIDERS:
            provider_name = provider.__name__
            try:
                variants = await provider(tweet_url, client)
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
                    headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
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


async def _send_video_or_document(ctx: ContextTypes.DEFAULT_TYPE, video_path: Path, tweet_url: str) -> bool:
    logger = logging.getLogger("handle_message")
    with video_path.open("rb") as video:
        for attempt, delay in enumerate([1, 4, 16, None], start=1):
            try:
                await ctx.bot.send_video(
                    chat_id=CHANNEL_ID,
                    video=video,
                    caption=tweet_url,
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
                caption=tweet_url,
            )
            return True
        except TelegramError as exc:
            logger.error("send_document failed: %s", exc)
            return False


async def _alert_admin(ctx: ContextTypes.DEFAULT_TYPE, tweet_url: str) -> None:
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
            text=f"XVBOT failed to download: {tweet_url}",
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
                tweet_url = await extract_message_tweet_url(message.text, client)
            if not tweet_url:
                return

            temp_path = await download_best_video(tweet_url)
            if temp_path is None:
                logger.error("all providers failed for URL")
                await _alert_admin(ctx, tweet_url)
                return

            uploaded = await _send_video_or_document(ctx, temp_path, tweet_url)
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
    app.add_handler(MessageHandler(filters.Chat(CHANNEL_ID), handle_message))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

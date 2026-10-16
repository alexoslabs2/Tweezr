#!/usr/bin/env python3

import asyncio
import json
import logging
import os
import re
from collections import namedtuple
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse
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
MAX_VIDEO_SIZE_BYTES = min(MAX_VIDEO_SIZE_MB * 1024 * 1024, 50_000_000)
if MAX_VIDEO_SIZE_BYTES <= 0:
    raise ValueError("MAX_VIDEO_SIZE_MB must be positive")
PROCESSING_SEMAPHORE = asyncio.Semaphore(3)
TMP_DIR = Path("/tmp")
CHUNK_SIZE = 32 * 1024


class SecretRedactingFormatter(logging.Formatter):
    """Redact after formatting, including exception tracebacks and HTTP debug logs."""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for secret in (
            TOKEN,
            quote(TOKEN, safe=""),
            quote(quote(TOKEN, safe=""), safe=""),
        ):
            rendered = rendered.replace(secret, "[REDACTED]")
        return re.sub(
            r"\b(?:bot)?\d{5,}(?::|%3[aA]|%253[aA])[A-Za-z0-9_-]+",
            "[REDACTED]",
            rendered,
        )


LOG_DIR.mkdir(parents=True, exist_ok=True)
_formatter = SecretRedactingFormatter(
    "%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
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


async def extract_message_source_url(
    text: str, client: httpx.AsyncClient
) -> str | None:
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
                    quality_label=quality_label
                    if key in ("hd", "file", "file_url")
                    else None,
                    bitrate=None,
                )
            )
        return variants or None
    except Exception as exc:
        logger.warning("provider failed: %s", exc)
        return None


class ProviderFailure(ValueError):
    """A provider response does not meet its extraction contract."""


def _provider_failure(logger: logging.Logger, exc: Exception) -> None:
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        category = (
            "session_rejected"
            if response.status_code in (401, 403, 419)
            else "http_error"
        )
        logger.warning(
            "provider failed reason=%s status=%s", category, response.status_code
        )
    elif isinstance(exc, httpx.TimeoutException):
        logger.warning("provider failed reason=timeout")
    elif isinstance(exc, ProviderFailure):
        logger.warning("provider failed reason=%s", exc)
    else:
        logger.warning("provider failed reason=%s", type(exc).__name__)


def _json_payload(response: httpx.Response) -> dict:
    response.raise_for_status()
    if "json" not in response.headers.get("content-type", "").lower():
        raise ProviderFailure("unexpected_format: expected JSON")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderFailure("unexpected_format: invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderFailure("unexpected_format: expected JSON object")
    return payload


def _same_origin_url(base: str, target: str) -> str:
    resolved = urljoin(base, target)
    a, b = urlparse(base), urlparse(resolved)
    if b.scheme != "https" or a.netloc != b.netloc:
        raise ProviderFailure("unexpected_format: foreign form or redirect")
    return resolved


async def _provider_form(client, home: str, field: str):
    response = await client.get(home, headers=_provider_headers(home))
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    input_tag = soup.find("input", attrs={"name": field})
    form = input_tag.find_parent("form") if input_tag else None
    if form is None:
        raise ProviderFailure("unexpected_format: missing form")
    data = {item["name"]: item.get("value", "") for item in form.select("input[name]")}
    action = _same_origin_url(home, form.get("action") or home)
    return form, data, action


def _html_variants(html: str, selector: str, base: str) -> list[VideoVariant]:
    soup = BeautifulSoup(html, "lxml")
    variants = []
    seen = set()
    for link in soup.select(selector):
        raw = link.get("href") or link.get("src")
        if not isinstance(raw, str) or raw.startswith(("#", "javascript:")):
            continue
        url = _https_variant_url(urljoin(base, raw))
        if not url or url in seen or urlparse(url).path in ("", "/"):
            continue
        row = link.find_parent("tr") or link.find_parent(class_="download__item")
        label = row.get_text(" ", strip=True) if row else link.get_text(" ", strip=True)
        if "mp3" in label.lower() and "mp4" not in label.lower():
            continue
        seen.add(url)
        variants.append(VideoVariant(url, label or None, None))
    return variants


def _require_variants(variants: list[VideoVariant]) -> list[VideoVariant]:
    if not variants:
        raise ProviderFailure("no_variants")
    return variants


async def provider_savetwt(
    tweet_url: str,
    client: httpx.AsyncClient,
) -> list[VideoVariant] | None:
    logger = logging.getLogger("provider_savetwt")
    try:
        home = "https://savetwt.com/"
        _, data, action = await _provider_form(client, home, "url")
        if not data.get("_token"):
            raise ProviderFailure("unexpected_format: missing CSRF token")
        data["url"] = tweet_url
        response = await client.post(
            action,
            data=data,
            headers={
                **_provider_headers(home),
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        payload = _json_payload(response)
        redirect = payload.get("redirect")
        if not isinstance(redirect, str):
            raise ProviderFailure("unexpected_format: missing result redirect")
        result = await client.get(
            _same_origin_url(home, redirect), headers=_provider_headers(home)
        )
        result.raise_for_status()
        return _require_variants(
            _html_variants(
                result.text,
                'a.result__download__button[href], a[href*=".mp4"], video[src], video source[src]',
                home,
            )
        )
    except Exception as exc:
        _provider_failure(logger, exc)
        return None


async def provider_ssstwitter(
    tweet_url: str,
    client: httpx.AsyncClient,
) -> list[VideoVariant] | None:
    logger = logging.getLogger("provider_ssstwitter")
    try:
        home = "https://ssstwitter.com/"
        form, data, action = await _provider_form(client, home, "id")
        values = form.get("include-vals", "")
        for key, quoted, numeric in re.findall(r"(\w+):\s*(?:'([^']*)'|(\d+))", values):
            data[key] = quoted or numeric
        if not data.get("tt") or not data.get("ts"):
            raise ProviderFailure("unexpected_format: missing form token")
        data["id"] = tweet_url
        response = await client.post(
            action,
            data=data,
            headers={**_provider_headers(home), "HX-Request": "true"},
        )
        response.raise_for_status()
        return _require_variants(
            _html_variants(
                response.text,
                'a.download_link[href], a.download-btn[href], a[href*=".mp4"]',
                home,
            )
        )
    except Exception as exc:
        _provider_failure(logger, exc)
        return None


async def provider_tweeload(
    tweet_url: str,
    client: httpx.AsyncClient,
) -> list[VideoVariant] | None:
    logger = logging.getLogger("provider_tweeload")
    try:
        home = "https://tweeload.com/"
        _, data, action = await _provider_form(client, home, "url")
        data["url"] = tweet_url
        response = await client.post(
            action,
            data=data,
            headers=_provider_headers(home),
        )
        response.raise_for_status()
        return _require_variants(
            _html_variants(
                response.text,
                "a.download__item__info__actions__button[href]:not(.premium__locked__btn)",
                home,
            )
        )
    except Exception as exc:
        _provider_failure(logger, exc)
        return None


async def provider_twittervideodownloader(
    tweet_url: str,
    client: httpx.AsyncClient,
) -> list[VideoVariant] | None:
    logger = logging.getLogger("provider_twittervideodownloader")
    try:
        home = "https://twittervideodownloader.com/en/"
        _, data, action = await _provider_form(client, home, "tweet")
        if not data.get("csrfmiddlewaretoken"):
            raise ProviderFailure("unexpected_format: missing CSRF token")
        data["tweet"] = tweet_url
        response = await client.post(
            action,
            data=data,
            headers=_provider_headers(home),
        )
        response.raise_for_status()
        return _require_variants(_html_variants(response.text, 'a[href*=".mp4"]', home))
    except Exception as exc:
        _provider_failure(logger, exc)
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
        media_type = (
            cells[1].get_text(" ", strip=True).lower() if len(cells) > 1 else ""
        )
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
            data={"page": tweet_url, "ftype": "all", "ajax": "1"},
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
        variants = variants or _parse_twmate_html_variants(response.text)
        if not variants and "not able to detect any video" in response.text.lower():
            raise ProviderFailure("remote_no_video")
        return _require_variants(variants)
    except Exception as exc:
        _provider_failure(logger, exc)
        return None


async def provider_getxbot(
    tweet_url: str,
    client: httpx.AsyncClient,
) -> list[VideoVariant] | None:
    logger = logging.getLogger("provider_getxbot")
    try:
        response = await client.post(
            "https://www.getxbot.com/api/parse",
            json={"videoId": tweet_url, "p": 1},
            headers=_provider_headers("https://www.getxbot.com/"),
        )
        payload = _json_payload(response)
        if payload.get("code") != 0:
            raise ProviderFailure("remote_error")
        groups = payload.get("data")
        if not isinstance(groups, dict):
            raise ProviderFailure("unexpected_format: missing video groups")
        # Keep one video per source post, consistently with the other adapters.
        videos = next(
            (items for items in groups.values() if isinstance(items, list) and items),
            [],
        )
        variants = []
        for item in videos:
            if not isinstance(item, dict):
                continue
            if item.get("content_type") != "video/mp4":
                continue
            variant_url = _https_variant_url(item.get("url"))
            if not variant_url:
                continue
            variants.append(
                VideoVariant(
                    url=variant_url,
                    quality_label=item.get("size"),
                    bitrate=_optional_int(item.get("bitrate")),
                )
            )
        return _require_variants(variants)
    except Exception as exc:
        _provider_failure(logger, exc)
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
            if (
                not isinstance(item, dict)
                or str(item.get("type", "")).lower() != "video"
            ):
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
    provider_twittervideodownloader,
    provider_getxbot,
    provider_ssstwitter,
    provider_savetwt,
    provider_tweeload,
    provider_twmate,
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


RESOLUTION_RE = re.compile(r"(\d{2,5})\s*[xX×]\s*(\d{2,5})")


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
                _provider_failure(logging.getLogger(provider_name), exc)
                variants = None

            if not variants:
                logger.info("provider=%s outcome=no_variants", provider_name)
                continue

            pending = list(variants)
            seen = set()
            while pending:
                best = pick_best_variant(pending)
                pending.remove(best)
                if best.url in seen:
                    continue
                seen.add(best.url)
                try:
                    path = await _download_variant(client, best, source_url)
                except Exception as exc:
                    _provider_failure(logging.getLogger(provider_name), exc)
                    continue
                logger.info(
                    "provider=%s outcome=downloaded bytes=%s quality=%s",
                    provider_name,
                    path.stat().st_size,
                    best.quality_label,
                )
                return path

    return None


async def _download_variant(client, variant: VideoVariant, source_url: str) -> Path:
    if not _https_variant_url(variant.url):
        raise ProviderFailure("invalid_media_url")
    temp_path = TMP_DIR / f"xvbot_{uuid4().hex}.mp4"
    complete = False
    try:
        async with client.stream(
            "GET",
            variant.url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": source_url,
                "Accept": "video/mp4, application/octet-stream",
            },
        ) as response:
            response.raise_for_status()
            if response.url.scheme != "https":
                raise ProviderFailure("invalid_media_redirect")
            media_type = (
                response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if media_type not in (
                "video/mp4",
                "application/octet-stream",
                "binary/octet-stream",
                "",
            ):
                raise ProviderFailure("invalid_media_type")
            declared = response.headers.get("content-length", "")
            expected = int(declared) if declared.isdigit() else None
            if expected is not None and expected > MAX_VIDEO_SIZE_BYTES:
                raise ProviderFailure("media_too_large")
            size = 0
            prefix = bytearray()
            with temp_path.open("xb") as output:
                async for chunk in response.aiter_bytes(chunk_size=CHUNK_SIZE):
                    size += len(chunk)
                    if size > MAX_VIDEO_SIZE_BYTES:
                        raise ProviderFailure("media_too_large")
                    if len(prefix) < 12:
                        prefix.extend(chunk[: 12 - len(prefix)])
                        if len(prefix) >= 12 and prefix[4:8] != b"ftyp":
                            raise ProviderFailure("invalid_mp4_signature")
                    output.write(chunk)
            if size < 12 or prefix[4:8] != b"ftyp":
                raise ProviderFailure("invalid_mp4_signature")
            if (
                expected is not None
                and not response.headers.get("content-encoding")
                and size != expected
            ):
                raise ProviderFailure("incomplete_media")
        complete = True
        return temp_path
    finally:
        if not complete:
            temp_path.unlink(missing_ok=True)


def _file_too_large_error(exc: TelegramError) -> bool:
    message = str(exc).lower()
    return "file is too big" in message or "request entity too large" in message


async def _send_video_or_document(
    ctx: ContextTypes.DEFAULT_TYPE, video_path: Path, source_url: str
) -> bool:
    logger = logging.getLogger("handle_message")
    if video_path.stat().st_size > MAX_VIDEO_SIZE_BYTES:
        logger.error("upload refused reason=media_too_large")
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
                    logger.error("upload refused reason=telegram_size_limit")
                    return False
                if "video" in str(exc).lower() and any(
                    word in str(exc).lower()
                    for word in ("invalid", "unsupported", "wrong")
                ):
                    break
                logger.error("send_video failed: %s", exc)
                return False
            except NetworkError as exc:
                if delay is None:
                    logger.error("send_video failed after retries: %s", exc)
                    return False
                logger.warning(
                    "send_video network error on attempt %s: %s", attempt, exc
                )
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
        logger.warning(
            "ADMIN_CHAT_ID matches CHANNEL_ID; skipping plain-text admin alert"
        )
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

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS)
            ) as client:
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


def main():
    LOGGER.info("starting xvbot")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.Chat(CHANNEL_ID), handle_message))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

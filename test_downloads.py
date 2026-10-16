"""Regression tests for live provider contracts, bounded downloads and safe logs."""

import asyncio
import io
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("CHANNEL_ID", "-1001704658742")
os.environ.setdefault("LOG_DIR", "/tmp/xvbot-test-logs")

import httpx
import pytest

import bot

FIXTURES = Path(__file__).parent / "tests/fixtures/providers"
SOURCE = "https://x.com/example/status/123"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["savetwt", "ssstwitter", "tweeload", "twittervideodownloader"]
)
async def test_provider_form_session_and_real_html_contract(name):
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            assert request.method == "GET"
            return httpx.Response(
                200,
                text=(FIXTURES / f"{name}-form.html").read_text(),
                headers={"set-cookie": "session=fixture; Path=/; Secure"},
            )
        assert "session=fixture" in request.headers["cookie"]
        if request.method == "POST":
            data = parse_qs(request.content.decode(), keep_blank_values=True)
            assert data[
                "id"
                if name == "ssstwitter"
                else "tweet"
                if name == "twittervideodownloader"
                else "url"
            ] == [SOURCE]
            assert (
                "application/x-www-form-urlencoded" in request.headers["content-type"]
            )
            if name == "savetwt":
                assert data["_token"] == ["fixture-csrf-token"]
                return httpx.Response(200, json={"redirect": "/download"})
            if name == "ssstwitter":
                assert data["tt"] == ["fixture-token"]
                assert data["ts"] == ["1792160000"]
                assert request.headers["HX-Request"] == "true"
            else:
                assert request.url.path == "/download"
            if name == "twittervideodownloader":
                assert data["csrfmiddlewaretoken"] == ["fixture-csrf-token"]
                assert data["gql"] == ["fixture-gql"]
        return httpx.Response(200, text=(FIXTURES / f"{name}-result.html").read_text())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as client:
        variants = await getattr(bot, f"provider_{name}")(SOURCE, client)
    assert variants
    assert len(variants) == (1 if name == "tweeload" else 3)
    assert all("mp3" not in (v.quality_label or "").lower() for v in variants)
    assert bot._resolution_pixels(bot.pick_best_variant(variants).quality_label) == (
        320 * 528 if name == "tweeload" else 720 * 1192
    )


@pytest.mark.asyncio
async def test_getxbot_current_contract_and_hls_exclusion():
    def respond(request):
        assert request.url.path == "/api/parse"
        assert json.loads(request.content) == {"videoId": SOURCE, "p": 1}
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "video1": [
                        {
                            "content_type": "application/x-mpegURL",
                            "url": "https://media.example/hls",
                        },
                        {
                            "content_type": "video/mp4",
                            "url": "https://media.example/video",
                            "size": "480x794",
                            "bitrate": 950000,
                        },
                    ]
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await bot.provider_getxbot(SOURCE, client) == [
            bot.VideoVariant("https://media.example/video", "480x794", 950000)
        ]


@pytest.mark.parametrize(
    "status,body,kind,reason",
    [
        (200, "<html>home</html>", "text/html", "unexpected_format"),
        (200, "not json", "application/json", "unexpected_format"),
        (200, "[]", "application/json", "unexpected_format"),
        (401, '{"message":"Token empty"}', "application/json", "session_rejected"),
        (403, "CSRF verification failed", "text/html", "session_rejected"),
        (
            419,
            '{"message":"CSRF token mismatch"}',
            "application/json",
            "session_rejected",
        ),
    ],
)
@pytest.mark.asyncio
async def test_provider_failures_have_actionable_categories(
    status, body, kind, reason, caplog
):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(status, text=body, headers={"content-type": kind})
        )
    ) as client:
        assert await bot.provider_getxbot(SOURCE, client) is None
    assert reason in caplog.text


@pytest.mark.asyncio
async def test_savetwt_rejects_foreign_result_redirect(caplog):
    def respond(request):
        if request.method == "GET":
            return httpx.Response(
                200, text=(FIXTURES / "savetwt-form.html").read_text()
            )
        return httpx.Response(200, json={"redirect": "https://foreign.example/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await bot.provider_savetwt(SOURCE, client) is None
    assert "foreign form or redirect" in caplog.text


@pytest.mark.asyncio
async def test_twmate_reports_remote_failure(caplog):
    def respond(request):
        assert parse_qs(request.content.decode())["ajax"] == ["1"]
        return httpx.Response(
            200,
            text="our system was not able to detect any video at the adress you provided.",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await bot.provider_twmate(SOURCE, client) is None
    assert "remote_no_video" in caplog.text


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks, cancel=False):
        self.chunks = chunks
        self.reads = 0
        self.cancel = cancel

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.cancel:
            raise asyncio.CancelledError()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,chunks,reason",
    [
        (
            {"content-type": "text/html"},
            [b"<html>blocked</html>"],
            "invalid_media_type",
        ),
        (
            {"content-type": "video/mp4"},
            [b"<html>blocked</html>"],
            "invalid_mp4_signature",
        ),
        ({"content-type": "video/mp4"}, [b""], "invalid_mp4_signature"),
        ({"content-length": "999"}, [MP4], "media_too_large"),
        ({}, [MP4, b"a" * 70], "media_too_large"),
        ({"content-length": "40"}, [MP4], "incomplete_media"),
    ],
)
async def test_bad_media_is_rejected_and_partial_files_removed(
    headers, chunks, reason, tmp_path, monkeypatch
):
    monkeypatch.setattr(bot, "TMP_DIR", tmp_path)
    monkeypatch.setattr(bot, "MAX_VIDEO_SIZE_BYTES", 100)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, headers=headers, stream=ChunkStream(chunks))
        )
    ) as client:
        with pytest.raises(bot.ProviderFailure, match=reason):
            await bot._download_variant(
                client,
                bot.VideoVariant("https://media.example/video", None, None),
                SOURCE,
            )
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_oversized_response_is_rejected_before_reading_body(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(bot, "TMP_DIR", tmp_path)
    stream = ChunkStream([MP4])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                headers={"content-length": str(bot.MAX_VIDEO_SIZE_BYTES + 1)},
                stream=stream,
            )
        )
    ) as client:
        with pytest.raises(bot.ProviderFailure, match="media_too_large"):
            await bot._download_variant(
                client,
                bot.VideoVariant("https://media.example/video", None, None),
                SOURCE,
            )
    assert stream.reads == 0
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_cancellation_cleans_partial_download(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "TMP_DIR", tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=ChunkStream([MP4], cancel=True))
        )
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await bot._download_variant(
                client,
                bot.VideoVariant("https://media.example/video", None, None),
                SOURCE,
            )
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_download_tries_lower_quality_and_next_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "TMP_DIR", tmp_path)
    monkeypatch.setattr(bot, "MAX_VIDEO_SIZE_BYTES", 100)
    requests = []

    async def first(source, client):
        return [bot.VideoVariant("https://media.example/fail", None, 1)]

    async def second(source, client):
        return [
            bot.VideoVariant("https://media.example/big", "720x1192", 2000),
            bot.VideoVariant("https://media.example/small", "480x794", 1000),
        ]

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/fail":
            return httpx.Response(503)
        if request.url.path == "/big":
            return httpx.Response(
                200, headers={"content-length": "999"}, stream=ChunkStream([MP4])
            )
        return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})

    factory = httpx.AsyncClient
    monkeypatch.setattr(bot, "PROVIDERS", [first, second])
    monkeypatch.setattr(
        bot.httpx,
        "AsyncClient",
        lambda **kw: factory(transport=httpx.MockTransport(respond), **kw),
    )
    path = await bot.download_best_video(SOURCE)
    assert requests == ["/fail", "/big", "/small"]
    assert path.read_bytes() == MP4
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("encoded", [0, 1, 2])
@pytest.mark.parametrize("traceback", [False, True])
def test_secret_redaction_in_messages_arguments_and_exceptions(
    encoded, traceback, monkeypatch
):
    monkeypatch.setattr(bot, "TOKEN", "123456789:FAKE_configured_token_for_testing")
    secret = bot.TOKEN
    for _ in range(encoded):
        secret = quote(secret, safe="")
    logger = logging.getLogger("redaction-test")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(bot.SecretRedactingFormatter("%(message)s"))
    logger.addHandler(handler)
    try:
        if traceback:
            try:
                raise RuntimeError(
                    f"request https://api.telegram.org/bot{secret}/getUpdates"
                )
            except RuntimeError:
                logger.error("failure %s", secret, exc_info=True)
        else:
            logger.error(
                "HTTP Request: POST https://api.telegram.org/bot%s/getUpdates", secret
            )
    finally:
        logger.removeHandler(handler)
    assert secret not in stream.getvalue()
    assert "[REDACTED]" in stream.getvalue()


def test_configured_handlers_both_redact_other_telegram_tokens():
    token = "123456789:FAKE_other_token_for_testing"
    record = logging.LogRecord(
        "httpx", logging.WARNING, __file__, 1, "request %s", (token,), None
    )
    for handler in (bot._file_handler, bot._stream_handler):
        assert token not in handler.format(record)
        assert "[REDACTED]" in handler.format(record)


@pytest.mark.asyncio
async def test_provider_timeout_is_classified(caplog):
    def respond(request):
        raise httpx.ReadTimeout("timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await bot.provider_getxbot(SOURCE, client) is None
    assert "reason=timeout" in caplog.text


@pytest.mark.asyncio
async def test_homepage_after_redirect_is_not_a_download_result(caplog):
    def respond(request):
        if request.method == "POST":
            return httpx.Response(302, headers={"location": "/"})
        return httpx.Response(200, text=(FIXTURES / "tweeload-form.html").read_text())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as client:
        assert await bot.provider_tweeload(SOURCE, client) is None
    assert "reason=no_variants" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected_documents",
    [
        ("Request Entity Too Large", 0),
        ("VIDEO_CONTENT_TYPE_INVALID", 1),
    ],
)
async def test_document_fallback_only_for_video_format_errors(
    error, expected_documents, tmp_path
):
    class FakeBot:
        documents = 0

        async def send_video(self, **kwargs):
            raise bot.BadRequest(error)

        async def send_document(self, **kwargs):
            self.documents += 1

    video = tmp_path / "video.mp4"
    video.write_bytes(MP4)
    telegram = FakeBot()
    result = await bot._send_video_or_document(
        SimpleNamespace(bot=telegram), video, SOURCE
    )
    assert result is bool(expected_documents)
    assert telegram.documents == expected_documents

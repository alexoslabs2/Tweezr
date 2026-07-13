import os
import logging
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("CHANNEL_ID", "-1001704658742")
os.environ.setdefault("LOG_DIR", "/tmp/xvbot-test-logs")

import pytest

import bot


class MockResponse:
    def __init__(
        self,
        json_data=None,
        text="",
        url="https://provider.test/response",
        headers=None,
        status_code=200,
    ):
        self._json_data = json_data
        self.text = text
        self.url = url
        self.headers = headers or {}
        self.status_code = status_code
        self.history = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def json(self):
        return self._json_data

    def raise_for_status(self):
        return None


class MockAsyncClient:
    def __init__(self, responses):
        if isinstance(responses, list):
            self.responses = responses
        else:
            self.responses = [responses]
        self.response_index = 0
        self.requests = []

    def _next_response(self):
        if self.response_index >= len(self.responses):
            return self.responses[-1]
        response = self.responses[self.response_index]
        self.response_index += 1
        return response

    async def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return self._next_response()

    async def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        return self._next_response()

    async def head(self, url, **kwargs):
        self.requests.append(("HEAD", url, kwargs))
        return self._next_response()

    def stream(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self._next_response()


@pytest.mark.asyncio
async def test_provider_savetwt_returns_variants():
    client = MockAsyncClient(
        MockResponse(
            json_data={
                "links": [
                    {"url": "https://cdn.example/video-720.mp4", "quality": "1280x720"},
                    {"url": "https://cdn.example/video-360.mp4", "quality": "640x360"},
                ]
            }
        )
    )

    variants = await bot.provider_savetwt(
        "https://x.com/example/status/123",
        client,
    )

    assert variants
    assert variants[0] == bot.VideoVariant("https://cdn.example/video-720.mp4", "1280x720", None)
    assert client.requests[0][2]["data"] == {"url": "https://x.com/example/status/123"}


@pytest.mark.asyncio
async def test_provider_ssstwitter_returns_variants():
    client = MockAsyncClient(
        MockResponse(
            text="""
            <html>
              <body>
                <a href="https://cdn.example/video-480.mp4">854x480</a>
                <a href="https://cdn.example/not-video.jpg">image</a>
              </body>
            </html>
            """
        )
    )

    variants = await bot.provider_ssstwitter(
        "https://twitter.com/example/status/123",
        client,
    )

    assert variants
    assert variants[0] == bot.VideoVariant("https://cdn.example/video-480.mp4", "854x480", None)
    assert client.requests[0][2]["data"] == {"id": "https://twitter.com/example/status/123"}


@pytest.mark.asyncio
async def test_provider_tweeload_returns_variants():
    client = MockAsyncClient(
        MockResponse(
            json_data={
                "data": {
                    "links": [
                        {"url": "https://cdn.example/video-low.mp4", "bitrate": 320000},
                        {"url": "https://cdn.example/video-high.mp4", "bitrate": 1200000},
                    ]
                }
            }
        )
    )

    variants = await bot.provider_tweeload(
        "https://x.com/example/status/123",
        client,
    )

    assert variants
    assert variants[1] == bot.VideoVariant("https://cdn.example/video-high.mp4", None, 1200000)
    assert client.requests[0][2]["json"] == {"url": "https://x.com/example/status/123"}


@pytest.mark.asyncio
async def test_provider_twittervideodownloader_returns_variants():
    client = MockAsyncClient(
        MockResponse(
            json_data=[
                {"url": "https://cdn.example/video-720.mp4", "resolution": "1280x720"},
                {"url": "https://cdn.example/video-360.mp4", "resolution": "640x360"},
            ]
        )
    )

    variants = await bot.provider_twittervideodownloader(
        "https://x.com/example/status/123",
        client,
    )

    assert variants
    assert variants[0] == bot.VideoVariant("https://cdn.example/video-720.mp4", "1280x720", None)
    assert client.requests[0][2]["data"] == {"tweet": "https://x.com/example/status/123"}


@pytest.mark.asyncio
async def test_provider_twmate_returns_variants():
    client = MockAsyncClient(
        MockResponse(
            text="""
            <table class="table files-table">
              <tbody>
                <tr>
                  <td>1548x1170</td>
                  <td>mp4</td>
                  <td><a class="btn-dl" href="https://cdn.example/video-1170.mp4">download</a></td>
                </tr>
                <tr>
                  <td>952x720</td>
                  <td>mp4</td>
                  <td><a class="btn-dl" href="https://cdn.example/video-720.mp4">download</a></td>
                </tr>
              </tbody>
            </table>
            """
        )
    )

    variants = await bot.provider_twmate(
        "https://twitter.com/example/status/123",
        client,
    )

    assert variants
    assert variants[0] == bot.VideoVariant("https://cdn.example/video-1170.mp4", "1548x1170", None)
    assert client.requests[0][2]["data"] == {
        "page": "https://twitter.com/example/status/123",
        "ftype": "all",
    }


@pytest.mark.asyncio
async def test_provider_getxbot_returns_variants():
    client = MockAsyncClient(
        MockResponse(
            json_data={
                "result": {
                    "videos": [
                        {"url": "https://cdn.example/video-low.mp4", "bitrate": 256000},
                        {"url": "https://cdn.example/video-high.mp4", "bitrate": "1500000"},
                    ]
                }
            }
        )
    )

    variants = await bot.provider_getxbot(
        "https://x.com/example/status/123",
        client,
    )

    assert variants
    assert variants[1] == bot.VideoVariant("https://cdn.example/video-high.mp4", None, 1500000)
    assert client.requests[0][2]["json"] == {"url": "https://x.com/example/status/123"}


@pytest.mark.asyncio
async def test_provider_redgifs_returns_variants():
    client = MockAsyncClient(
        [
            MockResponse(json_data={"token": "temp-token"}),
            MockResponse(
                json_data={
                    "gif": {
                        "width": 1920,
                        "height": 1080,
                        "urls": {
                            "hd": "https://media.redgifs.com/example-hd.mp4",
                            "sd": "https://media.redgifs.com/example-sd.mp4",
                        },
                    }
                }
            ),
        ]
    )

    variants = await bot.provider_redgifs(
        "https://www.redgifs.com/watch/decisivecelebrateduromastyxmaliensis",
        client,
    )

    assert variants
    assert variants[0] == bot.VideoVariant(
        "https://media.redgifs.com/example-hd.mp4",
        "1920x1080",
        None,
    )
    assert client.requests[0][0:2] == ("GET", "https://api.redgifs.com/v2/auth/temporary")
    assert client.requests[1][0:2] == (
        "GET",
        "https://api.redgifs.com/v2/gifs/decisivecelebrateduromastyxmaliensis",
    )
    assert client.requests[1][2]["headers"]["Authorization"] == "Bearer temp-token"


@pytest.mark.asyncio
async def test_provider_eporner_returns_direct_mp4_variants(monkeypatch):
    def fake_extract_info(self, url, download):
        assert url == "https://www.eporner.com/video-AbC123/example-video/"
        assert download is False
        return {
            "formats": [
                {
                    "url": "http://cdn.example/video-720.mp4",
                    "protocol": "https",
                    "ext": "mp4",
                    "width": 1280,
                    "height": 720,
                    "tbr": 1500.5,
                },
                {
                    "url": "https://cdn.example/video-1080.mp4",
                    "protocol": "https",
                    "ext": "mp4",
                    "width": 1920,
                    "height": 1080,
                },
                {
                    "url": "https://cdn.example/video-480-av1.mp4",
                    "protocol": "https",
                    "ext": "mp4",
                    "width": 854,
                    "height": 480,
                    "vcodec": "av1",
                },
                {
                    "url": "https://cdn.example/video-720-av1.mp4",
                    "protocol": "https",
                    "ext": "mp4",
                    "width": 1280,
                    "height": 720,
                },
                {
                    "url": "https://cdn.example/playlist.m3u8",
                    "protocol": "m3u8_native",
                    "ext": "mp4",
                    "height": 720,
                },
            ]
        }

    monkeypatch.setattr(bot._HttpsOnlyYoutubeDL, "extract_info", fake_extract_info)

    variants = await bot.provider_eporner(
        "https://www.eporner.com/video-AbC123/example-video/",
        MockAsyncClient(MockResponse()),
    )

    assert variants == [
        bot.VideoVariant("https://cdn.example/video-720.mp4", "1280x720", 1500500),
        bot.VideoVariant("https://cdn.example/video-1080.mp4", "1920x1080", None),
    ]


def test_eporner_extractor_upgrades_internal_requests_to_https(monkeypatch):
    monkeypatch.setattr(bot.YoutubeDL, "urlopen", lambda self, request: request)
    downloader = object.__new__(bot._HttpsOnlyYoutubeDL)
    request = bot.YtDlpRequest("http://www.eporner.com/xhr/video/AbC123")

    upgraded = downloader.urlopen(request)

    assert upgraded.url == "https://www.eporner.com/xhr/video/AbC123"


def test_pick_best_variant_prefers_720p_over_higher_bitrate():
    variants = [
        bot.VideoVariant("https://cdn.example/720.mp4", "1280x720", 500000),
        bot.VideoVariant("https://cdn.example/1080.mp4", "1920x1080", 2000000),
        bot.VideoVariant("https://cdn.example/360.mp4", "640x360", None),
    ]

    best = bot.pick_best_variant(variants)

    assert best.url == "https://cdn.example/720.mp4"


def test_pick_best_variant_uses_best_resolution_below_720p():
    variants = [
        bot.VideoVariant("https://cdn.example/360.mp4", "640x360", 2000000),
        bot.VideoVariant("https://cdn.example/480.mp4", "854x480", 500000),
    ]

    assert bot.pick_best_variant(variants).url == "https://cdn.example/480.mp4"


def test_pick_best_variant_uses_smallest_resolution_above_720p():
    variants = [
        bot.VideoVariant("https://cdn.example/2160.mp4", "3840x2160", 4000000),
        bot.VideoVariant("https://cdn.example/1080.mp4", "1920x1080", 1000000),
    ]

    assert bot.pick_best_variant(variants).url == "https://cdn.example/1080.mp4"


def test_pick_best_variant_uses_bitrate_when_resolution_is_unknown():
    variants = [
        bot.VideoVariant("https://cdn.example/low.mp4", None, 500000),
        bot.VideoVariant("https://cdn.example/high.mp4", "HD", 2000000),
    ]

    assert bot.pick_best_variant(variants).url == "https://cdn.example/high.mp4"


def test_pick_best_variant_honors_configured_height(monkeypatch):
    monkeypatch.setattr(bot, "PREFERRED_VIDEO_HEIGHT", 480)
    variants = [
        bot.VideoVariant("https://cdn.example/480.mp4", "854x480", 500000),
        bot.VideoVariant("https://cdn.example/720.mp4", "1280x720", 1000000),
    ]

    assert bot.pick_best_variant(variants).url == "https://cdn.example/480.mp4"


def test_pick_best_variant_accepts_source_specific_height():
    variants = [
        bot.VideoVariant("https://cdn.example/480.mp4", "854x480", 500000),
        bot.VideoVariant("https://cdn.example/720.mp4", "1280x720", 1000000),
    ]

    assert (
        bot.pick_best_variant(variants, preferred_height=480).url
        == "https://cdn.example/480.mp4"
    )


def test_pick_best_fitting_variant_uses_highest_resolution_under_cap():
    variants = [
        bot.VideoVariant("https://cdn.example/240.mp4", "240p", None, 31_000_000),
        bot.VideoVariant("https://cdn.example/360.mp4", "360p", None, 58_000_000),
        bot.VideoVariant("https://cdn.example/480.mp4", "480p", None, 112_000_000),
    ]

    best = bot.pick_best_fitting_variant(
        variants,
        max_size_bytes=50 * 1024 * 1024,
        preferred_height=480,
    )

    assert best.url == "https://cdn.example/240.mp4"


def test_pick_best_fitting_variant_returns_none_when_every_known_variant_is_too_large():
    variants = [
        bot.VideoVariant("https://cdn.example/360.mp4", "360p", None, 58_000_000),
        bot.VideoVariant("https://cdn.example/480.mp4", "480p", None, 112_000_000),
    ]

    assert (
        bot.pick_best_fitting_variant(
            variants,
            max_size_bytes=50 * 1024 * 1024,
            preferred_height=480,
        )
        is None
    )


@pytest.mark.asyncio
async def test_probe_eporner_variant_sizes_uses_one_byte_ranges():
    variants = [
        bot.VideoVariant("https://cdn.example/240.mp4", "240p", None),
        bot.VideoVariant("https://cdn.example/480.mp4", "480p", None),
    ]
    client = MockAsyncClient(
        [
            MockResponse(headers={"Content-Range": "bytes 0-0/31029784"}, status_code=206),
            MockResponse(headers={"Content-Range": "bytes 0-0/112438476"}, status_code=206),
        ]
    )

    measured = await bot._probe_eporner_variant_sizes(
        "https://www.eporner.com/video-AbC123/example/",
        variants,
        client,
    )

    assert [variant.size_bytes for variant in measured] == [31_029_784, 112_438_476]
    assert all(request[2]["headers"]["Range"] == "bytes=0-0" for request in client.requests)


def test_pick_best_variant_uses_resolution_when_bitrate_unknown():
    variants = [
        bot.VideoVariant("https://cdn.example/480.mp4", "854x480", None),
        bot.VideoVariant("https://cdn.example/720.mp4", "1280x720", None),
    ]

    best = bot.pick_best_variant(variants)

    assert best.url == "https://cdn.example/720.mp4"


def test_extract_tweet_url_matches_valid_urls():
    text = "watch this https://x.com/example_user/status/1234567890 and ignore https://x.com/other/status/2"

    assert bot.extract_tweet_url(text) == "https://x.com/example_user/status/1234567890"


def test_extract_tweet_url_normalizes_http_urls():
    text = "http://twitter.com/example/status/123"

    assert bot.extract_tweet_url(text) == "https://twitter.com/example/status/123"


def test_extract_tweet_url_rejects_non_matching_strings():
    assert bot.extract_tweet_url("https://x.com/example") is None
    assert bot.extract_tweet_url("no tweet here") is None


def test_extract_redgifs_url_matches_watch_urls():
    text = "watch this https://www.redgifs.com/watch/decisivecelebrateduromastyxmaliensis"

    assert (
        bot.extract_redgifs_url(text)
        == "https://www.redgifs.com/watch/decisivecelebrateduromastyxmaliensis"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "http://www.eporner.com/hd-porn/AbC123/example-video/",
            "https://www.eporner.com/hd-porn/AbC123/example-video/",
        ),
        (
            "https://www.eporner.com/embed/AbC123",
            "https://www.eporner.com/embed/AbC123",
        ),
        (
            "https://www.eporner.com/video-AbC123/example-video/",
            "https://www.eporner.com/video-AbC123/example-video/",
        ),
    ],
)
def test_extract_eporner_url_matches_supported_video_urls(url, expected):
    assert bot.extract_eporner_url(f"watch this {url}") == expected


def test_extract_eporner_url_rejects_homepage():
    assert bot.extract_eporner_url("https://www.eporner.com/") is None


@pytest.mark.asyncio
async def test_extract_message_source_url_returns_first_supported_url():
    client = MockAsyncClient(MockResponse())
    text = (
        "first https://www.redgifs.com/watch/decisivecelebrateduromastyxmaliensis "
        "then https://x.com/example/status/123"
    )

    assert (
        await bot.extract_message_source_url(text, client)
        == "https://www.redgifs.com/watch/decisivecelebrateduromastyxmaliensis"
    )


def test_provider_routing_uses_dedicated_eporner_provider():
    assert bot._providers_for_url("https://www.eporner.com/video-AbC123/example/") == [
        bot.provider_eporner
    ]


def test_eporner_uses_source_specific_480p_preference():
    assert (
        bot._preferred_height_for_url("https://www.eporner.com/video-AbC123/example/")
        == 480
    )
    assert bot._preferred_height_for_url("https://x.com/example/status/123") == 720


@pytest.mark.asyncio
async def test_send_video_or_document_rejects_file_exceeding_upload_cap(tmp_path, monkeypatch):
    class MockBot:
        def __init__(self):
            self.video_calls = 0
            self.document_calls = 0

        async def send_video(self, **kwargs):
            self.video_calls += 1

        async def send_document(self, **kwargs):
            self.document_calls += 1

    video_path = tmp_path / "large.mp4"
    video_path.write_bytes(b"large")
    mock_bot = MockBot()
    monkeypatch.setattr(bot, "MAX_VIDEO_SIZE_BYTES", 3)

    uploaded = await bot._send_video_or_document(
        SimpleNamespace(bot=mock_bot),
        video_path,
        "https://www.redgifs.com/watch/example",
    )

    assert uploaded is False
    assert mock_bot.video_calls == 0
    assert mock_bot.document_calls == 0


@pytest.mark.asyncio
async def test_send_video_does_not_retry_network_error_413(tmp_path):
    class MockBot:
        def __init__(self):
            self.video_calls = 0
            self.document_calls = 0

        async def send_video(self, **kwargs):
            self.video_calls += 1
            raise bot.NetworkError("Request Entity Too Large (413)")

        async def send_document(self, **kwargs):
            self.document_calls += 1

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    mock_bot = MockBot()

    uploaded = await bot._send_video_or_document(
        SimpleNamespace(bot=mock_bot),
        video_path,
        "https://www.eporner.com/video-AbC123/example/",
    )

    assert uploaded is False
    assert mock_bot.video_calls == 1
    assert mock_bot.document_calls == 0


def test_logging_redacts_telegram_token_and_suppresses_http_request_logs():
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        f"POST https://api.telegram.org/bot{bot.TOKEN}/getMe",
        (),
        None,
    )

    assert bot._secret_filter.filter(record) is True
    assert bot.TOKEN not in record.getMessage()
    assert "<redacted>" in record.getMessage()
    assert logging.getLogger("httpx").level == logging.WARNING

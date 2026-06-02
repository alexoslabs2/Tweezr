import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("CHANNEL_ID", "-1001704658742")
os.environ.setdefault("LOG_DIR", "/tmp/xvbot-test-logs")

import pytest

import bot


class MockResponse:
    def __init__(self, json_data=None, text="", url="https://provider.test/response"):
        self._json_data = json_data
        self.text = text
        self.url = url
        self.history = []

    def json(self):
        return self._json_data

    def raise_for_status(self):
        return None


class MockAsyncClient:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return self.response


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


def test_pick_best_variant_prefers_highest_bitrate():
    variants = [
        bot.VideoVariant("https://cdn.example/720.mp4", "1280x720", 500000),
        bot.VideoVariant("https://cdn.example/1080.mp4", "1920x1080", 2000000),
        bot.VideoVariant("https://cdn.example/360.mp4", "640x360", None),
    ]

    best = bot.pick_best_variant(variants)

    assert best.url == "https://cdn.example/1080.mp4"


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

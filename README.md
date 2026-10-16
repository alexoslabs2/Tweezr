# Tweezr

Tweezr is a long-lived Telegram bot that watches one private channel for X/Twitter status URLs, RedGifs watch URLs, and Erome album URLs; downloads one video, reposts it into the same channel, and deletes the original URL message. For Erome albums, only the first valid video returned by EroDown is downloaded; images and additional album videos are ignored.

## Files

- `bot.py` - the complete bot service
- `requirements.txt` - pinned Python runtime dependencies
- `.env.example` - documented configuration template
- `test_providers.py` - offline provider tests
- `deploy/` - systemd, logrotate, and Docker Compose deployment files

## Configuration

Runtime configuration is loaded from `/etc/xvbot/.env` by `bot.py`.
For Docker Compose, `deploy/docker-compose.yml` reads the repo-local `.env` file and injects it as environment variables. Keep `.env` private; it is ignored by Git and Docker image builds.

Create a local config from the template:

```bash
cp .env.example .env
```

Then fill in `TELEGRAM_BOT_TOKEN` and `CHANNEL_ID`.

## Local Test

```bash
python -m venv /tmp/xvbot-venv
/tmp/xvbot-venv/bin/pip install -r requirements.txt pytest pytest-asyncio
env PYTHONDONTWRITEBYTECODE=1 /tmp/xvbot-venv/bin/python -m pytest -q -p no:cacheprovider
```

The test suite is offline and mocks provider responses.

Provider contract regressions use sanitized form/result fragments in
`tests/fixtures/providers/`, captured on 2026-10-16. They cover session cookies,
CSRF fields, extensionless media links, HTML results, and the GetXBot JSON API.
Download tests cover size fallback, invalid media, interrupted transfers, and log
redaction. Use Python 3.13, matching the Docker image and pinned dependencies.

## Download behavior

X providers are tried in order: TwitterVideoDownloader, GetXBot, SSSTwitter,
SaveTWT, Tweeload, and TWMate. Each adapter follows its site's form/session or
API contract. A provider can still refuse a post or change its interface; logs
distinguish HTTP/session errors, unexpected formats, and missing variants.

Within each provider, variants are tried from highest quality downwards. The
download checks the response size before consuming its body when available,
enforces the limit while streaming, and validates the MP4 signature. Failed or
cancelled transfers are removed. The effective size cap is the smaller of
`MAX_VIDEO_SIZE_MB` (MiB) and 50,000,000 bytes. Oversized files are never sent as
documents to bypass the public Telegram API limit. A document fallback is used
only when Telegram rejects a supported-size file as an invalid/unsupported video.

## Log credentials

Both console and rotating-file output redact the configured Telegram token,
URL-encoded forms, other Telegram-shaped tokens, and exception tracebacks.
HTTPX/httpcore request/debug logging is reduced to WARNING; redaction also applies
if those loggers are enabled for debugging. Provider response bodies and session
tokens are not written to application logs.

If a token was previously exposed, revoke it in BotFather and update
`TELEGRAM_BOT_TOKEN` in the deployment environment, then recreate the container.
The protection applies to new log output; existing logs and external copies still
need to be handled separately.

## Docker Deploy

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

## systemd Deploy

Install the env file and service assets on the host:

```bash
sudo install -d -m 700 /etc/xvbot
sudo install -m 600 .env /etc/xvbot/.env
sudo install -d /opt/xvbot
sudo install -m 644 bot.py requirements.txt /opt/xvbot/
```

Then install the files from `deploy/` according to your host conventions.

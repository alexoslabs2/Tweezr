# Tweezr

Tweezr is a long-lived Telegram bot that watches one private channel for X/Twitter status URLs, RedGifs watch URLs, and Eporner video URLs. It downloads the preferred video variant, reposts it into the same channel, and deletes the original URL message.

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

X and RedGifs selection defaults to 720p through `PREFERRED_VIDEO_HEIGHT=720`. For Eporner, Tweezr excludes AV1, measures each conventional MP4 with a one-byte ranged request, and downloads the highest resolution that fits `MAX_VIDEO_SIZE_MB=50`. `EPORNER_PREFERRED_VIDEO_HEIGHT=480` is used only when sizes cannot be measured. Oversized files and Telegram HTTP 413 responses are permanent failures, not retryable network errors. Eporner extraction uses `yt-dlp`, runs in a dedicated worker thread outside the async event loop, and accepts public video URLs without cookies or authentication.

Telegram request logging is suppressed and all configured token values are redacted from remaining log messages. Rotate the token immediately if it has ever appeared in logs.

## Local Test

```bash
python -m venv /tmp/xvbot-venv
/tmp/xvbot-venv/bin/pip install -r requirements.txt pytest pytest-asyncio
env PYTHONDONTWRITEBYTECODE=1 /tmp/xvbot-venv/bin/python -m pytest -q -p no:cacheprovider
```

The test suite is offline and mocks provider responses.

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

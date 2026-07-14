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

Cloud Bot API mode remains the code default: leave `TELEGRAM_BOT_API_BASE_URL` and `TELEGRAM_BOT_API_FILE_URL` empty and retain `MAX_VIDEO_SIZE_MB=50`. When custom endpoints are configured, both URLs are required and Tweezr continues to upload multipart data; PTB client `local_mode` is intentionally disabled because the bot and API server do not share identical file paths.

All providers share a strict short-edge resolution policy. Known variants below `MIN_VIDEO_HEIGHT=720` are rejected, exact `PREFERRED_VIDEO_HEIGHT=720` is preferred, and the smallest tier above it is the fallback. `REJECT_UNKNOWN_VIDEO_HEIGHT=true` rejects missing or unparseable resolution metadata. Eporner retains its one-byte size probes and supports explicit preferred/minimum overrides. Every download is bounded by `MAX_VIDEO_SIZE_MB` while streaming, preserves `MIN_FREE_DISK_MB`, and must pass read-only `ffprobe` checks for MP4, H.264 video, AAC audio when present/expected, valid duration, and minimum resolution before upload. No transcoding is performed.

Telegram request logging is suppressed and all configured token values are redacted from remaining log messages. Rotate the token immediately if it has ever appeared in logs.

## Local Test

```bash
python -m venv /tmp/xvbot-venv
/tmp/xvbot-venv/bin/pip install -r requirements.txt pytest pytest-asyncio
env PYTHONDONTWRITEBYTECODE=1 /tmp/xvbot-venv/bin/python -m pytest -q -p no:cacheprovider
```

The test suite is offline and mocks provider responses.

## Docker Deploy

The Compose deployment builds Telegram's official Local Bot API Server from the pinned `0a9e5696ba149c99bedf972f040d2e28776a8a4f` revision (version 10.1), starts it with `--local`, and exposes port 8081 only to the internal Compose network. API server state and Tweezr temporary media use separate disk-backed named volumes.

Obtain `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from `my.telegram.org`, copy `.env.example` to the ignored `.env`, and fill the three Telegram credentials plus `CHANNEL_ID`. The Compose configuration passes the API credentials only to the local server and the bot token only to Tweezr.

```bash
docker compose -f deploy/docker-compose.yml build telegram-bot-api xvbot
docker compose -f deploy/docker-compose.yml up -d telegram-bot-api
docker compose -f deploy/docker-compose.yml ps
```

Before the first cutover, stop every existing Tweezr poller and call `logOut` exactly once against Telegram's cloud Bot API. Do not put the bot token in shell history; use a protected interactive or deployment-secret mechanism for that call. Then start Tweezr and verify the internal service:

```bash
docker compose -f deploy/docker-compose.yml up -d xvbot
docker compose -f deploy/docker-compose.yml exec xvbot python -c "import os,urllib.request; base=os.environ['TELEGRAM_BOT_API_BASE_URL']; token=os.environ['TELEGRAM_BOT_TOKEN']; print(urllib.request.urlopen(base+token+'/getMe').status)"
```

Test in order: a small video, a qualifying 720p video above 50 MB, each supported source, and a known sub-720p/unknown-resolution result. Confirm successful uploads play inline and delete the URL post, while all failures preserve it. Monitor container health and free disk space during large transfers.

### Cloud rollback

1. Stop Tweezr so only one poller can use the token.
2. Call `logOut` against the Local Bot API Server using a protected token mechanism.
3. Clear both custom Bot API URL variables and restore `MAX_VIDEO_SIZE_MB=50`.
4. Start Tweezr in cloud mode and verify `getMe`, polling, upload, and deletion.

Telegram can delay cloud login for roughly ten minutes after local-server logout. Keep `telegram-bot-api-data` until rollback and the chosen retention period are complete. Temporary media in `xvbot-media` is disposable and must not be backed up.

## systemd Deploy

Install the env file and service assets on the host:

```bash
sudo install -d -m 700 /etc/xvbot
sudo install -m 600 .env /etc/xvbot/.env
sudo install -d /opt/xvbot
sudo install -m 644 bot.py requirements.txt /opt/xvbot/
```

Then install the files from `deploy/` according to your host conventions.

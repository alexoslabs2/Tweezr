# Tweezr

Tweezr is a long-lived Telegram bot that watches one private channel for media URLs, downloads the best available media, reposts it into the same channel, and deletes the original URL message.

## Supported sources

### Automatic (watch mode)
The bot listens for messages posted in the channel and acts automatically:

| URL pattern | Behaviour |
|---|---|
| `x.com/*` / `twitter.com/*/status/*` | Downloads best video via multiple providers; falls back to images via fxtwitter API |
| `redgifs.com/watch/*` | Downloads best video via RedGifs API |
| `erome.com/a/*` | Downloads first video from album via EroDown; images and additional videos are ignored |

### `/fetch` command (bulk mode)
Send `/fetch <url>` in the channel to download all media from a source:

| URL pattern | Behaviour |
|---|---|
| `redgifs.com/users/<username>` | All videos from a RedGifs user profile (paginated) |
| `boards.4chan.org/<board>/thread/<id>` | All images and videos from a 4chan thread |

Only one fetch runs at a time. Use `/stop` to cancel an in-progress fetch — the bot finishes the current item, then stops and reports how many items were sent.

## Files

- `bot.py` — the complete bot service
- `requirements.txt` — pinned Python runtime dependencies
- `.env.example` — documented configuration template
- `test_providers.py` — offline provider tests
- `deploy/` — systemd, logrotate, and Docker Compose deployment files

## Configuration

Runtime configuration is loaded from `/etc/xvbot/.env` by `bot.py`.
For Docker Compose, `deploy/docker-compose.yml` reads the repo-local `.env` file and injects it as environment variables. Keep `.env` private; it is ignored by Git and Docker image builds.

Create a local config from the template:

```bash
cp .env.example .env
```

Then fill in at minimum `TELEGRAM_BOT_TOKEN` and `CHANNEL_ID`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | BotFather token |
| `CHANNEL_ID` | yes | — | Private channel numeric ID (e.g. `-1001234567890`) |
| `DOWNLOAD_TIMEOUT_SECONDS` | no | `60` | Per-request HTTP timeout |
| `MAX_VIDEO_SIZE_MB` | no | `50` | Files above this are sent as documents |
| `REQUEST_USER_AGENT` | no | `Mozilla/5.0 (compatible; XVBOT/1.0)` | Outbound User-Agent |
| `LOG_LEVEL` | no | `INFO` | Python logging level |
| `LOG_DIR` | no | `/var/log/xvbot` | Log file directory |
| `ADMIN_CHAT_ID` | no | — | Telegram chat ID to DM when all providers fail |

## Twitter/X video providers

When a tweet URL is detected, the bot tries the following providers in order, picks the highest-quality variant, and falls back to the next provider on failure:

1. savetwt.com
2. ssstwitter.com
3. tweeload.com
4. twittervideodownloader.com
5. twmate.com
6. getxbot.com

If all video providers fail and the tweet contains only images, the bot fetches them via the fxtwitter API (`api.fxtwitter.com`) and sends them as photos.

## Docker Deploy

```bash
docker build -t xvbot:latest .
docker run -d --name xvbot --env-file .env xvbot:latest
```

Or with Docker Compose:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

## Local Test

```bash
python -m venv /tmp/xvbot-venv
/tmp/xvbot-venv/bin/pip install -r requirements.txt pytest pytest-asyncio
env PYTHONDONTWRITEBYTECODE=1 /tmp/xvbot-venv/bin/python -m pytest -q -p no:cacheprovider
```

The test suite is offline and mocks provider responses.

## systemd Deploy

Install the env file and service assets on the host:

```bash
sudo install -d -m 700 /etc/xvbot
sudo install -m 600 .env /etc/xvbot/.env
sudo install -d /opt/xvbot
sudo install -m 644 bot.py requirements.txt /opt/xvbot/
```

Then install the files from `deploy/` according to your host conventions.

# Tweezr

Tweezr is a long-lived Telegram bot that watches one private channel for X/Twitter status URLs, downloads the best available video through a public-provider cascade, reposts the video into the same channel, and deletes the original URL message.

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

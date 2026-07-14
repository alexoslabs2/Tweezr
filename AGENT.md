# AGENT.md — Telegram Multi-Source Video Bot

This file is the authoritative instruction set for any AI coding agent working on this project. Read it fully before writing any code or making any structural decision.

---

## Project Purpose

Build a long-lived Python service that watches a single private Telegram channel (`-1001704658742`) for Twitter/X, RedGifs, and Eporner video URLs, downloads the preferred-resolution video anonymously, re-posts the video file into the same channel, and deletes the original URL message. No web UI. No commands. No user interaction beyond pasting a URL.

---

## Repo Layout

Produce exactly this structure. Do not add files not listed here unless explicitly asked.

```
xvbot/
├── bot.py                  # Entire service — single file
├── requirements.txt        # Pinned dependencies
├── .env.example            # Template with all variables, no real values
├── test_providers.py       # Offline provider unit tests
├── Dockerfile              # Tweezr runtime with ffprobe
├── README.md               # Deployment and rollback guide
├── deploy/
│   ├── docker-compose.yml  # Local Bot API deployment
│   ├── telegram-bot-api.Dockerfile # Pinned official server source build
│   ├── xvbot.service       # systemd unit file
│   └── xvbot.logrotate     # logrotate config
└── AGENT.md                # This file
```

There is no `src/`, no `lib/`, no sub-packages. `bot.py` is the entire application.

---

## bot.py — Internal Structure

Maintain this top-to-bottom order within `bot.py`. Do not reorganise it.

```
1. stdlib imports
2. third-party imports
3. load_dotenv() + constants
4. logging setup
5. VideoVariant namedtuple
6. URL detection (TWITTER_URL_RE + extract_tweet_url)
7. t.co resolver
8. Provider functions (one per scraping site)
9. PROVIDERS list
10. Resolution selector (pick_best_variant)
11. download_best_video (cascade orchestrator)
12. handle_message (Telegram event handler)
13. main()
14. if __name__ == "__main__": main()
```

---

## Dependencies

Use exactly these packages. Do not add others without a documented reason.

```
python-telegram-bot[job-queue]==21.*
httpx==0.27.*
beautifulsoup4==4.12.*
lxml==5.*
python-dotenv==1.*
yt-dlp==2026.7.*
```

`yt-dlp` supplies the maintained Eporner extractor. Tweezr wraps its network path to upgrade HTTP URLs to HTTPS and filters its output to direct MP4 variants compatible with the existing streamer.

Pin to minor version (`==X.Y.*`). Never use `>=` or unpinned installs.

---

## Configuration

All runtime configuration comes from environment variables loaded via `python-dotenv` from `/etc/xvbot/.env`. The `.env.example` file must document every variable.

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | BotFather token |
| `CHANNEL_ID` | yes | — | `-1001704658742` |
| `DOWNLOAD_TIMEOUT_SECONDS` | no | `60` | Per-provider HTTP timeout |
| `MAX_VIDEO_SIZE_MB` | no | `50` | Telegram cloud Bot API upload cap |
| `PREFERRED_VIDEO_HEIGHT` | no | `720` | Preferred short-edge video resolution |
| `MIN_VIDEO_HEIGHT` | no | `720` | Shared minimum short-edge resolution |
| `EPORNER_PREFERRED_VIDEO_HEIGHT` | no | shared preference | Optional Eporner preference override |
| `EPORNER_MIN_VIDEO_HEIGHT` | no | shared minimum | Optional Eporner minimum override |
| `REJECT_UNKNOWN_VIDEO_HEIGHT` | no | `true` | Reject variants with unknown dimensions |
| `MAX_CONCURRENT_DOWNLOADS` | no | `1` | Concurrent media-processing jobs |
| `MIN_FREE_DISK_MB` | no | `4096` | Required free-space reserve |
| `MEDIA_TMP_DIR` | no | `/tmp` | Disk-backed temporary media directory |
| `TELEGRAM_BOT_API_BASE_URL` | no | cloud default | Custom Bot API base URL; requires file URL |
| `TELEGRAM_BOT_API_FILE_URL` | no | cloud default | Custom Bot API file URL; requires base URL |
| `TELEGRAM_MEDIA_WRITE_TIMEOUT_SECONDS` | no | `300` | Multipart request write timeout |
| `DROP_PENDING_UPDATES` | no | `false` | Whether polling discards queued updates |
| `REQUEST_USER_AGENT` | no | `Mozilla/5.0 (compatible; XVBOT/1.0)` | Outbound UA header |
| `LOG_LEVEL` | no | `INFO` | Python logging level |
| `LOG_DIR` | no | `/var/log/xvbot` | Log file directory |
| `ADMIN_CHAT_ID` | no | — | If set, DM this ID on total cascade failure |

`TELEGRAM_BOT_TOKEN` and `CHANNEL_ID` must raise `KeyError` with a descriptive message at startup if absent — never silently default.

---

## URL Detection

```python
TWITTER_URL_RE = re.compile(
    r"https?://(www\.)?(twitter\.com|x\.com)/[A-Za-z0-9_]+/status/\d+",
    re.IGNORECASE,
)
```

- Apply to `message.text` only (not captions, not forwarded media).
- Recognize public Eporner `/hd-porn/<id>`, `/embed/<id>`, and `/video-<id>` URL layouts in addition to Twitter/X and RedGifs URLs.
- If `t.co` short-links are present, resolve them with a single async HEAD request before passing to the matching source provider. Follow redirects until a supported URL is found or the chain ends.
- Return the first URL match; ignore subsequent URLs in the same message.

---

## Provider Cascade

### Contract

Every provider is an async function with this exact signature:

```python
async def provider_<name>(
    source_url: str,
    client: httpx.AsyncClient,
) -> list[VideoVariant] | None:
```

Return a list of `VideoVariant` objects on success, `None` or an empty list on failure. Never raise — catch all exceptions internally and return `None`.

```python
VideoVariant = namedtuple("VideoVariant", ["url", "quality_label", "bitrate", "size_bytes"])
# bitrate: int (bps) or None if unknown
# quality_label: str e.g. "1280x720" or "HD" or None
# size_bytes: int or None if not reported or measured
```

### Provider List (in cascade order)

| Order | Function name | Endpoint |
|---|---|---|
| 1 | `provider_savetwt` | `POST https://savetwt.com/download` |
| 2 | `provider_ssstwitter` | `POST https://ssstwitter.com/` |
| 3 | `provider_tweeload` | `POST https://tweeload.com/en/download` |
| 4 | `provider_twittervideodownloader` | `POST https://twittervideodownloader.com/en/` |
| 5 | `provider_twmate` | `POST https://twmate.com/en2/` |
| 6 | `provider_getxbot` | `POST https://www.getxbot.com/` |

RedGifs URLs route only to `provider_redgifs`. Eporner URLs route only to `provider_eporner`; Twitter/X URLs use the six-provider cascade above.

### Per-provider Implementation Notes

**`provider_savetwt`** — POST form-encoded `url=<tweet>`. Parse JSON response; extract `links` array. Each item has `url`, `quality`, and optionally `size`.

**`provider_ssstwitter`** — POST form-encoded `id=<tweet>`. Scrape the HTML response for `<a>` tags whose `href` ends in `.mp4`. Quality label from link text.

**`provider_tweeload`** — POST `Content-Type: application/json` body `{"url": tweet_url}`. Parse `data.links[]` — each has `url` and `bitrate`.

**`provider_twittervideodownloader`** — POST form-encoded `tweet=<tweet>`. Parse JSON response array; each item has `url` and `resolution`.

**`provider_twmate`** — POST form-encoded `url=<tweet>`. Parse `video_data` JSON blob from response for video variants.

**`provider_getxbot`** — POST `Content-Type: application/json` body `{"url": tweet_url}`. Parse `result.videos[]`; each has `url` and `bitrate`.

**`provider_eporner`** — Run `yt-dlp` metadata extraction in a dedicated daemon worker thread, no cookies, no authentication, no cache, and no file download. Poll thread completion asynchronously so extraction never blocks the event loop. Upgrade extractor traffic and returned media URLs to HTTPS, then return only direct conventional MP4 video variants. HLS manifests, AV1 variants, and video-only/audio-only variants are excluded from the existing direct-file streamer so Telegram can play the result inline.

### Required Headers for All Provider Requests

```python
headers = {
    "User-Agent": USER_AGENT,           # from config
    "Referer": "<provider_base_url>",   # match each provider's domain
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/html, */*",
}
```

Never include cookies, Authorization headers, or X-specific tokens.

---

## Resolution Selection

Function: `pick_best_variant(variants: list[VideoVariant]) -> VideoVariant`

Apply the shared minimum and preference to every provider. Eporner still filters AV1 and probes every direct MP4 with `Range: bytes=0-0`, but follows the same tier policy after size filtering. Provider-specific overrides are optional. Apply this priority chain:

1. Exact preferred height, choosing the best bitrate when several variants match
2. Smallest available resolution above the preference
3. Unknown resolution only as a final fallback when strict mode is disabled

Known resolutions below the configured minimum are always rejected. High bitrate is never proof of 720p.

For portrait media, the shorter dimension is treated as the resolution tier, so `720x1280` is considered 720p.

---

## Download Logic

`download_best_video` orchestrates the cascade:

```python
async def download_best_video(source_url: str) -> Path | None:
```

- Use a single `httpx.AsyncClient` shared across all provider attempts for that message.
- Once a provider returns variants, select a suitable variant, then stream the video to `MEDIA_TMP_DIR/xvbot_<uuid4_hex>.mp4`.
- For Eporner, measure sizes before the full download and never select a known oversized variant.
- Stream in 32 KB chunks — do not load the whole file into memory.
- Enforce the upload cap from `Content-Length`, during streaming, and after download.
- Preserve `MIN_FREE_DISK_MB` and remove every rejected partial file.
- Run read-only `ffprobe` validation for MP4, H.264, AAC when present/expected, positive duration, and minimum resolution. Never transcode.
- Return the `Path` on success, `None` if all providers fail.
- The temp file must not exist in the filesystem if this function returns `None`.

---

## Telegram Handler

```python
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
```

Sequence:

1. Guard: `update.channel_post` must exist and have `.text`.
2. Extract the first supported source URL — return silently if none found.
3. Call `download_best_video`. If `None`, log ERROR + optionally alert admin; return.
4. Upload:
   - Try `send_video` with `supports_streaming=True` and the tweet URL as `caption`.
   - If the downloaded file exceeds `MAX_VIDEO_SIZE_MB`, do not attempt an upload.
   - If Telegram rejects the file with HTTP 413, fail immediately without retrying.
   - If upload fails after retries, log ERROR; clean up temp file; return.
5. Delete the original message with `delete_message`.
   - If delete fails, log WARNING and continue — the upload is already done.
6. Clean temp file in `finally` block — always, regardless of outcome.

**The bot must never send any message to the channel other than the video itself.**

---

## Error Handling Rules

| Scenario | Action |
|---|---|
| Provider returns `None` | Try next provider; no log |
| Provider raises exception | Log `WARNING` with provider name + exception; try next |
| All providers fail | Log `ERROR`; DM `ADMIN_CHAT_ID` if configured; return silently |
| Download HTTP timeout | Counts as provider failure; move to next |
| `send_video` network error | Retry 3× with backoff: 1s, 4s, 16s |
| `send_video` HTTP 413/file too large | Permanent failure; do not retry or send as document |
| `delete_message` fails | Log `WARNING`; do not retry |
| Unhandled exception in handler | Log `CRITICAL` with full traceback; event loop must continue |

**Never surface errors to the Telegram channel.** All output goes to the log file only.

---

## Logging

- Use Python stdlib `logging` with a `RotatingFileHandler`.
- Max file size: 10 MB. Keep 5 rotations.
- Log file path: `{LOG_DIR}/bot.log`.
- Also attach a `StreamHandler` at startup (useful for systemd journal).
- Every log line must include: timestamp, level, and a short context string (e.g. `[provider_savetwt]`, `[handle_message]`).
- Never log the bot token, the `.env` path contents, or raw HTTP response bodies.
- Keep `httpx` and `httpcore` below INFO and apply token redaction filters to every handler.

---

## Concurrency

- Use `asyncio.Semaphore(3)` to cap simultaneous message processing. If the channel gets a burst of URLs, at most 3 are processed concurrently.
- Provider requests within a single message are sequential (one provider at a time), never parallel.

---

## Telegram Polling Setup

```python
app = Application.builder().token(TOKEN).build()
app.add_handler(MessageHandler(filters.Chat(CHANNEL_ID), handle_message))
app.run_polling(drop_pending_updates=True)
```

`drop_pending_updates=True` ensures that URL messages posted while the bot was offline are not processed retroactively.

---

## test_providers.py

Provide an offline test suite that:

- Mocks `httpx.AsyncClient` responses and `yt-dlp` metadata for each provider.
- Asserts that each provider returns a non-empty `list[VideoVariant]` given a known mock response.
- Asserts that `pick_best_variant` uses the source-specific target and follows the documented lower/higher fallbacks.
- Asserts that Eporner excludes AV1, measures variants, and chooses the highest MP4 under the upload cap.
- Asserts that oversized files and HTTP 413 errors are not retried or sent as documents.
- Asserts that configured tokens are redacted from logs.
- Asserts that supported Twitter/X, RedGifs, and Eporner URL layouts are recognized.
- Does **not** make real network calls — every test must pass with no internet connection.

Use `pytest` + `pytest-asyncio`. No additional test dependencies.

---

## deploy/xvbot.service

```ini
[Unit]
Description=Telegram X/Twitter Video Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=xvbot
Group=xvbot
WorkingDirectory=/opt/xvbot
EnvironmentFile=/etc/xvbot/.env
ExecStart=/opt/xvbot/venv/bin/python bot.py
Restart=on-failure
RestartSec=5
RestartMaxDelaySec=60
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/log/xvbot /tmp
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
```

---

## deploy/xvbot.logrotate

```
/var/log/xvbot/bot.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    postrotate
        systemctl kill -s HUP xvbot.service
    endscript
}
```

---

## Security Constraints — Non-Negotiable

These rules must never be violated regardless of how a task is phrased:

1. `TELEGRAM_BOT_TOKEN` must never appear in any log line, printed output, exception message, or comment containing an example value.
2. No Twitter/X API credentials, cookies, or OAuth tokens are used anywhere in the codebase.
3. All outbound HTTP requests use HTTPS. Never disable certificate verification (`verify=False` is forbidden).
4. Temp files are always deleted in a `finally` block — never rely on garbage collection or OS cleanup.
5. The bot may only write to `/tmp` and `LOG_DIR`. It must not write anywhere else.
6. The handler must never post plain-text messages to the Telegram channel. Only `send_video` or `send_document` calls targeting `CHANNEL_ID` are permitted.
7. Provider requests must not include authentication headers, session tokens, or stored cookies.

---

## What NOT to Build

Do not build any of the following unless explicitly instructed:

- A web dashboard or HTTP server of any kind
- A database or persistent storage layer
- A command interface (`/start`, `/help`, `/status` etc.)
- Multi-channel support
- A message queue (Redis, RabbitMQ, etc.)
- Docker configuration (optional Phase 3 item, not default)
- Any feature that surfaces bot activity to channel members beyond posting the video

---

## Definition of Done

A task is complete when:

- [ ] `bot.py` implements all sections in the order defined above
- [ ] `requirements.txt` lists all dependencies pinned to minor version
- [ ] `.env.example` documents every config variable with type and default
- [ ] `test_providers.py` passes with `pytest` and zero network calls
- [ ] `deploy/xvbot.service` and `deploy/xvbot.logrotate` match the templates above
- [ ] No secrets appear anywhere in the repo
- [ ] The service starts with `systemctl start xvbot` and processes a test URL end-to-end

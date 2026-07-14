FROM python:3.13-slim-bookworm

ARG FFMPEG_PACKAGE_VERSION=7:5.1.9-0+deb12u1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends "ffmpeg=${FFMPEG_PACKAGE_VERSION}" \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home --home-dir /opt/xvbot xvbot \
    && mkdir -p /etc/xvbot /var/log/xvbot /var/lib/xvbot/media \
    && chown -R xvbot:xvbot /opt/xvbot /var/log/xvbot /var/lib/xvbot

WORKDIR /opt/xvbot

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

USER xvbot

CMD ["python", "bot.py"]

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --system --uid 10001 --create-home --home-dir /opt/xvbot xvbot \
    && mkdir -p /etc/xvbot /var/log/xvbot \
    && chown -R xvbot:xvbot /opt/xvbot /var/log/xvbot

WORKDIR /opt/xvbot

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

USER xvbot

CMD ["python", "bot.py"]

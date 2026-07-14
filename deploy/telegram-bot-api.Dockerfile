FROM ubuntu:24.04 AS build

ARG TELEGRAM_BOT_API_REF=0a9e5696ba149c99bedf972f040d2e28776a8a4f

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates clang cmake git gperf libc++-dev libc++abi-dev \
        libssl-dev make zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --filter=blob:none https://github.com/tdlib/telegram-bot-api.git /src \
    && cd /src \
    && git checkout "${TELEGRAM_BOT_API_REF}" \
    && git submodule update --init --recursive --depth 1 \
    && cmake -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_CXX_FLAGS="-stdlib=libc++" \
        -DCMAKE_EXE_LINKER_FLAGS="-stdlib=libc++" \
    && cmake --build build --target telegram-bot-api --parallel 2

FROM ubuntu:24.04

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates libc++1 libc++abi1 libssl3 zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10002 --home-dir /var/lib/telegram-bot-api telegram-api \
    && mkdir -p /var/lib/telegram-bot-api/tmp \
    && chown -R telegram-api:telegram-api /var/lib/telegram-bot-api

COPY --from=build /src/build/telegram-bot-api/telegram-bot-api /usr/local/bin/telegram-bot-api

USER telegram-api
WORKDIR /var/lib/telegram-bot-api
EXPOSE 8081

ENTRYPOINT ["telegram-bot-api"]

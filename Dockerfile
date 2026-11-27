# retrorender – rendering service for retro and chiptune music
#
# Stage 1 builds vgmstream-cli and stage 2 libvgm (vgm2wav) from source.
# Stage 3 is the runtime image: Python + libgme + libopenmpt + sidplayfp + ffmpeg + numpy.

FROM debian:bookworm-slim AS vgmstream
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates cmake make g++ pkg-config \
        libmpg123-dev libvorbis-dev libspeex-dev libopus-dev \
        libavformat-dev libavcodec-dev libavutil-dev libswresample-dev \
    && rm -rf /var/lib/apt/lists/*
# Switch to a release tag (e.g. --branch r2023) for reproducible builds.
RUN git clone --depth 1 https://github.com/vgmstream/vgmstream /src \
    && cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_AUDACIOUS=OFF -DBUILD_V123=OFF \
    && cmake --build /build --target vgmstream_cli -j"$(nproc)" \
    && install -m755 "$(find /build -name vgmstream-cli -type f | head -1)" /usr/local/bin/vgmstream-cli

FROM debian:bookworm-slim AS libvgm
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates cmake make g++ pkg-config zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
# This build may fail without failing the image: libvgm then shows as missing in
# /health, and VGM files with non-Sega chips give a clear error message.
RUN mkdir -p /out/bin /out/lib \
    && git clone --depth 1 https://github.com/ValleyBell/libvgm /src \
    && cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_LIBAUDIO=OFF -DBUILD_TESTS=OFF -DBUILD_PLAYER=OFF -DBUILD_VGM2WAV=ON \
    && cmake --build /build -j"$(nproc)" \
    && find /build -name vgm2wav -type f -perm -u+x -exec cp {} /out/bin/ \; \
    && find /build -name '*.so*' -exec cp -P {} /out/lib/ \; \
    || echo '!!! libvgm build failed, continuing without vgm2wav'

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-numpy python3-pil ca-certificates libgme0 libopenmpt0 sidplayfp ffmpeg zlib1g p7zip-full \
        libmpg123-0 libvorbisfile3 libspeex1 libopus0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=vgmstream /usr/local/bin/vgmstream-cli /usr/local/bin/vgmstream-cli
COPY --from=libvgm /out/bin/ /usr/local/bin/
COPY --from=libvgm /out/lib/ /usr/local/lib/
RUN ldconfig
COPY app /app
COPY config.toml /config/config.toml
# Readable for everyone regardless of permissions/ACLs on the host (the app runs as 568)
RUN chmod -R a+rX /app /config

# Filled in by GitHub Actions, shown in /health and the web UI
ARG BUILD_COMMIT=local
ARG BUILD_DATE=
ENV RETRO_BUILD_COMMIT=$BUILD_COMMIT \
    RETRO_BUILD_DATE=$BUILD_DATE
ENV RETRO_CONFIG=/config/config.toml \
    RETRO_LIBRARY=/library \
    RETRO_PORT=8090 \
    PYTHONUNBUFFERED=1
EXPOSE 8090
# 568 = TrueNAS default user "apps"
USER 568:568
WORKDIR /app
HEALTHCHECK --interval=60s --timeout=5s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8090/health')"
CMD ["python3", "server.py"]

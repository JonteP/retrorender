# retrorender – rendering service for retro and chiptune music
#
# Stage 1 builds vgmstream-cli, stage 2 libvgm (vgm2wav) and stage 3 xsfplay (PSF family)
# from source. Stage 4 is the runtime image: Python + libgme + libopenmpt + sidplayfp +
# ffmpeg + numpy.

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

# xsfplay: our CLI around the xSF emulator cores (tools/xsfplay). Every core is pinned to a
# commit so a change upstream cannot break or silently change playback.
FROM debian:bookworm-slim AS xsfplay
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates cmake make gcc g++ zlib1g-dev python3 \
    && rm -rf /var/lib/apt/lists/*
# fetch <repo> <commit or tag> <dir>
RUN printf '#!/bin/sh\nset -e\nmkdir -p "$3" && cd "$3" && git init -q && git remote add origin "https://github.com/$1" && git fetch -q --depth 1 origin "$2" && git checkout -q FETCH_HEAD\n' > /usr/local/bin/fetch \
    && chmod +x /usr/local/bin/fetch
RUN fetch kode54/psflib 95509e0c6f13d769593bbf51a1b0e0efdc355ba1 /src/psflib \
    && fetch kode54/Highly_Experimental ccffd7b5f21ab8feaed78544b677e7da0ad43ce7 /src/he \
    && fetch kode54/Highly_Theoretical 2998a4bf550949cd2daee249e725a64462cf15e0 /src/ht \
    && fetch derselbst/lazyusf2 00d19c9b1e0ae4924d395310844a94d871743c89 /src/lazyusf2 \
    && fetch CyberBotX/in_xsf 74fceae1f09f2e42afff4f71fb68b1952f494916 /src/in_xsf \
    && fetch mgba-emu/mgba 0.10.3 /src/mgba
RUN cmake -S /src/mgba -B /src/mgba/build -DCMAKE_BUILD_TYPE=Release -DLIBMGBA_ONLY=ON \
        -DBUILD_STATIC=ON -DBUILD_SHARED=OFF -DM_CORE_GB=OFF -DUSE_ZLIB=OFF -DUSE_PNG=OFF \
        -DUSE_LIBZIP=OFF -DUSE_MINIZIP=OFF -DUSE_SQLITE3=OFF -DUSE_ELF=OFF -DUSE_LZMA=OFF \
        -DUSE_EDITLINE=OFF -DUSE_FFMPEG=OFF -DUSE_DEBUGGERS=OFF -DUSE_GDB_STUB=OFF \
        -DENABLE_SCRIPTING=OFF -DUSE_LUA=OFF -DUSE_DISCORD_RPC=OFF -DUSE_EPOXY=OFF \
        -DBUILD_GL=OFF -DBUILD_GLES2=OFF -DBUILD_GLES3=OFF \
    && cmake --build /src/mgba/build --target mgba -j"$(nproc)"
COPY tools/xsfplay /src/xsfplay
RUN make -C /src/xsfplay -j"$(nproc)" PSFLIB=/src/psflib HE=/src/he HT=/src/ht \
        USF=/src/lazyusf2 MGBA=/src/mgba IN_XSF=/src/in_xsf OUT=/out \
    && strip /out/xsfplay \
    && python3 /src/xsfplay/smoketest.py /out/xsfplay

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-numpy python3-pil ca-certificates libgme0 libopenmpt0 sidplayfp ffmpeg zlib1g p7zip-full \
        libmpg123-0 libvorbisfile3 libspeex1 libopus0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=vgmstream /usr/local/bin/vgmstream-cli /usr/local/bin/vgmstream-cli
COPY --from=libvgm /out/bin/ /usr/local/bin/
COPY --from=libvgm /out/lib/ /usr/local/lib/
COPY --from=xsfplay /out/xsfplay /usr/local/bin/xsfplay
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

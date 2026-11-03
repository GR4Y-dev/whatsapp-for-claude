# syntax=docker/dockerfile:1
# v0.6.0 source is pinned by commit, independent of a movable tag.
FROM golang:1.26-bookworm AS bridge
ENV GOMAXPROCS=2 GOMEMLIMIT=512MiB
WORKDIR /src
RUN git clone https://github.com/Sealjay/mcp-whatsapp.git . \
    && git checkout --detach 078ac8c5e3f8a6f32c6a5902e4859f86c853330f
RUN go mod download \
    && CGO_ENABLED=1 go build -p 1 -trimpath -ldflags '-s -w -X main.Version=v0.6.0' \
       -o /out/whatsapp-mcp ./cmd/whatsapp-mcp

# Build from Debian's signed, security-maintained source package. All native
# decoders remain available; no graphics, LLVM, device capture or remote URLs.
FROM debian:bookworm-slim AS audio
# Make the heavy compiles run one after another, even with BuildKit parallelism.
COPY --from=bridge /out/whatsapp-mcp /tmp/bridge-built
RUN sed -i 's/Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends build-essential dpkg-dev pkg-config nasm libopus-dev
WORKDIR /src
RUN apt-get source ffmpeg \
    && cd ffmpeg-* \
    && ./configure --disable-autodetect --enable-libopus --disable-debug --disable-doc \
       --disable-ffplay --disable-ffprobe --disable-avdevice --disable-postproc \
       --disable-network --disable-protocols --enable-protocol=file,pipe \
       --disable-encoders --enable-encoder=libopus --disable-muxers --enable-muxer=ogg \
       --disable-filters --enable-filter=aresample,anull,aformat \
    && make -j2 ffmpeg \
    && install -s ffmpeg /usr/local/bin/ffmpeg

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libopus0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 10001 -m app
COPY --from=bridge /out/whatsapp-mcp /usr/local/bin/whatsapp-mcp
COPY --from=audio /usr/local/bin/ffmpeg /usr/local/libexec/ffmpeg-audio
# Force audio-only output even if an uploaded media file also has video.
COPY ffmpeg /usr/local/bin/ffmpeg
RUN chmod 755 /usr/local/bin/ffmpeg
USER app
WORKDIR /home/app
RUN mkdir -p /home/app/store
EXPOSE 8765
VOLUME ["/home/app/store"]
ENTRYPOINT ["whatsapp-mcp", "-store", "/home/app/store"]
CMD ["serve"]

# Separate optional adapter package; no device credentials in this build.
FROM ubuntu:22.04@sha256:5ec03bb3441e8b0bf3b4f9cd4629a1ae763010dc3035bb8da3ae6cf026486401 AS build
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates curl cmake g++ make libssl-dev libprotobuf-dev protobuf-compiler zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fsSL https://codeload.github.com/aws-samples/aws-iot-securetunneling-localproxy/tar.gz/8a71618acf536923e882387e8d9eca9dbc7fdbe2 -o /tmp/proxy.tgz \
    && echo "c14efa9facc2048a1020ee93cce4aead7dc6251f415e7cabc75273c38ccd6d88  /tmp/proxy.tgz" | sha256sum -c - \
    && tar -xzf /tmp/proxy.tgz --strip-components=1 && rm /tmp/proxy.tgz
# Boost source/hash are pinned by upstream fc_deps.json (1.87.0).
# Protobuf uses the supported Ubuntu package and is recorded in build-versions.txt.
RUN cmake -S . -B /build -DBUILD_TESTS=OFF -DLOCALPROXY_RELEASE=ON \
    -DDISABLE_SSL_HOST_VERIFY_OPT=ON -DLOCALPROXY_BOOST_SOURCE=fetch \
    -DLOCALPROXY_PROTOBUF_SOURCE=system -DPROTOBUF_PKG_VERSION=3.12 \
    && cmake --build /build --parallel 2 \
    && mkdir /package && cp /build/bin/localproxy /package/ \
    && cp LICENSE /package/LICENSE.localproxy \
    && dpkg-query -W > /package/build-versions.txt \
    && echo 8a71618acf536923e882387e8d9eca9dbc7fdbe2 > /package/source-revision.txt \
    && cd /package && sha256sum localproxy > localproxy.sha256
FROM scratch AS artifact
COPY --from=build /package/ /

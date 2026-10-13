FROM node:22-slim AS camofox-browser

# Pinned Camoufox version for reproducible builds
# Update these when upgrading Camoufox
ARG CAMOUFOX_VERSION=135.0.1
ARG CAMOUFOX_RELEASE=beta.24
ARG ARCH=x86_64

# Install dependencies for Camoufox (Firefox-based)
RUN apt-get update && apt-get install -y \
    # Firefox dependencies
    libgtk-3-0 \
    libdbus-glib-1-2 \
    libxt6 \
    libasound2 \
    libx11-xcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    # Mesa OpenGL/EGL for WebGL support (software rendering via llvmpipe)
    # Without these, Firefox cannot create WebGL contexts -- a major bot detection signal
    libegl1-mesa \
    libgl1-mesa-dri \
    libgbm1 \
    # Xvfb virtual display -- runs Camoufox as if on a real desktop (better anti-detection)
    xvfb \
    # Fonts
    fonts-liberation \
    fonts-noto-color-emoji \
    fontconfig \
    # Utils
    ca-certificates \
    curl \
    unzip \
    # yt-dlp runtime dependency
    python3-minimal \
    # Audio + ASR deps for Buster / reCAPTCHA audio challenge solving
    ffmpeg \
    libasound2 \
    libsndfile1 \
    python3-pip \
    # faster-whisper deps
    libomp5 \
    && rm -rf /var/lib/apt/lists/*

# Pre-bake Camoufox browser binary into image via bind mount (downloaded by Makefile)
# Note: unzip returns exit code 1 for warnings (Unicode filenames), so we use || true and verify
RUN --mount=type=bind,source=dist,target=/dist \
    mkdir -p /root/.cache/camoufox \
    && (unzip -q /dist/camoufox-${ARCH}.zip -d /root/.cache/camoufox || true) \
    && chmod -R 755 /root/.cache/camoufox \
    && echo "{\"version\":\"${CAMOUFOX_VERSION}\",\"release\":\"${CAMOUFOX_RELEASE}\"}" > /root/.cache/camoufox/version.json \
    && test -f /root/.cache/camoufox/camoufox-bin && echo "Camoufox installed successfully"

# Install yt-dlp for YouTube transcript extraction (no browser needed)
RUN --mount=type=bind,source=dist,target=/dist \
    install -m 755 /dist/yt-dlp-${ARCH} /usr/local/bin/yt-dlp

# Install local ASR for Buster-style reCAPTCHA audio solving (optional).
RUN pip3 install --break-system-packages --no-cache-dir faster-whisper || pip3 install --no-cache-dir faster-whisper

# Optional: install Buster captcha-solver Firefox extension.
# Place the signed .xpi at dist/buster.xpi (download from AMO or build from source).
# Camoufox is launched with the extension path; it is auto-loaded per session.
RUN --mount=type=bind,source=dist,target=/dist \
    if [ -f /dist/buster.xpi ]; then \
      mkdir -p /opt/camofox-extensions && \
      cp /dist/buster.xpi /opt/camofox-extensions/buster.xpi && \
      echo "buster extension installed"; \
    else \
      echo "buster.xpi not found; Buster extension not installed (Buster-style solver will use local ASR instead)"; \
    fi

WORKDIR /app

COPY package.json ./
COPY scripts/ ./scripts/
RUN npm install --production

# camoufox-js probes the proxy's public IP before it will launch a browser, using impit with a 5s
# timeout and WITHOUT ignoreTlsErrors. Behind a TLS-intercepting proxy (Bright Data Web Unlocker)
# that probe can never succeed, so the browser never starts and every render fails. Verified in
# this image: ignoreTlsErrors=false -> CONNECT rejected; ignoreTlsErrors=true -> 200.
# Upstream takes no option for this, hence the patch; the longer timeout suits unlocking proxies,
# which are slower than a plain residential exit.
RUN sed -i 's/timeout: 5000,/timeout: 20000, ignoreTlsErrors: true,/'         node_modules/camoufox-js/dist/ip.js     && grep -q "ignoreTlsErrors: true" node_modules/camoufox-js/dist/ip.js

COPY server.js ./
COPY camofox.config.json ./
COPY lib/ ./lib/
COPY plugins/ ./plugins/
COPY scripts/ ./scripts/

# Install default plugin dependencies (apt packages + post-install hooks)
RUN sh scripts/install-plugin-deps.sh

ENV NODE_ENV=production
ENV CAMOFOX_PORT=9377

EXPOSE 9377

CMD ["sh", "-c", "node --max-old-space-size=${MAX_OLD_SPACE_SIZE:-128} server.js"]

# Optional: rebuild plugin deps after adding third-party plugins
# Usage: docker build --target with-plugins -t camofox-browser .
FROM camofox-browser AS with-plugins
COPY plugins/ ./plugins/
COPY camofox.config.json ./
COPY scripts/install-plugin-deps.sh /tmp/install-plugin-deps.sh
RUN /tmp/install-plugin-deps.sh && rm /tmp/install-plugin-deps.sh

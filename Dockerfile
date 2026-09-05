FROM python:3.12-slim

# This Dockerfile is provided for local source builds. SubDuet does not publish an official
# prebuilt image. Debian's FFmpeg package may be GPL-enabled; see THIRD_PARTY_NOTICES.md before
# distributing a resulting image.

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 paircue \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent paircue \
    && mkdir -p /media /state /torrents \
    && chown -R 10001:10001 /media /state /torrents

WORKDIR /app
COPY pyproject.toml README.md LICENSE DEPENDENCY_POLICY.md THIRD_PARTY_NOTICES.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["subduet", "serve"]

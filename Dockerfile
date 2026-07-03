FROM node:22-bookworm

ARG CODEX_VERSION
ARG CODEX_RELAY_VERSION=latest
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

ENV TZ=Asia/Shanghai
ENV HOME=/home/codex
ENV CODEX_VERSION=${CODEX_VERSION}
ENV CODEX_RELAY_VERSION=${CODEX_RELAY_VERSION}
ENV PATH="/usr/local/bin:/home/codex/.local/bin:${PATH}"

RUN apt update && \
    apt install -y \
      git \
      curl \
      vim \
      nano \
      tmux \
      ca-certificates \
      openssh-client \
      bash \
      less \
      ripgrep \
      bubblewrap \
      jq \
      && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /home/codex /workspace

RUN npm_config_proxy="${HTTP_PROXY}" \
    npm_config_https_proxy="${HTTPS_PROXY}" \
    npm_config_noproxy="${NO_PROXY}" \
    npm_config_fetch_timeout=60000 \
    npm_config_fetch_retries=2 \
    npm install -g @openai/codex@${CODEX_VERSION} codex-relay@${CODEX_RELAY_VERSION}

COPY docker/start-codex-container.sh /usr/local/bin/start-codex-container

RUN chmod +x /usr/local/bin/start-codex-container

WORKDIR /workspace

LABEL org.opencontainers.image.title="codex-dev"
LABEL org.opencontainers.image.description="OpenAI Codex CLI development container"
LABEL org.opencontainers.image.version="${CODEX_VERSION}"
LABEL org.opencontainers.image.codex-relay.version="${CODEX_RELAY_VERSION}"

EXPOSE 8787

CMD ["start-codex-container"]

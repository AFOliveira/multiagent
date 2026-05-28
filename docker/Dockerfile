FROM python:3.12-slim

ARG NODE_MAJOR=22
ARG PI_VERSION=0.70.6
ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        fd-find \
        git \
        jq \
        openssh-client \
        procps \
        ripgrep \
        sudo \
        tini \
        xz-utils \
    && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g "@mariozechner/pi-coding-agent@${PI_VERSION}" pi-web-access \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/git-multiagent
COPY . /opt/git-multiagent
RUN install -m 0755 docker/entrypoint.sh /usr/local/bin/git-multiagent-container-entrypoint \
    && python -m pip install .

ENTRYPOINT ["tini", "--", "/usr/local/bin/git-multiagent-container-entrypoint"]
CMD ["multiagent", "--help"]

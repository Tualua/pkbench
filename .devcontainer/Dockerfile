FROM mcr.microsoft.com/devcontainers/python:3.12-trixie

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        shellcheck \
        shfmt \
        jq \
        openssh-client \
        tmux \
        mc \
    && rm -rf /var/lib/apt/lists/*

RUN pipx install ruff

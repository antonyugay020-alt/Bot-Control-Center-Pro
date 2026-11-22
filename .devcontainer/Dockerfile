# Reproducible headless PySide6/Qt test runtime for the *cloud* controller.
# No GPU, browser, model, paid API, bot or production runtime is started.
FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libgl1 \
      libegl1 \
      libopengl0 \
      libglib2.0-0 \
      libdbus-1-3 \
      libfontconfig1 \
      libxkbcommon-x11-0 \
      libxcb-cursor0 \
      libxcb-xinerama0 \
      libxcb-icccm4 \
      libxcb-image0 \
      libxcb-keysyms1 \
      libxcb-render-util0 \
    && rm -rf /var/lib/apt/lists/*

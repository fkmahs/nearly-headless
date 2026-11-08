# The Linux image `wk test` runs this repo's checks in (Windows and macOS laptops with Docker running). wk builds it
# once and again only when this file changes. Keep it to what the tests need: Python 3.12 and git.
# The exact Python patch version, so every laptop builds the same image.
FROM python:3.12.16-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

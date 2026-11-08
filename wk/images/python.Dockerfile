# The kit's default Linux image for a Python repo with no wk-test.Dockerfile of its own: `nh test` runs its checks
# here on Windows and macOS laptops. Python and git only; a repo that needs more ships its own wk-test.Dockerfile.
# The exact patch version, so every laptop builds the same image.
FROM python:3.12.16-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

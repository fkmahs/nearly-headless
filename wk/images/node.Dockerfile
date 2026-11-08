# The kit's default Linux image for a Node repo with no wk-test.Dockerfile of its own: `nh test` runs its checks
# here on Windows and macOS laptops. Node, npm and git only; the checks run offline, so they use the repo's own
# node_modules. A repo that needs more ships its own wk-test.Dockerfile.
# The exact version, so every laptop builds the same image.
FROM node:22.12.0-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

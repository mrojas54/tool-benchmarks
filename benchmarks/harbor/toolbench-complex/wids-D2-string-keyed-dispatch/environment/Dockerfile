FROM node:22-bookworm-slim

ARG REPO_URL=https://github.com/mrojas54/wids-nyc-reading-group-assistant
ARG REPO_SHA=a39cdd0

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash agent

WORKDIR /app

RUN git clone "${REPO_URL}" . \
    && git checkout --detach "${REPO_SHA}"

COPY defect.patch /tmp/defect.patch

RUN git apply /tmp/defect.patch \
    && rm /tmp/defect.patch \
    && cd web \
    && npm ci

# The pinned parent commit contains the correct code. Replace its history with a
# single defective starting commit so an agent cannot recover the answer with
# `git show` while still retaining a useful working tree for edits and diffs.
RUN rm -rf .git \
    && git init \
    && git config user.email "benchmark@example.invalid" \
    && git config user.name "Toolbench Harbor" \
    && git add --all \
    && git commit -m "benchmark starting state" \
    && chown -R agent:agent /app

#!/bin/sh
set -e

# Runs as root only to fix ownership of bind-mounted host paths, which may
# belong to whatever uid created them (e.g. after a fresh `git clone` +
# `mkdir` on a new machine) rather than the `node` user this image runs as.
# No untrusted input (Telegram messages, source feeds) is read
# before this handoff — gosu drops all root privileges for everything after.
#
# /app/config/config.json is chowned directly, not via -R on its parent dir:
# only the file itself is a bind mount (so config/ can hold a tracked,
# anonymized example at build time without being overwritten) — the /app/config
# directory is part of the read-only image layer and chown on it fails.
chown -R node:node /data /home/node
chown node:node /app/config/config.json

exec gosu node "$@"

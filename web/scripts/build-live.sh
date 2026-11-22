#!/usr/bin/env bash
# Build the web UI into web/live, the copy web/server.js serves.
#
# pulsar-web.service runs this before every start, so
# `systemctl --user restart pulsar-web.service` deploys the current source.
# It rebuilds only when an input is newer than the last build (--force
# always rebuilds). The build goes to live-next and is swapped in only when
# it succeeds; a failed build leaves the previous live copy in place.
# Its own kit dir keeps it apart from ad-hoc `pnpm build` test runs.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

LOCK_DIR="${XDG_RUNTIME_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/pulsar}"
mkdir -p "$LOCK_DIR" || exit 1
[ -n "${XDG_RUNTIME_DIR:-}" ] || chmod 700 "$LOCK_DIR"
exec 9>"$LOCK_DIR/pulsar-web-build.lock" || exit 1
flock 9

inputs=(src static svelte.config.js vite.config.js server.js package.json pnpm-lock.yaml)
if [ "${1:-}" != "--force" ] && [ -f live/.built ] &&
   [ -z "$(find -L "${inputs[@]}" -newer live/.built -print -quit)" ]; then
  echo "build-live: live copy is current"
  exit 0
fi

started=$(date +%s.%N)
rm -rf live-next
if PULSAR_WEB_OUT=live-next PULSAR_WEB_KIT_DIR=.svelte-kit-live node_modules/.bin/vite build; then
  # Copies of static/ keep their source mtimes. The server's ETag is size +
  # mtime, so an unchanged background or icon stays a 304 after a rebuild
  # instead of downloading again.
  (cd static && find . -type f -print0) | while IFS= read -r -d '' f; do
    for out in "live-next/client/$f" "live-next/client/$f.gz" "live-next/client/$f.br"; do
      [ -f "$out" ] && touch -r "static/$f" "$out"
    done
  done
  # Stamp with the START time: an edit saved during the build is newer and
  # triggers the next rebuild.
  touch -d "@$started" live-next/.built
  rm -rf live-prev
  [ -d live ] && mv live live-prev
  mv live-next live
  echo "build-live: live copy updated"
else
  echo "build-live: BUILD FAILED, keeping the previous live copy" >&2
  rm -rf live-next
  [ -d live ] || exit 1
fi

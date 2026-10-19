#!/bin/bash
# Merp Relay guarded release, executed on the target over SSH. Uploaded fresh with every release.
# Usage: guarded-release.sh <dir>   where <dir> holds:
#   target.env      REL, DOCROOT_LINK, URL, ORIGIN, MARKERS (|-separated), MIN_BYTES, BACKUP_ROOT, HOLDER,
#                   optional CONFIG_GUARD_FILE + CONFIG_GUARD_TEXT, ARTISAN=1 (Laravel: config:cache / view:clear)
#   bundle.tar      release-relative files (public/..., config/..., ...)
#   after.sha256    sha256 of every file in bundle.tar
#   before.sha256   expected current sha256 of files being replaced; refuses if live differs (may be empty)
#   bumps.tsv       optional: file<TAB>old<TAB>new exact replacements, each must match exactly once
# Backs up everything it touches, verifies the page and its assets, and rolls back on any failure.
# Last line: RELEASE_OK backup=..., RELEASE_ABORT <reason> (nothing changed) or RELEASE_ROLLED_BACK <reason>.
set -euo pipefail
IN=$1
# shellcheck disable=SC1091
. "$IN/target.env"
STAMP=$(date +%Y%m%dT%H%M%S)
BK="$BACKUP_ROOT/$STAMP-$HOLDER"
STAGE="/tmp/relay-stage-$STAMP"
PAGE="/tmp/relay-page-$STAMP.html"
LOCK="$BACKUP_ROOT/.relay-lock"
abort() { echo "RELEASE_ABORT $1"; exit 1; }

cd "$REL" || abort "release dir missing"
if [ -n "${DOCROOT_LINK:-}" ] && [ "$(readlink -f "$DOCROOT_LINK")" != "$REL/public" ]; then abort "docroot changed"; fi
if [ -n "${CONFIG_GUARD_FILE:-}" ] && ! grep -qF "$CONFIG_GUARD_TEXT" "$CONFIG_GUARD_FILE"; then abort "config guard not found"; fi

# Remote lock: one release at a time per target, whoever starts it.
mkdir -p "$BACKUP_ROOT"
if ! (set -o noclobber; echo "$HOLDER $STAMP" > "$LOCK") 2>/dev/null; then abort "target locked by $(cat "$LOCK" 2>/dev/null)"; fi
trap 'rm -f "$LOCK" "$PAGE"; rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE" && tar -xf "$IN/bundle.tar" -C "$STAGE"
( cd "$STAGE" && sha256sum -c --quiet "$IN/after.sha256" ) || abort "bundle checksum"
if [ -s "$IN/before.sha256" ]; then sha256sum -c --quiet "$IN/before.sha256" >/dev/null 2>&1 || abort "live files differ from expected; not overwriting"; fi

FILES=$(cut -c67- "$IN/after.sha256" | sed 's/^\*//')
BUMPFILES=$( [ -s "$IN/bumps.tsv" ] && cut -f1 "$IN/bumps.tsv" | sort -u || true )
mkdir -p "$BK"
: > "$BK/new-files.txt"
for f in $FILES $BUMPFILES ${ARTISAN:+bootstrap/cache/config.php}; do
  if [ -e "$f" ]; then mkdir -p "$BK/tree/$(dirname "$f")"; cp -p "$f" "$BK/tree/$f"; else echo "$f" >> "$BK/new-files.txt"; fi
done
echo "backup=$BK"

rollback() {
  [ -d "$BK/tree" ] && ( cd "$BK/tree" && find . -type f | while read -r f; do cp -p "$f" "$REL/${f#./}"; done )
  while read -r f; do [ -n "$f" ] && rm -f "$REL/$f"; done < "$BK/new-files.txt"
  if [ "${ARTISAN:-0}" = "1" ]; then php artisan view:clear --no-interaction >/dev/null || true; fi
  echo "RELEASE_ROLLED_BACK $1"
  exit 3
}

for f in $FILES; do mkdir -p "$(dirname "$f")"; cp "$STAGE/$f" "$f"; done
sha256sum -c --quiet "$IN/after.sha256" || rollback "copy checksum"

if [ -s "$IN/bumps.tsv" ]; then
  php -r '
  foreach (file($argv[1], FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $line) {
    [$file, $old, $new] = explode("\t", $line);
    $s = file_get_contents($file);
    if (substr_count($s, $old) !== 1) { fwrite(STDERR, "expected exactly one $old in $file\n"); exit(2); }
    file_put_contents("$file.tmp", str_replace($old, $new, $s)); rename("$file.tmp", $file);
    echo "bumped $file: $new\n";
  }' "$IN/bumps.tsv" || rollback "version bump"
fi
if [ "${ARTISAN:-0}" = "1" ]; then
  if printf '%s\n' $FILES $BUMPFILES | grep -q '^config/'; then
    for f in $(printf '%s\n' $FILES $BUMPFILES | grep '^config/'); do php -l "$f" >/dev/null || rollback "syntax $f"; done
    php artisan config:cache --no-interaction >/dev/null || rollback "config:cache"
  fi
  php artisan view:clear --no-interaction >/dev/null || true
fi

sleep 2
curl -s "$URL?relay=$STAMP" > "$PAGE"
bytes=$(wc -c < "$PAGE"); echo "after_bytes=$bytes"
[ "$bytes" -gt "${MIN_BYTES:-1}" ] || rollback "page too small ($bytes bytes)"
IFS='|' read -r -a markers <<< "$MARKERS"
for m in "${markers[@]}"; do [ -z "$m" ] || grep -qF "$m" "$PAGE" || rollback "marker missing: $m"; done
if [ -s "$IN/bumps.tsv" ]; then
  while IFS=$'\t' read -r _ _ new; do grep -qF "$new" "$PAGE" || rollback "bumped link not on page: $new"; done < "$IN/bumps.tsv"
fi
fail=0
for a in $(grep -o '\(src\|href\)="/[^"/][^"]*\.\(css\|js\|png\|jpg\|webp\|svg\)[^"]*"' "$PAGE" | sed 's/^[a-z]*="//; s/"$//' | sort -u); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$ORIGIN$a"); [ "$code" = "200" ] || { echo "asset $code $a"; fail=1; }
done
for f in $FILES; do case "$f" in public/*)
  code=$(curl -s -o /dev/null -w '%{http_code}' "$ORIGIN/${f#public/}"); [ "$code" = "200" ] || { echo "file $code $f"; fail=1; };; esac; done
[ "$fail" = "0" ] || rollback "asset check"
echo "RELEASE_OK backup=$BK"

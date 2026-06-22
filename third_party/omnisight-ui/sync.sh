#!/usr/bin/env bash
# Re-vendor the launcher-web lib surface + design-system from an omnisight-ui checkout.
# Usage: sync.sh <omnisight-ui-checkout> [ref]   ( --check diffs against PINNED_REF )
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${1:?usage: sync.sh <omnisight-ui checkout> [ref]}"
REF="${2:-$(cat "$HERE/PINNED_REF")}"
( cd "$SRC" && git fetch -q origin && git checkout -q "$REF" )
rm -rf "$HERE/launcher-web" "$HERE/design-system"
mkdir -p "$HERE/launcher-web/components" "$HERE/launcher-web/lib" "$HERE/launcher-web/scripts"
cp "$SRC"/launcher-web/components/{AppGrid,AppTile,CategoryStrip}.tsx "$HERE/launcher-web/components/"
cp "$SRC"/launcher-web/lib/{icons,index,launch,manifest,types}.ts      "$HERE/launcher-web/lib/"
cp "$SRC"/launcher-web/scripts/gen_web_theme.py                        "$HERE/launcher-web/scripts/"
cp "$SRC"/launcher-web/package.json "$SRC"/launcher-web/tsconfig.json  "$HERE/launcher-web/"
cp -r "$SRC/design-system" "$HERE/design-system"
( cd "$SRC" && git rev-parse HEAD ) > "$HERE/PINNED_REF"
echo "synced launcher-web lib + design-system from $SRC @ $REF"

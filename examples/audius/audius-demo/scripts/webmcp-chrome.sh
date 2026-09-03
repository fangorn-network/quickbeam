#!/usr/bin/env bash
# Launch Chrome with WebMCP on, against a throwaway profile.
#
# `document.modelContext` does not exist in a normal browser — it is behind
# `enable-webmcp-testing`, off by default, which is why the app registers nothing for
# almost every visitor. This turns it on for one profile so you can drive the tools by
# hand (or point an agent at them) without touching your real Chrome.
#
# The flag is written into the profile's `Local State` rather than passed on the
# command line: chrome://flags entries live in that file, and a flag set this way
# survives restarts of the same profile.
#
#   scripts/webmcp-chrome.sh [url]        # default http://localhost:5180
#
# Then, in the tab's console — Chrome wants the tool OBJECT, not its name, and takes
# the arguments as a JSON STRING:
#
#   const mc = document.modelContext;
#   const t = (await mc.getTools()).find((x) => x.name === "search-music");
#   await mc.executeTool(t, JSON.stringify({ query: "late night driving", limit: 3 }));
set -euo pipefail

URL="${1:-http://localhost:5180}"
PROFILE="${WEBMCP_PROFILE:-${TMPDIR:-/tmp}/audius-webmcp-profile}"

CHROME="${CHROME_BIN:-}"
if [ -z "$CHROME" ]; then
    for c in google-chrome google-chrome-stable chromium chromium-browser \
             "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"; do
        if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then CHROME="$c"; break; fi
    done
fi
[ -n "$CHROME" ] || { echo "No Chrome found. Set CHROME_BIN=/path/to/chrome" >&2; exit 1; }

mkdir -p "$PROFILE"
# Only on first use: overwriting Local State on every run would discard whatever else
# the profile has accumulated, including the app's own localStorage-backed state.
[ -f "$PROFILE/Local State" ] ||
    echo '{"browser":{"enabled_labs_experiments":["enable-webmcp-testing@1"]}}' > "$PROFILE/Local State"

echo "chrome:   $CHROME"
echo "profile:  $PROFILE   (delete it to start clean)"
echo "url:      $URL"
echo
echo "If document.modelContext is undefined, check chrome://flags for"
echo "'WebMCP' — the flag name changes between Chrome versions."

exec "$CHROME" --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check "$URL"

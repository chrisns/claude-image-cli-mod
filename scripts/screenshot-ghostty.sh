#!/bin/zsh
# Take a screenshot of a Claude Code session in a new Ghostty window.
#
#   scripts/screenshot-ghostty.sh <out.png> "<prompt>"
#
# Ghostty draws real pixels, so this shows the `image` renderer. The prompt is
# the first message of the session. macOS only; needs Python 3 with pyobjc
# (`python3 -m pip install pyobjc-framework-Quartz`) to find the window.
set -eu

out=${1:?usage: screenshot-ghostty.sh out.png "prompt"}
prompt=${2:?prompt}
repo=${0:A:h:h}

runner=$(mktemp /tmp/claude-ghostty-XXXXXX)
cat > $runner <<RUN
#!/bin/zsh
# A clean environment: the CLAUDE_* variables of a launching session must not leak in.
for v in \$(env | grep -E '^(CLAUDE|ANTHROPIC)' | cut -d= -f1); do unset \$v; done
cd $repo
exec claude --model haiku --plugin-dir $repo/mod ${(qq)prompt}
RUN
chmod +x $runner

open -na Ghostty --args --window-width=110 --window-height=40 --font-size=13 \
  --quit-after-last-window-closed=true --command=$runner

window() {
  python3 - <<'PY'
import Quartz
for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID):
    if w.get("kCGWindowOwnerName") == "Ghostty" and w["kCGWindowBounds"]["Height"] > 100:
        print(w["kCGWindowNumber"])
        break
PY
}

# The session starts, runs the prompt and answers in about 30 seconds.
sleep 45
id=$(window)
[[ -n $id ]] || { echo "no Ghostty window found" >&2; exit 1; }

screencapture -x -o -l "$id" "$out"
pkill -f "Ghostty.app/Contents/MacOS/ghostty --window-width=110" || true
rm -f $runner
echo "wrote $out"

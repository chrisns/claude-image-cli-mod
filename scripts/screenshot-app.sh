#!/bin/zsh
# Take a screenshot of a Claude Code session in a new Ghostty or kitty window.
#
#   scripts/screenshot-app.sh <ghostty|kitty> <out.png> "<prompt>"
#
# Both draw real pixels, so this shows the `image` renderer. The prompt is the
# first message of the session (Haiku). macOS only; needs Python 3 with pyobjc
# (`python3 -m pip install pyobjc-framework-Quartz`) to find the window.
set -eu

app=${1:?usage: screenshot-app.sh ghostty|kitty out.png "prompt"}
out=${2:?out.png}
prompt=${3:?prompt}
repo=${0:A:h:h}

runner=$(mktemp /tmp/claude-terminal-XXXXXX)
cat > $runner <<RUN
#!/bin/zsh
# A clean environment: the CLAUDE_* variables of a launching session must not leak in.
for v in \$(env | grep -E '^(CLAUDE|ANTHROPIC)' | cut -d= -f1); do unset \$v; done
cd $repo
exec claude --model haiku --plugin-dir $repo/mod ${(qq)prompt}
RUN
chmod +x $runner

case $app in
  ghostty)
    owner=Ghostty
    open -na Ghostty --args --window-width=110 --window-height=40 --font-size=13 \
      --quit-after-last-window-closed=true --command=$runner
    ;;
  kitty)
    owner=kitty
    open -na kitty --args -o remember_window_size=no -o initial_window_width=110c \
      -o initial_window_height=40c -o font_size=13 -o macos_quit_when_last_window_closed=yes $runner
    ;;
  *) echo "ghostty or kitty" >&2; exit 2 ;;
esac

window() {
  OWNER=$owner python3 - <<'PY'
import os, Quartz
for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID):
    if w.get("kCGWindowOwnerName") == os.environ["OWNER"] and w["kCGWindowBounds"]["Height"] > 100:
        print(w["kCGWindowNumber"])
        break
PY
}

# The session starts, runs the prompt and answers in about 30 seconds.
sleep 45
id=$(window)
[[ -n $id ]] || { echo "no $owner window found" >&2; exit 1; }

screencapture -x -o -l "$id" "$out"
pkill -f "$runner" || true
rm -f $runner
echo "wrote $out"

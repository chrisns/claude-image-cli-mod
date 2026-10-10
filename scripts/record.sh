#!/bin/zsh
# Record a Claude Code session in a new iTerm2 window as an animated GIF.
#
#   scripts/record.sh <out.gif> "<prompt>" [columns] [rows]
#
# It starts `claude` (Haiku) with this repository's mod, types the prompt,
# captures the window about five times a second until the turn ends, and
# builds the GIF with scripts/make-gif.py. macOS, iTerm2 and ffmpeg only.
set -eu

out=${1:?usage: record.sh out.gif "prompt" [columns] [rows]}
prompt=${2:?prompt}
columns=${3:-110}
rows=${4:-46}
repo=${0:A:h:h}
frames=$(mktemp -d /tmp/claude-record-XXXXXX)

id=$(osascript <<OSA
tell application "iTerm"
  activate
  set w to (create window with default profile)
  tell current session of w
    set columns to $columns
    set rows to $rows
    write text "clear; cd $repo && claude --model haiku --plugin-dir $repo/mod"
  end tell
  return id of w
end tell
OSA
)

press() { osascript -e "tell application \"iTerm\" to tell current session of window id $id to write text (ASCII character 13) newline no"; }
screen() { osascript -e "tell application \"iTerm\" to tell current session of window id $id to get contents"; }

for _ in {1..40}; do
  sleep 1
  screen | grep -q "Claude Code v" && break
done
sleep 4

osascript -e "tell application \"iTerm\" to tell current session of window id $id to write text \"$prompt\""
sleep 1.5; press; sleep 1; press

# Capture until the turn has ended and the overlay has had time to draw.
n=0
ended=0
while true; do
  screencapture -x -o -l "$id" "$frames/$(printf %05d $n).png"
  n=$((n + 1))
  if [[ $ended -eq 0 ]] && screen | grep -Eq " for [0-9]+[sm].* · done "; then ended=$n; fi
  [[ $ended -gt 0 && $n -ge $((ended + 15)) ]] && break
  [[ $n -ge 400 ]] && break
  sleep 0.12
done

osascript -e "tell application \"iTerm\" to tell current session of window id $id to write text \"/exit\""
sleep 1; press; sleep 2
osascript -e "tell application \"iTerm\" to close window id $id" || true

python3 "$repo/scripts/make-gif.py" "$frames" "$out"

# KEEP_FRAMES=<folder> keeps the captures, to build the GIF again with other settings.
if [[ -n ${KEEP_FRAMES:-} ]]; then rm -rf "$KEEP_FRAMES"; mv "$frames" "$KEEP_FRAMES"; else rm -rf "$frames"; fi

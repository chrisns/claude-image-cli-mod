#!/bin/zsh
# Record a Claude Code session in a new iTerm2 window as an animated GIF.
#
#   scripts/record.sh <out.gif> "<prompt>" [columns] [rows]
#   WINDOW=<iTerm2 window id> scripts/record.sh <out.gif> "<prompt>"
#
# CLAUDE_ARGS adds arguments to the `claude` it starts (a --settings file, say).
# With WINDOW it records in a session that is open already, and leaves it
# open: a usage guard such as spare10 asks for consent once per session, and
# only you can give it.
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

if [[ -n ${WINDOW:-} ]]; then
  id=$WINDOW
else
id=$(osascript <<OSA
tell application "iTerm"
  activate
  set w to (create window with default profile)
  tell current session of w
    set columns to $columns
    set rows to $rows
    write text "clear; cd $repo && claude --model haiku --plugin-dir $repo/mod ${CLAUDE_ARGS:-}"
  end tell
  return id of w
end tell
OSA
)
fi

press() { osascript -e "tell application \"iTerm\" to tell current session of window id $id to write text (ASCII character 13) newline no"; }
screen() { osascript -e "tell application \"iTerm\" to tell current session of window id $id to get contents"; }

# A dialog (a permission prompt, a usage guard) waits for you, not for this
# script: stop, and touch nothing in the session.
dialog() { screen | grep -v "^ *$" | tail -3 | grep -q "Enter to select"; }
give_up() {
  [[ -z ${WINDOW:-} ]] && { osascript -e "tell application \"iTerm\" to close window id $id" || true; }
  echo "a dialog waits in the Claude Code session: answer it in a session of your own, then run this again" >&2
  exit 3
}

# Whether the prompt is still in the input box (its last line starting with ❯).
unsent() { screen | grep "^❯" | tail -1 | sed 's/^❯[[:space:] ]*//' | grep -q '[^[:space:] ]'; }

# Send the prompt. The paste can take the first Return; another goes only
# while the prompt is still unsent, never into a dialog that opened meanwhile.
submit() {
  osascript -e "tell application \"iTerm\" to tell current session of window id $id to write text \"$1\""
  sleep 1.5
  press

  for _ in 1 2 3; do
    sleep 1.5
    dialog && give_up
    unsent || return 0
    press
  done
}

if [[ -z ${WINDOW:-} ]]; then
  for _ in {1..40}; do
    sleep 1
    screen | grep -q "Claude Code v" && break
  done
  sleep 4
fi

# The turns that are done before this prompt: the end of this one is one more.
before=$(screen | grep -Ec " for [0-9]+[sm].* · done " || true)

submit "$prompt"

# Capture until the turn has ended and the overlay has had time to draw.
n=0
ended=0
while true; do
  screencapture -x -o -l "$id" "$frames/$(printf %05d $n).png"
  n=$((n + 1))
  if [[ $ended -eq 0 ]] && [[ $(screen | grep -Ec " for [0-9]+[sm].* · done " || true) -gt $before ]]; then ended=$n; fi
  [[ $ended -eq 0 ]] && dialog && { rm -rf "$frames"; give_up; }
  [[ $ended -gt 0 && $n -ge $((ended + 15)) ]] && break
  [[ $n -ge 400 ]] && break
  sleep 0.12
done

# Close the window: never type into a session that may show a dialog (an
# answer typed there would be the script's, not yours). A WINDOW stays open.
[[ -z ${WINDOW:-} ]] && { osascript -e "tell application \"iTerm\" to close window id $id" || true; }

# STILL=<file.png> also saves the last frame, cut the same way.
still=()
[[ -n ${STILL:-} ]] && still=(--still "$STILL")
python3 "$repo/scripts/make-gif.py" "$frames" "$out" "${still[@]}"

# KEEP_FRAMES=<folder> keeps the captures, to build the GIF again with other settings.
if [[ -n ${KEEP_FRAMES:-} ]]; then rm -rf "$KEEP_FRAMES"; mv "$frames" "$KEEP_FRAMES"; else rm -rf "$frames"; fi

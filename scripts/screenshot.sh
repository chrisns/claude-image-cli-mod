#!/bin/zsh
# Take a screenshot of a Claude Code session in a new iTerm2 window.
#
#   scripts/screenshot.sh <out.png> <with|without> "<prompt>" [columns] [rows]
#
# It opens a window, starts `claude` (with this repository's mod when the second
# argument is "with"), types the prompt, waits for the reply, saves a
# screenshot of the window and closes it. macOS and iTerm2 only.
# The screenshot needs the Screen Recording permission for your terminal.
set -eu

out=${1:?usage: screenshot.sh out.png with|without "prompt" [columns] [rows]}
mode=${2:?with or without}
prompt=${3:?prompt}
columns=${4:-110}
rows=${5:-46}

repo=${0:A:h:h}
flags="--model haiku"
[[ $mode == with ]] && flags="--model haiku --plugin-dir $repo/mod"

id=$(osascript <<OSA
tell application "iTerm"
  activate
  set w to (create window with default profile)
  tell current session of w
    set columns to $columns
    set rows to $rows
    write text "clear; cd $repo && claude $flags"
  end tell
  return id of w
end tell
OSA
)

press() { osascript -e "tell application \"iTerm\" to tell current session of window id $id to write text (ASCII character 13) newline no"; }
screen() { osascript -e "tell application \"iTerm\" to tell current session of window id $id to get contents"; }

# Wait for the prompt. A new folder asks for trust first: accept it for this repository.
for _ in {1..40}; do
  sleep 1
  if screen | grep -q "trust this folder"; then
    osascript -e "tell application \"iTerm\" to tell current session of window id $id to write text (ASCII character 27) & \"[B\" newline no"
    sleep 0.5; press
  fi
  screen | grep -q "Claude Code v" && break
done
sleep 4

osascript -e "tell application \"iTerm\" to tell current session of window id $id to write text \"$prompt\""
sleep 2; press; sleep 3; press

# Wait until the turn is over: the status line no longer says it is working.
sleep 12
for _ in {1..60}; do
  screen | grep -Eq "(Baked|Brewed|Churned|Cogitated|Cooked|Crunched|Worked|Sautéed|Saut.ed) for" && break
  sleep 2
done
sleep 3

screencapture -x -o -l "$id" "$out"

osascript -e "tell application \"iTerm\" to tell current session of window id $id to write text \"/exit\""
sleep 1; press; sleep 2
osascript -e "tell application \"iTerm\" to close window id $id" || true
echo "wrote $out"

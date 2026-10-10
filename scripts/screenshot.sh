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

# A dialog (a permission prompt, a usage guard) waits for you, not for this
# script: stop, and touch nothing in the session.
dialog() { screen | grep -v "^ *$" | tail -3 | grep -q "Enter to select"; }
give_up() {
  osascript -e "tell application \"iTerm\" to close window id $id" || true
  echo "a dialog waits in the Claude Code session: answer it in a session of your own, then run this again" >&2
  exit 3
}

# Whether the prompt is still in the input box (its last line starting with ❯).
unsent() { screen | grep "^❯" | tail -1 | sed 's/^❯[[:space:] ]*//' | grep -q '[^[:space:] ]'; }

# Send the prompt. The paste can take the first Return; another goes only
# while the prompt is still unsent, never into a dialog that opened meanwhile.
submit() {
  # The prompt goes as an argument, never into the script's source: a quote
  # or a backslash in it is typed as it is.
  osascript - "$id" "$1" <<'OSA'
on run argv
  tell application "iTerm" to tell current session of window id (item 1 of argv as integer) to write text (item 2 of argv)
end run
OSA
  sleep 1.5
  press

  for _ in 1 2 3; do
    sleep 1.5
    dialog && give_up
    unsent || return 0
    press
  done
}

# Wait for the prompt. A folder Claude Code has not seen asks for trust first:
# that is your decision, so stop and let you answer it once.
for _ in {1..40}; do
  sleep 1
  screen | grep -q "trust this folder" && give_up
  screen | grep -q "Claude Code v" && break
done
sleep 4

submit "$prompt"

# Wait until the turn is over: the status line no longer says it is working.
sleep 12
for _ in {1..60}; do
  screen | grep -Eq " for [0-9]+[sm].* · done " && break
  dialog && give_up
  sleep 2
done
sleep 3

screencapture -x -o -l "$id" "$out"

# Close the window: never type into a session that may show a dialog (an
# answer typed there would be the script's, not yours).
osascript -e "tell application \"iTerm\" to close window id $id" || true
echo "wrote $out"

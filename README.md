# inline-images

**See the pictures in your Claude Code transcript.**

[![test](https://github.com/chrisns/claude-image-cli-mod/actions/workflows/test.yml/badge.svg)](https://github.com/chrisns/claude-image-cli-mod/actions/workflows/test.yml)
[![MIT licence](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)

inline-images is a mod for [Claude Code](https://claude.com/claude-code). Many command-line tools print images with the [iTerm2 inline images protocol](https://iterm2.com/documentation-images.html): `imgcat`, `it2cat`, `chafa -f iterm`, `timg -p iterm`, `wezterm imgcat`, matplotlib backends and more. Claude Code does not show these images. You see "Ran 1 shell command", and the model reads a wall of base64.

This mod shows each image under the tool call that printed it. It also shows the images that Claude sends to you.

| Without the mod | With the mod |
|---|---|
| ![Claude Code shows only "Ran 1 shell command"](docs/screenshots/without-mod.png) | ![Claude Code shows the photo of the Earth under the Bash call](docs/screenshots/with-mod.png) |

The picture on the right is a real iTerm2 image, at full resolution, inside a running Claude Code session.

## Contents

- [Quick start](#quick-start)
- [Features](#features)
- [Terminals](#terminals)
- [Options](#options)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Uninstall](#uninstall)
- [Develop](#develop)

## Quick start

1. Install the mod. Type this in a Claude Code session:

   ```
   /plugin install inline-images --marketplace chrisns/claude-image-cli-mod
   ```

   Answer `y` to add the marketplace. Then choose a scope.

2. Install Pillow, the image library that the mod uses:

   ```
   python3 -m pip install Pillow
   ```

3. For real pixels in iTerm2, do two more steps:

   - Run `python3 -m pip install iterm2`.
   - In iTerm2, open **Settings > General > Magic** and select **Enable Python API**.

4. Test it. Ask Claude:

   > Download https://raw.githubusercontent.com/chrisns/claude-image-cli-mod/main/docs/fixtures/earth.jpg and show it with imgcat.

   If you do not have `imgcat`, this command prints the same sequence:

   ```
   printf '\033]1337;File=inline=1:%s\a\n' "$(base64 < earth.jpg | tr -d '\n')"
   ```

   You see the Earth under the Bash call, with a caption such as `earth.jpg · 800×800 · JPEG · 141.6 KB`.

## Features

- **Previews in the transcript.** Each image appears under the tool call that printed it, with its name, size and format.
- **Real pixels.** iTerm2, kitty and Ghostty show the real image. Other terminals show a preview in coloured blocks.
- **Images that Claude sends.** An image file that Claude delivers with `SendUserFile` or `SendUserMessage` appears under the delivery.
- **Click to open.** Click a picture to open it in your system's viewer, for example Preview on macOS.
- **A clean context.** The model reads `[inline image: earth.jpg, 141.6 KB, shown to the user]`, not 190,000 characters of base64.
- **Large images.** Claude Code cuts tool output at 30,000 characters. The mod reads the whole output from the file where Claude Code saves it.
- **The right shape.** The mod measures your terminal's cell size and keeps the picture's aspect ratio.
- **The whole protocol.** `File=`, chunked `MultipartFile`, the tmux passthrough wrapper, the `BEL` and `ST` terminators, `width`, `height` and `preserveAspectRatio`. A download (`inline=0`) is not shown, and its note says so.
- **Transparency** and several images in one command.

![One command that prints a PNG with a transparent background and a gradient](docs/screenshots/gallery.png)

## Terminals

| Terminal | What you see | How |
|---|---|---|
| iTerm2 | Real pixels | The [iTerm2 overlay](#real-pixels-in-iterm2). It needs the `iterm2` Python module and the Python API. |
| iTerm2 without the Python API | Coloured blocks | The `cells` renderer. |
| kitty, Ghostty | Real pixels | Claude Code's own `Image` element, which uses the kitty graphics protocol. |
| Any other terminal | Coloured blocks | The `cells` renderer. Each cell holds 2 by 2 blocks of colour. |

This is the same photo in Ghostty:

![The photo of the Earth in Ghostty, drawn with real pixels](docs/screenshots/with-mod-ghostty.png)

This is the block preview that other terminals show:

![The photo of the Earth drawn with coloured block characters](docs/screenshots/with-mod-cells.png)

## Options

Change the options in the config menu of Claude Code, or under `pluginConfigs` in your settings.

| Option | Default | What it does |
|---|---|---|
| `renderer` | `auto` | `auto` uses `iterm` in iTerm2, `image` in kitty and Ghostty, and `cells` in other terminals. You can also set `iterm`, `image` or `cells`. |
| `max_columns` | `100` | The widest that a preview can be, from 1 to 255 columns. A preview is never wider than the transcript. |
| `max_rows` | `28` | The tallest that a preview can be, from 1 to 255 rows. |
| `palette` | `0` | Reduce a block preview to this many colours, from 0 to 256. `0` keeps all colours. A value near `32` can look cleaner on a busy picture. |
| `hide_from_model` | `true` | Replace the image data in the tool result with a short note. |
| `python` | `python3` | The Python 3 that runs the helper and the iTerm2 overlay. |

## How it works

1. A `ui.render` hook looks for `ESC ] 1337 ; File=` in the output of each tool call.
2. [`mod/hooks/osc1337.ts`](mod/hooks/osc1337.ts) reads the sequences.
3. [`mod/bin/render.py`](mod/bin/render.py) decodes the image and scales it to fit a box of cells. It reads the cell size from the terminal with `TIOCGWINSZ`.
4. The mod draws the box with the renderer that your terminal supports.
5. A transparent click layer ([`mod/hooks/click.tsx`](mod/hooks/click.tsx)) lies over each picture. A click opens the picture.
6. A `session.append` hook removes the image data from what the model reads. The transcript keeps the whole output, so the preview comes back when you resume a session.

### Real pixels in iTerm2

Claude Code owns the screen, so a mod cannot print an escape sequence. Claude Code's `Image` element uses kitty Unicode placeholders, and iTerm2 does not draw those. So the `iterm` renderer works beside Claude Code:

1. The mod draws the block preview. The first 5 cells of each row hold a hidden marker: braille glyphs in the colour of the cell. The marker carries the box's id and the row number.
2. [`mod/bin/iterm_overlay.py`](mod/bin/iterm_overlay.py) starts with the session. It watches the screen through the iTerm2 Python API.
3. When it finds a box, it writes the real image over it with the iTerm2 protocol. It saves the cursor before and restores it after.

<details>
<summary>How the overlay keeps the screen correct</summary>

Claude Code repaints only the cells that it thinks have changed. An image that is in the wrong place stays there. These rules prevent that:

- Each marker glyph includes the row number. When a box moves, every marker cell changes. Claude Code repaints them, and the overlay finds the box in its new place.
- A box must stay in one place for one look. The overlay reads the screen again just before it draws.
- The overlay draws only rows that show the box and nothing else. So an image never hides a hint or a menu that Claude Code draws over the transcript.
- If Claude Code repaints part of an image in place, the overlay draws the image again.
- A box that the edge of the screen cuts gets a cropped image.
- A large image goes as a chunked `MultipartFile` in one write. iTerm2 prints a single `File=` sequence of about 1 MB as text.

</details>

## Troubleshooting

**No preview, and the raw output shows.** The mod is not loaded. Run `/plugin` and check that `inline-images` is enabled.

**A dim line says `no preview: …`.** The helper could not draw the image. The text after `no preview:` gives the reason:

- `no image decoder`: install Pillow, or set the `python` option to a Python that has it.
- `not a supported image format`: the mod draws only PNG, JPEG, GIF, WebP, BMP, TIFF and ICO.
- `the image is too large`: the image has more than 40 million pixels, which is too many to decode safely.

**iTerm2 shows blocks, not real pixels.**

1. Check that `python3 -c "import iterm2"` works in the Python that the `python` option names.
2. Check that iTerm2 > Settings > General > Magic > **Enable Python API** is on.
3. Start a new Claude Code session.

To see why the overlay is off, set the `renderer` option to `iterm`. A message then tells you the reason.

**The picture tears while you scroll.** In iTerm2, the overlay draws again when the screen stops moving. A picture can look broken for a moment during a fast scroll.

**Over ssh.** Real pixels need the mod and the terminal on the same machine. Over ssh you see the block preview.

## FAQ

**Can Claude see the images?** No. The model reads a short note. To let Claude look at a picture, ask it to read the image file.

**Why not sixel?** Claude Code draws its screen itself. A mod can give it only text, cells and kitty images. The iTerm2 overlay is the one way to draw pixels beside it.

**Where does the mod keep the images?** In your user cache folder: `~/Library/Caches/inline-images` on macOS, or `~/.cache/inline-images` on Linux. Only your user can read the folder. The mod deletes files that are older than 24 hours.

**Does it send anything over the network?** No.

**Is it safe to show an image from an unknown source?** The mod treats all tool output as untrusted. Read [SECURITY.md](SECURITY.md) for what it does.

**Why braille glyphs in the iTerm2 marker?** No prompt, reply or diff uses them, and each glyph is one cell wide.

## Uninstall

```
/plugin uninstall inline-images
```

To remove the marketplace too, run `/plugin marketplace remove claude-image-cli-mod`. To remove the cache, delete `~/Library/Caches/inline-images` on macOS, or `~/.cache/inline-images` on Linux.

## Develop

Run the mod from a clone:

```
claude --plugin-dir ./mod
```

Run the checks:

```
claude plugin validate .
claude plugin test mod
python3 -m unittest discover -s mod/tests -p 'test_*.py'
```

The [CI workflow](.github/workflows/test.yml) runs the same checks on Linux and macOS, with Pillow and with ImageMagick.

To type-check, start one session with the mod, so that Claude Code writes the types into `mod/.claude-plugin/types`. Then run `npx -p typescript tsc -p mod`.

To see each draw of the iTerm2 overlay, start `claude` with `INLINE_IMAGES_DEBUG=/tmp/overlay.log`.

### Screenshots

The scripts in [`scripts/`](scripts) drive iTerm2 and Ghostty on macOS. They start a session with Haiku, send a prompt and save the window. Your terminal needs the Screen Recording permission.

```
scripts/screenshot.sh docs/screenshots/raw-with.png with 'Use the Bash tool to run ~/.iterm2/imgcat docs/fixtures/earth.jpg. Do not read the file yourself. Reply with the single word: done.'
python3 scripts/crop-screenshot.py docs/screenshots/raw-with.png docs/screenshots/with-mod.png
```

`crop-screenshot.py` removes the input box and the status lines.

## Credits

- `docs/fixtures/earth.jpg` is [The Earth seen from Apollo 17](https://commons.wikimedia.org/wiki/File:The_Earth_seen_from_Apollo_17.jpg), a NASA photo in the public domain.
- `docs/fixtures/earth-cutout.png` is the same photo with a transparent background.
- `docs/fixtures/gradient.png` was made for this project. It is under the MIT licence.

## Licence

MIT. See [LICENSE](LICENSE).

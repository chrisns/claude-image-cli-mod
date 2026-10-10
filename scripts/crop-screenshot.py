#!/usr/bin/env python3
"""Crop a Claude Code screenshot to the transcript.

Removes the input box, Claude Code's status line, iTerm2's status bar and the
line just above the input box, which show things that do not belong in a README
(the prompt suggestion, an account name, a host name, a usage notice). The cut
goes above the top one of the two long horizontal rules in the lower half of
the window.

    crop-screenshot.py in.png out.png [--width 1400]
"""

import argparse

from PIL import Image


def is_rule(pixels, width, y):
    """A row that is one colour across the whole window, and not the background."""
    row = [pixels[x, y][:3] for x in range(4, width - 4, 3)]
    first = row[len(row) // 2]
    same = sum(1 for pixel in row if sum(abs(a - b) for a, b in zip(pixel, first)) < 24)

    return first != (0, 0, 0) and same >= 0.985 * len(row)


def input_box_top(image):
    """The y of the upper rule of Claude Code's input box, or None.

    Rules are rows of one colour from edge to edge. iTerm2's status bar is a
    thick block of them at the very bottom; the input box has a thin rule above
    and below it. A white picture in the transcript is thick and uniform too, so
    only thick groups near the bottom count as status bars.
    """
    pixels = image.load()
    width, height = image.size
    groups = []

    for y in range(height - 1, height // 2, -1):
        if is_rule(pixels, width, y):
            if groups and groups[-1][-1] - y <= 1:
                groups[-1].append(y)
            else:
                groups.append([y])

    bars = [group[-1] for group in groups if len(group) > 2 and group[0] > height * 0.85]
    above = [group for group in groups if not bars or group[0] < min(bars)]
    thin = [group for group in above if len(group) <= 2]

    return thin[1][-1] if len(thin) >= 2 else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("target")
    parser.add_argument("--width", type=int, default=1400, help="scale down to this width")
    parser.add_argument("--line", type=int, default=20, help="points above the input box to cut: the spinner and any notice")
    arguments = parser.parse_args()

    image = Image.open(arguments.source).convert("RGB")
    pixels = image.load()
    width, height = image.size

    top = input_box_top(image)

    if top is None:
        raise SystemExit("could not find the input box")

    # The upper rule of the input box, less the line above it, where Claude Code
    # puts the spinner and notices (usage limits) that do not belong in a README.
    scale = 2 if width >= 1200 else 1  # a Retina capture has two pixels per point
    cut = top - 4 - arguments.line * scale
    image = image.crop((0, 0, width, cut))

    if image.width > arguments.width:
        image = image.resize((arguments.width, round(image.height * arguments.width / image.width)), Image.LANCZOS)

    image.save(arguments.target, optimize=True)
    print(f"wrote {arguments.target} {image.size}")


if __name__ == "__main__":
    main()

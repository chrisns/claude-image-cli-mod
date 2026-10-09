import { describe, expect, test } from 'claude-code/testing'

import {
  caption,
  drawsPixels,
  outputText,
  overlayMessages,
  parseReply,
  readOptions,
  savedPath,
  wantsOverlay,
  withoutImages,
  withText,
} from '../hooks/preview.ts'
import { parseArguments } from '../hooks/osc1337.ts'

describe('parseReply', () => {
  test('reads the last line of the helper output', () => {
    expect(parseReply<{ n: number }>('noise\n{"ok": true, "n": 3}\n', '', 0)).toEqual({ ok: true, n: 3 })
  })

  test('turns a helper failure into its message', () => {
    expect(() => parseReply('{"ok": false, "error": "no decoder"}', '', 1)).toThrow('no decoder')
  })

  test('says what the process wrote when it printed no JSON', () => {
    expect(() => parseReply('', 'Traceback\nImportError: nope\n', 1)).toThrow('ImportError: nope')
    expect(() => parseReply('', '', 127)).toThrow('exited with 127')
  })
})

describe('caption', () => {
  const head = parseArguments(`name=${btoa('/tmp/some/dir/photo.jpg')};size=2048;inline=1`)

  test('shows the file name, size in pixels, format and bytes', () => {
    const found = { path: '/x', format: 'JPEG', width: 1024, height: 768, frames: 1, bytes: 219023 }

    expect(caption(head, found)).toBe('photo.jpg · 1024×768 · JPEG · 213.9 KB')
  })

  test('says which frame of an animation it shows', () => {
    const found = { path: '/x', format: 'GIF', width: 10, height: 10, frames: 12, bytes: 10 }

    expect(caption(head, found)).toContain('frame 1 of 12')
  })

  test('falls back to what the sender declared', () => {
    expect(caption(head, undefined)).toBe('photo.jpg · 2.0 KB')
  })
})

describe('terminal detection', () => {
  test('kitty and Ghostty draw pixels', () => {
    expect(drawsPixels('ghostty', undefined, undefined)).toBe(true)
    expect(drawsPixels(undefined, 'xterm-kitty', undefined)).toBe(true)
    expect(drawsPixels(undefined, undefined, '1')).toBe(true)
  })

  test('iTerm2 and the rest get cells', () => {
    expect(drawsPixels('iTerm.app', 'xterm-256color', undefined)).toBe(false)
    expect(drawsPixels('Apple_Terminal', 'xterm-256color', undefined)).toBe(false)
  })
})

describe('options', () => {
  test('defaults', () => {
    expect(readOptions({}, '/mod')).toEqual({
      renderer: 'auto',
      maxColumns: 100,
      maxRows: 28,
      palette: 0,
      python: 'python3',
      helper: '/mod/bin/render.py',
      overlay: '/mod/bin/iterm_overlay.py',
    })
  })

  test('keeps values in range', () => {
    const options = readOptions({ renderer: 'cells', max_columns: 9000, max_rows: 0, palette: 999, python: '' }, '/mod')

    expect(options).toMatchObject({ renderer: 'cells', maxColumns: 255, maxRows: 1, palette: 256, python: 'python3' })
  })

  test('an unknown renderer is auto', () => {
    expect(readOptions({ renderer: 'sixel' }, '/mod').renderer).toBe('auto')
  })

  test('iterm is a renderer', () => {
    expect(readOptions({ renderer: 'iterm' }, '/mod').renderer).toBe('iterm')
  })
})

describe('iTerm2 overlay', () => {
  test('auto turns it on in iTerm2 only', () => {
    expect(wantsOverlay('auto', 'iTerm.app')).toBe(true)
    expect(wantsOverlay('auto', 'ghostty')).toBe(false)
    expect(wantsOverlay('cells', 'iTerm.app')).toBe(false)
    expect(wantsOverlay('iterm', 'Apple_Terminal')).toBe(true)
  })

  test('reads whole lines and keeps the part line', () => {
    const { messages, rest } = overlayMessages('{"ok": true, "ready": true}\nnoise\n{"ok": fa')

    expect(messages).toEqual([{ ok: true, ready: true }])
    expect(rest).toBe('{"ok": fa')
  })
})

describe('tool output', () => {
  test('text from a Bash record or from a string', () => {
    expect(outputText({ stdout: 'a' })).toBe('a')
    expect(outputText('b')).toBe('b')
    expect(outputText({ other: 1 })).toBeUndefined()
    expect(outputText(undefined)).toBeUndefined()
  })

  test('replaces the text and keeps the rest of the record', () => {
    expect(withText({ stdout: 'a', stderr: 'e' }, 'z')).toEqual({ stdout: 'z', stderr: 'e' })
    expect(withText('a', 'z')).toBe('z')
  })

  test('finds where a large output was saved', () => {
    expect(savedPath({ persistedOutputPath: '/tmp/x.txt' })).toBe('/tmp/x.txt')
    expect(savedPath({ persistedOutputPath: '' })).toBeUndefined()
    expect(savedPath({})).toBeUndefined()
  })
})

describe('withoutImages', () => {
  const ESC = '\u001b'
  const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
  const legacy = `${ESC}]1337;File=inline=1;width=20:${PNG}\u0007\n`
  const wrapped = `${ESC}Ptmux;${ESC}${ESC}]1337;File=inline=1:${PNG}\u0007${ESC}\\\n`

  test('an output of only images says how many', () => {
    expect(withoutImages(legacy)).toBe('(inline image)')
    expect(withoutImages(legacy + wrapped)).toBe('(2 inline images)')
  })

  test('other text stays', () => {
    expect(withoutImages(`made it\n${legacy}`)).toBe('made it\n\n')
  })

  test('data cut off by a size limit is still an image', () => {
    expect(withoutImages(`${ESC}]1337;File=inline=1:AAAA`)).toBe('(inline image)')
  })
})

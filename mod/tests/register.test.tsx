import { describe, expect, mock, test } from 'claude-code/testing'

// The hooks module end to end: a tool row is drawn through the plugin, with the
// image helper (bin/render.py) and the system's `open` answered by the test.

const ESC = '\u001b'
const BEL = '\u0007'
// A 1x1 PNG.
const PNG =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
const name = (text: string) => btoa(text)
const image = (file = 'earth.jpg', args = 'inline=1') => `${ESC}]1337;File=${args};name=${name(file)}:${PNG}${BEL}`

const STORED = {
  ok: true,
  path: '/cache/abc.png',
  named: '/cache/named/abc/earth.jpg',
  format: 'PNG',
  width: 800,
  height: 400,
  frames: 1,
  bytes: 70,
}

type Run = { argv: readonly string[]; init?: { stdin?: string } }
type Helper = {
  calls: string[][]
  opened: string[][]
  fail?: (command: string) => string | undefined
}

/** Answer every process the plugin runs: render.py's commands, and `open`. */
function world(on: any, helper: Partial<Helper> = {}): Helper {
  const state: Helper = { calls: [], opened: [], ...helper }

  on('process.run', (_$: unknown, e: Run) => {
    const [program, , command = '', ...rest] = e.argv

    if (program === 'open' || program === 'xdg-open') {
      state.opened.push([...e.argv])
      return { value: { exitCode: 0, stdout: '', stderr: '' } }
    }

    state.calls.push([command, ...rest])
    const failure = state.fail?.(command)

    if (failure !== undefined) {
      return { value: { exitCode: 1, stdout: JSON.stringify({ ok: false, error: failure }), stderr: '' } }
    }

    const value = (key: string) => rest[rest.indexOf(key) + 1]
    let reply: unknown

    if (command === 'inspect') {
      reply = STORED
    } else if (command === 'cell') {
      reply = { ok: true, width: 8, height: 16 }
    } else if (command === 'png') {
      reply = { ok: true, path: '/cache/abc.box.png' }
    } else if (command === 'scan') {
      reply = { ok: true, images: [{ ...STORED, args: 'inline=1' }, { ...STORED, args: 'inline=1' }] }
    } else if (command === 'cells') {
      const count = Number(value('--columns')) * Number(value('--rows'))
      const words = new Uint32Array(count * 3)

      for (let cell = 0; cell < count; cell++) {
        words.set([0x2588, 0xff8800, 0x01000000], cell * 3)
      }

      reply = { ok: true, cells: new Uint8Array(words.buffer).toBase64() }
    }

    return { value: { exitCode: 0, stdout: `${JSON.stringify(reply)}\n`, stderr: '' } }
  })

  return state
}

/** Stands in for the engine's own row, and keeps the output it was asked to draw. */
function engineRow(on: any) {
  const drawn: unknown[] = []

  on('ui.render', (_$: unknown, e: { component: string; props: { output?: unknown; isExpanded?: boolean } }) => {
    drawn.push(e.component === 'ToolGroup' ? e.props.isExpanded : e.props.output)
    return { type: 'Text', props: {}, children: ['engine row'] }
  })

  return drawn
}

const VIEWPORT = { columns: 100, rows: 40 }
const bash = (stdout: string, extra: Record<string, unknown> = {}) => ({
  tool_use_id: 'row-1',
  tool: 'Bash',
  output: { stdout, stderr: '', ...extra },
  isErrored: false,
})
const mountRow = ($: any, props: unknown, surface = 'terminal') =>
  $.ui.mount({ plugin: 'inline-images', surface, component: 'ToolResult', props, requestId: 'row-1', viewport: VIEWPORT })

describe('a Bash result that printed an image', () => {
  test('draws the picture, a click layer and a caption link, and hides the data', async ($, on) => {
    mock.env(on, { TERM_PROGRAM: 'Apple_Terminal' })
    world(on)
    const rows = engineRow(on)
    const ui = await mountRow($, bash(`${image()}\n`))

    expect(await ui.find({ type: 'Raster' })).toBeDefined()
    expect((await ui.find({ type: 'Client' }))?.key).toBe('inline-click-row-1-0')

    const link = await ui.find({ type: 'Link' })
    expect(link?.props.href).toBe('file:///cache/named/abc/earth.jpg')
    expect(link?.text).toContain('earth.jpg')
    expect(rows).toEqual([{ stdout: '(inline image)', stderr: '' }])
  })

  test('a click opens the stored copy, never a path the click layer sends', async ($, on) => {
    mock.env(on, { TERM_PROGRAM: 'Apple_Terminal' })
    const helper = world(on)
    engineRow(on)
    const ui = await mountRow($, bash(image()))

    await ui.post({ open: '/etc/passwd' })
    expect(helper.opened).toEqual([])

    await ui.pointer({ type: 'down', x: 2, y: 1, button: 'left' })
    await ui.pointer({ type: 'up', x: 2, y: 1, button: 'left' })
    expect(helper.opened).toEqual([['open', '/cache/named/abc/earth.jpg']])
  })

  test('a click that ends outside the picture opens nothing', async ($, on) => {
    mock.env(on, { TERM_PROGRAM: 'Apple_Terminal' })
    const helper = world(on)
    engineRow(on)
    const ui = await mountRow($, bash(image()))

    await ui.resize({ columns: 10, rows: 5 })
    await ui.pointer({ type: 'down', x: 2, y: 1, button: 'left' })
    await ui.pointer({ type: 'up', x: 40, y: 1, button: 'left' })
    expect(helper.opened).toEqual([])
  })

  test('a helper failure is tried once more, then shown as a dim line', async ($, on) => {
    mock.env(on, { TERM_PROGRAM: 'Apple_Terminal' })
    const helper = world(on, { fail: command => (command === 'inspect' ? 'no image decoder' : undefined) })
    engineRow(on)
    const ui = await mountRow($, bash(image()))

    expect((await ui.find({ type: 'Text', text: /no preview: no image decoder/ }))?.text).toContain('earth.jpg')
    expect(helper.calls.filter(([command]) => command === 'inspect')).toHaveLength(2)
  })

  test('an errored row is left alone', async ($, on) => {
    const helper = world(on)
    const rows = engineRow(on)
    await mountRow($, { ...bash(image()), isErrored: true })

    expect(helper.calls).toEqual([])
    expect(rows[0]).toEqual({ stdout: image(), stderr: '' })
  })

  test('a download (inline=0) is not drawn and keeps a note', async ($, on) => {
    const helper = world(on)
    const rows = engineRow(on)
    const ui = await mountRow($, bash(image('report.png', 'inline=0')))

    expect(await ui.find({ type: 'Raster' })).toBeUndefined()
    expect(helper.calls).toEqual([])
    expect(rows[0]).toEqual({ stdout: '[file download: report.png, 70 B, not shown]', stderr: '' })
  })

  test('shell integration codes (OSC 1337 but no image) cost nothing', async ($, on) => {
    const helper = world(on)
    const rows = engineRow(on)
    const text = `${ESC}]1337;CurrentDir=/tmp${BEL}ok`
    await mountRow($, bash(text))

    expect(helper.calls).toEqual([])
    expect(rows[0]).toEqual({ stdout: text, stderr: '' })
  })
})

describe('renderers', () => {
  test('kitty draws real pixels with an Image', async ($, on) => {
    mock.env(on, { TERM: 'xterm-kitty' })
    world(on)
    engineRow(on)
    const ui = await mountRow($, bash(image()))

    expect((await ui.find({ type: 'Image' }))?.props.source).toEqual({ file: '/cache/abc.box.png', format: 'png' })
    expect(await ui.find({ type: 'Raster' })).toBeUndefined()
  })

  test('renderer cells draws cells even in kitty', { options: { renderer: 'cells' } }, async ($, on) => {
    mock.env(on, { TERM: 'xterm-kitty' })
    world(on)
    engineRow(on)
    const ui = await mountRow($, bash(image()))

    expect(await ui.find({ type: 'Raster' })).toBeDefined()
  })

  test('iTerm2 cells carry the overlay marker', async ($, on) => {
    mock.env(on, { TERM_PROGRAM: 'iTerm.app', ITERM_SESSION_ID: 'w0t0p0:ABC' })
    const helper = world(on)
    engineRow(on)
    await mountRow($, bash(image()))

    expect(helper.calls.find(([command]) => command === 'cells')).toContain('--marker')
  })

  test('other terminals get no marker', async ($, on) => {
    mock.env(on, { TERM_PROGRAM: 'Apple_Terminal' })
    const helper = world(on)
    engineRow(on)
    await mountRow($, bash(image()))

    expect(helper.calls.find(([command]) => command === 'cells')).not.toContain('--marker')
  })
})

describe('limits', () => {
  test('one row draws at most 16 pictures and says how many it left out', async ($, on) => {
    mock.env(on, { TERM_PROGRAM: 'Apple_Terminal' })
    const helper = world(on)
    engineRow(on)
    const many = Array.from({ length: 20 }, (_, index) => image(`${index}.png`)).join('\n')
    const ui = await mountRow($, bash(many))

    expect(await ui.findAll({ type: 'Raster' })).toHaveLength(16)
    expect((await ui.find({ type: 'Text', text: /4 more inline images not shown/ })) !== undefined).toBe(true)
    expect(helper.calls.filter(([command]) => command === 'inspect').length).toBeLessThanOrEqual(16)
  })
})

describe('large outputs', () => {
  test('a cut output is read from its saved file, and the row counts every picture', async ($, on) => {
    mock.env(on, { TERM_PROGRAM: 'Apple_Terminal' })
    const helper = world(on)
    const rows = engineRow(on)
    const cut = `${ESC}]1337;MultipartFile=inline=1${BEL}${ESC}]1337;FilePart=AAAA`
    const ui = await mountRow($, bash(cut, { persistedOutputPath: '/tmp/out.txt' }))

    expect(await ui.findAll({ type: 'Raster' })).toHaveLength(2)
    expect(helper.calls.filter(([command]) => command === 'scan')).toEqual([['scan', '/tmp/out.txt']])
    expect((rows[0] as { stdout: string }).stdout).toBe('(2 inline images)')
  })
})

describe('the result line of a tool row', () => {
  test('a cut output counts the pictures of its saved file', async ($, on) => {
    world(on)
    const rows = engineRow(on)
    const cut = `${ESC}]1337;MultipartFile=inline=1${BEL}${ESC}]1337;FilePart=AAAA`
    const props = {
      tool_use_id: 'row-1',
      tool: 'Bash',
      input: {},
      isRunning: false,
      isErrored: false,
      isInterrupted: false,
      output: { stdout: cut, stderr: '', persistedOutputPath: '/tmp/out.txt' },
    }
    await $.ui.mount({ plugin: 'inline-images', surface: 'terminal', component: 'ToolUse', props, viewport: VIEWPORT })

    expect((rows[0] as { stdout: string }).stdout).toBe('(2 inline images)')
  })
})

describe('delivered files', () => {
  test('an image that SendUserFile delivered is drawn from a copy of the file', async ($, on) => {
    mock.env(on, { TERM_PROGRAM: 'Apple_Terminal' })
    const helper = world(on)
    engineRow(on)
    const props = {
      tool_use_id: 'row-1',
      tool: 'SendUserFile',
      isErrored: false,
      output: { attachments: [{ path: '/Users/me/photo.jpg', size: 9, isImage: true, media_type: 'image/jpeg' }] },
    }
    const ui = await mountRow($, props)

    expect(await ui.find({ type: 'Raster' })).toBeDefined()
    expect(helper.calls.find(([command]) => command === 'inspect')).toEqual([
      'inspect',
      '--file',
      '/Users/me/photo.jpg',
      '--name',
      'photo.jpg',
    ])
  })
})

describe('surfaces', () => {
  test('a desktop row is not drawn by the mod, but its data is still hidden', async ($, on) => {
    const helper = world(on)
    const rows = engineRow(on)
    await mountRow($, bash(image()), 'desktop')

    expect(helper.calls).toEqual([])
    expect((rows[0] as { stdout: string }).stdout).not.toContain(PNG)
  })

  test('a folded group with a picture is unfolded on the terminal only', async ($, on) => {
    mock.env(on, { TERM_PROGRAM: 'Apple_Terminal' })
    world(on)
    const rows = engineRow(on)
    const group = {
      calls: [{ tool_use_id: 'c1', tool: 'Bash', input: {}, isRunning: false, isErrored: false, isInterrupted: false, output: { stdout: image() } }],
      isActive: false,
      isExpanded: false,
    }

    await $.ui.mount({ plugin: 'inline-images', surface: 'desktop', component: 'ToolGroup', props: group, viewport: VIEWPORT })
    await $.ui.mount({ plugin: 'inline-images', surface: 'terminal', component: 'ToolGroup', props: group, viewport: VIEWPORT })
    expect(rows).toEqual([false, true])
  })
})

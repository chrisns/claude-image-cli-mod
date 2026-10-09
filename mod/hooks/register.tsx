import type { EngineInterface, Register, RenderInput } from 'claude-code'

import { DEFAULT_CELL, fit, type Cell } from './layout.ts'
import { stripBlocks } from './model.ts'
import { baseName, extract, hasImageSequence, parseArguments, type InlineHead } from './osc1337.ts'
import {
  caption,
  deliveredImages,
  drawsPixels,
  fileUrl,
  imageKey,
  Lru,
  outputText,
  overlayMessages,
  parseReply,
  readOptions,
  savedPath,
  wantsOverlay,
  withoutImages,
  withText,
  type Inspected,
  type Options,
  type Pic,
} from './preview.ts'

type Settings = Parameters<Register>[1]

// A tool output that may hold images: its text, and where the whole of it is
// saved when the tool cut the text short.
type TextSource = { text: string; saved?: string }
// Or an image file a tool delivered to the person (SendUserFile, SendUserMessage).
type FileSource = { file: string }
type Source = TextSource | FileSource

type Scanned = { images: (Inspected & { args: string })[] }

// The most pictures one row draws, and how many it decodes at once: a loop of
// imgcat over a folder must not start hundreds of processes.
const MAX_IMAGES = 16
const PARALLEL = 4

// What the helper decoded, by image. The engine keeps a drawing per input, but
// a resize or a reload draws again, so the work is kept here, bounded.
const inspected = new Lru<string, Promise<Inspected>>(200)
// The images of a saved output, by its path: reading it again on each redraw
// would read up to 512 MB.
const scans = new Lru<string, Promise<Scanned>>(32)
// One terminal cell in pixels, by the width of the screen: it changes with the font.
const cellSizes = new Lru<number, Cell>(8)

// The iTerm2 overlay (bin/iterm_overlay.py): `live` once it watches the screen.
let overlay: 'off' | 'starting' | 'live' | 'failed' = 'off'

// What a click on a preview opens, by the drawing and the click layer's key:
// only files this mod drew, looked up here and never taken from the message.
const clickTargets = new Lru<string, string>(500)
const clickKey = (requestId: string, element: string) => `${requestId}\u0000${element}`

/** The sources of images in one tool result: delivered image files, and text with image sequences. */
function sourcesOf(output: unknown): Source[] {
  const files: Source[] = deliveredImages(output).map(file => ({ file }))
  const text = outputText(output)

  return text !== undefined && hasImageSequence(text) ? [...files, { text, saved: savedPath(output) }] : files
}

/** The output with its image data cut out, as the row shows it; `shown` counts what is drawn. */
function strippedOutput(output: unknown, shown?: number): unknown {
  const text = outputText(output)

  if (text === undefined || !hasImageSequence(text)) {
    return output
  }

  // A cut output has no whole image left in its text: the count comes from the scan.
  const count = shown ?? (savedPath(output) === undefined ? undefined : 1)

  return withText(output, withoutImages(text, count))
}

/** Remember a promise under a key, and forget it if it fails, so the next draw tries again. */
function remember<V>(cache: Lru<string, Promise<V>>, key: string, start: () => Promise<V>): Promise<V> {
  let found = cache.get(key)

  if (found === undefined) {
    found = start()
    cache.set(key, found)
    found.catch(() => cache.delete(key))
  }

  return found
}

/**
 * The previews for every image in these sources, one tree each, and how many
 * pictures there are.
 *
 * `e` is the row being drawn: its surface picks the elements, its viewport the size.
 */
async function previews($: EngineInterface, e: RenderInput, settings: Settings, sources: Source[]) {
  if (sources.length === 0 || e.surface !== 'terminal') {
    return { nodes: [], shown: 0 }
  }

  const options: Options = readOptions(settings, $.plugin.root)

  const run = async <T,>(args: string[], stdin?: string): Promise<T> => {
    const ran = await $.process.run([options.python, options.helper, ...args], { stdin, timeoutMs: 60000 })

    return parseReply<T>(ran.stdout, ran.stderr, ran.exitCode)
  }

  const found: Pic[] = []

  for (const source of sources) {
    if ('file' in source) {
      // A delivered file. It is copied into the cache as it is now: the file may
      // change later, so the key holds the row as well as the path.
      const path = source.file
      const key = `file:${e.requestId}:${path}`
      const head: InlineHead = {
        name: path,
        width: { unit: 'auto' },
        height: { unit: 'auto' },
        isAspectPreserved: true,
        isInline: true,
      }

      found.push({
        head,
        inspect: () => remember(inspected, key, () => run<Inspected>(['inspect', '--file', path, '--name', baseName(path)])),
        forget: () => inspected.delete(key),
      })
      continue
    }

    const { text, saved } = source

    if (saved !== undefined) {
      // The tool cut the text short: the whole output is in a file.
      try {
        const scanned = await remember(scans, saved, () => run<Scanned>(['scan', saved]))

        for (const { args, ...stored } of scanned.images) {
          const head = parseArguments(args)

          if (head.isInline) {
            found.push({ head, inspect: async () => stored, forget: () => scans.delete(saved) })
          }
        }

        continue
      } catch {
        // The file is gone: draw what the text still holds.
      }
    }

    for (const image of extract(text).images) {
      if (!image.isInline) {
        continue
      }

      const key = imageKey(image.base64)
      const name = image.name === '' ? [] : ['--name', image.name]

      found.push({
        head: image,
        inspect: () => remember(inspected, key, () => run<Inspected>(['inspect', ...name], image.base64)),
        forget: () => inspected.delete(key),
      })
    }
  }

  if (found.length === 0) {
    return { nodes: [], shown: 0 }
  }

  // One cell in pixels, as the terminal reports its size. Without it a cell is guessed.
  const columns = e.viewport?.columns ?? 80
  let cell = cellSizes.get(columns)

  if (cell === undefined) {
    cell = await run<{ width: number | null; height: number | null }>(['cell'])
      .then((size): Cell => (size.width && size.height ? { width: size.width, height: size.height } : DEFAULT_CELL))
      .catch(() => DEFAULT_CELL)
    cellSizes.set(columns, cell)
  }

  const program = await $.env.get('TERM_PROGRAM')

  // Markers whenever the overlay is wanted here and has not failed: a row drawn while
  // it starts (as after a reload) is not always drawn again once it is ready.
  const usesOverlay =
    overlay !== 'failed' && (await $.env.get('ITERM_SESSION_ID')) !== undefined && wantsOverlay(options.renderer, program)
  const isPixelTerminal = drawsPixels(program, await $.env.get('TERM'), await $.env.get('KITTY_WINDOW_ID'))
  const isPixels = options.renderer === 'image' || (options.renderer === 'auto' && isPixelTerminal)
  const { Box, Text, Raster, Image, Client, Link } = $.ui.resolve(e)

  const picture = async (pic: Pic, index: number, isRetry = false): Promise<ReturnType<typeof Box>> => {
    const { head } = pic
    let stored: Inspected | undefined

    try {
      stored = await pic.inspect()

      // A click opens the copy in the cache: its extension is that of the format
      // the helper decoded, so the system never runs a file that only looks like
      // an image. It carries the sender's file name, for the viewer's title bar.
      const opens = stored.named ?? stored.path
      // Unique per row and picture: two layers under one key share one instance.
      const layer = `inline-click-${e.requestId}-${index}`
      clickTargets.set(clickKey(e.requestId, layer), opens)

      const box = fit(stored, head, {
        viewportColumns: columns,
        maxColumns: options.maxColumns,
        // The viewport's height is no cap: it is what the window was when the row was
        // first drawn, and a change of height alone does not draw the row again.
        maxRows: options.maxRows,
        cell: cell ?? DEFAULT_CELL,
      })
      const size = [
        '--columns',
        String(box.columns),
        '--rows',
        String(box.rows),
        '--cell-width',
        String(cell?.width ?? DEFAULT_CELL.width),
        '--cell-height',
        String(cell?.height ?? DEFAULT_CELL.height),
        ...(head.isAspectPreserved ? [] : ['--stretch']),
      ]

      const drawn = isPixels ? (
        <Image
          key={`inline-image-${index}`}
          source={{ file: (await run<{ path: string }>(['png', stored.path, ...size])).path, format: 'png' }}
          columns={box.columns}
          rows={box.rows}
          alt={caption(head, stored)}
        />
      ) : (
        <Raster
          key={`inline-image-${index}`}
          columns={box.columns}
          rows={box.rows}
          cells={
            (
              await run<{ cells: string }>([
                'cells',
                stored.path,
                ...size,
                '--palette',
                String(options.palette),
                ...(usesOverlay ? ['--marker'] : []),
              ])
            ).cells
          }
        />
      )

      return (
        <Box key={`inline-box-${index}`} flexDirection="column" paddingLeft={2}>
          <Box width={box.columns} height={box.rows}>
            {drawn}
            {/* A click on the picture opens the file in the system's own viewer. */}
            <Box position="absolute" top={0} left={0} width={box.columns} height={box.rows}>
              <Client key={layer} module="./click.tsx" width={box.columns} height={box.rows} />
            </Box>
          </Box>
          {/* Where the terminal sends no clicks, cmd+click on the caption opens it. */}
          <Link href={fileUrl(opens)} label={caption(head, stored)} />
        </Box>
      )
    } catch (problem) {
      // What was remembered may be gone (a cache prune): read the image again, once.
      pic.forget()

      if (!isRetry) {
        return picture(pic, index, true)
      }

      const reason = problem instanceof Error ? problem.message : String(problem)

      return (
        <Box key={`inline-box-${index}`} paddingLeft={2}>
          <Text dimColor>{`[inline image: ${caption(head, stored)}, no preview: ${reason}]`}</Text>
        </Box>
      )
    }
  }

  // At most PARALLEL helpers at a time.
  const shown = found.slice(0, MAX_IMAGES)
  const nodes: ReturnType<typeof Box>[] = new Array(shown.length)
  let next = 0

  const worker = async () => {
    while (next < shown.length) {
      const index = next++
      const pic = shown[index]

      if (pic !== undefined) {
        nodes[index] = await picture(pic, index)
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(PARALLEL, shown.length) }, worker))

  if (found.length > MAX_IMAGES) {
    nodes.push(
      <Box key="inline-more" paddingLeft={2}>
        <Text dimColor>{`[${found.length - MAX_IMAGES} more inline images not shown]`}</Text>
      </Box>,
    )
  }

  return { nodes, shown: found.length }
}

export const register: Register = (on, settings) => {
  const hidesFromModel = settings.hide_from_model !== false

  // In iTerm2, start the overlay that draws real pixels over the cell previews.
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    const options = readOptions(settings, $.plugin.root)
    const sessionId = await $.env.get('ITERM_SESSION_ID')

    if (overlay !== 'off' || sessionId === undefined || !wantsOverlay(options.renderer, await $.env.get('TERM_PROGRAM'))) {
      return started
    }

    overlay = 'starting'

    // The loop is the child's life: it runs on after this hook returns and ends with the module.
    void (async () => {
      let buffer = ''
      let reason = ''
      let lastError = ''

      try {
        for await (const piece of $.process.spawn({ argv: [options.python, options.overlay, '--session', sessionId] })) {
          if (piece.stream === 'stderr') {
            lastError = piece.text.trim().split('\n').pop() ?? lastError
            continue
          }

          const { messages, rest } = overlayMessages(buffer + piece.text)
          buffer = rest

          for (const message of messages) {
            if (message.ready === true) {
              overlay = 'live'
              // Rows drawn before now have no marker: draw them again.
              $.ui.invalidate('ui.render')
            } else if (message.ok === false && message.error !== undefined) {
              reason = message.error
            }
          }
        }
      } catch (problem) {
        reason = problem instanceof Error ? problem.message : String(problem)
      }

      const wasLive = overlay === 'live'
      overlay = 'failed'
      // Draw the rows again without markers.
      $.ui.invalidate('ui.render')

      const why = reason || lastError || 'the overlay stopped'
      const message = `inline-images: ${wasLive ? 'the iTerm2 overlay stopped' : 'block previews only, no real pixels in iTerm2'}: ${why}`

      // Asked for by name, the person hears why it is off. In auto mode a missing
      // setup (no iterm2 module, no Python API) is the normal case: debug log only.
      if (options.renderer === 'iterm' || wasLive) {
        $.ui.toast(message)
      } else {
        $.ui.log(message, { to: 'debug' })
      }
    })()

    return started
  })

  // What the model reads. The transcript keeps the whole output, so the preview
  // can be drawn again after a resume.
  on('session.append', { door: 'tool-result' }, ($, e, next) => {
    if (!hidesFromModel) {
      return next(e)
    }

    const content = stripBlocks(e.message.content)

    return next(content === e.message.content ? e : { ...e, message: { ...e.message, content: [...content] } })
  }).catch(($, e, next) => next(e))

  // A click on a preview: open the file in the system's own viewer (Preview on macOS).
  on('ui.message', async ($, e, next) => {
    const { open: asked } = (e.data ?? {}) as { open?: unknown }
    const open = clickTargets.get(clickKey(e.requestId, e.element))

    if (asked === true && open !== undefined) {
      const opened = await $.process.run(['open', open]).catch(() => undefined)

      if (opened === undefined || opened.exitCode !== 0) {
        await $.process.run(['xdg-open', open]).catch(() => undefined)
      }
    }

    return next(e)
  })

  // A call drawn on its own row: the pictures go under its result. One hook for
  // every kind of source, so the click layers of one row never share a key.
  on('ui.render', { component: 'ToolResult' }, async ($, e, next) => {
    const sources = e.props.isErrored ? [] : sourcesOf(e.props.output)

    if (sources.length === 0) {
      return next(e)
    }

    const { nodes, shown } = await previews($, e, settings, sources)
    const output = strippedOutput(e.props.output, e.surface === 'terminal' ? shown : undefined)
    const base = await next({ ...e, props: { ...e.props, output } })

    if (nodes.length === 0) {
      return base
    }

    const { Box } = $.ui.resolve(e)

    return (
      <Box flexDirection="column">
        {base}
        {nodes}
      </Box>
    )
  }).catch(($, e, next) =>
    // A failed preview must not let megabytes of base64 through as text.
    next.called ? next(e) : next({ ...e, props: { ...e.props, output: strippedOutput(e.props.output) } }),
  )

  // A call drawn in a group row, or the result line of its own row: no image
  // data as text, however the row is drawn.
  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    if (e.props.isErrored) {
      return next(e)
    }

    // A cut output's text holds no whole image: count the pictures of the saved file.
    const saved = savedPath(e.props.output)
    const text = outputText(e.props.output)
    let shown: number | undefined

    if (saved !== undefined && text !== undefined && hasImageSequence(text)) {
      const options = readOptions(settings, $.plugin.root)
      const scanned = await remember(scans, saved, async () => {
        const ran = await $.process.run([options.python, options.helper, 'scan', saved], { timeoutMs: 60000 })

        return parseReply<Scanned>(ran.stdout, ran.stderr, ran.exitCode)
      }).catch(() => undefined)

      shown = scanned?.images.filter(image => parseArguments(image.args).isInline).length
    }

    const output = strippedOutput(e.props.output, shown)

    return output === e.props.output ? next(e) : next({ ...e, props: { ...e.props, output } })
  }).catch(($, e, next) => (next.called ? next(e) : next({ ...e, props: { ...e.props, output: strippedOutput(e.props.output) } })))

  // A run of calls folds into one line, which would hide the pictures. On the
  // terminal, unfold a group that holds one and draw the pictures under it.
  on('ui.render', { component: 'ToolGroup' }, async ($, e, next) => {
    const sources = e.surface !== 'terminal' ? [] : e.props.calls.flatMap(call => (call.isErrored ? [] : sourcesOf(call.output)))

    if (sources.length === 0) {
      return next(e)
    }

    const { nodes } = await previews($, e, settings, sources)

    if (nodes.length === 0) {
      return next(e)
    }

    const base = await next({ ...e, props: { ...e.props, isExpanded: true } })
    const { Box } = $.ui.resolve(e)

    return (
      <Box flexDirection="column">
        {base}
        {nodes}
      </Box>
    )
  }).catch(($, e, next) => next(e))
}

import type { EngineInterface, Register, RenderInput } from 'claude-code'

import { DEFAULT_CELL, fit, type Cell } from './layout.ts'
import { limitDiagrams, MERMAID_GUIDE, mermaidKind, splitMermaid } from './mermaid.ts'
import { stripBlocks } from './model.ts'
import { baseName, extract, hasImageSequence, parseArguments, type InlineHead } from './osc1337.ts'
import {
  caption,
  cellRuns,
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
// Or a Mermaid diagram from one of Claude's replies.
type MermaidSource = { mermaid: string }
type Source = TextSource | FileSource | MermaidSource

type Scanned = { images: (Inspected & { args: string })[] }

// The most pictures one row draws, and how many it decodes at once: a loop of
// imgcat over a folder must not start hundreds of processes.
const MAX_IMAGES = 16
const PARALLEL = 4
// A draw that took longer than this is drawn once more when it is ready.
const SLOW_DRAW_MS = 800
// render.py gives mmdc 90 seconds, and as long again for the SVG it reads
// when the picture looks like Mermaid's error picture.
const MERMAID_TIMEOUT_MS = 200000

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

// Whether this machine can draw Mermaid diagrams (mmdc is installed): Claude
// is told it can draw them only then.
let canDrawMermaid = false

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

/** The cache key of a diagram's picture. */
const diagramKey = (theme: string, source: string) => `mermaid:${theme}:${imageKey(source)}`

// Diagrams whose picture is ready: drawn at once. Any other is rendered in the
// background while its reply shows a short line, then drawn: a render hook that
// waits seconds for a browser leaves its reply unpainted.
const readyDiagrams = new Lru<string, true>(200)
// Why a diagram would not draw. The same source fails the same way, and each
// try starts a browser: it is tried once, and every redraw shows this reason.
const diagramErrors = new Lru<string, string>(200)

// The rows whose slow draw asked to be drawn again, by what they draw. One more
// draw each: a draw that is slow every time (a helper that fails slowly) would
// otherwise ask again at every draw, for ever.
const redrawn = new Lru<string, true>(500)

const reasonOf = (problem: unknown) => (problem instanceof Error ? problem.message : String(problem))

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

/** A diagram's picture, rendered once; a diagram that failed fails again at once, with its reason. */
function drawDiagram($: EngineInterface, options: Options, source: string): Promise<Inspected> {
  const key = diagramKey(options.mermaidTheme, source)
  const failed = diagramErrors.get(key)

  if (failed !== undefined) {
    return Promise.reject(new Error(failed))
  }

  return remember(inspected, key, async () => {
    try {
      const ran = await $.process.run([options.python, options.helper, 'mermaid', '--theme', options.mermaidTheme], {
        stdin: source,
        timeoutMs: MERMAID_TIMEOUT_MS,
      })

      return parseReply<Inspected>(ran.stdout, ran.stderr, ran.exitCode)
    } catch (problem) {
      diagramErrors.set(key, reasonOf(problem))
      throw problem
    }
  })
}

/** The images of a saved output, read once until a picture of it fails. */
function scanSaved($: EngineInterface, options: Options, saved: string): Promise<Scanned> {
  return remember(scans, saved, async () => {
    const ran = await $.process.run([options.python, options.helper, 'scan', saved], { timeoutMs: 60000 })

    return parseReply<Scanned>(ran.stdout, ran.stderr, ran.exitCode)
  })
}

/**
 * The previews for every image in these sources, one tree each, and how many
 * pictures there are.
 *
 * `e` is the row being drawn: its surface picks the elements, its viewport the size.
 */
async function previews($: EngineInterface, e: RenderInput, settings: Settings, sources: Source[], asText = false) {
  if (sources.length === 0 || e.surface !== 'terminal') {
    return { nodes: [], shown: 0 }
  }

  const options: Options = readOptions(settings, $.plugin.root)
  const startedAt = await $.clock.now()

  const run = async <T,>(args: string[], stdin?: string): Promise<T> => {
    const ran = await $.process.run([options.python, options.helper, ...args], { stdin, timeoutMs: 60000 })

    return parseReply<T>(ran.stdout, ran.stderr, ran.exitCode)
  }

  const found: Pic[] = []

  for (const source of sources) {
    if ('mermaid' in source) {
      const diagram = source.mermaid
      const key = diagramKey(options.mermaidTheme, diagram)
      const kind = mermaidKind(diagram)

      found.push({
        key,
        head: { name: '', width: { unit: 'auto' }, height: { unit: 'auto' }, isAspectPreserved: true, isInline: true },
        title: `${kind} diagram`,
        fallback: `\`\`\`mermaid\n${diagram}\n\`\`\``,
        inspect: () => drawDiagram($, options, diagram),
        forget: () => inspected.delete(key),
      })
      continue
    }

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
        key,
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
        const scanned = await scanSaved($, options, saved)

        scanned.images.forEach(({ args }, index) => {
          const head = parseArguments(args)

          if (head.isInline) {
            found.push({
              key: `scan:${saved}:${index}`,
              head,
              // Read through the scan each time: after a forget (its stored copy
              // was pruned), the retry scans the file again.
              inspect: async () => {
                const image = (await scanSaved($, options, saved)).images[index]

                if (image === undefined) {
                  throw new Error('the saved output changed')
                }

                return image
              },
              forget: () => scans.delete(saved),
            })
          }
        })

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
        key,
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
  const { Box, Text, Raster, Image, Client, Link, Markdown } = $.ui.resolve(e)
  const label = (pic: Pic, stored: Inspected | undefined) =>
    pic.title === undefined ? caption(pic.head, stored) : stored === undefined ? pic.title : `${pic.title} \u00b7 ${stored.width}\u00d7${stored.height}`

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
        // A diagram carries small text: it may be taller than an image.
        maxRows: pic.fallback === undefined ? options.maxRows : options.diagramMaxRows,
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
          alt={label(pic, stored)}
        />
      ) : (
        await (async () => {
          const { cells } = await run<{ cells: string }>([
            'cells',
            stored.path,
            ...size,
            '--palette',
            String(options.palette),
            ...(usesOverlay ? ['--marker'] : []),
          ])

          if (!asText) {
            return <Raster key={`inline-image-${index}`} columns={box.columns} rows={box.rows} cells={cells} />
          }

          // The same cells as rows of coloured text, for a site that paints no Raster.
          return (
            <Box key={`inline-image-${index}`} flexDirection="column">
              {cellRuns(cells, box.columns, box.rows).map((runs, row) => (
                <Text key={`row-${row}`} wrap="truncate">
                  {runs.map((part, at) => (
                    <Text key={`run-${at}`} color={part.color} backgroundColor={part.backgroundColor}>
                      {part.text}
                    </Text>
                  ))}
                </Text>
              ))}
            </Box>
          )
        })()
      )

      return (
        <Box key={`inline-box-${index}`} flexDirection="column" paddingLeft={2}>
          <Box width={box.columns} height={box.rows}>
            {/* A click on the picture opens the file in the system's own viewer. The
                layer comes first: drawn after the picture, its empty region would paint
                over a picture made of text, as in a reply. */}
            <Box position="absolute" top={0} left={0} width={box.columns} height={box.rows}>
              <Client key={layer} module="./click.tsx" width={box.columns} height={box.rows} />
            </Box>
            {drawn}
          </Box>
          {/* Where the terminal sends no clicks, cmd+click on the caption opens it. */}
          <Link href={fileUrl(opens)} label={label(pic, stored)} />
        </Box>
      )
    } catch (problem) {
      // What was remembered may be gone (a cache prune): read the image again, once.
      pic.forget()

      if (!isRetry) {
        return picture(pic, index, true)
      }

      const reason = reasonOf(problem)

      if (pic.fallback !== undefined) {
        // A diagram that will not draw: its source, as the reply wrote it, and why.
        return (
          <Box key={`inline-box-${index}`} flexDirection="column" paddingLeft={2}>
            <Markdown text={pic.fallback} />
            <Text dimColor>{`[${pic.title ?? 'diagram'} not drawn: ${reason}]`}</Text>
          </Box>
        )
      }

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

  // A slow first draw (a diagram rendered, an image decoded) is laid out but not
  // always painted. Draw once more when it is ready: from the cache, so fast.
  // Once for what these pictures are, because a failure is not cached and the
  // draw that it asks for is as slow again.
  const drawing = `${e.surface}\u0000${shown.map(pic => pic.key).join('\u0000')}`

  if ((await $.clock.now()) - startedAt > SLOW_DRAW_MS && redrawn.get(drawing) === undefined) {
    redrawn.set(drawing, true)
    $.clock.after(100, () => $.ui.invalidate('ui.render'))
  }

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

  // Tell Claude that a ```mermaid block in a reply becomes a picture.
  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    const options = readOptions(settings, $.plugin.root)

    if (!options.mermaid || !canDrawMermaid || !e.surfaces.includes('terminal')) {
      return composed
    }

    return { ...composed, sections: [...composed.sections, { id: 'inline-images:mermaid', text: MERMAID_GUIDE, scope: 'session' as const }] }
  }).catch(($, e, next) => next(e))

  // Each complete ```mermaid block in a reply: drawn as a picture, in its place.
  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    const options = readOptions(settings, $.plugin.root)

    if (!options.mermaid || e.surface !== 'terminal') {
      return next(e)
    }

    // Past the most pictures a row draws, a diagram stays as its source.
    const segments = limitDiagrams(splitMermaid(e.props.text), MAX_IMAGES)
    const sources = segments.flatMap(segment => (segment.kind === 'mermaid' ? [segment.source] : []))

    if (sources.length === 0) {
      return next(e)
    }

    const { Box, Markdown, Text } = $.ui.resolve(e)
    const pending = sources.filter(source => readyDiagrams.get(diagramKey(options.mermaidTheme, source)) === undefined)

    // Render the new diagrams in the background, then draw the reply again.
    for (const source of new Set(pending)) {
      const key = diagramKey(options.mermaidTheme, source)

      // A failure is drawn too: the source and the reason, by previews.
      void drawDiagram($, options, source)
        .catch(() => undefined)
        .finally(() => {
          readyDiagrams.set(key, true)
          $.ui.invalidate('ui.render')
        })
    }

    // The ready diagrams in one call: one click layer key each, so a diagram
    // that the reply holds twice is drawn twice. A reply's row paints no
    // Raster, so the cells are drawn as coloured text.
    const ready = sources.filter(source => !pending.includes(source))
    const drawnReady = (await previews($, e, settings, ready.map(source => ({ mermaid: source })), true)).nodes
    let nextReady = 0
    const nodes = sources.map((source, index) => {
      if (pending.includes(source)) {
        return (
          <Box key={`inline-pending-${index}`} paddingLeft={2}>
            <Text dimColor>{`\u22ef drawing a ${mermaidKind(source)} diagram`}</Text>
          </Box>
        )
      }

      return drawnReady[nextReady++]
    })
    const [first, ...rest] = segments
    // The engine draws the first text (and the reply's bullet); the rest follows.
    const base = await next({ ...e, props: { ...e.props, text: first?.kind === 'text' ? first.text : '' } })
    const parts: ReturnType<typeof Box>[] = []
    let drawn = first?.kind === 'mermaid' ? 1 : 0

    if (first?.kind === 'mermaid' && nodes[0] !== undefined) {
      parts.push(nodes[0])
    }

    rest.forEach((segment, index) => {
      if (segment.kind === 'mermaid') {
        const node = nodes[drawn++]

        if (node !== undefined) {
          parts.push(node)
        }
      } else if (segment.text.trim() !== '') {
        parts.push(
          <Box key={`inline-text-${index}`} paddingLeft={2}>
            <Markdown text={segment.text} />
          </Box>,
        )
      }
    })

    return (
      <Box flexDirection="column">
        {base}
        {parts}
      </Box>
    )
  }).catch(($, e, next) => next(e))

  // At the start: can this machine draw Mermaid diagrams (only then is Claude
  // told it can draw them)? And in iTerm2, start the overlay that draws real
  // pixels over the cell previews.
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    const options = readOptions(settings, $.plugin.root)

    if (options.mermaid) {
      // A failed check only means no diagrams: it must not stop the overlay below.
      const found = await $.process
        .run([options.python, options.helper, 'mermaid-check'], { timeoutMs: 15000 })
        .then(ran => parseReply<{ mmdc: string | null }>(ran.stdout, ran.stderr, ran.exitCode))
        .catch(() => undefined)

      canDrawMermaid = typeof found?.mmdc === 'string'

      if (!canDrawMermaid) {
        $.ui.log('inline-images: no mmdc, so Mermaid diagrams stay as text (npm install -g @mermaid-js/mermaid-cli)', { to: 'debug' })
      }
    }

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
      // The helper picks the opener for the platform (on Linux `open` can be
      // openvt), and opens only an image in the cache.
      const options = readOptions(settings, $.plugin.root)

      await $.process
        .run([options.python, options.helper, 'open', open], { timeoutMs: 15000 })
        .then(ran => parseReply<{ ok: true }>(ran.stdout, ran.stderr, ran.exitCode))
        .catch(problem => $.ui.log(`inline-images: cannot open ${open}: ${reasonOf(problem)}`, { to: 'debug' }))
    }

    return next(e)
  }).catch(($, e, next) => next(e))

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
      const scanned = await scanSaved($, readOptions(settings, $.plugin.root), saved).catch(() => undefined)

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

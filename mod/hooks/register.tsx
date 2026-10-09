import type { EngineInterface, Register, RenderInput } from 'claude-code'

import { DEFAULT_CELL, fit, type Cell } from './layout.ts'
import { stripBlocks } from './model.ts'
import { extract, parseArguments, type InlineHead } from './osc1337.ts'
import {
  caption,
  deliveredImages,
  fileUrl,
  drawsPixels,
  imageKey,
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
type Source = TextSource | { file: string }

// Decoding runs once per image. The engine keeps a drawing per input, but a
// resize draws again, so the decoded image is kept here.
const inspected = new Map<string, Promise<Inspected>>()

// The iTerm2 overlay (bin/iterm_overlay.py): `live` once it watches the screen.
// Cells carry its marker only then, so a terminal without it shows no marker.
let overlay: 'off' | 'starting' | 'live' | 'failed' = 'off'

// The files a click on a preview may open: only ones this mod drew.
const openable = new Set<string>()

const hasImage = (output: unknown) => outputText(output)?.includes(']1337;') === true

const sourceOf = (output: unknown): TextSource[] => {
  const text = outputText(output)

  return text !== undefined && text.includes(']1337;') ? [{ text, saved: savedPath(output) }] : []
}

/**
 * The previews for every image in these outputs, one tree each.
 *
 * `e` is the row being drawn: its surface picks the elements, its viewport the size.
 */
async function previews($: EngineInterface, e: RenderInput, settings: Settings, sources: Source[]) {
  const options: Options = readOptions(settings, $.plugin.root)

  if (sources.length === 0 || e.surface !== 'terminal') {
    return []
  }

  const run = async <T,>(args: string[], stdin?: string): Promise<T> => {
    const ran = await $.process.run([options.python, options.helper, ...args], { stdin, timeoutMs: 60000 })

    return parseReply<T>(ran.stdout, ran.stderr, ran.exitCode)
  }

  const found: Pic[] = []

  for (const source of sources) {
    if ('file' in source) {
      // A delivered file: the picture is the file itself.
      const path = source.file
      const head: InlineHead = {
        name: path,
        width: { unit: 'auto' },
        height: { unit: 'auto' },
        isAspectPreserved: true,
        isInline: true,
      }

      found.push({
        head,
        file: path,
        inspect: () => {
          let stored = inspected.get(path)

          if (stored === undefined) {
            stored = run<Inspected>(['inspect', '--file', path])
            inspected.set(path, stored)
            stored.catch(() => inspected.delete(path))
          }

          return stored
        },
      })
      continue
    }

    const { text, saved } = source

    if (saved !== undefined) {
      // The tool cut the text short: the whole output is in a file.
      try {
        const scanned = await run<{ images: (Inspected & { args: string })[] }>(['scan', saved])

        for (const { args, ...stored } of scanned.images) {
          const head = parseArguments(args)

          if (head.isInline) {
            found.push({ head, inspect: async () => stored })
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

      found.push({
        head: image,
        inspect: () => {
          let stored = inspected.get(key)

          if (stored === undefined) {
            stored = run<Inspected>(['inspect', ...(image.name === '' ? [] : ['--name', image.name])], image.base64)
            inspected.set(key, stored)
            // A failure must not stick: the next draw tries again.
            stored.catch(() => inspected.delete(key))
          }

          return stored
        },
      })
    }
  }

  // One cell in pixels, as the terminal reports its size. Without it a cell is guessed.
  const cell = await run<{ width: number | null; height: number | null }>(['cell'])
    .then((size): Cell => (size.width && size.height ? { width: size.width, height: size.height } : DEFAULT_CELL))
    .catch(() => DEFAULT_CELL)

  const isPixelTerminal = drawsPixels(
    await $.env.get('TERM_PROGRAM'),
    await $.env.get('TERM'),
    await $.env.get('KITTY_WINDOW_ID'),
  )
  const { Box, Text, Raster, Image, Client, Link } = $.ui.resolve(e)

  const draw = async ({ head, inspect, file }: Pic, index: number) => {
    let stored: Inspected | undefined

    try {
      stored = await inspect()
      const opens = file ?? stored.named ?? stored.path
      openable.add(opens)

      const box = fit(stored, head, {
        viewportColumns: e.viewport?.columns ?? 80,
        maxColumns: options.maxColumns,
        // The viewport's height is no cap: it is what the window was when the row was
        // first drawn, and a change of height alone does not draw the row again.
        maxRows: options.maxRows,
        cell,
      })
      const size = [
        '--columns',
        String(box.columns),
        '--rows',
        String(box.rows),
        '--cell-width',
        String(cell.width),
        '--cell-height',
        String(cell.height),
        ...(head.isAspectPreserved ? [] : ['--stretch']),
      ]
      const isPixels = options.renderer === 'image' || (options.renderer === 'auto' && isPixelTerminal)

      const picture = isPixels ? (
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
                ...(overlay === 'live' ? ['--marker'] : []),
              ])
            ).cells
          }
        />
      )

      return (
        <Box key={`inline-box-${index}`} flexDirection="column" paddingLeft={2}>
          <Box width={box.columns} height={box.rows}>
            {picture}
            {/* A click on the picture opens the file in the system's own viewer. */}
            <Box position="absolute" top={0} left={0} width={box.columns} height={box.rows}>
              <Client
                key={`inline-click-${index}`}
                module="./click.tsx"
                props={{ path: opens }}
                width={box.columns}
                height={box.rows}
              />
            </Box>
          </Box>
          {/* Where the terminal sends no clicks, cmd+click on the caption opens it. */}
          <Link href={fileUrl(opens)} label={caption(head, stored)} />
        </Box>
      )
    } catch (problem) {
      const reason = problem instanceof Error ? problem.message : String(problem)

      return (
        <Box key={`inline-box-${index}`} paddingLeft={2}>
          <Text dimColor>{`[inline image: ${caption(head, stored)}, no preview: ${reason}]`}</Text>
        </Box>
      )
    }
  }

  return Promise.all(found.map(draw))
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
      let reason = 'the overlay stopped'

      try {
        for await (const piece of $.process.spawn({ argv: [options.python, options.overlay, '--session', sessionId] })) {
          if (piece.stream !== 'stdout') {
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
      $.ui.invalidate('ui.render')

      if (!wasLive) {
        $.ui.toast(`inline-images: block previews only, no real pixels in iTerm2: ${reason}`)
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
    const { open } = (e.data ?? {}) as { open?: unknown }

    if (typeof open === 'string' && openable.has(open)) {
      const opened = await $.process.run(['open', open]).catch(() => undefined)

      if (opened === undefined || opened.exitCode !== 0) {
        await $.process.run(['xdg-open', open]).catch(() => undefined)
      }
    }

    return next(e)
  })

  // A file delivered to the person: show the picture under the delivery.
  on('ui.render', { component: 'ToolResult' }, async ($, e, next) => {
    const files = e.props.isErrored ? [] : deliveredImages(e.props.output)

    if (files.length === 0) {
      return next(e)
    }

    const base = await next(e)
    const drawn = await previews(
      $,
      e,
      settings,
      files.map(file => ({ file })),
    )
    const { Box } = $.ui.resolve(e)

    return drawn.length === 0 ? (
      base
    ) : (
      <Box flexDirection="column">
        {base}
        {drawn}
      </Box>
    )
  })

  // A call drawn on its own row: the picture goes under the result.
  on('ui.render', { component: 'ToolResult' }, async ($, e, next) => {
    const sources = e.props.isErrored ? [] : sourceOf(e.props.output)

    if (sources.length === 0) {
      return next(e)
    }

    const rest = withoutImages(sources[0]?.text ?? '')
    const base = await next({ ...e, props: { ...e.props, output: withText(e.props.output, rest) } })
    const drawn = await previews($, e, settings, sources)

    if (drawn.length === 0) {
      return base
    }

    const { Box } = $.ui.resolve(e)

    return (
      <Box flexDirection="column">
        {base}
        {drawn}
      </Box>
    )
  })

  // A call drawn in a group row: no image data as text, however the row is drawn.
  on('ui.render', { component: 'ToolUse' }, ($, e, next) => {
    if (e.props.isErrored || !hasImage(e.props.output)) {
      return next(e)
    }

    const text = withoutImages(outputText(e.props.output) ?? '')

    return next({ ...e, props: { ...e.props, output: withText(e.props.output, text) } })
  })

  // A run of calls folds into one line, which would hide the picture. Unfold
  // the group that holds one and draw the pictures under it.
  on('ui.render', { component: 'ToolGroup' }, async ($, e, next) => {
    const sources = e.props.calls.flatMap(call => (call.isErrored ? [] : sourceOf(call.output)))

    if (sources.length === 0) {
      return next(e)
    }

    const base = await next({ ...e, props: { ...e.props, isExpanded: true } })
    const drawn = await previews($, e, settings, sources)

    if (drawn.length === 0) {
      return base
    }

    const { Box } = $.ui.resolve(e)

    return (
      <Box flexDirection="column">
        {base}
        {drawn}
      </Box>
    )
  })
}

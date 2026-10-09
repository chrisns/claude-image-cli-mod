import type { EngineInterface, Register, RenderInput } from 'claude-code'

import { DEFAULT_CELL, fit, type Cell } from './layout.ts'
import { stripBlocks } from './model.ts'
import { extract, parseArguments } from './osc1337.ts'
import {
  caption,
  drawsPixels,
  imageKey,
  outputText,
  parseReply,
  readOptions,
  savedPath,
  withText,
  type Inspected,
  type Options,
  type Pic,
} from './preview.ts'

type Settings = Parameters<Register>[1]

// A tool output that may hold images: its text, and where the whole of it is
// saved when the tool cut the text short.
type Source = { text: string; saved?: string }

// Decoding runs once per image. The engine keeps a drawing per input, but a
// resize draws again, so the decoded image is kept here.
const inspected = new Map<string, Promise<Inspected>>()

const hasImage = (output: unknown) => outputText(output)?.includes(']1337;') === true

// The text of an output once its images are cut out: the pictures take their place.
// An output that was only images says so, where the engine would say "No output".
const withoutImages = (text: string) => {
  const { text: rest, images } = extract(text, () => '', '')
  // Data that a size limit cut off holds an image that `images` cannot count.
  const count = Math.max(1, images.filter(image => image.isInline).length)

  if (rest.trim() !== '' || !text.includes(']1337;')) {
    return rest
  }

  return count === 1 ? '(inline image)' : `(${count} inline images)`
}

const sourceOf = (output: unknown): Source[] => {
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

  for (const { text, saved } of sources) {
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
            stored = run<Inspected>(['inspect'], image.base64)
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
  const { Box, Text, Raster, Image } = $.ui.resolve(e)

  const draw = async ({ head, inspect }: Pic, index: number) => {
    let stored: Inspected | undefined

    try {
      stored = await inspect()

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
            (await run<{ cells: string }>(['cells', stored.path, ...size, '--palette', String(options.palette)])).cells
          }
        />
      )

      return (
        <Box key={`inline-box-${index}`} flexDirection="column" paddingLeft={2}>
          {picture}
          <Text dimColor>{caption(head, stored)}</Text>
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

  // What the model reads. The transcript keeps the whole output, so the preview
  // can be drawn again after a resume.
  on('session.append', { door: 'tool-result' }, ($, e, next) => {
    if (!hidesFromModel) {
      return next(e)
    }

    const content = stripBlocks(e.message.content)

    return next(content === e.message.content ? e : { ...e, message: { ...e.message, content: [...content] } })
  }).catch(($, e, next) => next(e))

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

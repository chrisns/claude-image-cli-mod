import { extract, formatBytes, type InlineHead } from './osc1337.ts'

export type Options = {
  renderer: 'auto' | 'cells' | 'image' | 'iterm'
  maxColumns: number
  maxRows: number
  palette: number
  python: string
  helper: string
  overlay: string
}

export type Inspected = {
  path: string
  format: string
  width: number
  height: number
  frames: number
  bytes: number
  /** The stored image under the name its sender gave, when it gave one. */
  named?: string
}

/** An image that is ready to size and draw: how the sender asked for it, and where it is stored. */
export type Pic = {
  head: InlineHead
  inspect: () => Promise<Inspected>
  /** The file a click opens, when it is not the stored copy: a delivered file. */
  file?: string
}

/** The file URL of a path, for a link a terminal opens. */
export function fileUrl(path: string): string {
  return `file://${path.split('/').map(encodeURIComponent).join('/')}`
}


/** Whether this terminal draws real pixels for an Image element. */
export function drawsPixels(program: string | undefined, term: string | undefined, kitty: string | undefined): boolean {
  return program === 'ghostty' || term === 'xterm-ghostty' || term === 'xterm-kitty' || kitty !== undefined
}

/** The key under which a decoded image is remembered. */
export function imageKey(base64: string): string {
  return `${base64.length}:${base64.slice(0, 64)}:${base64.slice(-64)}`
}

/** Where a tool saved an output that was too large to keep whole, when it did. */
export function savedPath(output: unknown): string | undefined {
  if (typeof output === 'object' && output !== null && 'persistedOutputPath' in output) {
    const { persistedOutputPath } = output as { persistedOutputPath: unknown }

    return typeof persistedOutputPath === 'string' && persistedOutputPath !== '' ? persistedOutputPath : undefined
  }

  return undefined
}

/** Read the last line the helper printed: its reply, or the reason it failed. */
export function parseReply<T>(stdout: string, stderr: string, exitCode: number): T {
  const line = stdout.trim().split('\n').pop() ?? ''

  let reply: { ok?: boolean; error?: string } | undefined

  try {
    reply = JSON.parse(line)
  } catch {
    const hint = stderr.trim().split('\n').pop()
    throw new Error(hint === undefined || hint === '' ? `the image helper exited with ${exitCode}` : hint)
  }

  if (reply?.ok !== true) {
    throw new Error(reply?.error ?? 'the image helper failed')
  }

  return reply as T
}

export function caption(head: InlineHead, found: Inspected | undefined): string {
  const parts: string[] = []

  if (head.name !== '') {
    // imgcat sends the path it was given: the file name says enough.
    parts.push(head.name.split('/').pop() ?? head.name)
  }

  if (found !== undefined) {
    parts.push(`${found.width}\u00d7${found.height}`, found.format)

    if (found.frames > 1) {
      parts.push(`frame 1 of ${found.frames}`)
    }

    parts.push(formatBytes(found.bytes))
  } else if (head.declaredBytes !== undefined) {
    parts.push(formatBytes(head.declaredBytes))
  }

  return parts.join(' \u00b7 ')
}

export function readOptions(
  options: Readonly<Record<string, string | number | boolean | readonly string[]>>,
  root: string,
): Options {
  const renderer = options.renderer
  const number = (value: unknown, fallback: number) =>
    typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : fallback

  return {
    renderer: renderer === 'cells' || renderer === 'image' || renderer === 'iterm' ? renderer : 'auto',
    maxColumns: Math.max(1, Math.min(255, number(options.max_columns, 100))),
    maxRows: Math.max(1, Math.min(255, number(options.max_rows, 28))),
    palette: Math.min(256, number(options.palette, 0)),
    python: typeof options.python === 'string' && options.python !== '' ? options.python : 'python3',
    helper: `${root}/bin/render.py`,
    overlay: `${root}/bin/iterm_overlay.py`,
  }
}

/** Whether to draw real pixels over the cells with the iTerm2 overlay. */
export function wantsOverlay(renderer: Options['renderer'], program: string | undefined): boolean {
  return renderer === 'iterm' || (renderer === 'auto' && program === 'iTerm.app')
}

/** The overlay's one-line JSON messages, from the text it has written so far. */
export function overlayMessages(buffer: string): { messages: { ok?: boolean; ready?: boolean; error?: string }[]; rest: string } {
  const lines = buffer.split('\n')
  const rest = lines.pop() ?? ''
  const messages = lines.flatMap(line => {
    try {
      return [JSON.parse(line)]
    } catch {
      return []
    }
  })

  return { messages, rest }
}

/** The text of a tool's output, wherever the tool kept it. */
export function outputText(output: unknown): string | undefined {
  if (typeof output === 'string') {
    return output
  }

  if (typeof output === 'object' && output !== null && 'stdout' in output) {
    const { stdout } = output as { stdout: unknown }

    return typeof stdout === 'string' ? stdout : undefined
  }

  return undefined
}

/** The same output with its text replaced. */
export function withText(output: unknown, text: string): unknown {
  return typeof output === 'string' ? text : { ...(output as Record<string, unknown>), stdout: text }
}

/**
 * The text of an output once its images are cut out: the pictures take their place.
 * An output that was only images says so, where the engine would say "No output".
 */
export function withoutImages(text: string): string {
  const { text: rest, images } = extract(text, () => '', '')
  // Data that a size limit cut off holds an image that `images` cannot count.
  const count = Math.max(1, images.filter(image => image.isInline).length)

  if (rest.trim() !== '' || !text.includes(']1337;')) {
    return rest
  }

  return count === 1 ? '(inline image)' : `(${count} inline images)`
}

/**
 * The image files a tool delivered to the person, from its result's attachments
 * (SendUserFile, SendUserMessage): the ones it marked as images.
 */
export function deliveredImages(output: unknown): string[] {
  if (typeof output !== 'object' || output === null || !('attachments' in output)) {
    return []
  }

  const { attachments } = output as { attachments: unknown }

  if (!Array.isArray(attachments)) {
    return []
  }

  return attachments.flatMap(attachment => {
    const { path, isImage, media_type } = (attachment ?? {}) as Record<string, unknown>
    const image = isImage === true || (typeof media_type === 'string' && media_type.startsWith('image/'))

    return typeof path === 'string' && path !== '' && image ? [path] : []
  })
}

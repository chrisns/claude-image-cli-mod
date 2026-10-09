import { formatBytes, type InlineHead } from './osc1337.ts'

export type Options = {
  renderer: 'auto' | 'cells' | 'image'
  maxColumns: number
  maxRows: number
  palette: number
  python: string
  helper: string
}

export type Inspected = {
  path: string
  format: string
  width: number
  height: number
  frames: number
  bytes: number
}

/** An image that is ready to size and draw: how the sender asked for it, and where it is stored. */
export type Pic = { head: InlineHead; inspect: () => Promise<Inspected> }


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
    renderer: renderer === 'cells' || renderer === 'image' ? renderer : 'auto',
    maxColumns: Math.max(1, Math.min(255, number(options.max_columns, 100))),
    maxRows: Math.max(1, Math.min(255, number(options.max_rows, 28))),
    palette: Math.min(256, number(options.palette, 0)),
    python: typeof options.python === 'string' && options.python !== '' ? options.python : 'python3',
    helper: `${root}/bin/render.py`,
  }
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

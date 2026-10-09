// Parser for the iTerm2 inline images protocol (OSC 1337).
// https://iterm2.com/documentation-images.html
//
// Three wire forms exist and all of them reach a tool result as plain text:
//
//   ESC ] 1337 ; File=<args>:<base64> BEL                      whole image, one sequence
//   ESC ] 1337 ; MultipartFile=<args> BEL                      start of a chunked image
//   ESC ] 1337 ; FilePart=<base64> BEL  (repeated)             one chunk
//   ESC ] 1337 ; FileEnd BEL                                   end of a chunked image
//
// BEL may be ST (ESC \) instead, and tmux wraps each sequence in a DCS
// passthrough (ESC P tmux ; <sequence with every ESC doubled> ESC \).

export type Dimension =
  | { unit: 'auto' }
  | { unit: 'cells' | 'px' | 'percent'; value: number }

export type InlineHead = {
  /** The file name the sender gave, decoded; empty when none. */
  name: string
  /** The size the sender declared in bytes, when it gave one. */
  declaredBytes?: number
  width: Dimension
  height: Dimension
  /** `preserveAspectRatio`: true unless the sender wrote 0. */
  isAspectPreserved: boolean
  /** `inline=1`. An `inline=0` image is a download and is never drawn. */
  isInline: boolean
}

export type InlineImage = InlineHead & {
  /** The image file, base64, whitespace removed. */
  base64: string
}

export type Extracted = {
  /** The input with every image sequence replaced by `marker(image)`. */
  text: string
  images: InlineImage[]
}

const ESC = '\u001b'
const BEL = '\u0007'
const ST = `${ESC}\\`

// A tmux DCS passthrough: ESC P tmux ; <body> ESC \   (body has ESC doubled)
const TMUX = new RegExp(`${ESC}Ptmux;((?:[^${ESC}]|${ESC}${ESC})*)${ESC}\\\\`, 'g')

// One OSC 1337 file sequence: the verb, an optional `=` and arguments, a terminator.
// The body is lazy so that it stops at the first BEL or ST.
const SEQUENCE = new RegExp(
  `${ESC}\\]1337;(File|MultipartFile|FilePart|FileEnd)(?:=([^${BEL}${ESC}\u009c]*))?(?:${BEL}|${ESC}\\\\|\u009c)`,
  'g',
)

const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/

export function unwrapTmux(text: string): string {
  return text.replace(TMUX, (_all, body: string) => body.split(`${ESC}${ESC}`).join(ESC))
}

function decodeName(encoded: string): string {
  try {
    const bytes = Uint8Array.fromBase64(encoded)
    return new TextDecoder().decode(bytes)
  } catch {
    return ''
  }
}

export function parseDimension(raw: string | undefined): Dimension {
  if (raw === undefined || raw === '' || raw === 'auto') {
    return { unit: 'auto' }
  }

  const match = /^(\d+(?:\.\d+)?)(px|%)?$/.exec(raw)

  if (match === null) {
    return { unit: 'auto' }
  }

  const value = Number(match[1])

  if (value <= 0) {
    return { unit: 'auto' }
  }

  const unit = match[2] === 'px' ? 'px' : match[2] === '%' ? 'percent' : 'cells'

  return { unit, value }
}

/** Read the `key=value;key=value` arguments of a File or MultipartFile sequence. */
export function parseArguments(raw: string): InlineHead {
  const args = new Map<string, string>()

  for (const pair of raw.split(';')) {
    const at = pair.indexOf('=')

    if (at > 0) {
      args.set(pair.slice(0, at), pair.slice(at + 1))
    }
  }

  const size = Number(args.get('size'))
  const name = args.get('name')

  return {
    name: name === undefined ? '' : decodeName(name),
    declaredBytes: Number.isFinite(size) && size > 0 ? size : undefined,
    width: parseDimension(args.get('width')),
    height: parseDimension(args.get('height')),
    isAspectPreserved: args.get('preserveAspectRatio') !== '0',
    isInline: args.get('inline') === '1',
  }
}

/** Decoded size of a base64 string, without decoding it. */
export function base64Bytes(base64: string): number {
  const padding = base64.endsWith('==') ? 2 : base64.endsWith('=') ? 1 : 0

  return Math.floor((base64.length * 3) / 4) - padding
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`
  }

  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`
  }

  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** The text that stands in for an image sequence once it has been cut out. */
export function describe(image: InlineImage): string {
  const name = image.name === '' ? 'unnamed' : image.name.split('/').pop()
  const bytes = formatBytes(base64Bytes(image.base64))

  return `[inline image: ${name}, ${bytes}]`
}

/** The text that stands in for image data that stops before its end. */
export const INCOMPLETE = '[inline image: cut off]'

// An image sequence that starts and never ends, to the end of the text.
const UNFINISHED = new RegExp(`${ESC}\\]1337;(?:File|MultipartFile|FilePart)[^${BEL}${ESC}\u009c]*$`)

/**
 * Cut every iTerm2 inline image out of `input`.
 *
 * A sequence whose payload is not base64 is left in the text untouched: it is
 * not an image, and guessing would hide it. Image data that stops before its
 * end (a tool cut the output at a size limit) is replaced with `incomplete`.
 */
export function extract(
  input: string,
  marker: (image: InlineImage) => string = describe,
  incomplete: string = INCOMPLETE,
): Extracted {
  if (!input.includes(']1337;')) {
    return { text: input, images: [] }
  }

  const text = unwrapTmux(input)
  const images: InlineImage[] = []

  let output = ''
  let copied = 0

  // A chunked image being assembled.
  let open: { start: number; head: InlineHead; parts: string[] } | undefined

  const finish = (end: number, head: InlineHead, payload: string, start: number) => {
    const base64 = payload.replace(/\s+/g, '')

    if (base64 === '' || !BASE64.test(base64)) {
      return false
    }

    const image: InlineImage = { ...head, base64 }

    output += text.slice(copied, start)
    copied = end
    images.push(image)
    output += marker(image)

    return true
  }

  for (const match of text.matchAll(SEQUENCE)) {
    const [whole, verb, body = ''] = match
    const start = match.index
    const end = start + whole.length

    if (verb === 'File') {
      open = undefined
      const colon = body.indexOf(':')

      if (colon >= 0) {
        finish(end, parseArguments(body.slice(0, colon)), body.slice(colon + 1), start)
      }
    } else if (verb === 'MultipartFile') {
      open = { start, head: parseArguments(body), parts: [] }
    } else if (verb === 'FilePart') {
      open?.parts.push(body)
    } else if (verb === 'FileEnd' && open !== undefined) {
      finish(end, open.head, open.parts.join(''), open.start)
      open = undefined
    }
  }

  // An image that started and never ended: a size limit cut the output short.
  const tail = text.slice(copied)
  const unfinished = open === undefined ? UNFINISHED.exec(tail)?.index : open.start - copied

  if (unfinished !== undefined && unfinished >= 0) {
    output += tail.slice(0, unfinished) + incomplete
  } else {
    output += tail
  }

  return { text: output, images }
}

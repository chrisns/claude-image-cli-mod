// Parser for the iTerm2 inline images protocol (OSC 1337).
// https://iterm2.com/documentation-images.html
//
// Two wire forms exist and both reach a tool result as plain text:
//
//   ESC ] 1337 ; File=<args>:<base64> BEL                      a whole image, one sequence
//
//   ESC ] 1337 ; MultipartFile=<args> BEL                      a chunked image (imgcat 3):
//   ESC ] 1337 ; FilePart=<base64> BEL  (repeated)             its start, its chunks
//   ESC ] 1337 ; FileEnd BEL                                   and its end
//
// BEL may be ST (ESC \) or 8-bit ST (U+009C) instead, and tmux wraps each
// sequence in a DCS passthrough (ESC P tmux ; <the sequence, ESC doubled> ESC \).
//
// The input is untrusted (any program, file or web page a tool prints), so
// every scan here is linear in its length and nothing from it reaches the
// screen or the model unchecked.

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

const TMUX_START = `${ESC}Ptmux;`

// One OSC 1337 file sequence: the verb, an optional `=` and arguments, a terminator.
// The body is a negated class, so it stops at the first BEL, ESC or 8-bit ST.
const SEQUENCE = new RegExp(
  `${ESC}\\]1337;(File|MultipartFile|FilePart|FileEnd)(?:=([^${BEL}${ESC}\u009c]*))?(?:${BEL}|${ESC}\\\\|\u009c)`,
  'g',
)

const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/

// The longest file name kept: enough for a caption, too short to flood one.
const NAME_LIMIT = 120

/** Whether text holds the start of an iTerm2 image: a cheap test before any parse. */
export function hasImageSequence(text: string): boolean {
  return /\]1337;(?:File|MultipartFile)=/.test(text)
}

/**
 * Undo tmux's DCS passthrough: ESC P tmux ; <body, every ESC doubled> ESC \.
 *
 * A scan with indexOf, not a regex: a regex over an unterminated passthrough
 * with many ESC ESC pairs backtracks in quadratic time, and the text is untrusted.
 */
export function unwrapTmux(text: string): string {
  if (!text.includes(TMUX_START)) {
    return text
  }

  let output = ''
  let at = 0

  for (;;) {
    const start = text.indexOf(TMUX_START, at)

    if (start < 0) {
      break
    }

    // The body ends at the first ESC \ whose ESC is not one of a doubled pair.
    let index = start + TMUX_START.length
    let end = -1

    while (index < text.length) {
      const escape = text.indexOf(ESC, index)

      if (escape < 0 || escape + 1 >= text.length) {
        break
      }

      if (text[escape + 1] === ESC) {
        index = escape + 2
      } else if (text[escape + 1] === '\\') {
        end = escape
        break
      } else {
        index = escape + 1
      }
    }

    if (end < 0) {
      break // never terminated: leave the rest as it is
    }

    output += text.slice(at, start) + text.slice(start + TMUX_START.length, end).split(`${ESC}${ESC}`).join(ESC)
    at = end + 2
  }

  return output + text.slice(at)
}

/**
 * A file name from the sender, made safe to show: no control or format
 * characters (an ESC would be an escape sequence in a caption; a newline or a
 * bidi override would forge text), and not longer than NAME_LIMIT.
 */
export function cleanName(name: string): string {
  const cleaned = name.replace(/[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu, '').trim()

  return cleaned.length > NAME_LIMIT ? `${cleaned.slice(0, NAME_LIMIT - 1)}\u2026` : cleaned
}

function decodeName(encoded: string): string {
  try {
    const bytes = Uint8Array.fromBase64(encoded)
    return cleanName(new TextDecoder().decode(bytes))
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

  // Absurd sizes (a width of 400 nines is Infinity) are no request at all.
  if (!Number.isFinite(value) || value <= 0 || value > 100_000) {
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

/** The file name alone, from the path a sender such as imgcat gives. */
export function baseName(name: string): string {
  return name.split('/').pop() || name
}

/**
 * The text that stands in for an image sequence once it has been cut out.
 * An `inline=0` file is a download: nobody sees it, and the text says so.
 */
export function describe(image: InlineImage): string {
  const name = image.name === '' ? 'unnamed' : baseName(image.name)
  const bytes = formatBytes(base64Bytes(image.base64))

  return image.isInline ? `[inline image: ${name}, ${bytes}]` : `[file download: ${name}, ${bytes}, not shown]`
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
  if (!/\]1337;(?:File|MultipartFile|FilePart)/.test(input)) {
    return { text: input, images: [] }
  }

  const text = unwrapTmux(input)
  const images: InlineImage[] = []

  let output = ''
  let copied = 0

  // A chunked image being assembled, and any text printed between its chunks
  // (a progress line, say), which is kept and goes after the image.
  let open: { start: number; head: InlineHead; parts: string[]; between: string; last: number } | undefined

  const finish = (end: number, head: InlineHead, payload: string, start: number, between = '') => {
    const base64 = payload.replace(/\s+/g, '')

    if (base64 === '' || !BASE64.test(base64)) {
      return false
    }

    const image: InlineImage = { ...head, base64 }

    output += text.slice(copied, start)
    copied = end
    images.push(image)
    output += marker(image) + between

    return true
  }

  for (const match of text.matchAll(SEQUENCE)) {
    const [whole, verb, body = ''] = match
    const start = match.index
    const end = start + whole.length

    if (open !== undefined && verb !== 'MultipartFile' && verb !== 'File') {
      open.between += text.slice(open.last, start)
      open.last = end
    }

    if (verb === 'File') {
      open = undefined
      const colon = body.indexOf(':')

      if (colon >= 0) {
        finish(end, parseArguments(body.slice(0, colon)), body.slice(colon + 1), start)
      }
    } else if (verb === 'MultipartFile') {
      open = { start, head: parseArguments(body), parts: [], between: '', last: end }
    } else if (verb === 'FilePart') {
      open?.parts.push(body)
    } else if (verb === 'FileEnd' && open !== undefined) {
      finish(end, open.head, open.parts.join(''), open.start, open.between)
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

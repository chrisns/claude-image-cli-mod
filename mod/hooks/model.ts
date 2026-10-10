import { describe, extract } from './osc1337.ts'

type Block = { type: string; [field: string]: unknown }

/** Cut every inline image out of one string; the same string comes back when there is none. */
export function strip(text: string): string {
  // A download (inline=0) is never drawn: its note says so, and is left as it is.
  return extract(text, image => (image.isInline ? describe(image).replace(/]$/, `, data left out; ${PREVIEW}]`) : describe(image)), SHOWN_CUT).text
}

// What the model is told the user sees. The note is written before any draw, and
// a draw can fail (no decoder, a format the helper refuses) or never happen (a
// surface the mod does not draw on): it claims no more than the mod can promise.
const PREVIEW = 'the user sees a preview if their terminal can draw it'

/** The note for image data that a size limit cut off: the mod reads it whole from the saved output. */
export const SHOWN_CUT = `[inline image data left out here; ${PREVIEW}]`

/**
 * Rewrite the content of a tool result so that no image data is in it.
 *
 * Returns the same array when there is nothing to cut, so a caller can tell
 * that no rewrite is needed.
 */
export function stripBlocks(content: readonly Block[]): readonly Block[] {
  let changed = false

  const next = content.map((block): Block => {
    if (block.type !== 'tool_result') {
      return block
    }

    const inner = block.content

    if (typeof inner === 'string') {
      const text = strip(inner)
      changed ||= text !== inner

      return text === inner ? block : { ...block, content: text }
    }

    if (Array.isArray(inner)) {
      const parts = (inner as Block[]).map((part): Block => {
        if (part.type !== 'text' || typeof part.text !== 'string') {
          return part
        }

        const text = strip(part.text)
        changed ||= text !== part.text

        return text === part.text ? part : { ...part, text }
      })

      return { ...block, content: parts }
    }

    return block
  })

  return changed ? next : content
}

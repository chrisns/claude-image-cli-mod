// Mermaid diagrams in Claude's replies.
//
// A reply is markdown. Each complete ```mermaid fenced block in it is drawn as
// a picture in its place; the text around it stays text. A block that is not
// closed yet (the reply is still streaming) stays text until it is.

export type Segment = { kind: 'text'; text: string } | { kind: 'mermaid'; source: string; fence: string }

// An opening fence: up to three spaces, three or more ` or ~, then the info string.
const OPENING = /^( {0,3})(`{3,}|~{3,})[ \t]*([^\n]*)$/

/** Split markdown into text and complete Mermaid blocks, in order. */
export function splitMermaid(markdown: string): Segment[] {
  if (!/mermaid/i.test(markdown)) {
    return [{ kind: 'text', text: markdown }]
  }

  const lines = markdown.split('\n')
  const segments: Segment[] = []
  let text: string[] = []
  let index = 0

  const flush = () => {
    if (text.length > 0) {
      segments.push({ kind: 'text', text: text.join('\n') })
      text = []
    }
  }

  while (index < lines.length) {
    const line = lines[index] ?? ''
    const opening = OPENING.exec(line)

    if (opening === null) {
      text.push(line)
      index++
      continue
    }

    const [, , fence = '```', info = ''] = opening
    const marker = fence[0] ?? '`'
    // The closing fence: the same character, at least as many, nothing after it.
    const closing = new RegExp(`^ {0,3}${marker === '`' ? '`' : '~'}{${fence.length},}[ \\t]*$`)
    let end = index + 1

    while (end < lines.length && !closing.test(lines[end] ?? '')) {
      end++
    }

    const isMermaid = info.trim().split(/\s+/)[0]?.toLowerCase() === 'mermaid'

    if (end >= lines.length || !isMermaid) {
      // Another language, or a block still being written: it is text, as it is.
      const last = end >= lines.length ? lines.length : end + 1
      text.push(...lines.slice(index, last))
      index = last
      continue
    }

    flush()
    segments.push({
      kind: 'mermaid',
      source: lines.slice(index + 1, end).join('\n'),
      fence: lines.slice(index, end + 1).join('\n'),
    })
    index = end + 1
  }

  flush()

  return segments
}

/** The diagram's type: the first word after any front matter and %% comments. */
export function mermaidKind(source: string): string {
  const lines = source.split('\n')
  let index = 0

  while (index < lines.length) {
    const line = (lines[index] ?? '').trim()

    if (line === '---') {
      index++
      while (index < lines.length && (lines[index] ?? '').trim() !== '---') {
        index++
      }
      index++
      continue
    }

    if (line !== '' && !line.startsWith('%%')) {
      return line.split(/\s+/)[0]?.replace(/[^A-Za-z0-9-]/g, '') || 'diagram'
    }

    index++
  }

  return 'diagram'
}

/**
 * What the model reads in its system prompt when diagrams can be drawn: that a
 * ```mermaid block becomes a picture, and the Wardley map syntax, which is new.
 */
export const MERMAID_GUIDE = `# Diagrams render as pictures

This terminal draws Mermaid diagrams. Each \`\`\`mermaid fenced block you write in a reply is shown to the user as a rendered picture in its place (the source is not shown); the user can click it to open it full size. So when a diagram explains something better than prose — a flow, a sequence of calls, a state machine, a data model, a timeline, a hierarchy, a plan, a strategy map — draw it: write a \`\`\`mermaid block. Do not say you cannot show diagrams.

Rules:
- One diagram per \`\`\`mermaid block. Keep the source valid Mermaid (version 11): a syntax error shows the user an error and the source instead of a picture.
- Every Mermaid type works: flowchart, sequenceDiagram, classDiagram, stateDiagram-v2, erDiagram, journey, gantt, pie, quadrantChart, requirementDiagram, gitGraph, C4Context, mindmap, timeline, sankey-beta, xychart-beta, block-beta, packet-beta, kanban, architecture-beta, radar-beta, treemap-beta, and wardley-beta.
- Put labels with spaces or punctuation in quotes where the diagram type needs it (flowchart node text in [ ], sequence messages after :).

Wardley maps use \`wardley-beta\`. Coordinates are [visibility, evolution], each 0 to 1: visibility 1 is the top (closest to the user need), evolution 0 is Genesis and 1 is Commodity. Syntax:

\`\`\`mermaid
wardley-beta
title Tea shop
size [1100, 800]
evolution Genesis -> Custom Built -> Product -> Commodity
anchor Business [0.95, 0.63]
anchor Public [0.95, 0.78]
component Cup of Tea [0.79, 0.61] label [19, -4]
component Tea [0.63, 0.81]
component Hot Water [0.52, 0.80]
component Kettle [0.43, 0.35] inertia
component Power [0.10, 0.70] (outsource)
Business -> Cup of Tea
Public -> Cup of Tea
Cup of Tea -> Tea
Cup of Tea -> Hot Water
Hot Water -> Kettle ; limited by
Kettle +> Power
evolve Kettle 0.62
pipeline Kettle {
  component Campfire Kettle [0.30]
  component Electric Kettle [0.60]
}
note "Standardising power allows kettles to evolve faster" [0.23, 0.33]
annotations [0.72, 0.03]
annotation 1,[0.48, 0.55] "Hot water is a commodity"
accelerator Open source [0.40, 0.45]
deaccelerator Regulation [0.30, 0.25]
\`\`\`

- \`anchor\` is a user or need; \`component\` a capability. Names may contain spaces; quote a name that has other punctuation.
- \`A -> B\` is a dependency; \`A +> B\` a flow; \`A -> B ; text\` labels the link.
- \`evolve Name 0.62\` moves a component to a new evolution; \`pipeline Name { component Option [evolution] }\` shows options.
- Declare every name before you use it: a link, \`evolve\` or \`pipeline\` that names a component not declared with \`component Name [v, e]\` is an error. A pipeline's own options are declared inside it.
- After a component's coordinates: \`label [x, y]\` offsets its label, \`inertia\` marks resistance, and \`(build)\`, \`(buy)\` or \`(outsource)\` marks the sourcing method.
- \`note "text" [v, e]\` adds a note; \`annotations [v, e]\` places the annotation list and \`annotation 1,[v, e] "text"\` numbers a point.
- \`size [width, height]\` and \`evolution A -> B -> C -> D\` are optional.
`

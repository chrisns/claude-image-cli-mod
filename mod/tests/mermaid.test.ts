import { describe, expect, test } from 'claude-code/testing'

import { MERMAID_GUIDE, mermaidKind, splitMermaid } from '../hooks/mermaid.ts'

describe('splitMermaid', () => {
  test('text with no diagram is one text segment', () => {
    expect(splitMermaid('just words\n```js\nx\n```')).toEqual([{ kind: 'text', text: 'just words\n```js\nx\n```' }])
  })

  test('a diagram between two paragraphs', () => {
    const reply = 'Here is the flow:\n\n```mermaid\nflowchart LR\n  A --> B\n```\n\nThat is all.'

    expect(splitMermaid(reply)).toEqual([
      { kind: 'text', text: 'Here is the flow:\n' },
      { kind: 'mermaid', source: 'flowchart LR\n  A --> B', fence: '```mermaid\nflowchart LR\n  A --> B\n```' },
      { kind: 'text', text: '\nThat is all.' },
    ])
  })

  test('two diagrams, a tilde fence and an info string after the language', () => {
    const reply = '```mermaid\npie\n```\n~~~Mermaid title\nmindmap\n~~~'
    const diagrams = splitMermaid(reply).filter(segment => segment.kind === 'mermaid')

    expect(diagrams.map(segment => (segment.kind === 'mermaid' ? segment.source : ''))).toEqual(['pie', 'mindmap'])
  })

  test('a block that is not closed yet stays text', () => {
    const streaming = 'Drawing it:\n```mermaid\nflowchart LR\n  A --> B'

    expect(splitMermaid(streaming)).toEqual([{ kind: 'text', text: streaming }])
  })

  test('a longer fence holds a shorter one inside', () => {
    const reply = '````mermaid\nflowchart LR\n  A["```"] --> B\n````'
    const [segment] = splitMermaid(reply)

    expect(segment).toMatchObject({ kind: 'mermaid', source: 'flowchart LR\n  A["```"] --> B' })
  })

  test('the word mermaid in another block is not a diagram', () => {
    const reply = '```bash\nnpm install -g @mermaid-js/mermaid-cli\n```'

    expect(splitMermaid(reply)).toEqual([{ kind: 'text', text: reply }])
  })
})

describe('mermaidKind', () => {
  test('the first word, after front matter and comments', () => {
    expect(mermaidKind('flowchart LR\nA-->B')).toBe('flowchart')
    expect(mermaidKind('---\ntitle: x\n---\n%% a comment\nwardley-beta\n')).toBe('wardley-beta')
    expect(mermaidKind('')).toBe('diagram')
  })
})

describe('MERMAID_GUIDE', () => {
  test('teaches the Wardley syntax the renderer accepts', () => {
    for (const piece of ['wardley-beta', 'anchor ', 'component ', 'evolve ', 'pipeline ', 'note "', 'annotation 1,[', 'accelerator ']) {
      expect(MERMAID_GUIDE).toContain(piece)
    }
  })
})

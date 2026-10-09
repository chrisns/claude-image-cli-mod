import { describe, expect, test } from 'claude-code/testing'

import { cleanName, extract, hasImageSequence, parseDimension, unwrapTmux } from '../hooks/osc1337.ts'
import { stripBlocks } from '../hooks/model.ts'

const ESC = '\u001b'
const BEL = '\u0007'
const ST = `${ESC}\\`

// 1x1 PNG
const PNG =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
const NAME = 'dGlueS5wbmc=' // tiny.png

const whole = (args: string, end = BEL) => `${ESC}]1337;File=${args}:${PNG}${end}`

describe('File= (one sequence)', () => {
  test('cuts the image and keeps the text around it', () => {
    const { text, images } = extract(`before\n${whole(`name=${NAME};size=70;inline=1`)}\nafter`, () => '<img>')

    expect(text).toBe('before\n<img>\nafter')
    expect(images).toHaveLength(1)
    expect(images[0]?.name).toBe('tiny.png')
    expect(images[0]?.declaredBytes).toBe(70)
    expect(images[0]?.isInline).toBe(true)
    expect(images[0]?.base64).toBe(PNG)
  })

  test('accepts ST as the terminator', () => {
    const { images } = extract(whole('inline=1', ST))

    expect(images).toHaveLength(1)
  })

  test('marks inline=0 as a download, not a picture', () => {
    const { images } = extract(whole('inline=0'))

    expect(images[0]?.isInline).toBe(false)
  })

  test('reads width, height and preserveAspectRatio', () => {
    const { images } = extract(whole('inline=1;width=40;height=50%;preserveAspectRatio=0'))

    expect(images[0]?.width).toEqual({ unit: 'cells', value: 40 })
    expect(images[0]?.height).toEqual({ unit: 'percent', value: 50 })
    expect(images[0]?.isAspectPreserved).toBe(false)
  })

  test('finds several images in one output', () => {
    const { text, images } = extract(`a${whole('inline=1')}b${whole('inline=1')}c`, () => '|')

    expect(text).toBe('a|b|c')
    expect(images).toHaveLength(2)
  })

  test('replaces data that a size limit cut off', () => {
    const cut = `ok\n${ESC}]1337;File=inline=1:${PNG.slice(0, 20)}`

    expect(extract(cut)).toEqual({ text: 'ok\n[inline image: cut off]', images: [] })
  })

  test('replaces a chunked image that never ends', () => {
    const cut = `${ESC}]1337;MultipartFile=inline=1${BEL}${ESC}]1337;FilePart=${PNG}${BEL}${ESC}]1337;FilePart=AAAA`

    expect(extract(cut)).toEqual({ text: '[inline image: cut off]', images: [] })
  })

  test('keeps a whole image and drops the cut one after it', () => {
    const { text, images } = extract(`${whole('inline=1')}${ESC}]1337;File=inline=1:AAAA`, () => '<img>')

    expect(text).toBe('<img>[inline image: cut off]')
    expect(images).toHaveLength(1)
  })

  test('leaves a payload that is not base64 alone', () => {
    const bad = `${ESC}]1337;File=inline=1:not base64 at all!${BEL}`

    expect(extract(bad).images).toHaveLength(0)
    expect(extract(bad).text).toBe(bad)
  })
})

describe('MultipartFile (imgcat 3)', () => {
  const half = Math.floor(PNG.length / 2)
  const chunks = (start = `${ESC}]1337;MultipartFile=inline=1;name=${NAME}${BEL}`) =>
    `${start}${ESC}]1337;FilePart=${PNG.slice(0, half)}${BEL}${ESC}]1337;FilePart=${PNG.slice(half)}${BEL}${ESC}]1337;FileEnd${BEL}`

  test('joins the parts', () => {
    const { text, images } = extract(`x${chunks()}\n`, () => '<img>')

    expect(text).toBe('x<img>\n')
    expect(images[0]?.base64).toBe(PNG)
    expect(images[0]?.name).toBe('tiny.png')
  })

  test('leaves an image with no FileEnd alone', () => {
    const open = chunks().replace(`${ESC}]1337;FileEnd${BEL}`, '')

    expect(extract(open).images).toHaveLength(0)
  })

  test('ignores a FilePart with no MultipartFile', () => {
    expect(extract(`${ESC}]1337;FilePart=${PNG}${BEL}`).images).toHaveLength(0)
  })
})

describe('tmux passthrough', () => {
  test('unwraps a DCS and undoes the doubled ESC', () => {
    const inner = whole('inline=1')
    const wrapped = `${ESC}Ptmux;${inner.split(ESC).join(`${ESC}${ESC}`)}${ST}`

    expect(unwrapTmux(wrapped)).toBe(inner)
    expect(extract(wrapped).images).toHaveLength(1)
  })
})

describe('parseDimension', () => {
  const cases: [string | undefined, unknown][] = [
    ['auto', { unit: 'auto' }],
    [undefined, { unit: 'auto' }],
    ['10', { unit: 'cells', value: 10 }],
    ['200px', { unit: 'px', value: 200 }],
    ['50%', { unit: 'percent', value: 50 }],
    ['0', { unit: 'auto' }],
    ['wide', { unit: 'auto' }],
  ]

  for (const [raw, expected] of cases) {
    test(String(raw), () => {
      expect(parseDimension(raw)).toEqual(expected)
    })
  }
})

describe('stripBlocks', () => {
  const result = (content: unknown) => [{ type: 'tool_result', tool_use_id: 't1', content }]

  test('cuts the data out of a string result', () => {
    const [block] = stripBlocks(result(`hi\n${whole(`name=${NAME};inline=1`)}\n`))

    expect(block?.content).toBe('hi\n[inline image: tiny.png, 70 B, shown to the user]\n')
  })

  test('cuts the data out of text blocks and keeps other blocks', () => {
    const image = { type: 'image', source: {} }
    const [block] = stripBlocks(result([{ type: 'text', text: whole('inline=1') }, image]))

    expect(block?.content).toEqual([{ type: 'text', text: '[inline image: unnamed, 70 B, shown to the user]' }, image])
  })

  test('says the user saw an image whose data a size limit cut off', () => {
    const [block] = stripBlocks(result(`${ESC}]1337;File=inline=1:${PNG.slice(0, 20)}`))

    expect(block?.content).toBe('[inline image data left out here; the user saw the image]')
  })

  test('returns the same array when there is nothing to cut', () => {
    const content = result('plain output')

    expect(stripBlocks(content)).toBe(content)
  })
})

describe('untrusted input', () => {
  test('an unterminated tmux passthrough with many ESC pairs is scanned in linear time', () => {
    const hostile = `${ESC}Ptmux;${`${ESC}${ESC}Ptmux;`.repeat(40_000)}`
    const started = Date.now()

    expect(unwrapTmux(hostile)).toBe(hostile)
    expect(Date.now() - started).toBeLessThan(500)
  })

  test('a large tmux passthrough unwraps', () => {
    const inner = whole('inline=1')
    const big = `${ESC}Ptmux;${inner.split(ESC).join(`${ESC}${ESC}`)}${'x'.repeat(3_000_000)}${ST}`

    expect(unwrapTmux(big).startsWith(inner)).toBe(true)
  })

  test('a name loses control and format characters, and its length', () => {
    expect(cleanName(`a${ESC}]52;c;evil${BEL}\nb`)).toBe('a]52;c;evilb')
    expect(cleanName('right\u202Eto-left.png')).toBe('rightto-left.png')
    expect(cleanName('x'.repeat(500))).toHaveLength(120)
  })

  test('a name with an escape sequence reaches no caption or note', () => {
    const evil = btoa(`photo${ESC}]52;c;Zm9v${BEL}.png`)
    const { images, text } = extract(whole(`inline=1;name=${evil}`))

    expect(images[0]?.name).toBe('photo]52;c;Zm9v.png')
    expect(text).not.toContain(ESC)
  })

  test('an absurd size is no size', () => {
    expect(parseDimension('9'.repeat(400))).toEqual({ unit: 'auto' })
    expect(parseDimension('100001')).toEqual({ unit: 'auto' })
  })

  test('text printed between the chunks of an image is kept', () => {
    const half = Math.floor(PNG.length / 2)
    const chunked =
      `a${ESC}]1337;MultipartFile=inline=1${BEL}${ESC}]1337;FilePart=${PNG.slice(0, half)}${BEL}` +
      `50%${ESC}]1337;FilePart=${PNG.slice(half)}${BEL}100%${ESC}]1337;FileEnd${BEL}b`

    expect(extract(chunked, () => '<img>').text).toBe('a<img>50%100%b')
  })

  test('only File and MultipartFile start an image', () => {
    expect(hasImageSequence(whole('inline=1'))).toBe(true)
    expect(hasImageSequence(`${ESC}]1337;MultipartFile=inline=1${BEL}`)).toBe(true)
    expect(hasImageSequence(`${ESC}]1337;CurrentDir=/tmp${BEL}`)).toBe(false)
    expect(hasImageSequence('grep 1337 osc1337.ts')).toBe(false)
  })

  test('a download is described as one', () => {
    const [block] = stripBlocks([{ type: 'tool_result', tool_use_id: 't', content: whole(`inline=0;name=${NAME}`) }])

    expect(block?.content).toBe('[file download: tiny.png, 70 B, not shown]')
  })
})

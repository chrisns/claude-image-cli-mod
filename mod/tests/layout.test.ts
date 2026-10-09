import { describe, expect, test } from 'claude-code/testing'

import { fit } from '../hooks/layout.ts'

const auto = { width: { unit: 'auto' }, height: { unit: 'auto' }, isAspectPreserved: true } as const
const limits = { viewportColumns: 100, maxColumns: 80, maxRows: 24, cell: { width: 8, height: 16 } }

describe('fit', () => {
  test('a wide picture takes the widest allowed box and keeps its shape', () => {
    // 1600 x 800 pixels: twice as wide as tall, so 80 columns by 20 rows (cells are twice as tall as wide).
    expect(fit({ width: 1600, height: 800 }, auto, limits)).toEqual({ columns: 80, rows: 20 })
  })

  test('a tall picture is cut down by the row limit and the width follows', () => {
    const box = fit({ width: 400, height: 1600 }, auto, limits)

    expect(box.rows).toBe(24)
    expect(box.columns).toBe(12)
  })

  test('a small picture is not enlarged past its own size', () => {
    expect(fit({ width: 80, height: 32 }, auto, limits)).toEqual({ columns: 10, rows: 2 })
  })

  test('never wider than the transcript', () => {
    const box = fit({ width: 1600, height: 800 }, auto, { ...limits, viewportColumns: 40 })

    expect(box.columns).toBe(38)
  })

  test('honours a width in cells and lets the height follow', () => {
    expect(fit({ width: 400, height: 400 }, { ...auto, width: { unit: 'cells', value: 20 } }, limits)).toEqual({
      columns: 20,
      rows: 10,
    })
  })

  test('honours a width in percent of the transcript', () => {
    const box = fit({ width: 400, height: 400 }, { ...auto, width: { unit: 'percent', value: 50 } }, { ...limits, maxRows: 40 })

    expect(box.columns).toBe(49)
  })

  test('with both sides given and the aspect kept, the picture fits inside the box', () => {
    const request = { width: { unit: 'cells', value: 40 }, height: { unit: 'cells', value: 5 }, isAspectPreserved: true } as const
    const box = fit({ width: 400, height: 400 }, request, limits)

    expect(box).toEqual({ columns: 10, rows: 5 })
  })

  test('with both sides given and the aspect free, the box is used as asked', () => {
    const request = { width: { unit: 'cells', value: 40 }, height: { unit: 'cells', value: 5 }, isAspectPreserved: false } as const

    expect(fit({ width: 400, height: 400 }, request, limits)).toEqual({ columns: 40, rows: 5 })
  })

  test('follows the real shape of a cell', () => {
    // A cell 7 wide and 13 tall is not twice as tall as wide: a square picture needs more rows.
    const box = fit({ width: 400, height: 400 }, { ...auto, width: { unit: 'cells', value: 26 } }, { ...limits, cell: { width: 7, height: 13 } })

    expect(box).toEqual({ columns: 26, rows: 14 })
  })

  test('reads px sizes in the real cell size', () => {
    const box = fit({ width: 400, height: 400 }, { ...auto, width: { unit: 'px', value: 140 } }, { ...limits, cell: { width: 7, height: 14 } })

    expect(box.columns).toBe(20)
  })

  test('always at least one cell', () => {
    expect(fit({ width: 4000, height: 1 }, auto, limits).rows).toBe(1)
  })
})

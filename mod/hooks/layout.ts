import type { Dimension } from './osc1337.ts'

// What a cell is when the terminal does not say: the shape of most monospaced fonts.
export const DEFAULT_CELL: Cell = { width: 8, height: 16 }

export type Cell = { width: number; height: number }

export type Limits = {
  /** The columns the transcript has for this row. */
  viewportColumns: number
  /** The most columns a preview may take. */
  maxColumns: number
  /** The most rows a preview may take. */
  maxRows: number
  /** One cell, in pixels: only its shape and the scale of `px` sizes matter. */
  cell: Cell
}

export type Box = { columns: number; rows: number }

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high)
}

function toCells(dimension: Dimension, available: number, cellPixels: number): number | undefined {
  switch (dimension.unit) {
    case 'auto':
      return undefined
    case 'cells':
      return dimension.value
    case 'px':
      return dimension.value / cellPixels
    case 'percent':
      return (dimension.value / 100) * available
  }
}

/**
 * Work out the box of cells a preview takes.
 *
 * The sender's `width` and `height` are honoured as iTerm2 honours them: a
 * missing one follows the other, both missing means the image's own size, and
 * `preserveAspectRatio` (the default) fits the image inside the box.
 */
export function fit(
  pixels: { width: number; height: number },
  request: { width: Dimension; height: Dimension; isAspectPreserved: boolean },
  limits: Limits,
): Box {
  // The row is drawn with a two-cell gutter on the left.
  const room = Math.max(1, limits.viewportColumns - 2)
  const widest = Math.max(1, Math.min(room, limits.maxColumns))
  const tallest = Math.max(1, limits.maxRows)

  const cell = limits.cell.width > 0 && limits.cell.height > 0 ? limits.cell : DEFAULT_CELL

  // Rows per column for this image, once the cell shape is allowed for. A size
  // that makes no sense (zero, NaN) draws a square rather than nothing.
  const shape = (pixels.height / pixels.width) * (cell.width / cell.height)
  const ratio = Number.isFinite(shape) && shape > 0 ? shape : cell.width / cell.height

  const wantedColumns = toCells(request.width, room, cell.width)
  const wantedRows = toCells(request.height, tallest, cell.height)

  let columns: number
  let rows: number

  if (wantedColumns !== undefined && wantedRows !== undefined) {
    columns = wantedColumns
    rows = wantedRows

    if (request.isAspectPreserved) {
      // Shrink the larger side: the image fits inside the box.
      if (columns * ratio > rows) {
        columns = rows / ratio
      } else {
        rows = columns * ratio
      }
    }
  } else if (wantedColumns !== undefined) {
    columns = wantedColumns
    rows = columns * ratio
  } else if (wantedRows !== undefined) {
    rows = wantedRows
    columns = rows / ratio
  } else {
    columns = Number.isFinite(pixels.width) && pixels.width > 0 ? pixels.width / cell.width : widest
    rows = columns * ratio
  }

  // Cap the width, then the height, and keep the shape each time.
  if (columns > widest) {
    rows *= widest / columns
    columns = widest
  }

  if (rows > tallest) {
    columns *= tallest / rows
    rows = tallest
  }

  const whole = (value: number, high: number) => (Number.isFinite(value) ? clamp(Math.round(value), 1, high) : high)

  return {
    columns: whole(columns, widest),
    rows: whole(rows, tallest),
  }
}

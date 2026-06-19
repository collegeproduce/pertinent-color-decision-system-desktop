/**
 * Print-decisions Excel export.
 *
 * Ported from pertinent-color-export-pkg (exportPrintDecisions.ts), adapted to
 * consume this app's `documents` state directly. Per-page user overrides are
 * already baked into each page's `decision` (and flagged via `overridden`), so
 * the effective decision is simply page.decision — no separate overrides map.
 *
 * Output schema (one sheet "Print Decisions", one row per finished document):
 *   File Names | Total Pages | Status | Color page | Total B/W | Total Color
 *
 * Rules (kept verbatim from the package):
 *  - Only documents with status === 'done' are included.
 *  - Pages with no decision are "pending": excluded from both totals and the
 *    Color-page list.
 *  - "Total Color" is left blank while any page is still pending; "Total B/W"
 *    is always accurate.
 *  - Status ∈ Pending / BW / Color / Partial / Native.
 */
import * as XLSX from 'xlsx'

export const PRINT_DECISIONS_SHEET_NAME = 'Print Decisions'

export const PRINT_DECISION_HEADERS = [
  'File Names',
  'Total Pages',
  'Status',
  'Color page',
  'Total B/W',
  'Total Color',
]

/** Pure core: documents -> header row + one data row per finished document. */
export function buildPrintDecisionRows(documents) {
  const data = [[...PRINT_DECISION_HEADERS]]

  for (const doc of documents || []) {
    if (doc.status !== 'done') continue
    const pages = doc.pages || []
    if (pages.length === 0) continue

    const colorPages = []
    const bwPages = []
    const pendingPages = []

    for (const p of pages) {
      if (p.decision === 'Color') colorPages.push(p.page_id)
      else if (p.decision === 'B&W') bwPages.push(p.page_id)
      else pendingPages.push(p.page_id)
    }

    const totalColor = colorPages.length
    const totalBW = bwPages.length
    const hasPending = pendingPages.length > 0

    let status
    if (hasPending && totalColor === 0 && totalBW === 0) status = 'Pending'
    else if (totalColor === 0 && totalBW > 0) status = hasPending ? 'Partial' : 'BW'
    else if (totalColor > 0 && totalBW === 0) status = hasPending ? 'Partial' : 'Color'
    else if (totalColor > 0 && totalBW > 0) status = 'Partial'
    else status = 'Native'

    const colorPageList = colorPages.sort((a, b) => a - b).join(',')
    const totalPages = doc.total_pages ?? doc.summary?.total_pages ?? pages.length
    const totalColorCell = hasPending ? '' : totalColor

    data.push([doc.filename, totalPages, status, colorPageList, totalBW, totalColorCell])
  }

  return data
}

/** Build a styled workbook (bold header, auto-sized columns). */
export function buildPrintDecisionsWorkbook(documents) {
  const data = buildPrintDecisionRows(documents)

  const ws = XLSX.utils.aoa_to_sheet(data)
  const wb = XLSX.utils.book_new()
  XLSX.utils.book_append_sheet(wb, ws, PRINT_DECISIONS_SHEET_NAME)

  const range = XLSX.utils.decode_range(ws['!ref'] || 'A1')
  for (let col = range.s.c; col <= range.e.c; col++) {
    const address = XLSX.utils.encode_col(col) + '1'
    if (!ws[address]) continue
    ws[address].s = { font: { bold: true } }
  }

  ws['!cols'] = PRINT_DECISION_HEADERS.map((header, colIndex) => {
    let maxWidth = header.length
    for (let rowIndex = 1; rowIndex < data.length; rowIndex++) {
      const cellValue = data[rowIndex][colIndex]
      const cellLength = cellValue ? String(cellValue).length : 0
      maxWidth = Math.max(maxWidth, cellLength)
    }
    return { wch: Math.min(maxWidth + 2, 50) }
  })

  return wb
}

/** Timestamped default name, e.g. print-decisions_2026-06-19_14-30-00.xlsx */
export function defaultPrintDecisionsFilename(now = new Date()) {
  const timestamp = now
    .toISOString()
    .replace(/T/, '_')
    .replace(/\..+/, '')
    .replace(/:/g, '-')
  return `print-decisions_${timestamp}.xlsx`
}

/**
 * Browser/Electron entry point: build the workbook and trigger a download.
 * Returns the filename written, or null when there is nothing finished to export.
 */
export function exportPrintDecisionsXlsx(documents, opts = {}) {
  const ready = (documents || []).filter((d) => d.status === 'done')
  if (ready.length === 0) return null

  const wb = buildPrintDecisionsWorkbook(documents)
  const filename = opts.filename ?? defaultPrintDecisionsFilename()
  XLSX.writeFile(wb, filename)
  return filename
}

import React from 'react'
import './PageCard.css'

function PageCard({ page, onExpand }) {
  const isColor = page.decision === 'Color'
  const isPending = page.decision == null

  const handleClick = () => {
    if (isPending) return
    onExpand(page)
  }

  // Top-right word label:
  //  - color page flagged by the engine → "Review" (orange, needs a human look)
  //  - once the user has overridden it → "Color" (confirmed)
  //  - black & white → "BW"
  const stateClass = isPending ? 'pending' : isColor ? 'color' : 'bw'
  let label = null
  if (!isPending) {
    if (isColor) label = page.overridden ? 'Color' : 'Review'
    else label = 'BW'
  }
  const labelClass = isColor ? (page.overridden ? 'confirmed' : 'review') : 'bw'

  const title = isPending
    ? `Page ${page.page_id} — analysing...`
    : `Page ${page.page_id} — click to open`

  return (
    <div
      className={`page-thumb ${stateClass}`}
      onClick={handleClick}
      title={title}
    >
      <div className="thumb-image">
        {page.preview ? (
          <img src={page.preview} alt={`Page ${page.page_id}`} loading="lazy" />
        ) : (
          <div className="thumb-placeholder" />
        )}
      </div>
      {label && <span className={`page-label ${labelClass}`}>{label}</span>}
      <span className="page-num">{page.page_id}</span>
    </div>
  )
}

export default PageCard

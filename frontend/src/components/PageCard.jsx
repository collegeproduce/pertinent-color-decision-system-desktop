import React, { useState, useRef, useEffect } from 'react'
import './PageCard.css'

function PageCard({ page, onExpand }) {
  const isColor = page.decision === 'Color'
  const isPending = page.decision == null

  // A preview can 404 if it's requested before the (single-threaded) renderer
  // reaches that page. Retry a few times with a cache-bust so the thumbnail
  // fills in instead of staying blank.
  const [imgSrc, setImgSrc] = useState(page.preview)
  const retries = useRef(0)
  useEffect(() => {
    setImgSrc(page.preview)
    retries.current = 0
  }, [page.preview])

  const handleImgError = () => {
    if (!page.preview || retries.current >= 6) return
    retries.current += 1
    const base = page.preview.split('?')[0]
    setTimeout(() => setImgSrc(`${base}?r=${retries.current}`), 600 * retries.current)
  }

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
        {imgSrc ? (
          <img
            src={imgSrc}
            alt={`Page ${page.page_id}`}
            loading="lazy"
            onError={handleImgError}
          />
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

import React, { useRef } from 'react'
import './PageCard.css'

function PageCard({ page, onToggle, onExpand }) {
  const isColor = page.decision === 'Color'
  const isPending = page.decision == null
  const clickTimer = useRef(null)

  const handleClick = () => {
    if (isPending) return
    if (clickTimer.current) {
      clearTimeout(clickTimer.current)
      clickTimer.current = null
      onExpand(page)
      return
    }
    clickTimer.current = setTimeout(() => {
      clickTimer.current = null
      onToggle(page.page_id, isColor ? 'B&W' : 'Color')
    }, 220)
  }

  const handleExpandClick = (e) => {
    e.stopPropagation()
    if (isPending) return
    if (clickTimer.current) {
      clearTimeout(clickTimer.current)
      clickTimer.current = null
    }
    onExpand(page)
  }

  const stateClass = isPending ? 'pending' : isColor ? 'color' : 'bw'
  const title = isPending
    ? `Page ${page.page_id} — analysing...`
    : `Page ${page.page_id} — click to toggle, double-click to expand`

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
      {!isPending && <span className={`color-dot ${isColor ? 'color' : 'bw'}`} />}
      {page.overridden && <span className="override-dot" title="Overridden" />}
      <span className="page-num">{page.page_id}</span>
      {!isPending && (
        <button
          className="expand-btn"
          onClick={handleExpandClick}
          title="Open detail view"
        >
          ⤢
        </button>
      )}
    </div>
  )
}

export default PageCard

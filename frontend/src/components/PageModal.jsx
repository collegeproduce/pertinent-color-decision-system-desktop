import React, { useEffect } from 'react'
import './PageModal.css'

function PageModal({ page, totalPages, onClose, onToggle, onNavigate }) {
  const isColor = page.decision === 'Color'
  // A color page the engine flagged but the user hasn't confirmed yet is a
  // "review" page — it's awaiting a human B&W / Color decision.
  const needsReview = isColor && !page.overridden

  useEffect(() => {
    const handleKey = (e) => {
      if (e.key === 'Escape') onClose()
      else if (e.key === 'ArrowLeft') onNavigate(-1)
      else if (e.key === 'ArrowRight') onNavigate(1)
      else if (e.key === 'b' || e.key === 'B') onToggle(page.page_id, 'B&W')
      else if (e.key === 'c' || e.key === 'C') onToggle(page.page_id, 'Color')
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose, onNavigate, onToggle, page.page_id])

  const handleBackdropClick = (e) => {
    if (e.target === e.currentTarget) onClose()
  }

  return (
    <div className="page-modal-backdrop" onClick={handleBackdropClick}>
      <div className="page-modal">
        <button
          className="modal-nav modal-nav-prev"
          onClick={() => onNavigate(-1)}
          disabled={page.page_id <= 1}
          title="Previous page (←)"
        >
          ‹
        </button>

        <div className="modal-content">
          <div className="modal-image-wrap">
            <img src={page.preview} alt={`Page ${page.page_id}`} />
          </div>

          <div className="modal-info">
            <div className="modal-header">
              <span className="modal-page-num">
                Page {page.page_id} <span className="modal-total">/ {totalPages}</span>
              </span>
              {needsReview ? (
                <span className="modal-decision review">PENDING MANUAL REVIEW</span>
              ) : (
                <span className={`modal-decision ${isColor ? 'color' : 'bw'}`}>
                  {isColor ? 'Color' : 'B&W'}
                  {page.overridden && <span className="modal-override-tag">confirmed</span>}
                </span>
              )}
            </div>

            {needsReview && (
              <div className="modal-review-note">
                Flagged as <b>color</b> by the engine — choose B&amp;W or confirm Color below.
              </div>
            )}

            <div className="modal-detail">
              <div className="modal-detail-label">Source</div>
              <div className="modal-detail-value">{page.source}</div>
            </div>

            <div className="modal-detail">
              <div className="modal-detail-label">Reason</div>
              <div className="modal-detail-value">{page.reason}</div>
            </div>

            <div className="modal-mark">
              <div className="modal-mark-label">Mark as</div>
              <div className="modal-mark-actions">
                <button
                  className={`modal-mark-btn bw ${page.decision === 'B&W' ? 'active' : ''}`}
                  onClick={() => onToggle(page.page_id, 'B&W')}
                >
                  B&amp;W <kbd>B</kbd>
                </button>
                <button
                  className={`modal-mark-btn color ${isColor && page.overridden ? 'active' : ''}`}
                  onClick={() => onToggle(page.page_id, 'Color')}
                >
                  Color <kbd>C</kbd>
                </button>
              </div>
            </div>
          </div>
        </div>

        <button
          className="modal-nav modal-nav-next"
          onClick={() => onNavigate(1)}
          disabled={page.page_id >= totalPages}
          title="Next page (→)"
        >
          ›
        </button>

        <button className="modal-close" onClick={onClose} title="Close (Esc)">×</button>
      </div>
    </div>
  )
}

export default PageModal

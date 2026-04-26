import React, { useEffect } from 'react'
import './PageModal.css'

function PageModal({ page, totalPages, onClose, onToggle, onNavigate }) {
  const isColor = page.decision === 'Color'

  useEffect(() => {
    const handleKey = (e) => {
      if (e.key === 'Escape') onClose()
      else if (e.key === 'ArrowLeft') onNavigate(-1)
      else if (e.key === 'ArrowRight') onNavigate(1)
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose, onNavigate])

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
              <span className="modal-page-num">Page {page.page_id} <span className="modal-total">/ {totalPages}</span></span>
              <span className={`modal-decision ${isColor ? 'color' : 'bw'}`}>
                {isColor ? 'Color' : 'B&W'}
                {page.overridden && <span className="modal-override-tag">overridden</span>}
              </span>
            </div>

            <div className="modal-detail">
              <div className="modal-detail-label">Source</div>
              <div className="modal-detail-value">{page.source}</div>
            </div>

            <div className="modal-detail">
              <div className="modal-detail-label">Reason</div>
              <div className="modal-detail-value">{page.reason}</div>
            </div>

            <button
              className={`modal-toggle ${isColor ? 'to-bw' : 'to-color'}`}
              onClick={() => onToggle(page.page_id, isColor ? 'B&W' : 'Color')}
            >
              Change to {isColor ? 'B&W' : 'Color'}
            </button>
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

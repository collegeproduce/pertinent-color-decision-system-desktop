import React, { useState, useCallback } from 'react'
import axios from 'axios'
import './PageGrid.css'
import PageCard from './PageCard'
import PageModal from './PageModal'

function PageGrid({ document, onUpdate }) {
  const [filter, setFilter] = useState('all')
  const [zoom, setZoom] = useState(150)
  const [expandedPageId, setExpandedPageId] = useState(null)

  const handleOverride = useCallback(async (pageId, newDecision) => {
    try {
      const response = await axios.post(
        `/api/document/${document.doc_id}/page/${pageId}/override`,
        { decision: newDecision, reason: 'User manual override' }
      )

      const updatedPages = document.pages.map((page) =>
        page.page_id === pageId
          ? { ...page, decision: newDecision, overridden: true }
          : page
      )

      onUpdate({
        ...document,
        pages: updatedPages,
        summary: response.data.summary,
      })
    } catch (error) {
      alert('Failed to override decision')
    }
  }, [document, onUpdate])

  const filteredPages = document.pages.filter((page) => {
    if (filter === 'color') return page.decision === 'Color'
    if (filter === 'bw') return page.decision === 'B&W'
    return true
  })

  const expandedPage = expandedPageId != null
    ? document.pages.find(p => p.page_id === expandedPageId)
    : null

  const handleExpand = (page) => setExpandedPageId(page.page_id)
  const handleCloseModal = () => setExpandedPageId(null)

  const handleNavigateModal = (delta) => {
    const idx = document.pages.findIndex(p => p.page_id === expandedPageId)
    if (idx === -1) return
    const next = document.pages[idx + delta]
    if (next) setExpandedPageId(next.page_id)
  }

  return (
    <div className="page-grid-container">
      <div className="grid-toolbar">
        <div className="grid-filter-group">
          <button
            className={`filter-chip ${filter === 'all' ? 'active' : ''}`}
            onClick={() => setFilter('all')}
          >
            All <span className="chip-count">{document.pages.length}</span>
          </button>
          <button
            className={`filter-chip ${filter === 'color' ? 'active color' : ''}`}
            onClick={() => setFilter('color')}
          >
            Color <span className="chip-count">{document.summary.color_pages}</span>
          </button>
          <button
            className={`filter-chip ${filter === 'bw' ? 'active bw' : ''}`}
            onClick={() => setFilter('bw')}
          >
            B&amp;W <span className="chip-count">{document.summary.bw_pages}</span>
          </button>
        </div>

        <div className="zoom-control">
          <span className="zoom-label">Size</span>
          <input
            type="range"
            min="90"
            max="280"
            step="10"
            value={zoom}
            onChange={(e) => setZoom(Number(e.target.value))}
            className="zoom-slider"
          />
          <span className="zoom-value">{zoom}px</span>
        </div>
      </div>

      <div
        className="page-grid"
        style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${zoom}px, 1fr))` }}
      >
        {filteredPages.map((page) => (
          <PageCard
            key={page.page_id}
            page={page}
            onToggle={handleOverride}
            onExpand={handleExpand}
          />
        ))}
      </div>

      {expandedPage && (
        <PageModal
          page={expandedPage}
          totalPages={document.pages.length}
          onClose={handleCloseModal}
          onToggle={handleOverride}
          onNavigate={handleNavigateModal}
        />
      )}
    </div>
  )
}

export default PageGrid

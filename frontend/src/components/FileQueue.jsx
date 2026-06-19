import React, { useCallback, useState } from 'react'
import './FileQueue.css'

function FileQueue({ documents, onFileSelect, onFileUpload, onFileDelete, onDeleteAll, selectedDocId }) {
  const [isDragging, setIsDragging] = useState(false)

  const handleDrop = useCallback((e) => {
    e.preventDefault()
    setIsDragging(false)
    const files = Array.from(e.dataTransfer.files).filter(
      file => file.name.toLowerCase().endsWith('.pdf')
    )
    if (files.length > 0) onFileUpload(files)
  }, [onFileUpload])

  const handleDragOver = useCallback((e) => {
    e.preventDefault()
    setIsDragging(true)
  }, [])

  const handleDragLeave = useCallback((e) => {
    e.preventDefault()
    setIsDragging(false)
  }, [])

  const handleFileInput = (e) => {
    const files = Array.from(e.target.files)
    if (files.length > 0) onFileUpload(files)
    e.target.value = ''
  }

  const handleUploadClick = () => {
    document.getElementById('file-queue-input').click()
  }

  return (
    <div
      className={`file-queue-container ${isDragging ? 'dragging' : ''}`}
      onDrop={handleDrop}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
    >
      <div className="upload-bar">
        <button className="upload-box" onClick={handleUploadClick} title="Click to upload PDFs, or drag &amp; drop here">
          <span className="upload-box-icon">+</span>
          <span className="upload-box-text">Drop PDF</span>
        </button>
        {documents.length > 0 && (
          <button className="clear-all-btn" onClick={onDeleteAll} title="Delete all files">
            ×
          </button>
        )}
        <input
          id="file-queue-input"
          type="file"
          accept=".pdf"
          multiple
          onChange={handleFileInput}
          className="file-input"
        />
      </div>

      {isDragging && (
        <div className="drag-overlay">Drop PDFs to upload</div>
      )}

      <div className="docs-count">
        {documents.length} {documents.length === 1 ? 'document' : 'documents'}
      </div>

      <div className="file-list">
        {documents.length === 0 ? (
          <div className="empty-queue">
            <p>No documents yet.</p>
            <p className="empty-hint">Drop PDFs here or click Upload.</p>
          </div>
        ) : (
          documents.map((doc) => {
            const isActive = doc.doc_id === selectedDocId
            const status = doc.status || 'queued'
            const totalPages = doc.summary?.total_pages || doc.total_pages
            const isWorking = status === 'queued' || status === 'analyzing'
            return (
              <div
                key={doc.doc_id}
                className={`file-row ${isActive ? 'active' : ''} status-${status}`}
                onClick={() => onFileSelect(doc.doc_id)}
                title={status === 'error' ? (doc.error || 'error') : doc.filename}
              >
                {isWorking ? (
                  <span className="row-spinner" aria-hidden="true" />
                ) : status === 'error' ? (
                  <span className="row-error" aria-hidden="true">!</span>
                ) : null}
                <span className="file-row-name">{doc.filename}</span>
                <span className="file-row-meta">
                  {status === 'queued' ? 'queued' : status === 'analyzing' ? '…' : `${totalPages}p`}
                </span>
                <button
                  className="file-row-delete"
                  onClick={(e) => {
                    e.stopPropagation()
                    onFileDelete(doc.doc_id)
                  }}
                  title="Delete"
                >
                  ×
                </button>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}

export default FileQueue

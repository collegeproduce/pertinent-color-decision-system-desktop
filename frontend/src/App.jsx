import React, { useState, useRef, useCallback, useEffect } from 'react'
import './App.css'
import axios from 'axios'
import FileQueue from './components/FileQueue'
import PageGrid from './components/PageGrid'

const IS_ELECTRON = !!window.electronAPI
const API_BASE = IS_ELECTRON ? 'http://localhost:5000' : ''
axios.defaults.baseURL = API_BASE

// EventSource only takes absolute URLs in Electron file:// context.
const sseUrl = (path) => `${API_BASE}${path}`
// img src — relative is fine when proxied, absolute in Electron.
const previewUrl = (path) => `${API_BASE}${path}`

const buildPlaceholderPages = (docId, totalPages, renderedSet) =>
  Array.from({ length: totalPages }, (_, i) => {
    const pageId = i + 1
    return {
      page_id: pageId,
      preview: renderedSet.has(pageId)
        ? previewUrl(`/api/document/${docId}/preview/${pageId}.png`)
        : null,
      decision: null,
      source: null,
      reason: null,
      overridden: false,
    }
  })

function App() {
  const [documents, setDocuments] = useState([])
  const [selectedDocId, setSelectedDocId] = useState(null)
  const [error, setError] = useState(null)

  // Track which docs have an open SSE connection.
  const eventSources = useRef({})
  // Track rendered preview page_ids per doc, before `analyzed` arrives.
  const rendered = useRef({})

  const selectedDocument = documents.find((doc) => doc.doc_id === selectedDocId)

  const updateDoc = useCallback((docId, patch) => {
    setDocuments((prev) =>
      prev.map((doc) =>
        doc.doc_id === docId
          ? { ...doc, ...(typeof patch === 'function' ? patch(doc) : patch) }
          : doc
      )
    )
  }, [])

  const closeStream = useCallback((docId) => {
    const es = eventSources.current[docId]
    if (es) {
      es.close()
      delete eventSources.current[docId]
    }
  }, [])

  const openStream = useCallback(
    (docId) => {
      if (eventSources.current[docId]) return
      const es = new EventSource(sseUrl(`/api/document/${docId}/events`))
      eventSources.current[docId] = es

      es.addEventListener('meta', (e) => {
        const data = JSON.parse(e.data)
        updateDoc(docId, {
          status: data.status || 'analyzing',
          total_pages: data.total_pages,
        })
      })

      es.addEventListener('preview', (e) => {
        const { page_id } = JSON.parse(e.data)
        const set = rendered.current[docId] || (rendered.current[docId] = new Set())
        set.add(page_id)
        updateDoc(docId, (doc) => {
          // If decisions haven't arrived yet, refresh placeholder pages with the
          // newly available preview. After `analyzed` arrives, doc.pages is the
          // engine's authoritative list — don't overwrite.
          if (doc.status === 'done' && doc.pages?.[0]?.decision) return {}
          return {
            pages: buildPlaceholderPages(docId, doc.total_pages || 0, set),
          }
        })
      })

      es.addEventListener('analyzed', (e) => {
        const { pages, summary } = JSON.parse(e.data)
        // Rewrite preview URLs to absolute when running in Electron.
        const fixedPages = pages.map((p) => ({
          ...p,
          preview: previewUrl(p.preview),
        }))
        updateDoc(docId, {
          status: 'done',
          summary,
          pages: fixedPages,
        })
      })

      es.addEventListener('error', (e) => {
        let msg = 'processing failed'
        try {
          msg = JSON.parse(e.data).message || msg
        } catch {}
        updateDoc(docId, { status: 'error', error: msg })
        closeStream(docId)
      })

      es.addEventListener('done', () => {
        closeStream(docId)
      })

      // Network-level error (server down, CORS, etc.) — fires onerror without a
      // 'data' payload. Close the stream so we don't leak.
      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) {
          closeStream(docId)
        }
      }
    },
    [updateDoc, closeStream]
  )

  // Cleanup on unmount.
  useEffect(() => {
    return () => {
      Object.keys(eventSources.current).forEach(closeStream)
    }
  }, [closeStream])

  const handleFileUpload = async (files) => {
    setError(null)
    for (const file of files) {
      const formData = new FormData()
      formData.append('file', file)
      try {
        const response = await axios.post('/api/upload', formData, {
          headers: { 'Content-Type': 'multipart/form-data' },
        })
        const newDoc = {
          ...response.data,
          status: response.data.status || 'queued',
          pages: [],
          summary: response.data.summary || {
            total_pages: response.data.total_pages,
            color_pages: 0,
            bw_pages: 0,
            efficiency: 0,
          },
        }
        setDocuments((prev) => [...prev, newDoc])
        if (!selectedDocId) setSelectedDocId(newDoc.doc_id)
        rendered.current[newDoc.doc_id] = new Set()
        openStream(newDoc.doc_id)
      } catch (err) {
        console.error('Failed to upload:', file.name, err)
        setError(`Failed to upload ${file.name}`)
      }
    }
  }

  const handleFileSelect = (docId) => setSelectedDocId(docId)

  const handleFileDelete = async (docId) => {
    const docToDelete = documents.find((doc) => doc.doc_id === docId)
    if (!confirm(`Delete "${docToDelete?.filename}"?`)) return
    closeStream(docId)
    delete rendered.current[docId]
    try {
      await axios.delete(`/api/document/${docId}/clear`)
    } catch (err) {
      console.error('Failed to delete document:', err)
    }
    setDocuments((prev) => prev.filter((doc) => doc.doc_id !== docId))
    if (selectedDocId === docId) {
      const remaining = documents.filter((doc) => doc.doc_id !== docId)
      setSelectedDocId(remaining.length > 0 ? remaining[0].doc_id : null)
    }
  }

  const handleDeleteAll = async () => {
    if (!confirm('Delete all documents?')) return
    Object.keys(eventSources.current).forEach(closeStream)
    rendered.current = {}
    for (const doc of documents) {
      try {
        await axios.delete(`/api/document/${doc.doc_id}/clear`)
      } catch (err) {
        console.error('Failed to delete:', doc.filename)
      }
    }
    setDocuments([])
    setSelectedDocId(null)
  }

  const handleDocumentUpdate = (updatedDoc) => {
    setDocuments((prev) =>
      prev.map((doc) => (doc.doc_id === updatedDoc.doc_id ? updatedDoc : doc))
    )
  }

  const handleExportCSV = async () => {
    const ready = documents.filter((d) => d.status === 'done').map((d) => d.doc_id)
    if (ready.length === 0) {
      alert('No completed documents to export yet.')
      return
    }
    try {
      const response = await axios.post(
        '/api/export/csv',
        { doc_ids: ready, filename: 'pertinent_color_results.csv' },
        { responseType: 'blob' }
      )
      const url = window.URL.createObjectURL(new Blob([response.data]))
      const link = document.createElement('a')
      link.href = url
      link.setAttribute('download', 'pertinent_color_results.csv')
      document.body.appendChild(link)
      link.click()
      link.remove()
    } catch (err) {
      console.error('Failed to export CSV:', err)
      alert('Failed to export CSV')
    }
  }

  const summary = selectedDocument?.summary
  const isAnyProcessing = documents.some(
    (d) => d.status === 'queued' || d.status === 'analyzing'
  )

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-title">
          Pertinent Color
          {isAnyProcessing && <span className="header-spinner" />}
        </div>

        {summary && (
          <div className="header-stats">
            <span className="stat-pill">
              <b>{summary.total_pages}</b> pages
            </span>
            <span className="stat-pill color">
              <b>{summary.color_pages}</b> color
            </span>
            <span className="stat-pill bw">
              <b>{summary.bw_pages}</b> B&amp;W
            </span>
            <span className="stat-pill efficiency">
              <b>{(summary.efficiency || 0).toFixed(0)}%</b> efficiency
            </span>
          </div>
        )}

        <div className="header-actions">
          {documents.length > 0 && (
            <button className="header-btn" onClick={handleExportCSV}>
              Export CSV
            </button>
          )}
        </div>
      </header>

      <main className="app-main-layout">
        <div className="sidebar">
          <FileQueue
            documents={documents}
            selectedDocId={selectedDocId}
            onFileSelect={handleFileSelect}
            onFileUpload={handleFileUpload}
            onFileDelete={handleFileDelete}
            onDeleteAll={handleDeleteAll}
          />
        </div>

        <div className="main-panel">
          {error && (
            <div className="error-message">
              <span>!</span>
              <p>{error}</p>
            </div>
          )}

          {!selectedDocument && documents.length === 0 && (
            <div className="empty-state">
              <h2>No documents uploaded</h2>
              <p>Drop PDFs into the panel on the left to start.</p>
            </div>
          )}

          {selectedDocument && (
            <PageGrid
              key={selectedDocument.doc_id}
              document={selectedDocument}
              onUpdate={handleDocumentUpdate}
            />
          )}
        </div>
      </main>
    </div>
  )
}

export default App

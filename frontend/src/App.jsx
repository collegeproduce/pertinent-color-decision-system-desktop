import React, { useState, useRef, useCallback, useEffect } from 'react'
import './App.css'
import axios from 'axios'
import FileQueue from './components/FileQueue'
import PageGrid from './components/PageGrid'
import { exportPrintDecisionsXlsx } from './lib/exportPrintDecisions'

const IS_ELECTRON = !!window.electronAPI
const API_BASE = IS_ELECTRON ? 'http://localhost:5000' : ''
axios.defaults.baseURL = API_BASE

// EventSource only takes absolute URLs in Electron file:// context.
const sseUrl = (path) => `${API_BASE}${path}`
// img src — relative is fine when proxied, absolute in Electron.
const previewUrl = (path) => `${API_BASE}${path}`
// Append/refresh a cache-busting token so the browser re-requests an <img> that
// previously 404'd (preview rendered after the first load attempt).
let _bust = 0
const bustCache = (url) => `${url.split('?')[0]}?v=${++_bust}`

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
  // Docs that reached a terminal state (done/error) — never auto-reconnect these.
  const terminal = useRef(new Set())
  // Pending reconnect timers per doc, so we can cancel them on cleanup.
  const reopenTimers = useRef({})

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
          const previewsRendered = set.size
          // After `analyzed`, doc.pages is the engine's authoritative list. But
          // preview rendering (1 thread) lags behind analysis (8 workers), so a
          // page's thumbnail can arrive AFTER its decision. Don't drop it —
          // refresh just that one image with a cache-bust so a blank page fills
          // in instead of staying empty.
          if (doc.status === 'done' && doc.pages?.[0]?.decision) {
            return {
              previewsRendered,
              pages: doc.pages.map((p) =>
                p.page_id === page_id
                  ? { ...p, preview: bustCache(previewUrl(`/api/document/${docId}/preview/${page_id}.png`)) }
                  : p
              ),
            }
          }
          return {
            previewsRendered,
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
        terminal.current.add(docId)
        updateDoc(docId, { status: 'error', error: msg })
        closeStream(docId)
      })

      es.addEventListener('done', () => {
        terminal.current.add(docId)
        closeStream(docId)
      })

      // Network-level error (server down, CORS, sleep, backgrounding) — fires
      // onerror without a 'data' payload. EventSource sets CLOSED when it gives
      // up. If the doc hasn't finished, the analysis is still running on the
      // backend, so reconnect after a short delay — the SSE endpoint replays
      // current state on subscribe, so we catch up without losing progress.
      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) {
          closeStream(docId)
          if (!terminal.current.has(docId)) {
            clearTimeout(reopenTimers.current[docId])
            reopenTimers.current[docId] = setTimeout(() => openStream(docId), 1500)
          }
        }
      }
    },
    [updateDoc, closeStream]
  )

  // Cleanup on unmount.
  useEffect(() => {
    const timers = reopenTimers.current
    return () => {
      Object.keys(eventSources.current).forEach(closeStream)
      Object.values(timers).forEach(clearTimeout)
    }
  }, [closeStream])

  // Rehydrate previously-analysed documents on launch. The backend persists
  // finished work to disk and reloads it into its cache, so a closed/reopened
  // app (or a backend restart) no longer loses results. Opening each stream
  // replays the document's full state (pages + summary) back to us.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await axios.get('/api/documents')
        const docs = res.data?.documents || []
        if (cancelled || docs.length === 0) return
        const restored = docs.map((d) => ({
          doc_id: d.doc_id,
          filename: d.filename,
          status: d.status || 'done',
          total_pages: d.total_pages,
          summary: d.summary || {
            total_pages: d.total_pages,
            color_pages: 0,
            bw_pages: 0,
            efficiency: 0,
          },
          pages: [],
        }))
        setDocuments((prev) => {
          const have = new Set(prev.map((p) => p.doc_id))
          return [...prev, ...restored.filter((r) => !have.has(r.doc_id))]
        })
        setSelectedDocId((prev) => prev || restored[0].doc_id)
        restored.forEach((d) => {
          rendered.current[d.doc_id] = new Set()
        })
        // NOTE: we intentionally do NOT open a stream per restored doc — the
        // selected-doc effect below opens exactly one. Statuses for the rest
        // stay fresh via the poll effect.
      } catch {
        // No prior session (or backend not up yet) — start clean.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // Keep exactly ONE live SSE stream open — for the selected document only.
  // Opening a stream per document saturates the browser's ~6-connections-per-host
  // limit on bulk uploads, which starves preview-image requests (blank numbered
  // pages + a stuck spinner). The backend processes one doc at a time anyway, and
  // its SSE endpoint replays full current state on (re)subscribe, so switching the
  // single stream to whichever doc the user is viewing loses nothing.
  useEffect(() => {
    Object.keys(eventSources.current).forEach((id) => {
      if (id !== selectedDocId) closeStream(id)
    })
    if (selectedDocId) {
      terminal.current.delete(selectedDocId)
      openStream(selectedDocId)
    }
  }, [selectedDocId, openStream, closeStream])

  // Poll document statuses for the (unstreamed) sidebar while anything is still
  // processing. Cheap: one request every 2.5s, merged into status/summary only —
  // never clobbers the selected doc's SSE-driven pages.
  const anyInProgress = documents.some(
    (d) => d.status === 'queued' || d.status === 'analyzing'
  )
  useEffect(() => {
    if (!anyInProgress) return
    const tick = async () => {
      try {
        const res = await axios.get('/api/documents')
        const byId = Object.fromEntries(
          (res.data?.documents || []).map((d) => [d.doc_id, d])
        )
        setDocuments((prev) =>
          prev.map((doc) => {
            const u = byId[doc.doc_id]
            if (!u) return doc
            return {
              ...doc,
              status: u.status || doc.status,
              summary: u.summary || doc.summary,
              total_pages: u.total_pages ?? doc.total_pages,
            }
          })
        )
      } catch {
        // transient — try again next tick
      }
    }
    const iv = setInterval(tick, 2500)
    return () => clearInterval(iv)
  }, [anyInProgress])

  // When the window becomes visible again, make sure the selected doc's single
  // stream is reconnected if it dropped while hidden.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState !== 'visible') return
      if (selectedDocId && !eventSources.current[selectedDocId]) {
        terminal.current.delete(selectedDocId)
        openStream(selectedDocId)
      }
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [selectedDocId, openStream])

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
        // The single live stream is managed by the selected-doc effect; the poll
        // effect keeps non-selected docs' statuses fresh. No per-upload stream.
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
    // Client-side XLSX export (matches pertinent-color-export-pkg): one row per
    // finished document, with overrides already reflected in each page.decision.
    // Opens a native Save As dialog so the user chooses where to save.
    const ready = documents.filter((d) => d.status === 'done')
    if (ready.length === 0) {
      alert('No completed documents to export yet.')
      return
    }
    try {
      await exportPrintDecisionsXlsx(documents)
      // null return = user cancelled the Save dialog — nothing to report.
    } catch (err) {
      console.error('Failed to export:', err)
      alert('Failed to export spreadsheet')
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
              Export
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

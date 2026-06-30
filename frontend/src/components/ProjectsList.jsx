import { useState, useEffect, useCallback, useRef } from 'react'
import { api } from '../api/rapportini'

const REPORT_LABELS = {
  peve:   'Peve',
  fausto: 'Fausto',
}

const MONTH_NAMES = [
  'Gennaio','Febbraio','Marzo','Aprile','Maggio','Giugno',
  'Luglio','Agosto','Settembre','Ottobre','Novembre','Dicembre',
]

function rowKey(p) {
  return `${p.report_type}:${p.task_category}:${p.partner_name}:${p.project_name}`
}

export default function ProjectsList({ year, month, fixedType }) {
  const [localYear, setLocalYear]   = useState(year)
  const [localMonth, setLocalMonth] = useState(month)
  const [projects, setProjects]     = useState([])
  const [loading, setLoading]       = useState(false)
  const [loadError, setLoadError]   = useState(null)
  const [rowStatus, setRowStatus]       = useState({})
  // Seed badges from the last-known status (sessionStorage) so the "Stato" column
  // paints instantly on revisit; the effect below revalidates over the network.
  const [genStatus, setGenStatus]       = useState(() => api.getCachedStatus(year, month) || {})
  const [filter, setFilter]             = useState('')
  const [typeFilter, setTypeFilter]     = useState(fixedType || '')
  const [selectedProject, setSelectedProject] = useState(null)
  const [pdfStatus, setPdfStatus]       = useState('idle')
  const [pdfBlobUrl, setPdfBlobUrl]     = useState(null)
  const [pdfRefreshKey, setPdfRefreshKey] = useState(0)

  // Chat state
  const [chatHistory, setChatHistory]       = useState([])
  const [chatInput, setChatInput]           = useState('')
  const [chatLoading, setChatLoading]       = useState(false)
  const [hasModifications, setHasModifications] = useState(false)
  const chatBottomRef = useRef(null)

  // Refresh which rows already have a generated report in Supabase (drives the
  // "Stato" column). Kept separate from the project list so it can be re-pulled
  // after each generation without rebuilding the list from the CSV.
  const fetchGenStatus = useCallback(async ({ force = false } = {}) => {
    try {
      const data = await api.projectsStatus(localYear, localMonth, { force })
      // On success update badges; on failure keep whatever we already show
      // rather than blanking the whole column.
      if (data.success) setGenStatus(data.status || {})
    } catch {
      /* network error — keep prior badges */
    }
  }, [localYear, localMonth])

  const fetchProjects = useCallback(async ({ force = false } = {}) => {
    setLoading(true)
    setLoadError(null)
    try {
      const data = await api.listProjects(localYear, localMonth, { force })
      if (data.success) {
        setProjects(data.projects)
        setRowStatus({})
      } else {
        setLoadError(data.message || 'Errore nel caricamento progetti')
      }
    } catch (err) {
      setLoadError(`Errore di rete: ${err.message}`)
    } finally {
      setLoading(false)
    }
  }, [localYear, localMonth])

  // Load the list and the status independently so their latencies overlap
  // instead of stacking (status no longer waits for the project list to resolve).
  // Reseed badges from cache first so a period switch paints instantly.
  useEffect(() => {
    setGenStatus(api.getCachedStatus(localYear, localMonth) || {})
    fetchProjects()
    fetchGenStatus()
  }, [fetchProjects, fetchGenStatus, localYear, localMonth])

  function pdfUrl(p) {
    const params = new URLSearchParams({
      year: localYear, month: localMonth,
      report_type: p.report_type, task_category: p.task_category,
      partner_name: p.partner_name, project_name: p.project_name || '',
    })
    return `/api/pdf?${params}`
  }

  function zipUrl(p) {
    const params = new URLSearchParams({
      year: localYear, month: localMonth,
      report_type: p.report_type, task_category: p.task_category,
      partner_name: p.partner_name, project_name: p.project_name || '',
    })
    return `/api/rapportini/single/zip?${params}`
  }

  useEffect(() => {
    return () => { if (pdfBlobUrl) URL.revokeObjectURL(pdfBlobUrl) }
  }, [pdfBlobUrl])

  useEffect(() => {
    if (!selectedProject) {
      setPdfStatus('idle')
      setPdfBlobUrl(null)
      setChatHistory([])
      setChatInput('')
      setHasModifications(false)
      return
    }
    setPdfStatus('checking')
    setPdfBlobUrl(prev => { if (prev) URL.revokeObjectURL(prev); return null })

    const params = new URLSearchParams({
      year: localYear, month: localMonth,
      report_type: selectedProject.report_type,
      task_category: selectedProject.task_category,
      partner_name: selectedProject.partner_name,
      project_name: selectedProject.project_name || '',
      v: pdfRefreshKey,                          // cache-buster: unique URL after each edit so the browser refetches
    })
    let cancelled = false
    fetch(`/api/pdf?${params}`, { cache: 'no-store' })
      .then(async r => {
        if (cancelled) return
        if (!r.ok) { setPdfStatus('not_found'); return }
        const blob = await r.blob()
        if (cancelled) return
        setPdfBlobUrl(URL.createObjectURL(blob))
        setPdfStatus('found')
      })
      .catch(() => { if (!cancelled) setPdfStatus('error') })
    return () => { cancelled = true }
  }, [selectedProject, localYear, localMonth, pdfRefreshKey])

  useEffect(() => {
    if (!selectedProject) return
    const handler = e => { if (e.key === 'Escape') setSelectedProject(null) }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [selectedProject])

  // Auto-scroll chat to bottom
  useEffect(() => {
    chatBottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [chatHistory])

  async function handleUpdate(project) {
    const key = rowKey(project)
    setRowStatus(prev => ({ ...prev, [key]: 'running' }))
    try {
      const data = await api.generateSingle(
        localYear, localMonth,
        project.report_type,
        project.task_category,
        project.partner_name,
        project.project_name,
      )
      setRowStatus(prev => ({ ...prev, [key]: data.success ? 'success' : 'error' }))
      if (data.success) fetchGenStatus()
    } catch (err) {
      setRowStatus(prev => ({ ...prev, [key]: 'error' }))
    }
  }

  const anyRunning = Object.values(rowStatus).some(s => s === 'running')

  async function handleUpdateAll() {
    await Promise.all(filtered.map(p => handleUpdate(p)))
  }

  async function sendChatMessage() {
    if (!chatInput.trim() || chatLoading || !selectedProject) return
    const userText = chatInput.trim()

    setChatInput('')

    const newHistory = [...chatHistory, { role: 'user', text: userText }]
    setChatHistory(newHistory)
    setChatLoading(true)

    try {
      const data = await api.chatModify({
        year:          localYear,
        month:         localMonth,
        report_type:   selectedProject.report_type,
        task_category: selectedProject.task_category,
        partner_name:  selectedProject.partner_name,
        project_name:  selectedProject.project_name || '',
        message:       userText,
        history:       chatHistory,
      })

      const assistantText = data.success
        ? data.message
        : `Errore: ${data.message}`

      setChatHistory(prev => [
        ...prev,
        { role: 'assistant', text: assistantText, action: data.action, changes: data.changes || [], raw_response: data.raw_response || '' },
      ])

      if (data.success && data.pdf_refreshed) {
        setPdfRefreshKey(k => k + 1)
      }
      if (data.success && data.action === 'modify') {
        setHasModifications(true)
      }
      if (data.success && data.action === 'revert') {
        setHasModifications(false)
      }
    } catch (err) {
      setChatHistory(prev => [
        ...prev,
        { role: 'assistant', text: `Errore di rete: ${err.message}` },
      ])
    } finally {
      setChatLoading(false)
    }
  }

  async function handleRevert() {
    if (!selectedProject || chatLoading) return
    setChatLoading(true)
    try {
      const data = await api.chatRevert({
        year:          localYear,
        month:         localMonth,
        report_type:   selectedProject.report_type,
        task_category: selectedProject.task_category,
        partner_name:  selectedProject.partner_name,
        project_name:  selectedProject.project_name || '',
      })
      if (data.success) {
        setChatHistory(prev => [
          ...prev,
          { role: 'assistant', text: 'File ripristinato allo stato originale.', action: 'revert' },
        ])
        setHasModifications(false)
        setPdfRefreshKey(k => k + 1)
      } else {
        setChatHistory(prev => [
          ...prev,
          { role: 'assistant', text: `Errore nel ripristino: ${data.message}` },
        ])
      }
    } catch (err) {
      setChatHistory(prev => [
        ...prev,
        { role: 'assistant', text: `Errore di rete: ${err.message}` },
      ])
    } finally {
      setChatLoading(false)
    }
  }

  const filtered = projects.filter(p => {
    if (typeFilter && p.report_type !== typeFilter) return false
    if (!filter.trim()) return true
    const q = filter.toLowerCase()
    return (
      p.partner_name.toLowerCase().includes(q) ||
      p.project_name.toLowerCase().includes(q) ||
      REPORT_LABELS[p.report_type]?.toLowerCase().includes(q) ||
      p.task_category.toLowerCase().includes(q)
    )
  })

  return (
    <div className="projects-page">
      <div className="projects-toolbar">
        <div className="period-filter-group">
          <select
            className="period-select"
            value={localMonth}
            onChange={e => setLocalMonth(Number(e.target.value))}
          >
            {MONTH_NAMES.map((name, i) => (
              <option key={i + 1} value={i + 1}>{name}</option>
            ))}
          </select>
          <input
            className="year-input"
            type="number"
            value={localYear}
            min={2020}
            max={2099}
            onChange={e => setLocalYear(Number(e.target.value))}
          />
        </div>
        {!fixedType && (
          <div className="type-filter-group">
            {['', 'peve', 'fausto'].map(val => (
              <button
                key={val || 'all'}
                className={`btn-type-filter${typeFilter === val ? ' active' : ''}`}
                onClick={() => setTypeFilter(val)}
              >
                {val === '' ? 'Tutti' : REPORT_LABELS[val]}
              </button>
            ))}
          </div>
        )}
        <input
          className="projects-filter"
          type="text"
          placeholder="Filtra per partner, progetto…"
          value={filter}
          onChange={e => setFilter(e.target.value)}
        />
        <button
          className="btn-update-all"
          onClick={handleUpdateAll}
          disabled={loading || anyRunning || filtered.length === 0}
        >
          {anyRunning ? '⟳ In corso…' : 'Aggiorna tutti'}
        </button>
        {fixedType && (
          <a
            className="btn-update-all btn-download-link"
            href={`/api/rapportini/zip?year=${localYear}&month=${localMonth}&report_type=${fixedType}`}
            download
          >
            Scarica tutti
          </a>
        )}
        <button className="btn-reload" onClick={() => { fetchProjects({ force: true }); fetchGenStatus({ force: true }) }} disabled={loading}>
          {loading ? '⟳' : 'Ricarica lista'}
        </button>
      </div>

      {loadError && (
        <div className="projects-error">{loadError}</div>
      )}

      {!loading && !loadError && projects.length === 0 && (
        <div className="projects-empty">
          Nessun progetto trovato. Assicurati di aver scaricato i dati Odoo per il periodo selezionato.
        </div>
      )}

      {filtered.length > 0 && (
        <div className="projects-table-wrap">
          <table className="projects-table">
            <thead>
              <tr>
                {!fixedType && <th>Tipo</th>}
                <th>Categoria</th>
                <th>Partner</th>
                <th>Progetto</th>
                <th>Stato</th>
                <th>Azione</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(p => {
                const key    = rowKey(p)
                const status = rowStatus[key] ?? null
                return (
                  <tr key={key} className="project-row" onClick={() => setSelectedProject(p)}>
                    {!fixedType && <td>{REPORT_LABELS[p.report_type] ?? p.report_type}</td>}
                    <td>{p.task_category}</td>
                    <td>{p.partner_name}</td>
                    <td className="col-project">{p.project_name || '—'}</td>
                    <td>
                      {status === 'running'
                        ? <span className="badge badge--running">⟳ In corso…</span>
                        : status === 'error'
                        ? <span className="badge badge--error">✕ Errore</span>
                        : genStatus[key] === 'generated'
                        ? <span className="badge badge--success">✓ Generato</span>
                        : genStatus[key] === 'missing'
                        ? <span className="badge badge--muted">Non generato</span>
                        : null}
                    </td>
                    <td>
                      <div className="riassunti-actions">
                        <button
                          className="btn-update"
                          disabled={status === 'running'}
                          onClick={e => { e.stopPropagation(); handleUpdate(p) }}
                        >
                          {status === 'running' ? '⟳' : 'Aggiorna'}
                        </button>
                        <a
                          className="btn-update btn-download-link"
                          href={zipUrl(p)}
                          download
                          onClick={e => e.stopPropagation()}
                        >
                          Scarica
                        </a>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {selectedProject && (
        <div className="pdf-overlay" onClick={() => setSelectedProject(null)}>
          <div className="pdf-modal" onClick={e => e.stopPropagation()}>
            <div className="pdf-modal-header">
              <div className="pdf-modal-info">
                <span className={`type-chip type-chip--${selectedProject.report_type}`}>
                  {REPORT_LABELS[selectedProject.report_type]}
                </span>
                <span className="pdf-modal-partner">{selectedProject.partner_name}</span>
                {selectedProject.project_name && (
                  <span className="pdf-modal-proj">— {selectedProject.project_name}</span>
                )}
                <span className="pdf-modal-period">{MONTH_NAMES[localMonth - 1]} {localYear}</span>
              </div>
              <button className="pdf-modal-close" onClick={() => setSelectedProject(null)}>✕</button>
            </div>

            <div className="pdf-modal-body">
              {/* PDF pane */}
              <div className="pdf-pane">
                {pdfStatus === 'checking' && (
                  <div className="pdf-modal-status">Caricamento…</div>
                )}
                {pdfStatus === 'found' && pdfBlobUrl && (
                  <iframe
                    src={pdfBlobUrl}
                    className="pdf-iframe"
                    title="PDF preview"
                  />
                )}
                {(pdfStatus === 'not_found' || pdfStatus === 'error') && (
                  <div className="pdf-modal-status pdf-modal-status--error">
                    {pdfStatus === 'not_found'
                      ? 'PDF non trovato. Usa il pulsante "Aggiorna" per generarlo prima.'
                      : 'Errore durante il caricamento del PDF.'}
                  </div>
                )}
              </div>

              {/* Chat pane */}
              <div className="chat-pane">
                <div className="chat-pane-title">Chiedi a Gemini</div>
                <div className="chat-messages">
                  {chatHistory.length === 0 && (
                    <div className="chat-empty">
                      Chiedi di modificare il rapportino o fai una domanda sul suo contenuto.
                    </div>
                  )}
                  {chatHistory.map((msg, i) => (
                    <div key={i} className={`chat-msg chat-msg--${msg.role}`}>
                      {msg.role === 'assistant' && msg.action === 'modify' && (
                        <span className="chat-badge chat-badge--modified">file modificato</span>
                      )}
                      {msg.role === 'assistant' && msg.action === 'revert' && (
                        <span className="chat-badge chat-badge--reverted">file ripristinato</span>
                      )}
                      <p>{msg.text}</p>
                    </div>
                  ))}
                  {hasModifications && !chatLoading && (
                    <div className="chat-revert-row">
                      <button className="chat-revert-btn" onClick={handleRevert}>
                        Ripristina file originale
                      </button>
                    </div>
                  )}
                  {chatLoading && (
                    <div className="chat-msg chat-msg--assistant chat-msg--loading">
                      <span className="chat-spinner" />
                    </div>
                  )}
                  <div ref={chatBottomRef} />
                </div>
                <div className="chat-input-row">
                  <textarea
                    className="chat-textarea"
                    rows={2}
                    placeholder="Es: cambia la descrizione della riga 15…"
                    value={chatInput}
                    onChange={e => setChatInput(e.target.value)}
                    onKeyDown={e => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault()
                        sendChatMessage()
                      }
                    }}
                    disabled={chatLoading}
                  />
                  <button
                    className="chat-send-btn"
                    onClick={sendChatMessage}
                    disabled={chatLoading || !chatInput.trim()}
                  >
                    {chatLoading ? <span className="chat-spinner" /> : 'Invia'}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

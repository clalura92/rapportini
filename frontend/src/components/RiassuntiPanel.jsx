import { useState, useEffect, useCallback } from 'react'
import { api } from '../api/rapportini'

const MONTH_NAMES = [
  'Gennaio','Febbraio','Marzo','Aprile','Maggio','Giugno',
  'Luglio','Agosto','Settembre','Ottobre','Novembre','Dicembre',
]

function monthLabel(month) {
  const m = parseInt(month, 10)
  return MONTH_NAMES[m - 1] ?? month
}

export default function RiassuntiPanel() {
  const [files, setFiles]         = useState([])
  const [loading, setLoading]     = useState(false)
  const [error, setError]         = useState(null)
  const [rowStatus, setRowStatus] = useState({})

  const fetchFiles = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await api.listRiassunti()
      if (data.success) {
        setFiles(data.files)
      } else {
        setError(data.message || 'Errore nel caricamento')
      }
    } catch (err) {
      setError(`Errore di rete: ${err.message}`)
    } finally {
      setLoading(false)
    }
  }, [])

  async function handleRegenerate(f) {
    const key = f.filename
    setRowStatus(prev => ({ ...prev, [key]: 'running' }))
    try {
      const data = await api.generateRiassunti(f.year, f.month)
      setRowStatus(prev => ({ ...prev, [key]: data.success ? 'success' : 'error' }))
      if (data.success) fetchFiles()
    } catch {
      setRowStatus(prev => ({ ...prev, [key]: 'error' }))
    }
  }

  useEffect(() => { fetchFiles() }, [fetchFiles])

  // Group files by period
  const byPeriod = files.reduce((acc, f) => {
    if (!acc[f.period]) acc[f.period] = []
    acc[f.period].push(f)
    return acc
  }, {})

  const periods = Object.keys(byPeriod).sort().reverse()

  return (
    <div className="riassunti-panel">
      {error && (
        <div className="projects-error">{error}</div>
      )}

      {!loading && !error && files.length === 0 && (
        <div className="projects-empty">
          Nessun file trovato. Genera i riassunti dalla Dashboard.
        </div>
      )}

      {periods.map(period => {
        const [year, month] = period.split('_')
        return (
          <div key={period} className="riassunti-group">
            <div className="riassunti-group-header">
              {monthLabel(month)} {year}
            </div>
            <div className="projects-table-wrap">
              <table className="projects-table">
                <thead>
                  <tr>
                    <th>File</th>
                    <th>Dimensione</th>
                    <th>Stato</th>
                    <th>Azione</th>
                  </tr>
                </thead>
                <tbody>
                  {byPeriod[period].map(f => {
                    const status = rowStatus[f.filename] ?? null
                    return (
                      <tr key={f.filename}>
                        <td>{f.filename}</td>
                        <td className="col-project">{f.size_kb} KB</td>
                        <td>
                          {status === 'running' && <span className="badge badge--running">⟳ In corso…</span>}
                          {status === 'success' && <span className="badge badge--success">✓ Aggiornato</span>}
                          {status === 'error'   && <span className="badge badge--error">✕ Errore</span>}
                        </td>
                        <td>
                          <div className="riassunti-actions">
                            <button
                              className="btn-update"
                              disabled={status === 'running'}
                              onClick={() => handleRegenerate(f)}
                            >
                              {status === 'running' ? '⟳' : 'Aggiorna'}
                            </button>
                            <a
                              className="btn-update btn-download-link"
                              href={`/api/riassunto/file?period=${encodeURIComponent(f.period)}&filename=${encodeURIComponent(f.filename)}`}
                              download={f.filename}
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
          </div>
        )
      })}
    </div>
  )
}

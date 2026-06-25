const ACTION_LABELS = {
  download:  'Download Odoo',
  peve:      'Peve',
  fausto:    'Fausto',
  riassunti: 'Riassunti',
}

const STATUS_ICONS = {
  running: null,
  success: '✅',
  error:   '❌',
}

function formatTime(date) {
  return date.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function LogEntry({ entry }) {
  const icon = STATUS_ICONS[entry.status]

  return (
    <div className={`log-entry ${entry.status}`}>
      <div className="log-header">
        {entry.status === 'running' && <span className="spinner" style={{ width: 12, height: 12 }} />}
        {icon && <span>{icon}</span>}
        <span className="log-action-name">{ACTION_LABELS[entry.action]}</span>
        <span className="log-period">{entry.period}</span>
        <span className="log-time">{formatTime(entry.time)}</span>
      </div>
      <div className="log-message">{entry.message}</div>
      {entry.outputPath && (
        <div className="log-path">📁 {entry.outputPath}</div>
      )}
    </div>
  )
}

export default function ActivityLog({ entries, onClear }) {
  return (
    <div className="card">
      <div className="card-title">Log operazioni</div>

      {entries.length === 0 ? (
        <p className="log-empty">Nessuna operazione eseguita.</p>
      ) : (
        <>
          <div className="log-list">
            {entries.map(e => <LogEntry key={e.id} entry={e} />)}
          </div>
          <button className="log-clear-btn" onClick={onClear}>
            Cancella log
          </button>
        </>
      )}
    </div>
  )
}

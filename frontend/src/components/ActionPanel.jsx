const ACTION_META = {
  peve:      { label: 'Rapportini Peve',    icon: '📄', cls: 'btn-peve'      },
  fausto:    { label: 'Rapportini Fausto',  icon: '📄', cls: 'btn-fausto'    },
  riassunti: { label: 'Riassunti',          icon: '📊', cls: 'btn-riassunti' },
}

function ActionButton({ action, busy, lastStatus, onClick }) {
  const { label, icon, cls } = ACTION_META[action]
  const isRunning = busy === action

  return (
    <button
      className={`action-btn ${cls}`}
      onClick={() => onClick(action)}
      disabled={busy !== null}
    >
      {isRunning
        ? <span className="spinner" />
        : <span className="btn-icon">{icon}</span>
      }
      <span className="btn-label">{label}</span>
      {lastStatus && !isRunning && (
        <span className={`btn-status-badge badge-${lastStatus}`}>
          {lastStatus === 'success' ? '✓' : '✕'}
        </span>
      )}
    </button>
  )
}

export default function ActionPanel({ busy, lastResults, onAction }) {
  return (
    <div className="card">
      <div className="card-title">Genera rapportini</div>
      <div className="actions">
        {['peve', 'fausto', 'riassunti'].map(action => (
          <ActionButton
            key={action}
            action={action}
            busy={busy}
            lastStatus={lastResults[action]}
            onClick={onAction}
          />
        ))}
      </div>
    </div>
  )
}

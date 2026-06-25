const MONTH_NAMES = [
  'Gennaio','Febbraio','Marzo','Aprile','Maggio','Giugno',
  'Luglio','Agosto','Settembre','Ottobre','Novembre','Dicembre',
]

export default function PeriodSelector({ year, month, onYearChange, onMonthChange }) {
  const now = new Date()
  const isCurrentMonth = year === now.getFullYear() && month === now.getMonth() + 1

  function setCurrentMonth() {
    onYearChange(now.getFullYear())
    onMonthChange(now.getMonth() + 1)
  }

  return (
    <div className="card">
      <div className="card-title">Periodo di riferimento</div>
      <div className="period-row">
        <div className="field">
          <label className="field-label">Anno</label>
          <input
            type="number"
            value={year}
            min={2020}
            max={2099}
            onChange={e => onYearChange(Number(e.target.value))}
          />
        </div>
        <div className="field">
          <label className="field-label">Mese</label>
          <input
            type="number"
            value={month}
            min={1}
            max={12}
            onChange={e => onMonthChange(Number(e.target.value))}
          />
        </div>
        {!isCurrentMonth && (
          <button className="btn-ghost" onClick={setCurrentMonth} title="Vai al mese corrente">
            Oggi
          </button>
        )}
      </div>
      <p className="period-display">
        Periodo selezionato: <strong>{MONTH_NAMES[month - 1]} {year}</strong>
      </p>
    </div>
  )
}

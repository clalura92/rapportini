import { useState, useEffect, useRef } from 'react'
import { api } from '../api/rapportini'

// "Example" tab: three filters (year-month, employee, partner) whose option
// lists are queried LIVE from Odoo and cascade — picking a value in one filter
// narrows the options of the other two. The table below shows the matching
// account.analytic.line timesheet entries, also fetched live.
export default function ExamplePanel() {
  const [yearMonth, setYearMonth]   = useState('')
  const [employeeId, setEmployeeId] = useState('')
  const [partnerId, setPartnerId]   = useState('')

  const [options, setOptions] = useState({ year_months: [], employees: [], partners: [] })
  const [entries, setEntries] = useState([])
  const [truncated, setTruncated] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState(null)

  // Debounce + ignore stale responses: each effect run bumps a token; only the
  // latest run is allowed to commit results.
  const reqToken = useRef(0)

  useEffect(() => {
    const token = ++reqToken.current
    const params = { year_month: yearMonth, employee_id: employeeId, partner_id: partnerId }

    const timer = setTimeout(async () => {
      setLoading(true)
      setError(null)
      try {
        const [opts, ts] = await Promise.all([
          api.exampleOptions(params),
          api.exampleTimesheets(params),
        ])
        if (token !== reqToken.current) return  // a newer request superseded us
        if (!opts.success) { setError(opts.message || 'Errore nel caricamento dei filtri'); return }
        if (!ts.success)   { setError(ts.message   || 'Errore nel caricamento delle voci'); return }

        setOptions({
          year_months: opts.year_months || [],
          employees:   opts.employees   || [],
          partners:    opts.partners    || [],
        })
        setEntries(ts.entries || [])
        setTruncated(Boolean(ts.truncated))
      } catch (err) {
        if (token === reqToken.current) setError(`Errore di rete: ${err.message}`)
      } finally {
        if (token === reqToken.current) setLoading(false)
      }
    }, 250)

    return () => clearTimeout(timer)
  }, [yearMonth, employeeId, partnerId])

  // If the current selection vanished from its refreshed option list, reset it
  // to "All" to avoid showing empty results with a stale, invisible filter.
  useEffect(() => {
    if (yearMonth && !options.year_months.some(o => o.value === yearMonth)) setYearMonth('')
  }, [options.year_months]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (employeeId && !options.employees.some(o => String(o.id) === employeeId)) setEmployeeId('')
  }, [options.employees]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (partnerId && !options.partners.some(o => String(o.id) === partnerId)) setPartnerId('')
  }, [options.partners]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="example-panel">
      <p className="example-hint">
        I filtri sono interrogati in tempo reale da Odoo: selezionando un valore,
        le altre liste mostrano solo le opzioni compatibili.
      </p>

      <div className="projects-toolbar">
        <select
          className="period-select"
          value={yearMonth}
          onChange={e => setYearMonth(e.target.value)}
        >
          <option value="">Tutti i mesi</option>
          {options.year_months.map(o => (
            <option key={o.value} value={o.value}>{o.label} ({o.count})</option>
          ))}
        </select>

        <select
          className="period-select"
          value={employeeId}
          onChange={e => setEmployeeId(e.target.value)}
        >
          <option value="">Tutti i dipendenti</option>
          {options.employees.map(o => (
            <option key={o.id} value={String(o.id)}>{o.name} ({o.count})</option>
          ))}
        </select>

        <select
          className="period-select"
          value={partnerId}
          onChange={e => setPartnerId(e.target.value)}
        >
          <option value="">Tutti i partner</option>
          {options.partners.map(o => (
            <option key={o.id} value={String(o.id)}>{o.name} ({o.count})</option>
          ))}
        </select>

        {loading && <span className="example-loading">Caricamento…</span>}
      </div>

      {error && <div className="projects-error">{error}</div>}

      {truncated && !error && (
        <div className="example-note">
          Mostrate le prime {entries.length} voci. Affina i filtri per vedere il resto.
        </div>
      )}

      {!loading && !error && entries.length === 0 && (
        <div className="projects-empty">Nessuna voce trovata per i filtri selezionati.</div>
      )}

      {entries.length > 0 && (
        <div className="projects-table-wrap">
          <table className="projects-table">
            <thead>
              <tr>
                <th>Data</th>
                <th>Dipendente</th>
                <th>Partner</th>
                <th>Ore</th>
                <th>Task</th>
                <th>Progetto</th>
                <th>Descrizione</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e, i) => (
                <tr key={i}>
                  <td>{e.date}</td>
                  <td>{e.employee_name || '—'}</td>
                  <td>{e.partner_name || '—'}</td>
                  <td>{e.hours}</td>
                  <td>{e.task_name || '—'}</td>
                  <td className="col-project">{e.project_name || '—'}</td>
                  <td>{e.description || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

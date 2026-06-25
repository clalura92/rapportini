import { useState, useCallback } from 'react'
import { api } from './api/rapportini'
import Header from './components/Header'
import PeriodSelector from './components/PeriodSelector'
import ActionPanel from './components/ActionPanel'
import ActivityLog from './components/ActivityLog'
import ProjectsList from './components/ProjectsList'
import RiassuntiPanel from './components/RiassuntiPanel'

const now = new Date()
let nextId = 1

const ACTION_LABELS = {
  download:  'Download da Odoo',
  peve:      'Generazione rapportini Peve',
  fausto:    'Generazione rapportini Fausto',
  riassunti: 'Generazione riassunti',
}

export default function App() {
  const [year, setYear]       = useState(now.getFullYear())
  const [month, setMonth]     = useState(now.getMonth() + 1)
  const [busy, setBusy]       = useState(null)
  const [log, setLog]         = useState([])
  const [lastResults, setLastResults] = useState({})
  const [activeTab, setActiveTab] = useState('dashboard')
  const [projectsSubTab, setProjectsSubTab] = useState('riassunto')

  const addLog = useCallback((entry) => {
    setLog(prev => [{ id: nextId++, time: new Date(), ...entry }, ...prev])
  }, [])

  const updateLastLog = useCallback((patch) => {
    setLog(prev => {
      if (!prev.length) return prev
      const updated = [...prev]
      updated[0] = { ...updated[0], ...patch }
      return updated
    })
  }, [])

  async function runAction(action) {
    const period = `${year}-${String(month).padStart(2, '0')}`

    setBusy(action)
    addLog({ action, status: 'running', message: `${ACTION_LABELS[action]} in corso per ${period}…`, period })

    const apiCall = {
      download:   () => api.download(year, month),
      peve:       () => api.generatePeve(year, month),
      fausto:     () => api.generateFausto(year, month),
      riassunti:  () => api.generateRiassunti(year, month),
    }

    try {
      const data = await apiCall[action]()
      const status = data.success ? 'success' : 'error'
      updateLastLog({ status, message: data.message, outputPath: data.output_path ?? null })
      setLastResults(prev => ({ ...prev, [action]: status }))
    } catch (err) {
      updateLastLog({ status: 'error', message: `Errore di rete: ${err.message}` })
      setLastResults(prev => ({ ...prev, [action]: 'error' }))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="app-wrapper">
      <Header year={year} month={month} />

      <div className="tab-bar">
        <button
          className={`tab-btn${activeTab === 'dashboard' ? ' tab-btn--active' : ''}`}
          onClick={() => setActiveTab('dashboard')}
        >
          Dashboard
        </button>
        <button
          className={`tab-btn${activeTab === 'projects' ? ' tab-btn--active' : ''}`}
          onClick={() => setActiveTab('projects')}
        >
          Progetti
        </button>
      </div>

      {activeTab === 'dashboard' && (
        <div className="main-grid">
          <div className="left-col">
            <PeriodSelector
              year={year}
              month={month}
              onYearChange={setYear}
              onMonthChange={setMonth}
            />
            <ActionPanel
              busy={busy}
              lastResults={lastResults}
              onAction={runAction}
            />
          </div>

          <div className="right-col">
            <ActivityLog
              entries={log}
              onClear={() => setLog([])}
            />
          </div>
        </div>
      )}

      {activeTab === 'projects' && (
        <>
          <div className="subtab-bar">
            <button
              className={`subtab-btn${projectsSubTab === 'riassunto' ? ' subtab-btn--active' : ''}`}
              onClick={() => setProjectsSubTab('riassunto')}
            >
              Riassunto
            </button>
            <button
              className={`subtab-btn${projectsSubTab === 'fausto' ? ' subtab-btn--active' : ''}`}
              onClick={() => setProjectsSubTab('fausto')}
            >
              Rapportini Fausto
            </button>
            <button
              className={`subtab-btn${projectsSubTab === 'peve' ? ' subtab-btn--active' : ''}`}
              onClick={() => setProjectsSubTab('peve')}
            >
              Rapportini Peve
            </button>
          </div>
          {projectsSubTab === 'riassunto' && <RiassuntiPanel year={year} month={month} />}
          {projectsSubTab === 'fausto'    && <ProjectsList year={year} month={month} fixedType="fausto" />}
          {projectsSubTab === 'peve'      && <ProjectsList year={year} month={month} fixedType="peve" />}
        </>
      )}
    </div>
  )
}

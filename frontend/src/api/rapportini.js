async function post(path, body) {
  const res = await fetch(`/api${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  // 500s come back as JSON with {success: false, message: ...}
  return res.json()
}

// In-memory prefetch cache for the projects list, keyed by `${year}-${month}`.
// The dashboard warms this on load (see App.jsx) so switching to the Progetti
// tab resolves instantly with no network round-trip. The list only changes when
// the month is re-downloaded, so we keep entries until explicitly invalidated.
const _projectsCache = new Map()
const _projectsKey = (year, month) => `${year}-${month}`

function _fetchProjects(year, month) {
  return fetch(`/api/projects?year=${year}&month=${month}`)
    .then(r => r.json())
    .then(data => {
      // Don't cache failures, so a transient error can be retried.
      if (!data || !data.success) _projectsCache.delete(_projectsKey(year, month))
      return data
    })
    .catch(err => {
      _projectsCache.delete(_projectsKey(year, month))
      throw err
    })
}

export const api = {
  download:           (year, month) => post('/download',             { year, month }),
  generatePeve:       (year, month) => post('/generate/peve',        { year, month }),
  generateFausto:     (year, month) => post('/generate/fausto',      { year, month }),
  generateRiassunti:  (year, month) => post('/generate/riassunti',   { year, month }),
  listProjects:       (year, month, { force = false } = {}) => {
    const key = _projectsKey(year, month)
    if (force) _projectsCache.delete(key)
    if (!_projectsCache.has(key)) _projectsCache.set(key, _fetchProjects(year, month))
    return _projectsCache.get(key)
  },
  // Fire-and-forget warm-up for a period; safe to call repeatedly.
  prefetchProjects:   (year, month) => { api.listProjects(year, month).catch(() => {}) },
  invalidateProjects: (year, month) => _projectsCache.delete(_projectsKey(year, month)),
  listRiassunti:      ()            => fetch('/api/list/riassunti').then(r => r.json()),
  generateSingle:     (year, month, reportType, taskCategory, partnerName, projectName) =>
                        post('/generate/single', {
                          year, month,
                          report_type:   reportType,
                          task_category: taskCategory,
                          partner_name:  partnerName,
                          project_name:  projectName,
                        }),
  chatModify:         (body) => post('/chat/modify', body),
  chatRevert:         (body) => post('/chat/revert', body),
  // Example tab — live cascading Odoo filters. `p = { year_month, employee_id, partner_id }`
  // (empty strings = "All"); URLSearchParams drops empties when stringified below.
  exampleOptions:     (p) => fetch(`/api/example/options?${new URLSearchParams(p)}`).then(r => r.json()),
  exampleTimesheets:  (p) => fetch(`/api/example/timesheets?${new URLSearchParams(p)}`).then(r => r.json()),
}

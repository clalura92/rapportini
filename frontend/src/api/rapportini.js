async function post(path, body) {
  const res = await fetch(`/api${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  // 500s come back as JSON with {success: false, message: ...}
  return res.json()
}

// POST to an SSE endpoint that streams live progress. Each `progress` event
// invokes onProgress(message) so the caller can show "what's happening"; the
// final `done` event is returned as the result {success, message, output_path?}.
async function postStream(path, body, onProgress) {
  const res = await fetch(`/api${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.body) return res.json()  // no stream support → fall back to plain JSON

  const reader  = res.body.getReader()
  const decoder = new TextDecoder()
  let buf  = ''
  let done = null

  while (true) {
    const { value, done: streamDone } = await reader.read()
    if (streamDone) break
    buf += decoder.decode(value, { stream: true })
    // SSE frames are separated by a blank line.
    let sep
    while ((sep = buf.indexOf('\n\n')) !== -1) {
      const frame = buf.slice(0, sep)
      buf = buf.slice(sep + 2)
      const dataLine = frame.split('\n').find(l => l.startsWith('data:'))
      if (!dataLine) continue
      let payload
      try { payload = JSON.parse(dataLine.slice(5).trim()) } catch { continue }
      if (payload.type === 'progress') onProgress?.(payload.message)
      else if (payload.type === 'done') done = payload
    }
  }
  return done ?? { success: false, message: 'Stream interrotto prima del completamento' }
}

// In-memory prefetch cache for the projects list, keyed by `${year}-${month}`.
// The dashboard warms this on load (see App.jsx) so switching to the Progetti
// tab resolves instantly with no network round-trip. The list only changes when
// the month is re-downloaded, so we keep entries until explicitly invalidated.
const _projectsCache = new Map()
const _projectsKey = (year, month) => `${year}-${month}`

function _fetchProjects(year, month, force = false) {
  const qs = force ? '&force=1' : ''
  return fetch(`/api/projects?year=${year}&month=${month}${qs}`)
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
  download:           (year, month, onProgress) => postStream('/download',           { year, month }, onProgress),
  generatePeve:       (year, month, onProgress) => postStream('/generate/peve',       { year, month }, onProgress),
  generateFausto:     (year, month, onProgress) => postStream('/generate/fausto',     { year, month }, onProgress),
  generateRiassunti:  (year, month, onProgress) => postStream('/generate/riassunti',  { year, month }, onProgress),
  listProjects:       (year, month, { force = false } = {}) => {
    const key = _projectsKey(year, month)
    if (force) _projectsCache.delete(key)
    // When forcing, also tell the backend to bypass its own in-memory cache and
    // rebuild from the CSV — otherwise "Ricarica lista" only clears the frontend
    // copy and the server hands back the same stale list.
    if (!_projectsCache.has(key)) _projectsCache.set(key, _fetchProjects(year, month, force))
    return _projectsCache.get(key)
  },
  // Fire-and-forget warm-up for a period; safe to call repeatedly.
  prefetchProjects:   (year, month) => { api.listProjects(year, month).catch(() => {}) },
  // Which project rows actually have a generated report in Supabase. Not cached:
  // status changes every time a report is (re)generated.
  projectsStatus:     (year, month) =>
                        fetch(`/api/projects/status?year=${year}&month=${month}`).then(r => r.json()),
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

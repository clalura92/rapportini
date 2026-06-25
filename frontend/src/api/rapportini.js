async function post(path, body) {
  const res = await fetch(`/api${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  // 500s come back as JSON with {success: false, message: ...}
  return res.json()
}

export const api = {
  download:           (year, month) => post('/download',             { year, month }),
  generatePeve:       (year, month) => post('/generate/peve',        { year, month }),
  generateFausto:     (year, month) => post('/generate/fausto',      { year, month }),
  generateRiassunti:  (year, month) => post('/generate/riassunti',   { year, month }),
  listProjects:       (year, month) => fetch(`/api/projects?year=${year}&month=${month}`).then(r => r.json()),
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
}

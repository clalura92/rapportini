"""Live Odoo querying for the "Example" tab.

Showcases cascading filters: the option list for each filter is computed from a
domain built from the *other two* selections (so a filter never constrains
itself and can re-widen). Backed by Odoo `read_group` / `search_read` on
`account.analytic.line` — no CSV cache, intentionally live.
"""
import calendar
import xmlrpc.client as xmlrpc_client

# Base domain shared by every query — matches the existing extraction in
# download_from_odoo.py (only real, recent timesheet lines).
BASE_DOMAIN = [
    ['is_timesheet', '=', True],
    ['id', '>', 40000],
]

MONTHS_IT = [
    '', 'Gennaio', 'Febbraio', 'Marzo', 'Aprile', 'Maggio', 'Giugno',
    'Luglio', 'Agosto', 'Settembre', 'Ottobre', 'Novembre', 'Dicembre',
]


def connect(url, db, username, password):
    """Authenticate against Odoo and return (models_proxy, uid)."""
    common = xmlrpc_client.ServerProxy(f'{url}/xmlrpc/2/common')
    uid = common.authenticate(db, username, password, {})
    models = xmlrpc_client.ServerProxy(f'{url}/xmlrpc/2/object')
    return models, uid


def _date_range(year_month):
    """'YYYY-MM' -> (first_day, last_day) as 'YYYY-MM-DD' strings."""
    year, month = (int(p) for p in year_month.split('-'))
    last_day = calendar.monthrange(year, month)[1]
    return f'{year}-{month:02d}-01', f'{year}-{month:02d}-{last_day:02d}'


def build_domain(year_month=None, employee_id=None, partner_id=None):
    """Compose a search domain from the active filter selections.

    Any falsy argument is treated as "All" and omitted.
    """
    domain = list(BASE_DOMAIN)
    if year_month:
        date_from, date_to = _date_range(year_month)
        domain.append(['date', '>=', date_from])
        domain.append(['date', '<=', date_to])
    if employee_id:
        domain.append(['employee_id', '=', int(employee_id)])
    if partner_id:
        domain.append(['partner_id', '=', int(partner_id)])
    return domain


def _m2o(value):
    """Odoo many2one values come back as [id, "Name"] (or False)."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value[0], value[1]
    return None, ''


def _read_group(models, db, uid, password, domain, fields, groupby):
    return models.execute_kw(
        db, uid, password, 'account.analytic.line', 'read_group',
        [domain, fields, groupby], {'lazy': False})


def _m2o_options(models, db, uid, password, domain, field):
    """Distinct [id, name] options + counts for a many2one field."""
    rows = _read_group(models, db, uid, password, domain, [field], [field])
    out = []
    for row in rows:
        oid, name = _m2o(row.get(field))
        if oid is None:
            continue
        out.append({'id': oid, 'name': name, 'count': row.get('__count', 0)})
    out.sort(key=lambda o: o['name'].lower())
    return out


def _year_month_options(models, db, uid, password, domain):
    """Distinct year-months + counts, derived from the date field."""
    rows = _read_group(models, db, uid, password, domain, ['date'], ['date:month'])
    out = []
    for row in rows:
        value = None
        rng = row.get('__range') or {}
        date_rng = rng.get('date:month') or rng.get('date')
        if date_rng and date_rng.get('from'):
            value = date_rng['from'][:7]  # 'YYYY-MM-DD' -> 'YYYY-MM'
        else:
            # Fallback: parse the locale label e.g. "June 2026".
            label = row.get('date:month')
            if isinstance(label, str) and label.strip():
                value = label  # leave as-is; cannot reliably reformat
        if not value:
            continue
        if len(value) == 7 and value[4] == '-':
            y, m = value.split('-')
            label = f'{MONTHS_IT[int(m)]} {y}'
        else:
            label = value
        out.append({'value': value, 'label': label, 'count': row.get('__count', 0)})
    out.sort(key=lambda o: o['value'], reverse=True)
    return out


def get_options(creds, year_month=None, employee_id=None, partner_id=None):
    """Return cascading option lists for the three filters.

    Each filter's domain excludes its OWN current selection so it keeps showing
    all values reachable under the other two filters.
    """
    url, db, username, password = creds
    models, uid = connect(url, db, username, password)

    employees = _m2o_options(
        models, db, uid, password,
        build_domain(year_month=year_month, partner_id=partner_id),
        'employee_id')
    partners = _m2o_options(
        models, db, uid, password,
        build_domain(year_month=year_month, employee_id=employee_id),
        'partner_id')
    year_months = _year_month_options(
        models, db, uid, password,
        build_domain(employee_id=employee_id, partner_id=partner_id))

    return {'year_months': year_months, 'employees': employees, 'partners': partners}


def get_timesheets(creds, year_month=None, employee_id=None, partner_id=None, limit=500):
    """Return timesheet entries matching all three filters (live)."""
    url, db, username, password = creds
    models, uid = connect(url, db, username, password)

    domain = build_domain(year_month=year_month,
                          employee_id=employee_id, partner_id=partner_id)
    fields = ['date', 'employee_id', 'partner_id', 'duration_unit_amount',
              'task_name', 'project_id', 'x_studio_titolo']
    rows = models.execute_kw(
        db, uid, password, 'account.analytic.line', 'search_read',
        [domain], {'fields': fields, 'order': 'date desc', 'limit': limit})

    entries = []
    for r in rows:
        _, emp_name = _m2o(r.get('employee_id'))
        _, partner_name = _m2o(r.get('partner_id'))
        _, project_name = _m2o(r.get('project_id'))
        entries.append({
            'date': r.get('date') or '',
            'employee_name': emp_name,
            'partner_name': partner_name,
            'hours': r.get('duration_unit_amount') or 0,
            'task_name': r.get('task_name') or '',
            'project_name': project_name,
            'description': r.get('x_studio_titolo') or '',
        })
    return entries, len(entries) >= limit

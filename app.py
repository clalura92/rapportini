import os
import re
import sys
import json
import queue
import tempfile
import threading
import traceback
import subprocess
from datetime import datetime

import pandas as pd
import io
import zipfile

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file, send_from_directory, Response

from storage import download_to_bytes, list_prefix, to_storage_key
import report_config as cfg

import memlog
memlog.init('web')

load_dotenv()

# In dev: Vite runs on :5173 and proxies /api/* here.
# In prod: `npm run build` outputs to frontend_dist/; Flask serves it.
_ROOT = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIST = os.path.join(_ROOT, 'frontend_dist')
_static = FRONTEND_DIST if os.path.isdir(FRONTEND_DIST) else os.path.join(_ROOT, 'static')

app = Flask(__name__, static_folder=_static)

# ── Odoo credentials ──────────────────────────────────────────────────────────
ODOO_URL      = os.environ.get('ODOO_URL',      'https://solware.odoo.com')
ODOO_DB       = os.environ.get('ODOO_DB',       'dueesseti-solware1-main-7378424')
ODOO_USERNAME = os.environ.get('ODOO_USERNAME', 'fausto.luraschi@solware.it')
ODOO_PASSWORD = os.environ.get('ODOO_PASSWORD', 'Fausto@6148')

# ── Paths & business rules ────────────────────────────────────────────────────
# Live in report_config so the generation subprocess (generate_worker.py) shares
# them without importing — and thereby booting — this whole Flask app.
EXPORT_PATH      = cfg.EXPORT_PATH
OUTPUT_PEVE      = cfg.OUTPUT_PEVE
OUTPUT_FAUSTO    = cfg.OUTPUT_FAUSTO
OUTPUT_RIASSUNTO = cfg.OUTPUT_RIASSUNTO

FAUSTO_ELIGIBILITY_RULES   = cfg.FAUSTO_ELIGIBILITY_RULES
FAUSTO_TO_ISOLATE_LIST     = cfg.FAUSTO_TO_ISOLATE_LIST
FAUSTO_DICT_PARTNER_RENAME = cfg.FAUSTO_DICT_PARTNER_RENAME
PEVE_ELIGIBILITY_RULES     = cfg.PEVE_ELIGIBILITY_RULES
PEVE_TO_ISOLATE_LIST       = cfg.PEVE_TO_ISOLATE_LIST
PEVE_DICT_PARTNER_RENAME   = cfg.PEVE_DICT_PARTNER_RENAME
FILTERED_PARTNERS          = cfg.FILTERED_PARTNERS

_ensure_csv_local = cfg.ensure_csv_local


def _get_year_month(data):
    now = datetime.now()
    year = str(data.get('year', now.year))
    month = str(data.get('month', now.month))
    return year, month


# ── Projects cache ────────────────────────────────────────────────────────────
# /api/projects parses the (potentially large) Odoo CSV, which only changes when
# the month is re-downloaded from Odoo. Rather than parse on first request, we
# build the result eagerly — at worker startup for any CSV already on disk, and
# again right after each download — keyed by (year, month) in memory.
_PROJECTS_CACHE = {}


def _build_projects(year, month):
    """Build the combined Peve + Fausto project list IN THE SUBPROCESS.

    Listing projects parses the CSV with generazione_rapportini_*, which import
    Spire/fitz and load the .NET CLR. Importing those here would pin ~100MB in
    the long-lived web worker forever. Instead we run it as a 'projects' job in
    the short-lived child (which exits and releases everything) and keep only
    the small JSON list in web memory."""
    result = _run_worker_blocking({'kind': 'projects',
                                   'year': str(year), 'month': str(month)})
    if not result.get('success'):
        raise RuntimeError(result.get('error', 'projects build failed'))
    return (result.get('payload') or {}).get('projects', [])


def _warm_projects_cache(year, month):
    """Eagerly (re)build and store the projects list for a month, swallowing
    errors so a bad/missing CSV never crashes startup or a download."""
    try:
        _PROJECTS_CACHE[(str(year), str(month))] = _build_projects(year, month)
    except Exception:
        _PROJECTS_CACHE.pop((str(year), str(month)), None)


# NOTE: we deliberately do NOT warm the cache at boot anymore. Eager warming
# spawned a heavy CSV parse the moment the worker started; now the cache is
# built lazily on the first /api/projects request (and refreshed after each
# download), so the web worker boots light and stays light.


# ── Live progress streaming ───────────────────────────────────────────────────
# The generation scripts report progress through plain print() calls. To surface
# "what's happening" in the UI's Log operazioni we run the (blocking) script in a
# worker thread, capture its stdout, and stream each meaningful line to the
# browser as a Server-Sent Event. The final event carries the JSON result.

# Generation scripts mark per-project progress with a sentinel line so the UI can
# show a clean "Elaborazione 5/15 — …" counter instead of every single print().
# Format: "@@PROGRESS@@␟{done}␟{total}␟{label}" (␟ = unit separator, U+001F).
PROGRESS_PREFIX = '@@PROGRESS@@'
PROGRESS_SEP = '\x1f'


def _format_progress(line):
    """Translate a sentinel progress line into a human-readable message, or
    return None when `line` is not a progress sentinel."""
    if not line.startswith(PROGRESS_PREFIX):
        return None
    parts = line.split(PROGRESS_SEP)
    if len(parts) < 4:
        return None
    done, total, label = parts[1], parts[2], parts[3]
    return f'Elaborazione {done}/{total} — {label}'


def _is_progress_noise(line):
    """Drop debug chatter and the glob/dir/shape dumps the user doesn't want
    (those are the 'list of all files' lines), keeping only meaningful steps."""
    l = line.strip()
    if not l:
        return True
    if l.startswith(('[', '(', '{')):        # glob lists, dir listings, df.shape tuples
        return True
    if l.startswith('DEBUG'):
        return True
    if l.startswith('     ... file_path'):
        return True
    if l in ('sheet removed', 'sheet saved', 'sheet closed'):
        return True
    return False


def _sse(payload):
    return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'


def _stream_script(run_fn, progress_only=False):
    """Run `run_fn()` (which returns the success payload dict) in a thread while
    streaming its print() output as SSE 'progress' events, then a final 'done'
    event with {success, message, output_path?}.

    When `progress_only` is True only the structured progress counter is
    surfaced (every other print is hidden) — used by the generation endpoints so
    the Log operazioni tracks "Elaborazione N/M" rather than each single line."""
    q = queue.Queue()
    _DONE = object()
    result = {}

    class _LineWriter(io.TextIOBase):
        def __init__(self):
            self._buf = ''
        def write(self, s):
            self._buf += s
            while '\n' in self._buf:
                line, self._buf = self._buf.split('\n', 1)
                line = line.strip()
                progress = _format_progress(line)
                if progress is not None:
                    q.put(progress)
                elif not progress_only and not _is_progress_noise(line):
                    q.put(line)
            return len(s)
        def flush(self):
            pass

    def worker():
        old_stdout = sys.stdout
        sys.stdout = _LineWriter()
        try:
            result['payload'] = run_fn()
        except Exception:
            result['error'] = traceback.format_exc()
        finally:
            sys.stdout = old_stdout
            q.put(_DONE)

    def generate():
        threading.Thread(target=worker, daemon=True).start()
        while True:
            item = q.get()
            if item is _DONE:
                break
            yield _sse({'type': 'progress', 'message': item})
        if 'error' in result:
            yield _sse({'type': 'done', 'success': False, 'message': result['error']})
        else:
            yield _sse({'type': 'done', 'success': True, **(result.get('payload') or {})})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


_WORKER_SCRIPT = os.path.join(_ROOT, 'generate_worker.py')


def _run_worker_streaming(job, progress_only=True):
    """Run a generation job in generate_worker.py as a *separate process*,
    streaming its stdout as it goes and yielding (lines, result). Each line is a
    human-readable progress message; the final yielded tuple is (None, result)
    where result is {'success': bool, 'payload'|'error': ...}.

    Spire.XLS runs on an embedded .NET CLR whose heap is never returned to the
    OS within a process, so generating in the long-lived gunicorn worker leaves
    its RSS permanently elevated. Running in a child process means the OS
    reclaims *all* of that memory the instant the child exits — keeping the web
    worker under Render's 512MB cap across back-to-back Peve/Fausto runs."""
    fd, result_path = tempfile.mkstemp(suffix='.json', prefix='gen_result_')
    os.close(fd)
    job = {**job, 'result_path': result_path}

    memlog.snapshot(f"web: about to spawn worker for kind={job.get('kind')} "
                    f"{job.get('year')}-{job.get('month')}")
    env = {**os.environ, 'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'utf-8'}
    proc = subprocess.Popen(
        [sys.executable, '-u', _WORKER_SCRIPT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding='utf-8', errors='replace', env=env, cwd=_ROOT,
    )
    memlog.snapshot(f'web: worker spawned pid={proc.pid} (web RSS now is the '
                    f'resident baseline the child STACKS ON TOP of)')
    proc.stdin.write(json.dumps(job))
    proc.stdin.close()

    for raw in proc.stdout:
        line = raw.rstrip('\n').strip()
        progress = _format_progress(line)
        if progress is not None:
            yield progress, None
        elif not progress_only and not _is_progress_noise(line):
            yield line, None
    proc.wait()
    memlog.snapshot(f'web: worker pid={proc.pid} exited rc={proc.returncode} '
                    '(child memory now reclaimed by OS)')

    try:
        with open(result_path, 'r', encoding='utf-8') as f:
            result = json.load(f)
    except Exception:
        result = {'success': False,
                  'error': f'Worker terminato senza risultato (exit code {proc.returncode}).'}
    finally:
        try:
            os.remove(result_path)
        except OSError:
            pass
    # rc=-9 / no result file usually means the OS (or Render) SIGKILLed the child
    # — the classic OOM signature. Make that explicit on the shared timeline.
    if proc.returncode and proc.returncode < 0:
        memlog.note(f'web: WARNING worker pid={proc.pid} killed by signal '
                    f'{-proc.returncode} (negative rc) — likely OOM SIGKILL')
    memlog.snapshot(f"web: worker result success={result.get('success')} "
                    f"worker_peak={result.get('worker_peak_mb')}MB")
    yield None, result


def _run_worker_blocking(job):
    """Run a worker job to completion (no streaming) and return its
    {'success', 'payload'|'error'} result. Used for non-SSE callers like the
    projects-list build."""
    result = {'success': False, 'error': 'Nessun risultato.'}
    for _message, res in _run_worker_streaming(job, progress_only=True):
        if res is not None:
            result = res
    return result


def _stream_worker(job, progress_only=True, on_success=None):
    """SSE wrapper around `_run_worker_streaming`: stream progress events, then a
    final 'done' event. `on_success(payload)` runs in the web process after a
    successful job (e.g. to refresh the in-memory cache)."""
    def generate():
        result = {'success': False, 'error': 'Nessun risultato.'}
        for message, res in _run_worker_streaming(job, progress_only=progress_only):
            if res is None:
                yield _sse({'type': 'progress', 'message': message})
            else:
                result = res
        if result.get('success'):
            payload = result.get('payload') or {}
            if on_success:
                try:
                    on_success(payload)
                except Exception:
                    pass
            yield _sse({'type': 'done', 'success': True, **payload})
        else:
            yield _sse({'type': 'done', 'success': False,
                        'message': result.get('error', 'Errore sconosciuto')})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ── Frontend (SPA catch-all) ──────────────────────────────────────────────────
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    # Never let an unmatched /api/* path fall through to index.html — that
    # returns HTML where the frontend expects JSON ("Unexpected token '<'").
    if path.startswith('api/'):
        return jsonify({'success': False, 'message': f'Unknown API route: /{path}'}), 404
    static_dir = app.static_folder
    full = os.path.join(static_dir, path)
    if path and os.path.exists(full):
        return send_from_directory(static_dir, path)
    return send_from_directory(static_dir, 'index.html')


# ── Download from Odoo ────────────────────────────────────────────────────────
@app.route('/api/download', methods=['POST'])
def download():
    data = request.get_json(silent=True) or {}
    year, month = _get_year_month(data)
    export_name = f'{year}_{month}_timesheets_extraction.csv'
    def run():
        pd.options.mode.chained_assignment = None
        from download_from_odoo import download_csv_from_odoo
        msg = download_csv_from_odoo(
            ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD,
            year, month, EXPORT_PATH, export_name)
        _warm_projects_cache(year, month)  # fresh CSV → eagerly rebuild cached projects
        return {'message': msg or 'Download completato', 'output_path': EXPORT_PATH}
    return _stream_script(run)


# ── Generate Rapportini Peve ──────────────────────────────────────────────────
@app.route('/api/generate/peve', methods=['POST'])
def generate_peve():
    data = request.get_json(silent=True) or {}
    year, month = _get_year_month(data)
    return _stream_worker({'kind': 'peve', 'year': year, 'month': month})


# ── Generate Rapportini Fausto ────────────────────────────────────────────────
@app.route('/api/generate/fausto', methods=['POST'])
def generate_fausto():
    data = request.get_json(silent=True) or {}
    year, month = _get_year_month(data)
    return _stream_worker({'kind': 'fausto', 'year': year, 'month': month})


# ── Generate Riassunti ────────────────────────────────────────────────────────
@app.route('/api/generate/riassunti', methods=['POST'])
def generate_riassunti():
    data = request.get_json(silent=True) or {}
    year, month = _get_year_month(data)
    return _stream_worker({'kind': 'riassunti', 'year': year, 'month': month})


# ── List projects (Peve + Fausto) ────────────────────────────────────────────
@app.route('/api/projects', methods=['GET'])
def list_projects():
    year  = request.args.get('year',  str(datetime.now().year))
    month = request.args.get('month', str(datetime.now().month))

    cached = _PROJECTS_CACHE.get((str(year), str(month)))
    if cached is not None:
        return jsonify({'success': True, 'projects': cached})

    # Not pre-warmed for this month (e.g. never downloaded in this worker) —
    # build it now as a fallback and store it for next time.
    try:
        projects = _build_projects(year, month)
        _PROJECTS_CACHE[(str(year), str(month))] = projects
        return jsonify({'success': True, 'projects': projects})
    except Exception:
        return jsonify({'success': False, 'message': traceback.format_exc()}), 500


# ── Example tab: live cascading Odoo filters ─────────────────────────────────
_ODOO_CREDS = (ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD)


def _example_filter_args():
    """Read the three filter params; '' means 'All'."""
    return {
        'year_month':  request.args.get('year_month') or None,
        'employee_id': request.args.get('employee_id') or None,
        'partner_id':  request.args.get('partner_id') or None,
    }


@app.route('/api/example/options', methods=['GET'])
def example_options():
    try:
        import odoo_query
        opts = odoo_query.get_options(_ODOO_CREDS, **_example_filter_args())
        return jsonify({'success': True, **opts})
    except Exception:
        return jsonify({'success': False, 'message': traceback.format_exc()}), 500


@app.route('/api/example/timesheets', methods=['GET'])
def example_timesheets():
    try:
        import odoo_query
        entries, truncated = odoo_query.get_timesheets(_ODOO_CREDS, **_example_filter_args())
        return jsonify({'success': True, 'entries': entries, 'truncated': truncated})
    except Exception:
        return jsonify({'success': False, 'message': traceback.format_exc()}), 500


# ── Generate single rapportino ────────────────────────────────────────────────
@app.route('/api/generate/single', methods=['POST'])
def generate_single():
    data = request.get_json(silent=True) or {}
    year, month = _get_year_month(data)
    report_type   = data.get('report_type')
    task_category = data.get('task_category')
    partner_name  = data.get('partner_name')
    project_name  = data.get('project_name', '')
    if report_type not in ('peve', 'fausto'):
        return jsonify({'success': False, 'message': f'Tipo sconosciuto: {report_type}'}), 400
    # Run in the subprocess too — it invokes Spire.XLS, whose .NET heap would
    # otherwise accumulate in the long-lived web worker (see _run_worker_streaming).
    job = {'kind': 'single', 'year': year, 'month': month, 'report_type': report_type,
           'task_category': task_category, 'partner_name': partner_name, 'project_name': project_name}
    result = {'success': False, 'error': 'Nessun risultato.'}
    for _message, res in _run_worker_streaming(job, progress_only=True):
        if res is not None:
            result = res
    if result.get('success'):
        return jsonify({'success': True, **(result.get('payload') or {})})
    return jsonify({'success': False, 'message': result.get('error', 'Errore sconosciuto')}), 500


# ── List riassunti files ──────────────────────────────────────────────────────
@app.route('/api/list/riassunti', methods=['GET'])
def list_riassunti():
    storage_prefix = 'Output_Riassunto'
    files = []
    try:
        period_entries = list_prefix(storage_prefix)
        for entry in sorted(period_entries, key=lambda e: e.get('name', '')):
            if entry.get('id') is not None:
                continue  # skip files at root level
            period_dir = entry['name']
            parts = period_dir.split('_')
            if len(parts) != 2:
                continue
            year, month = parts[0], parts[1]
            file_entries = list_prefix(f'{storage_prefix}/{period_dir}')
            for fe in sorted(file_entries, key=lambda e: e.get('name', '')):
                if fe.get('id') is None:
                    continue  # skip sub-folders
                fname = fe['name']
                size_bytes = (fe.get('metadata') or {}).get('size', 0)
                files.append({
                    'period':   period_dir,
                    'year':     year,
                    'month':    month,
                    'filename': fname,
                    'size_kb':  round(size_bytes / 1024, 1),
                })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    return jsonify({'success': True, 'files': files})


# ── ZIP all riassunti ─────────────────────────────────────────────────────────
@app.route('/api/riassunto/zip', methods=['GET'])
def zip_riassunti():
    storage_prefix = 'Output_Riassunto'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        period_entries = list_prefix(storage_prefix)
        for entry in sorted(period_entries, key=lambda e: e.get('name', '')):
            if entry.get('id') is not None:
                continue
            period_dir = entry['name']
            file_entries = list_prefix(f'{storage_prefix}/{period_dir}')
            for fe in sorted(file_entries, key=lambda e: e.get('name', '')):
                if fe.get('id') is None:
                    continue
                fname = fe['name']
                file_data = download_to_bytes(f'{storage_prefix}/{period_dir}/{fname}')
                zf.writestr(os.path.join(period_dir, fname), file_data)
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name='Riassunti.zip')


# ── ZIP all rapportini for a period ───────────────────────────────────────────
@app.route('/api/rapportini/zip', methods=['GET'])
def zip_rapportini():
    year        = request.args.get('year', '')
    month       = request.args.get('month', '')
    report_type = request.args.get('report_type', '')
    if report_type == 'peve':
        storage_prefix = f'Output_Rapportini_Peve/{year}_{month}'
        zip_name = f'Rapportini_Peve_{year}_{month}.zip'
    elif report_type == 'fausto':
        storage_prefix = f'Output_Rapportini_Fausto/{year}_{month}'
        zip_name = f'Rapportini_Fausto_{year}_{month}.zip'
    else:
        return jsonify({'success': False, 'message': f'Tipo sconosciuto: {report_type}'}), 400
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        entries = list_prefix(storage_prefix)
        for fe in sorted(entries, key=lambda e: e.get('name', '')):
            if fe.get('id') is None:
                continue
            fname = fe['name']
            file_data = download_to_bytes(f'{storage_prefix}/{fname}')
            zf.writestr(fname, file_data)
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name=zip_name)


# ── Download riassunto file ───────────────────────────────────────────────────
@app.route('/api/riassunto/file', methods=['GET'])
def download_riassunto():
    period   = request.args.get('period', '')
    filename = request.args.get('filename', '')
    if not period or not filename:
        return jsonify({'success': False, 'message': 'Parametri mancanti'}), 400
    storage_key = f'Output_Riassunto/{period}/{filename}'
    try:
        data = download_to_bytes(storage_key)
    except Exception:
        return jsonify({'success': False, 'message': f'File non trovato: {filename}'}), 404
    ext = filename.rsplit('.', 1)[-1].lower()
    mime = ('application/vnd.ms-excel' if ext == 'xls'
            else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    return send_file(io.BytesIO(data), mimetype=mime, as_attachment=True, download_name=filename)


# ── Download single rapportino (PDF + XLSX zip) ───────────────────────────────
@app.route('/api/rapportini/single/zip', methods=['GET'])
def zip_single_rapportino():
    year          = request.args.get('year', '')
    month         = request.args.get('month', '')
    report_type   = request.args.get('report_type', '')
    task_category = request.args.get('task_category', '')
    partner_name  = request.args.get('partner_name', '')
    project_name  = request.args.get('project_name', '')

    if report_type == 'peve':
        storage_prefix = f'Output_Rapportini_Peve/{year}_{month}'
    elif report_type == 'fausto':
        storage_prefix = f'Output_Rapportini_Fausto/{year}_{month}'
    else:
        return jsonify({'success': False, 'message': f'Tipo sconosciuto: {report_type}'}), 400

    _proj    = project_name if project_name else ''
    _sep     = ' - ' if _proj else ''
    basename = (f'{year}-{month} _' + task_category.upper() + '_'
                + re.split(r'[ ]', partner_name)[0] + _sep + _proj)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for ext in ('pdf', 'xlsx'):
            fname = f'{basename}.{ext}'
            try:
                file_data = download_to_bytes(f'{storage_prefix}/{fname}')
                zf.writestr(fname, file_data)
            except Exception:
                pass  # file may not exist (e.g. PDF generation failed)
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name=f'{basename}.zip')


# ── Serve PDF preview ─────────────────────────────────────────────────────────
@app.route('/api/pdf', methods=['GET', 'HEAD'])
def serve_pdf():
    year          = request.args.get('year', '')
    month         = request.args.get('month', '')
    report_type   = request.args.get('report_type', '')
    task_category = request.args.get('task_category', '')
    partner_name  = request.args.get('partner_name', '')
    project_name  = request.args.get('project_name', '')

    if report_type == 'peve':
        storage_prefix = f'Output_Rapportini_Peve/{year}_{month}'
        local_root     = OUTPUT_PEVE
    elif report_type == 'fausto':
        storage_prefix = f'Output_Rapportini_Fausto/{year}_{month}'
        local_root     = OUTPUT_FAUSTO
    else:
        return jsonify({'success': False, 'message': f'Tipo sconosciuto: {report_type}'}), 400

    _proj    = project_name if project_name else ''
    _sep     = ' - ' if _proj else ''
    filename = (f'{year}-{month} _' + task_category.upper() + '_'
                + re.split(r'[ ]', partner_name)[0] + _sep + _proj + '.pdf')
    storage_key = f'{storage_prefix}/{filename}'
    # regenerate_pdf() writes the fresh PDF to this local path synchronously, so
    # prefer it over Supabase: the Supabase CDN can serve a stale copy for a few
    # seconds after an upsert, which left the preview showing the old file right
    # after a Gemini edit (until the modal was reopened). Fall back to Supabase
    # only on cold start, when the local file is absent.
    local_path = os.path.join(_ROOT, local_root, f'{year}_{month}', filename)

    if request.method == 'HEAD':
        if os.path.isfile(local_path):
            return '', 200
        from storage import object_exists
        if not object_exists(storage_key):
            return jsonify({'success': False, 'message': f'PDF non trovato: {filename}'}), 404
        return '', 200

    if os.path.isfile(local_path):
        with open(local_path, 'rb') as f:
            data = f.read()
    else:
        try:
            data = download_to_bytes(storage_key)
        except Exception:
            return jsonify({'success': False, 'message': f'PDF non trovato: {filename}'}), 404

    resp = send_file(io.BytesIO(data), mimetype='application/pdf')
    resp.headers['Content-Disposition'] = f'inline; filename="{filename}"'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


# ── AI Chat: modify rapportino ────────────────────────────────────────────────
@app.route('/api/chat/modify', methods=['POST'])
def chat_modify():
    data          = request.get_json(silent=True) or {}
    year          = str(data.get('year', ''))
    month         = str(data.get('month', ''))
    report_type   = data.get('report_type', '')
    task_category = data.get('task_category', '')
    partner_name  = data.get('partner_name', '')
    project_name  = data.get('project_name', '')
    message       = (data.get('message') or '').strip()
    history       = data.get('history', [])

    if not message:
        return jsonify({'success': False, 'message': 'Messaggio vuoto'}), 400

    try:
        from ai_chat import resolve_paths, read_xlsx_as_context, call_gemini, apply_changes, regenerate_pdf, restore_backup

        xlsx_path, pdf_path = resolve_paths(
            _ROOT, report_type, year, month,
            task_category, partner_name, project_name,
        )

        # Download XLSX from Supabase if not on local disk (cold start on Render)
        if not os.path.isfile(xlsx_path):
            try:
                os.makedirs(os.path.dirname(xlsx_path), exist_ok=True)
                with open(xlsx_path, 'wb') as f:
                    f.write(download_to_bytes(to_storage_key(xlsx_path)))
            except Exception:
                return jsonify({
                    'success': False,
                    'message': f'File non trovato: {os.path.basename(xlsx_path)}. Genera prima il rapportino.',
                }), 404

        context = read_xlsx_as_context(xlsx_path)
        result, raw_response = call_gemini(context, history, message)

        pdf_refreshed = False
        action = result.get('action', 'answer')

        if action == 'modify' and result.get('changes'):
            apply_changes(xlsx_path, pdf_path, result['changes'])
            regenerate_pdf(xlsx_path, pdf_path)
            pdf_refreshed = True
        elif action == 'revert':
            restore_backup(xlsx_path, pdf_path)
            regenerate_pdf(xlsx_path, pdf_path)
            pdf_refreshed = True

        return jsonify({
            'success':       True,
            'action':        action,
            'message':       result.get('message', ''),
            'changes':       result.get('changes', []),
            'changes_count': len(result.get('changes', [])),
            'pdf_refreshed': pdf_refreshed,
            'raw_response':  raw_response,
        })

    except Exception:
        return jsonify({'success': False, 'message': traceback.format_exc()}), 500


# ── AI Chat: revert rapportino to backup ──────────────────────────────────────
@app.route('/api/chat/revert', methods=['POST'])
def chat_revert():
    data          = request.get_json(silent=True) or {}
    year          = str(data.get('year', ''))
    month         = str(data.get('month', ''))
    report_type   = data.get('report_type', '')
    task_category = data.get('task_category', '')
    partner_name  = data.get('partner_name', '')
    project_name  = data.get('project_name', '')

    try:
        from ai_chat import resolve_paths, restore_backup

        xlsx_path, pdf_path = resolve_paths(
            _ROOT, report_type, year, month,
            task_category, partner_name, project_name,
        )

        restore_backup(xlsx_path, pdf_path)

        return jsonify({'success': True, 'pdf_refreshed': True})

    except FileNotFoundError as e:
        return jsonify({'success': False, 'message': str(e)}), 404
    except Exception:
        return jsonify({'success': False, 'message': traceback.format_exc()}), 500


# ── Memory diagnostics ────────────────────────────────────────────────────────
# Reproduce the OOM, then GET /api/debug/memlog and send me the whole output.
# The cg=USED/LIMIT column is the total memory of EVERY process in the Render
# instance vs the 512MB hard cap — when it approaches the limit, the next
# allocation is what triggers the kill.
@app.route('/api/debug/memlog', methods=['GET'])
def debug_memlog():
    # Capture the current web-worker state first so the file ends with a
    # fresh data point, then return the whole shared timeline as plain text.
    memlog.snapshot('web: /api/debug/memlog requested (current web state)')
    try:
        with open(memlog.LOG_PATH, 'r', encoding='utf-8') as f:
            body = f.read()
    except FileNotFoundError:
        body = '(no memlog file yet — generate a report first)\n'
    return Response(body, mimetype='text/plain; charset=utf-8')


@app.route('/api/debug/meminfo', methods=['GET'])
def debug_meminfo():
    memlog.snapshot('web: /api/debug/meminfo requested')
    cg_used, cg_limit = memlog._read_cgroup_mb()
    return jsonify({
        'success': True,
        'web_rss_mb': round(memlog._rss_mb() or 0, 1),
        'web_peak_mb': round(memlog.peak_mb(), 1),
        'cgroup_used_mb': round(cg_used, 1) if cg_used is not None else None,
        'cgroup_limit_mb': round(cg_limit, 1) if cg_limit is not None else None,
        'host_avail_mb': round(memlog._sys_avail_mb() or 0, 1),
        'web_concurrency': os.environ.get('WEB_CONCURRENCY'),
        'log_path': memlog.LOG_PATH,
    })


@app.route('/api/debug/memlog/clear', methods=['POST', 'GET'])
def debug_memlog_clear():
    try:
        open(memlog.LOG_PATH, 'w').close()
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    memlog.snapshot('web: memlog cleared — starting a clean repro run')
    return jsonify({'success': True, 'message': 'memlog cleared'})


if __name__ == '__main__':
    for path in [EXPORT_PATH, OUTPUT_PEVE, OUTPUT_FAUSTO, OUTPUT_RIASSUNTO, 'static']:
        os.makedirs(path, exist_ok=True)
    app.run(host='0.0.0.0', port=5000, debug=os.environ.get('FLASK_DEBUG') == '1', use_reloader=False)

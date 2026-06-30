import os
import re
import sys
import json
import queue
import tempfile
import threading
import traceback
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd
import io
import zipfile

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file, send_from_directory, Response

from storage import download_to_bytes, list_prefix, to_storage_key, upload_bytes
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

# Generated-status of the rows ("Generato"/"Non generato"). Unlike the project
# list, this changes every time a report is (re)generated, so we cache it in
# memory keyed by (year, month) and explicitly invalidate after each generation
# rather than rebuilding it on every Progetti visit (two Supabase list calls).
_STATUS_CACHE = {}


def _invalidate_status(year, month):
    _STATUS_CACHE.pop((str(year), str(month)), None)


# Supabase key for the persisted projects list (see _build_projects). Lives
# alongside the CSV so a cold/restarted worker can return the list with one fast
# read instead of re-spawning the subprocess and re-parsing the CSV.
def _projects_cache_key(year, month):
    return f'Cache/projects_{year}_{month}.json'


def _load_persisted_projects(year, month):
    """Return the previously-built projects list from Supabase, or None if it's
    absent/unreadable (then the caller rebuilds from the CSV)."""
    try:
        raw = download_to_bytes(_projects_cache_key(year, month))
        return json.loads(raw)
    except Exception:
        return None


def _persist_projects(year, month, projects):
    """Best-effort write of the built list to Supabase; never fatal."""
    try:
        upload_bytes(json.dumps(projects, ensure_ascii=False).encode('utf-8'),
                     _projects_cache_key(year, month))
    except Exception:
        pass


def _build_projects(year, month, force=False):
    """Build the combined Peve + Fausto project list.

    Fast path: read the small JSON we persisted to Supabase after the last build
    — survives worker restarts (common on Render's free tier), so a cold worker
    needn't re-spawn the subprocess at all.

    Slow path: parse the CSV in the subprocess. Listing parses with
    generazione_rapportini_*, whose import would otherwise pin ~100MB of .NET CLR
    in the long-lived web worker; running it as a short-lived 'projects' child
    keeps the web worker light, and we persist the result for next time.

    `force=True` skips the persisted read and re-pulls the CSV from Supabase
    before parsing, so a stale copy can't keep the list out of date — and the
    fresh result overwrites the persisted JSON."""
    if not force:
        persisted = _load_persisted_projects(year, month)
        if persisted is not None:
            return persisted
    result = _run_worker_blocking({'kind': 'projects',
                                   'year': str(year), 'month': str(month),
                                   'force': force})
    if not result.get('success'):
        raise RuntimeError(result.get('error', 'projects build failed'))
    projects = (result.get('payload') or {}).get('projects', [])
    _persist_projects(year, month, projects)
    return projects


def _warm_projects_cache(year, month):
    """Eagerly (re)build and store the projects list for a month, swallowing
    errors so a bad/missing CSV never crashes startup or a download.

    Called right after a fresh CSV download, so we force a rebuild (force=True)
    — skipping the persisted read and overwriting the stale JSON with the new
    list rather than serving the pre-download copy."""
    try:
        _PROJECTS_CACHE[(str(year), str(month))] = _build_projects(year, month, force=True)
    except Exception:
        _PROJECTS_CACHE.pop((str(year), str(month)), None)
    # A fresh CSV can add/remove rows, so the cached status keys may no longer
    # line up — drop it and let the next status request recompute.
    _invalidate_status(year, month)


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
    return _stream_worker({'kind': 'peve', 'year': year, 'month': month},
                          on_success=lambda _p: _invalidate_status(year, month))


# ── Generate Rapportini Fausto ────────────────────────────────────────────────
@app.route('/api/generate/fausto', methods=['POST'])
def generate_fausto():
    data = request.get_json(silent=True) or {}
    year, month = _get_year_month(data)
    return _stream_worker({'kind': 'fausto', 'year': year, 'month': month},
                          on_success=lambda _p: _invalidate_status(year, month))


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
    force = request.args.get('force') in ('1', 'true', 'yes')
    # include_status=1 attaches the generated-status map so the table can render
    # rows AND badges from a single round trip (the status endpoint stays for the
    # post-generation refresh). Reuses the same caches, so it adds no extra work.
    want_status = request.args.get('include_status') in ('1', 'true', 'yes')

    def _resp(projects):
        out = {'success': True, 'projects': projects}
        if want_status:
            out['status'] = _get_status(year, month, projects, force=force)
        return jsonify(out)

    # force=1 (the "Ricarica lista" button) bypasses the in-memory cache and
    # rebuilds from the CSV — the only way to recover from a stale entry (e.g.
    # an empty list cached before the month's CSV existed) without a restart.
    if not force:
        cached = _PROJECTS_CACHE.get((str(year), str(month)))
        if cached is not None:
            return _resp(cached)

    # Not pre-warmed for this month (e.g. never downloaded in this worker) —
    # build it now as a fallback and store it for next time.
    try:
        projects = _build_projects(year, month, force=force)
        _PROJECTS_CACHE[(str(year), str(month))] = projects
        if force:
            _invalidate_status(year, month)  # rebuilt list → recompute status (below)
        return _resp(projects)
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
        _invalidate_status(year, month)  # this row's badge must now read "Generato"
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


# ── Streaming ZIP helper ──────────────────────────────────────────────────────
class _ZipSink:
    """A write-only, non-seekable sink that buffers bytes for a streaming
    response. zipfile writes archive bytes here; the response generator drains
    it chunk by chunk. Because it exposes no tell()/seek(), zipfile falls back
    to data descriptors and never tries to rewind — so we can emit the archive
    incrementally instead of holding it all in memory."""
    def __init__(self):
        self._buf = bytearray()

    def write(self, data):
        self._buf.extend(data)
        return len(data)

    def flush(self):
        pass

    def drain(self):
        chunk = bytes(self._buf)
        self._buf.clear()
        return chunk


def _stream_zip(items, zip_name, window=4):
    """Stream a ZIP of (arcname, storage_key) pairs to the client.

    Render's free tier caps the whole instance at 512 MB. Building the entire
    archive in memory (download everything, then copy it all into a BytesIO)
    peaked at ~2x the payload and OOM-killed the worker on large months —
    the download then died mid-flight ("Site wasn't available" / Resume).

    Instead we keep only a small sliding WINDOW of files in flight: downloads
    overlap (so we keep most of the old thread-pool speed), but at most `window`
    file bodies live in RAM at once, and each is written into the response and
    freed as soon as the next is ready. Peak memory is bounded regardless of how
    big the month is.

    ZIP_STORED (no compression): the payload is PDF/XLSX, both already
    compressed, so deflating only burns CPU for ~no size gain."""
    items = list(items)

    def generate():
        sink = _ZipSink()
        with zipfile.ZipFile(sink, 'w', zipfile.ZIP_STORED) as zf:
            with ThreadPoolExecutor(max_workers=window) as ex:
                futures = {}
                n = len(items)
                next_submit = 0
                # Prime the window.
                while next_submit < n and next_submit < window:
                    futures[next_submit] = ex.submit(
                        download_to_bytes, items[next_submit][1])
                    next_submit += 1
                for i in range(n):
                    arcname = items[i][0]
                    data = futures.pop(i).result()
                    zf.writestr(arcname, data)
                    del data
                    # Refill the window so the next download is already running.
                    if next_submit < n:
                        futures[next_submit] = ex.submit(
                            download_to_bytes, items[next_submit][1])
                        next_submit += 1
                    chunk = sink.drain()
                    if chunk:
                        yield chunk
        # Central directory written on ZipFile close.
        tail = sink.drain()
        if tail:
            yield tail

    return Response(generate(), mimetype='application/zip', headers={
        'Content-Disposition': f'attachment; filename="{zip_name}"',
    })


# ── ZIP all riassunti ─────────────────────────────────────────────────────────
@app.route('/api/riassunto/zip', methods=['GET'])
def zip_riassunti():
    storage_prefix = 'Output_Riassunto'
    # Collect every (arcname, storage_key) pair, then stream them out.
    keys = []
    for entry in sorted(list_prefix(storage_prefix), key=lambda e: e.get('name', '')):
        if entry.get('id') is not None:
            continue
        period_dir = entry['name']
        file_entries = list_prefix(f'{storage_prefix}/{period_dir}')
        for fe in sorted(file_entries, key=lambda e: e.get('name', '')):
            if fe.get('id') is None:
                continue
            fname = fe['name']
            keys.append((os.path.join(period_dir, fname),
                         f'{storage_prefix}/{period_dir}/{fname}'))

    return _stream_zip(keys, 'Riassunti.zip')


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
    entries = list_prefix(storage_prefix)
    names = sorted(fe['name'] for fe in entries if fe.get('id') is not None)

    items = [(fname, f'{storage_prefix}/{fname}') for fname in names]
    return _stream_zip(items, zip_name)


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


def _report_basename(year, month, task_category, partner_name, project_name):
    """Reconstruct the filename (without extension) that generation writes for a
    given report row. Generation names files
    `{year}-{month} _{TASK}_{first word of partner}[ - {project}]` (see
    generazione_rapportini_*.create_rapportini), so the preview, single-zip
    download and generated-status check all resolve the same key from here."""
    proj = project_name or ''
    sep  = ' - ' if proj else ''
    return (f'{year}-{month} _' + (task_category or '').upper() + '_'
            + re.split(r'[ ]', partner_name or '')[0] + sep + proj)


def _output_prefix(report_type, year, month):
    """Supabase prefix holding generated reports for a type/period, or None for
    an unknown type."""
    if report_type == 'peve':
        return f'Output_Rapportini_Peve/{year}_{month}'
    if report_type == 'fausto':
        return f'Output_Rapportini_Fausto/{year}_{month}'
    return None


# ── Generated-status of the project rows ──────────────────────────────────────
# The project list comes from the Odoo timesheet CSV (i.e. what *could* be
# generated). This endpoint cross-references the Supabase output folders so the
# UI's "Stato" column reflects which rows actually have a generated report —
# kept separate from /api/projects because status changes as reports are
# generated, whereas the (cached) project list only changes on a new CSV.
def _list_existing(rtype, year, month):
    """Set of filenames generated for a type/period in Supabase (empty on error)."""
    try:
        entries = list_prefix(_output_prefix(rtype, year, month))
        return {e['name'] for e in entries if e.get('id') is not None}
    except Exception:
        return set()


def _compute_status(year, month, projects):
    """Cross-reference the project list against the Supabase output folders and
    return {row_key -> 'generated'|'missing'}. The two folder listings are
    independent network round trips, so we run them concurrently."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {rtype: ex.submit(_list_existing, rtype, year, month)
                   for rtype in ('fausto', 'peve')}
        existing = {rtype: fut.result() for rtype, fut in futures.items()}

    status = {}
    for p in projects:
        rtype    = p['report_type']
        basename = _report_basename(year, month, p['task_category'],
                                    p['partner_name'], p['project_name'])
        # A report exists if either artifact is present: the .xlsx is always
        # written, while Spire PDF conversion can fail independently.
        present = (f'{basename}.pdf'  in existing.get(rtype, set())
                   or f'{basename}.xlsx' in existing.get(rtype, set()))
        key = f"{rtype}:{p['task_category']}:{p['partner_name']}:{p['project_name']}"
        status[key] = 'generated' if present else 'missing'
    return status


def _get_status(year, month, projects, force=False):
    """Status map for the rows, served from _STATUS_CACHE unless `force` (the
    cache is invalidated on generation, so it can't go stale on its own)."""
    if not force:
        cached = _STATUS_CACHE.get((str(year), str(month)))
        if cached is not None:
            return cached
    status = _compute_status(year, month, projects)
    _STATUS_CACHE[(str(year), str(month))] = status
    return status


@app.route('/api/projects/status', methods=['GET'])
def projects_status():
    year  = request.args.get('year',  str(datetime.now().year))
    month = request.args.get('month', str(datetime.now().month))
    force = request.args.get('force') in ('1', 'true', 'yes')

    # Reuse the same project list the table is built from (build it if this
    # worker hasn't yet), so the returned keys line up row-for-row.
    projects = _PROJECTS_CACHE.get((str(year), str(month)))
    if projects is None:
        try:
            projects = _build_projects(year, month)
            _PROJECTS_CACHE[(str(year), str(month))] = projects
        except Exception:
            return jsonify({'success': False, 'message': traceback.format_exc()}), 500

    return jsonify({'success': True, 'status': _get_status(year, month, projects, force=force)})


# ── Download single rapportino (PDF + XLSX zip) ───────────────────────────────
@app.route('/api/rapportini/single/zip', methods=['GET'])
def zip_single_rapportino():
    year          = request.args.get('year', '')
    month         = request.args.get('month', '')
    report_type   = request.args.get('report_type', '')
    task_category = request.args.get('task_category', '')
    partner_name  = request.args.get('partner_name', '')
    project_name  = request.args.get('project_name', '')

    storage_prefix = _output_prefix(report_type, year, month)
    if storage_prefix is None:
        return jsonify({'success': False, 'message': f'Tipo sconosciuto: {report_type}'}), 400

    basename = _report_basename(year, month, task_category, partner_name, project_name)

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

    storage_prefix = _output_prefix(report_type, year, month)
    if storage_prefix is None:
        return jsonify({'success': False, 'message': f'Tipo sconosciuto: {report_type}'}), 400
    local_root = OUTPUT_PEVE if report_type == 'peve' else OUTPUT_FAUSTO

    filename = _report_basename(year, month, task_category, partner_name, project_name) + '.pdf'
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

import os
import re
import traceback
from datetime import datetime

import pandas as pd
import io
import zipfile

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file, send_from_directory

from storage import download_to_bytes, list_prefix, to_storage_key

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

# ── Paths (ephemeral /tmp on Render, local dir in dev) ────────────────────────
_ON_RENDER = os.environ.get('RENDER') is not None
_tmp = '/tmp' if _ON_RENDER else '.'
EXPORT_PATH      = _tmp + '/Odoo exports/'
OUTPUT_PEVE      = _tmp + '/Output_Rapportini_Peve/'
OUTPUT_FAUSTO    = _tmp + '/Output_Rapportini_Fausto/'
OUTPUT_RIASSUNTO = _tmp + '/Output_Riassunto/'

# ── Business rules ────────────────────────────────────────────────────────────
ELIGIBILITY_RULES = {
    'Stefano Uboldi': ['*'],
    'Matteo Franceschini': ['*'],
    'Giovanni Verderio': ['*'],
    'Filippo Cerutti': ['*'],
    'Francesco Cerutti': ['*'],
    'Tony Fogliaro': ['*'],
    'Daniele Cecchetto': ['*'],
    'Alessandro Peverelli': ['Tag S.r.l.'],
}

TO_ISOLATE_LIST = ['Frilli Srl', 'Corden Pharma Spa']
DICT_PARTNER_RENAME = {'CGT Compagnia Generale Trattori Spa': 'CGT Spa'}
FILTERED_PARTNERS = []


def _get_year_month(data):
    now = datetime.now()
    year = str(data.get('year', now.year))
    month = str(data.get('month', now.month))
    return year, month


def _ensure_csv_local(year, month):
    """Download the Odoo CSV from Supabase if it's not already on local disk."""
    csv_name = f'{year}_{month}_timesheets_extraction.csv'
    local_csv = EXPORT_PATH + csv_name
    if not os.path.isfile(local_csv):
        try:
            os.makedirs(EXPORT_PATH, exist_ok=True)
            with open(local_csv, 'wb') as f:
                f.write(download_to_bytes(f'Odoo exports/{csv_name}'))
        except Exception:
            pass  # generation modules will raise their own error if file is missing


# ── Frontend (SPA catch-all) ──────────────────────────────────────────────────
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
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
    try:
        pd.options.mode.chained_assignment = None
        from download_from_odoo import download_csv_from_odoo
        msg = download_csv_from_odoo(
            ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD,
            year, month, EXPORT_PATH, export_name)
        return jsonify({'success': True, 'message': msg or 'Download completato', 'output_path': EXPORT_PATH})
    except Exception:
        return jsonify({'success': False, 'message': traceback.format_exc()}), 500


# ── Generate Rapportini Peve ──────────────────────────────────────────────────
@app.route('/api/generate/peve', methods=['POST'])
def generate_peve():
    data = request.get_json(silent=True) or {}
    year, month = _get_year_month(data)
    try:
        pd.options.mode.chained_assignment = None
        _ensure_csv_local(year, month)
        import generazione_rapportini_peve as gen
        gen.create_rapportini(
            path_source=EXPORT_PATH,
            path_output=OUTPUT_PEVE,
            year=year,
            month=month,
            filtered_partners=FILTERED_PARTNERS,
            eligibility_rules=ELIGIBILITY_RULES,
            to_isolate_list=TO_ISOLATE_LIST,
            dict_partner_rename=DICT_PARTNER_RENAME,
            tasks=['Assistenza'])
        return jsonify({
            'success': True,
            'message': f'Rapportini Peve generati per {year}-{month}',
            'output_path': OUTPUT_PEVE + f'{year}_{month}/'
        })
    except Exception:
        return jsonify({'success': False, 'message': traceback.format_exc()}), 500


# ── Generate Rapportini Fausto ────────────────────────────────────────────────
@app.route('/api/generate/fausto', methods=['POST'])
def generate_fausto():
    data = request.get_json(silent=True) or {}
    year, month = _get_year_month(data)
    try:
        pd.options.mode.chained_assignment = None
        _ensure_csv_local(year, month)
        import generazione_rapportini_fausto as gen
        gen.create_rapportini(
            path_source=EXPORT_PATH,
            path_output=OUTPUT_FAUSTO,
            year=year,
            month=month,
            filtered_partners=FILTERED_PARTNERS,
            eligibility_rules=ELIGIBILITY_RULES,
            to_isolate_list=TO_ISOLATE_LIST,
            dict_partner_rename=DICT_PARTNER_RENAME,
            tasks=['Assistenza', 'Intervento'])
        return jsonify({
            'success': True,
            'message': f'Rapportini Fausto generati per {year}-{month}',
            'output_path': OUTPUT_FAUSTO + f'{year}_{month}/'
        })
    except Exception:
        return jsonify({'success': False, 'message': traceback.format_exc()}), 500


# ── Generate Riassunti ────────────────────────────────────────────────────────
@app.route('/api/generate/riassunti', methods=['POST'])
def generate_riassunti():
    data = request.get_json(silent=True) or {}
    year, month = _get_year_month(data)
    try:
        pd.options.mode.chained_assignment = None
        _ensure_csv_local(year, month)
        import generazione_riassunti as gen
        gen.create_riassunto(
            path_source=EXPORT_PATH,
            path_output=OUTPUT_RIASSUNTO,
            year=year,
            month=month,
            filtered_partners=FILTERED_PARTNERS,
            eligibility_rules=ELIGIBILITY_RULES,
            to_isolate_list=TO_ISOLATE_LIST,
            dict_partner_rename=DICT_PARTNER_RENAME,
            tasks=['Assistenza', 'Intervento'])
        return jsonify({
            'success': True,
            'message': f'Riassunti generati per {year}-{month}',
            'output_path': OUTPUT_RIASSUNTO
        })
    except Exception:
        return jsonify({'success': False, 'message': traceback.format_exc()}), 500


# ── List projects (Peve + Fausto) ────────────────────────────────────────────
@app.route('/api/projects', methods=['GET'])
def list_projects():
    year  = request.args.get('year',  str(datetime.now().year))
    month = request.args.get('month', str(datetime.now().month))
    try:
        pd.options.mode.chained_assignment = None
        _ensure_csv_local(year, month)
        import generazione_rapportini_peve as gen_a
        import generazione_rapportini_fausto as gen_f
        projects = []
        for p in gen_a.list_projects(EXPORT_PATH, year, month, ELIGIBILITY_RULES, TO_ISOLATE_LIST, DICT_PARTNER_RENAME, ['Assistenza']):
            projects.append({**p, 'report_type': 'peve'})
        for p in gen_f.list_projects(EXPORT_PATH, year, month, ELIGIBILITY_RULES, TO_ISOLATE_LIST, DICT_PARTNER_RENAME, ['Assistenza', 'Intervento']):
            projects.append({**p, 'report_type': 'fausto'})
        return jsonify({'success': True, 'projects': projects})
    except Exception:
        return jsonify({'success': False, 'message': traceback.format_exc()}), 500


# ── Generate single rapportino ────────────────────────────────────────────────
@app.route('/api/generate/single', methods=['POST'])
def generate_single():
    data = request.get_json(silent=True) or {}
    year, month = _get_year_month(data)
    report_type  = data.get('report_type')
    task_category = data.get('task_category')
    partner_name  = data.get('partner_name')
    project_name  = data.get('project_name', '')
    try:
        pd.options.mode.chained_assignment = None
        _ensure_csv_local(year, month)
        if report_type == 'peve':
            import generazione_rapportini_peve as gen
            gen.create_rapportini(
                path_source=EXPORT_PATH,
                path_output=OUTPUT_PEVE,
                year=year,
                month=month,
                filtered_partners=FILTERED_PARTNERS,
                eligibility_rules=ELIGIBILITY_RULES,
                to_isolate_list=TO_ISOLATE_LIST,
                dict_partner_rename=DICT_PARTNER_RENAME,
                tasks=['Assistenza'],
                only_task=task_category,
                only_partner=partner_name,
                only_project=project_name)
            out_dir = OUTPUT_PEVE + f'{year}_{month}/'
        elif report_type == 'fausto':
            import generazione_rapportini_fausto as gen
            gen.create_rapportini(
                path_source=EXPORT_PATH,
                path_output=OUTPUT_FAUSTO,
                year=year,
                month=month,
                filtered_partners=FILTERED_PARTNERS,
                eligibility_rules=ELIGIBILITY_RULES,
                to_isolate_list=TO_ISOLATE_LIST,
                dict_partner_rename=DICT_PARTNER_RENAME,
                tasks=['Assistenza', 'Intervento'],
                only_task=task_category,
                only_partner=partner_name,
                only_project=project_name)
            out_dir = OUTPUT_FAUSTO + f'{year}_{month}/'
        else:
            return jsonify({'success': False, 'message': f'Tipo sconosciuto: {report_type}'}), 400
        return jsonify({
            'success': True,
            'message': f'Aggiornato: {task_category} – {partner_name}',
            'output_path': out_dir,
        })
    except Exception:
        return jsonify({'success': False, 'message': traceback.format_exc()}), 500


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
    elif report_type == 'fausto':
        storage_prefix = f'Output_Rapportini_Fausto/{year}_{month}'
    else:
        return jsonify({'success': False, 'message': f'Tipo sconosciuto: {report_type}'}), 400

    _proj    = project_name if project_name else ''
    _sep     = ' - ' if _proj else ''
    filename = (f'{year}-{month} _' + task_category.upper() + '_'
                + re.split(r'[ ]', partner_name)[0] + _sep + _proj + '.pdf')
    storage_key = f'{storage_prefix}/{filename}'

    if request.method == 'HEAD':
        from storage import object_exists
        if not object_exists(storage_key):
            return jsonify({'success': False, 'message': f'PDF non trovato: {filename}'}), 404
        return '', 200

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


if __name__ == '__main__':
    for path in [EXPORT_PATH, OUTPUT_PEVE, OUTPUT_FAUSTO, OUTPUT_RIASSUNTO, 'static']:
        os.makedirs(path, exist_ok=True)
    app.run(host='0.0.0.0', port=5000, debug=os.environ.get('FLASK_DEBUG') == '1', use_reloader=False)

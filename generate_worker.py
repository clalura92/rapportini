"""Subprocess entry point for heavy report generation.

The Spire.XLS PDF conversion runs on an embedded .NET CLR whose heap is *not*
returned to the OS after Dispose()/gc.collect() — it stays reserved for the life
of the process. Running generation inside the long-lived gunicorn worker
therefore leaves the worker's RSS permanently elevated, so a second run (e.g.
Fausto after Peve) stacks on top and trips Render's 512MB cap.

By running each generation in this short-lived child process instead, the OS
reclaims *all* of its memory — Python heap and .NET heap alike — the instant it
exits. The web worker stays light and warm.

Protocol: a JSON job spec is read from stdin; progress is printed to stdout
(streamed to the browser by the parent); the final {success/payload} or {error}
is written to the JSON file at job['result_path'].

    {"kind": "peve"|"fausto"|"riassunti"|"single", "year": ..., "month": ...,
     "result_path": "...", ...single-only fields...}
"""
import os
import sys
import json
import traceback

from dotenv import load_dotenv

load_dotenv()

import memlog
memlog.init('worker')
memlog.snapshot('worker process started (after dotenv)')

import pandas as pd
memlog.snapshot('after import pandas')

import report_config as cfg


def _run(job):
    pd.options.mode.chained_assignment = None
    kind  = job['kind']
    year  = job['year']
    month = job['month']
    memlog.snapshot(f'_run begin kind={kind} {year}-{month}')
    cfg.ensure_csv_local(year, month)
    memlog.snapshot('after ensure_csv_local')

    if kind == 'projects':
        # Listing projects parses the CSV with the same heavy modules that load
        # the .NET CLR. Doing it HERE (in the short-lived child) instead of in
        # app.py keeps Spire/fitz out of the long-lived web worker entirely —
        # the web process only ever holds the small JSON result.
        import generazione_rapportini_peve as gen_a
        import generazione_rapportini_fausto as gen_f
        memlog.snapshot('projects: gen modules imported')
        df_source = gen_a.load_df(cfg.EXPORT_PATH, year, month)
        projects = []
        for p in gen_a.list_projects(cfg.EXPORT_PATH, year, month,
                                     cfg.PEVE_ELIGIBILITY_RULES, cfg.PEVE_TO_ISOLATE_LIST,
                                     cfg.PEVE_DICT_PARTNER_RENAME, ['Assistenza'], df=df_source):
            projects.append({**p, 'report_type': 'peve'})
        for p in gen_f.list_projects(cfg.EXPORT_PATH, year, month,
                                     cfg.FAUSTO_ELIGIBILITY_RULES, cfg.FAUSTO_TO_ISOLATE_LIST,
                                     cfg.FAUSTO_DICT_PARTNER_RENAME, ['Assistenza', 'Intervento'], df=df_source):
            projects.append({**p, 'report_type': 'fausto'})
        memlog.snapshot(f'projects: built {len(projects)} entries')
        return {'projects': projects}

    if kind == 'peve':
        import generazione_rapportini_peve as gen
        memlog.snapshot('after import generazione_rapportini_peve (spire/fitz/openpyxl loaded)')
        gen.create_rapportini(
            path_source=cfg.EXPORT_PATH,
            path_output=cfg.OUTPUT_PEVE,
            year=year, month=month,
            filtered_partners=cfg.FILTERED_PARTNERS,
            eligibility_rules=cfg.PEVE_ELIGIBILITY_RULES,
            to_isolate_list=cfg.PEVE_TO_ISOLATE_LIST,
            dict_partner_rename=cfg.PEVE_DICT_PARTNER_RENAME,
            tasks=['Assistenza'])
        memlog.snapshot('after create_rapportini peve DONE')
        return {'message': f"Rapportini Peve generati per {year}-{month}",
                'output_path': cfg.OUTPUT_PEVE + f'{year}_{month}/'}

    if kind == 'fausto':
        import generazione_rapportini_fausto as gen
        memlog.snapshot('after import generazione_rapportini_fausto (spire/fitz/openpyxl loaded)')
        gen.create_rapportini(
            path_source=cfg.EXPORT_PATH,
            path_output=cfg.OUTPUT_FAUSTO,
            year=year, month=month,
            filtered_partners=cfg.FILTERED_PARTNERS,
            eligibility_rules=cfg.FAUSTO_ELIGIBILITY_RULES,
            to_isolate_list=cfg.FAUSTO_TO_ISOLATE_LIST,
            dict_partner_rename=cfg.FAUSTO_DICT_PARTNER_RENAME,
            tasks=['Assistenza', 'Intervento'])
        memlog.snapshot('after create_rapportini fausto DONE')
        return {'message': f"Rapportini Fausto generati per {year}-{month}",
                'output_path': cfg.OUTPUT_FAUSTO + f'{year}_{month}/'}

    if kind == 'riassunti':
        import generazione_riassunti as gen
        memlog.snapshot('after import generazione_riassunti (spire/openpyxl loaded)')
        gen.create_riassunto(
            path_source=cfg.EXPORT_PATH,
            path_output=cfg.OUTPUT_RIASSUNTO,
            year=year, month=month,
            filtered_partners=cfg.FILTERED_PARTNERS,
            eligibility_rules=cfg.FAUSTO_ELIGIBILITY_RULES,
            to_isolate_list=cfg.FAUSTO_TO_ISOLATE_LIST,
            dict_partner_rename=cfg.FAUSTO_DICT_PARTNER_RENAME,
            tasks=['Assistenza', 'Intervento'])
        memlog.snapshot('after create_riassunto DONE')
        return {'message': f"Riassunti generati per {year}-{month}",
                'output_path': cfg.OUTPUT_RIASSUNTO}

    if kind == 'single':
        report_type   = job['report_type']
        task_category = job.get('task_category')
        partner_name  = job.get('partner_name')
        project_name  = job.get('project_name', '')
        if report_type == 'peve':
            import generazione_rapportini_peve as gen
            gen.create_rapportini(
                path_source=cfg.EXPORT_PATH,
                path_output=cfg.OUTPUT_PEVE,
                year=year, month=month,
                filtered_partners=cfg.FILTERED_PARTNERS,
                eligibility_rules=cfg.PEVE_ELIGIBILITY_RULES,
                to_isolate_list=cfg.PEVE_TO_ISOLATE_LIST,
                dict_partner_rename=cfg.PEVE_DICT_PARTNER_RENAME,
                tasks=['Assistenza'],
                only_task=task_category, only_partner=partner_name, only_project=project_name)
            out_dir = cfg.OUTPUT_PEVE + f'{year}_{month}/'
        elif report_type == 'fausto':
            import generazione_rapportini_fausto as gen
            gen.create_rapportini(
                path_source=cfg.EXPORT_PATH,
                path_output=cfg.OUTPUT_FAUSTO,
                year=year, month=month,
                filtered_partners=cfg.FILTERED_PARTNERS,
                eligibility_rules=cfg.FAUSTO_ELIGIBILITY_RULES,
                to_isolate_list=cfg.FAUSTO_TO_ISOLATE_LIST,
                dict_partner_rename=cfg.FAUSTO_DICT_PARTNER_RENAME,
                tasks=['Assistenza', 'Intervento'],
                only_task=task_category, only_partner=partner_name, only_project=project_name)
            out_dir = cfg.OUTPUT_FAUSTO + f'{year}_{month}/'
        else:
            raise ValueError(f'Tipo sconosciuto: {report_type}')
        return {'message': f'Aggiornato: {task_category} – {partner_name}',
                'output_path': out_dir}

    raise ValueError(f'Unknown job kind: {kind}')


def main():
    job = json.load(sys.stdin)
    result_path = job['result_path']
    try:
        payload = _run(job)
        result = {'success': True, 'payload': payload}
    except Exception:
        result = {'success': False, 'error': traceback.format_exc()}
    memlog.snapshot(f'worker FINAL success={result["success"]} '
                    f'peak={memlog.peak_mb():.1f}MB', kind='FINAL')
    result['worker_peak_mb'] = round(memlog.peak_mb(), 1)
    with open(result_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False)
    sys.stdout.flush()
    sys.exit(0 if result['success'] else 1)


if __name__ == '__main__':
    main()

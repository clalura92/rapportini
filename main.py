"""
CLI entry point.  Run:
    python main.py --year 2026 --month 5 --type all
    python main.py --year 2026 --month 5 --type peve

The web UI (app.py) is the primary interface; this file is for scripted / headless runs.
"""
import argparse
import sys
import os

import pandas as pd


# ── Paths ─────────────────────────────────────────────────────────────────────
EXPORT_PATH = 'Odoo exports/'
OUTPUT_PEVE = 'Output_Rapportini_Peve/'
OUTPUT_FAUSTO = 'Output_Rapportini_Fausto/'
OUTPUT_RIASSUNTO = 'Output_Riassunto/'

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

COMMON_ARGS = dict(
    filtered_partners=FILTERED_PARTNERS,
    eligibility_rules=ELIGIBILITY_RULES,
    to_isolate_list=TO_ISOLATE_LIST,
    dict_partner_rename=DICT_PARTNER_RENAME,
)


def run_download(year, month):
    from download_from_odoo import download_csv_from_odoo
    export_name = f'{year}_{month}_timesheets_extraction.csv'
    print(f'Downloading {export_name} from Odoo...')
    msg = download_csv_from_odoo(ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD,
                                  year, month, EXPORT_PATH, export_name)
    print(f'Download result: {msg}')


def run_peve(year, month):
    import generazione_rapportini_peve as gen
    print(f'\n--- Rapportini Peve {year}-{month} ---')
    gen.create_rapportini(path_source=EXPORT_PATH, path_output=OUTPUT_PEVE,
                          year=year, month=month, tasks=['Assistenza'], **COMMON_ARGS)


def run_fausto(year, month):
    import generazione_rapportini_fausto as gen
    print(f'\n--- Rapportini Fausto {year}-{month} ---')
    gen.create_rapportini(path_source=EXPORT_PATH, path_output=OUTPUT_FAUSTO,
                          year=year, month=month, tasks=['Assistenza', 'Intervento'], **COMMON_ARGS)


def run_riassunti(year, month):
    import generazione_riassunti as gen
    print(f'\n--- Riassunti {year}-{month} ---')
    gen.create_riassunto(path_source=EXPORT_PATH, path_output=OUTPUT_RIASSUNTO,
                         year=year, month=month, tasks=['Assistenza', 'Intervento'], **COMMON_ARGS)


RUNNERS = {
    'download':  run_download,
    'peve':      run_peve,
    'fausto':    run_fausto,
    'riassunti': run_riassunti,
}


if __name__ == '__main__':
    pd.options.mode.chained_assignment = None

    parser = argparse.ArgumentParser(description='Rapportini CLI')
    parser.add_argument('--year',  type=str, help='Anno (es. 2026)')
    parser.add_argument('--month', type=str, help='Mese (es. 5)')
    parser.add_argument('--type',  type=str, default='all',
                        choices=['all', 'download', 'peve', 'fausto', 'riassunti'],
                        help='Quale script eseguire (default: all)')
    args = parser.parse_args()

    year = args.year or input('Anno (es. 2026): ').strip()
    month = args.month or input('Mese (es. 5): ').strip()

    if not year or not month:
        print('Errore: anno e mese sono obbligatori.')
        sys.exit(1)

    for path in [EXPORT_PATH, OUTPUT_PEVE, OUTPUT_FAUSTO, OUTPUT_RIASSUNTO]:
        os.makedirs(path, exist_ok=True)

    if args.type == 'all':
        run_download(year, month)
        run_peve(year, month)
        run_fausto(year, month)
        run_riassunti(year, month)
    else:
        RUNNERS[args.type](year, month)

    print('\nCompletato.')

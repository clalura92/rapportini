"""Shared configuration for report generation.

Both the Flask web app (app.py) and the subprocess worker (generate_worker.py)
import paths and business rules from here, so the two stay in sync without the
worker having to import — and thereby boot — the whole Flask app.
"""
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))

# ── Paths (ephemeral /tmp on Render, local dir in dev) ────────────────────────
_ON_RENDER = os.environ.get('RENDER') is not None
_tmp = '/tmp' if _ON_RENDER else '.'
EXPORT_PATH      = _tmp + '/Odoo exports/'
OUTPUT_PEVE      = _tmp + '/Output_Rapportini_Peve/'
OUTPUT_FAUSTO    = _tmp + '/Output_Rapportini_Fausto/'
OUTPUT_RIASSUNTO = _tmp + '/Output_Riassunto/'

# ── Business rules ────────────────────────────────────────────────────────────
# Rapportini Fausto and Rapportini Peve use *different* employee-eligibility,
# partner-isolation and partner-rename rules. Riassunti follow the Fausto rules.

# Rapportini Fausto (and Riassunti)
FAUSTO_ELIGIBILITY_RULES = {
    'Stefano Uboldi': ['*'],
    'Matteo Franceschini': ['*'],
    'Giovanni Verderio': ['*'],
    'Filippo Cerutti': ['*'],
    'Tony Fogliaro': ['*'],
    'Daniele Cecchetto': ['*'],
    'Francesco Cerutti': ['*'],
    'Alessandro Peverelli': ['Tag S.r.l.'],
}
FAUSTO_TO_ISOLATE_LIST = ['Frilli Srl', 'Corden Pharma Spa']
FAUSTO_DICT_PARTNER_RENAME = {'CGT Compagnia Generale Trattori Spa': 'CGT Spa'}

# Rapportini Peve
PEVE_ELIGIBILITY_RULES = {
    'Diego Attubato': ['*'],
    'Alessandro Peverelli': ['*'],
}
PEVE_TO_ISOLATE_LIST = [
    'Ab Impianti Srl',
    'Ab Service Srl',
    'Burgo Group Spa',
    'Cgt Compagnia Generale Trattori Spa',
    'Cpl Concordia Soc. Coop.',
    'Ecotermica SRL',
    'Effetre Fenice Energia Srl',
    'Engie Servizi Spa',
    'Fedrigoni Spa',
    'Grastim Srl',
    'Intergen Srl',
    'Lucart Spa',
    'Siram Spa',
]
PEVE_DICT_PARTNER_RENAME = {
    'CGT Compagnia Generale Trattori Spa': 'CGT Spa',
    'Ab Impianti Srl': 'Ab Service Srl',
}

FILTERED_PARTNERS = []


def ensure_csv_local(year, month):
    """Download the Odoo CSV from Supabase if it's not already on local disk."""
    from storage import download_to_bytes
    csv_name = f'{year}_{month}_timesheets_extraction.csv'
    local_csv = EXPORT_PATH + csv_name
    if not os.path.isfile(local_csv):
        try:
            os.makedirs(EXPORT_PATH, exist_ok=True)
            with open(local_csv, 'wb') as f:
                f.write(download_to_bytes(f'Odoo exports/{csv_name}'))
        except Exception:
            pass  # generation modules will raise their own error if file is missing

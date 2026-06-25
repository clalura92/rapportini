"""
Multi-page logo calibration script.
Adjust LOGO_X_CM and LOGO_Y_CM, then run:
    .venv\\Scripts\\python calibrate_logo_multipage.py
Open calibrate_logo_multipage.xlsx, press Ctrl+P to check every page in print preview.
"""
import os
import openpyxl
from openpyxl.drawing.image import Image
from openpyxl.drawing.spreadsheet_drawing import AbsoluteAnchor
from openpyxl.drawing.xdr import XDRPoint2D, XDRPositiveSize2D
from openpyxl.utils.units import cm_to_EMU

# ── EDIT THESE TWO VALUES ──────────────────────────────────────────────────
LOGO_X_CM = 18.2    # cm from left edge of print area  → move left = smaller
LOGO_Y_CM = -1.3    # cm from top  edge of print area  → move up   = smaller
# ──────────────────────────────────────────────────────────────────────────

LOGO_W_CM = 53 / 72 * 2.54
LOGO_H_CM = 40 / 72 * 2.54
PAGE_H_CM = 17.92    # full A4 landscape page height

_HERE     = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(_HERE, "templates", "logo_solware.png")
SRC_PATH  = os.path.join(_HERE, "Output_Rapportini_Peve", "2026_6", "2026-6 _ASSISTENZA_Icmi.xlsx")
OUT_PATH  = os.path.join(_HERE, "calibrate_logo_multipage.xlsx")

wb = openpyxl.load_workbook(SRC_PATH)
ws = wb.active

m        = ws.page_margins
left_cm  = (m.left or 0.70) * 2.54
top_cm   = (m.top  or 0.75) * 2.54

anc_x = left_cm + LOGO_X_CM
anc_y = top_cm  + LOGO_Y_CM

# Detect page count from manual row breaks; fall back to row count / 32
breaks = sorted(b.id for b in ws.row_breaks.brk) if ws.row_breaks.brk else []
if breaks:
    num_pages = len(breaks) + 1
else:
    used_rows = ws.max_row
    num_pages = max(1, -(-used_rows // 32))   # ceiling division

print(f"Pages detected: {num_pages}")
print(f"Anchor X={anc_x:.3f} cm, Y={anc_y:.3f} cm from A1 (page 1)")

for page_idx in range(1, num_pages + 1):
    img = Image(LOGO_PATH)
    img.anchor = AbsoluteAnchor(
        pos=XDRPoint2D(
            cm_to_EMU(anc_x),
            cm_to_EMU(anc_y + (page_idx - 1) * PAGE_H_CM),
        ),
        ext=XDRPositiveSize2D(cm_to_EMU(LOGO_W_CM), cm_to_EMU(LOGO_H_CM)),
    )
    ws.add_image(img)
    print(f"  Page {page_idx}: logo Y = {anc_y + (page_idx - 1) * PAGE_H_CM:.3f} cm from A1")

wb.save(OUT_PATH)
print(f"\nSaved: {OUT_PATH}")
print("Press Ctrl+P in Excel to check all pages in print preview.")

os.startfile(OUT_PATH)

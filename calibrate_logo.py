"""
Logo calibration script for Excel.
Adjust LOGO_X_CM and LOGO_Y_CM, then run:
    .venv\\Scripts\\python calibrate_logo.py
Open the generated calibrate_logo.xlsx, go to File > Print to see print preview.
"""
import os
import openpyxl
from openpyxl.drawing.image import Image
from openpyxl.drawing.spreadsheet_drawing import AbsoluteAnchor
from openpyxl.drawing.xdr import XDRPoint2D, XDRPositiveSize2D
from openpyxl.utils.units import cm_to_EMU

# ── EDIT THESE TWO VALUES ──────────────────────────────────────────────────
# Distance from the top-left corner of the PRINTABLE area (inside margins)
LOGO_X_CM = 18.2   # cm from left edge of print area  → move left = smaller number
LOGO_Y_CM =  -1.3   # cm from top  edge of print area  → move up   = smaller number
# ──────────────────────────────────────────────────────────────────────────

LOGO_W_CM = 53 / 72 * 2.54   # ~1.87 cm  (keep as-is unless size is wrong)
LOGO_H_CM = 40 / 72 * 2.54   # ~1.41 cm

_HERE      = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH  = os.path.join(_HERE, "templates", "logo_solware.png")
TMPL_PATH  = os.path.join(_HERE, "templates", "Template - Rapportino - Peve.xlsx")
OUT_PATH   = os.path.join(_HERE, "calibrate_logo.xlsx")

wb = openpyxl.load_workbook(TMPL_PATH)
ws = wb.active

m = ws.page_margins
left_cm = (m.left   or 0.70) * 2.54
top_cm  = (m.top    or 0.75) * 2.54

# AbsoluteAnchor is measured from cell A1's top-left corner (= paper edge).
# We add back the margin so "0,0" means top-left of the printable area.
anchor_x = cm_to_EMU(left_cm + LOGO_X_CM)
anchor_y = cm_to_EMU(top_cm  + LOGO_Y_CM)

img = Image(LOGO_PATH)
img.anchor = AbsoluteAnchor(
    pos=XDRPoint2D(anchor_x, anchor_y),
    ext=XDRPositiveSize2D(cm_to_EMU(LOGO_W_CM), cm_to_EMU(LOGO_H_CM)),
)
ws.add_image(img)

wb.save(OUT_PATH)
print(f"Saved: {OUT_PATH}")
print(f"Logo at X={left_cm + LOGO_X_CM:.2f} cm, Y={top_cm + LOGO_Y_CM:.2f} cm from A1 corner")
print("Open the file, press Ctrl+P to see print preview, then adjust LOGO_X_CM / LOGO_Y_CM above.")

os.startfile(OUT_PATH)

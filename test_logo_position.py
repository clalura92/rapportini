import fitz
import glob
import os
import sys

# ── EDIT THESE FOUR VALUES ────────────────────────────────────
X0 = 620
Y0 = 77
X1 = X0 + 53
Y1 = Y0 + 40
# ─────────────────────────────────────────────────────────────

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "logo_solware.png")
OUT_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_logo_position.pdf")

# Pick source PDF: pass as argument or auto-find the most recent one
if len(sys.argv) > 1:
    src = sys.argv[1]
else:
    pdfs = [p for p in glob.glob("**/*.pdf", recursive=True) if "test_logo_position" not in p]
    if not pdfs:
        print("No PDF found. Run with: python test_logo_position.py path/to/file.pdf")
        sys.exit(1)
    src = max(pdfs, key=os.path.getmtime)
    print(f"Source: {src}")

doc  = fitz.open(src)
page = doc[0]

# Wipe only the exact area where the logo will be placed
page.add_redact_annot(fitz.Rect(X0, Y0, X1, Y1), fill=(1, 1, 1))
page.apply_redactions()

rect = fitz.Rect(X0, Y0, X1, Y1)
page.insert_image(rect, filename=LOGO_PATH, keep_proportion=True)

doc.save(OUT_PATH, garbage=4, deflate=True)
doc.close()
print(f"Saved:  {OUT_PATH}")
print(f"Rect:   fitz.Rect({X0}, {Y0}, {X1}, {Y1})")

os.startfile(OUT_PATH)

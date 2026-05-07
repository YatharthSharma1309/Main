"""
Re-runs the pipeline and saves crops + CSV to ./pipeline_output/
"""
import os, sys, io, csv
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import fitz
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))

from src.pdf_utils import (
    crop_questions_from_pdf, extract_figures_from_pdf,
    build_question_mapping, extract_question_texts_from_pdf,
)
from src.helpers import sanitize, latex_to_unicode
from api import _is_useful_text, _extraction_summary

OUT_DIR = os.path.join(os.path.dirname(__file__), "pipeline_output")
Q_DIR   = os.path.join(OUT_DIR, "question_crops")
FIG_DIR = os.path.join(OUT_DIR, "figures")
os.makedirs(Q_DIR,   exist_ok=True)
os.makedirs(FIG_DIR, exist_ok=True)

# ── 1. Build synthetic PDFs ──────────────────────────────────────────────────
QUESTIONS = [
    ("Q: 1", "Let p be a prime number and k be a positive integer.\n\n"
             "If p divides k2, then which of these is DEFINITELY divisible by p ?\n\n"
             "1 only k\n2 only k and 7 k\n3 only k, 7 k and k3\n4 all - k/2, k, 7 k and k3"),
    ("Q: 2", "sqrt(n) is a natural number such that n > 1.\n\n"
             "Which of these can DEFINITELY be expressed as a product of primes?\n\n"
             "i) sqrt(n)   ii) n   iii) n/2\n\n"
             "1 only ii)\n2 only i) and ii)\n3 all - i), ii) and iii)\n4 (cannot be determined without knowing n)"),
    ("Q: 3", "The HCF of k and 93 is 31, where k is a natural number.\n\n"
             "Which of these CAN be true for SOME VALUES of k ?\n\n"
             "i) k is a multiple of 31.\nii) k is a multiple of 93.\n"
             "iii) k is an even number.\niv) k is an odd number.\n\n"
             "1 only ii) and iii)\n2 only i), ii) and iii)\n"
             "3 only i), iii) and iv)\n4 all - i), ii), iii) and iv)"),
    ("Q: 4", "Let p and q be two natural numbers such that p > q. "
             "When p is divided by q, the remainder is r.\n\n"
             "i) r CANNOT be (p - q).\nii) r CAN either be q or (p - q).\n"
             "iii) r is DEFINITELY less than q.\n\n"
             "Which of the above statements is/are true?\n\n"
             "1 only ii)\n2 only iii)\n3 only i) and iii)\n"
             "4 (cannot be determined without knowing the values of p, q and r)"),
]
ANSWERS = {1: "3", 2: "2", 3: "3", 4: "2"}

q_path = os.path.join(OUT_DIR, "test_questions.pdf")
a_path = os.path.join(OUT_DIR, "test_answers.pdf")

q_doc = fitz.open()
page  = q_doc.new_page(width=595, height=842)
page.insert_text((50, 50), "Real Numbers", fontsize=12, fontname="helv")
page.insert_text((400, 50), "CLASS 10",    fontsize=10, fontname="helv")
y = 90
for label, body in QUESTIONS:
    page.insert_text((50, y), label, fontsize=11, fontname="hebo")
    y += 18
    for line in body.split('\n'):
        page.insert_text((60, y), line, fontsize=10, fontname="helv")
        y += 15
    y += 10
q_doc.save(q_path); q_doc.close()

a_doc  = fitz.open()
a_page = a_doc.new_page(width=595, height=842)
a_page.insert_text((50,  50), "Answer Key",  fontsize=14, fontname="hebo")
a_page.insert_text((50,  80), "The table below gives the correct answer for each question.", fontsize=10)
a_page.insert_text((50, 110), "Q.No  Correct Answers", fontsize=11, fontname="hebo")
y = 130
for q_num, ans in ANSWERS.items():
    a_page.insert_text((50, y), f"{q_num}  {ans}", fontsize=11)
    y += 18
a_doc.save(a_path); a_doc.close()

print(f"PDFs saved to {OUT_DIR}")

# ── 2. Run pipeline ──────────────────────────────────────────────────────────
crop_by_qnum = crop_questions_from_pdf(q_path, Q_DIR)
fig_data     = extract_figures_from_pdf(q_path, FIG_DIR)
mapping      = build_question_mapping(q_path, a_path, fig_data)
pdf_texts    = extract_question_texts_from_pdf(q_path)

results = []
for entry in mapping:
    q_num = entry["question_num"]
    figs  = entry.get("figure") or []
    raw   = pdf_texts.get(q_num, "")
    if _is_useful_text(raw):
        source = "pymupdf"
        q_text = sanitize(latex_to_unicode(raw))
    else:
        source = "vision (would call haiku)"
        q_text = "[vision fallback]"
    results.append({
        "question_num":  str(q_num),
        "question_text": q_text,
        "answers":       sanitize(latex_to_unicode(entry.get("answer", "N/A") or "N/A")),
        "figures":       ", ".join(os.path.basename(p) for p in figs),
        "source":        source,
        "crop_path":     crop_by_qnum.get(q_num, ""),
    })

# ── 3. Write CSV ─────────────────────────────────────────────────────────────
csv_path = os.path.join(OUT_DIR, "extraction_results.csv")
with open(csv_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=["question_num","question_text","answers","figures","source","crop_path"])
    writer.writeheader()
    writer.writerows(results)

print(f"\nCSV saved:  {csv_path}")
print(f"Crops dir:  {Q_DIR}")
print(f"\nCrop files generated:")
for r in results:
    crop = r["crop_path"]
    exists = os.path.exists(crop) if crop else False
    print(f"  Q{r['question_num']}: {os.path.basename(crop) if crop else 'N/A'}  [{'exists' if exists else 'MISSING'}]")

print(f"\nExtraction summary: {_extraction_summary(results)}")

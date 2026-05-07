"""
Full pipeline test for the PyMuPDF-first / Claude Haiku fallback feature.

Creates a synthetic PDF matching the exact format from the uploaded PDF
(Class 10 Math, Real Numbers, Q: N format) then runs the full pipeline.
"""

import os, sys, io, textwrap
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import fitz  # PyMuPDF
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))

DIVIDER = "-" * 60

# ── 1. Build synthetic question PDF ─────────────────────────────────────────
print("=" * 60)
print("1. BUILDING SYNTHETIC QUESTION PDF  (Q: N format)")
print("=" * 60)

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

# Build question PDF
q_doc = fitz.open()
page = q_doc.new_page(width=595, height=842)  # A4

# Header
page.insert_text((50, 50), "Real Numbers", fontsize=12, fontname="helv")
page.insert_text((400, 50), "CLASS 10", fontsize=10, fontname="helv")

y = 90
for label, body in QUESTIONS:
    # Question label (this is what _Q_PATTERN must match)
    page.insert_text((50, y), label, fontsize=11, fontname="hebo")
    y += 18
    for line in body.split('\n'):
        page.insert_text((60, y), line, fontsize=10, fontname="helv")
        y += 15
    y += 10  # gap between questions

q_path = os.path.join(os.path.dirname(__file__), "_test_questions.pdf")
q_doc.save(q_path)
q_doc.close()
print(f"  Questions PDF created: {q_path}  ({len(QUESTIONS)} questions)")

# ── 2. Build synthetic answer PDF  ──────────────────────────────────────────
ANSWERS = {1: "3", 2: "2", 3: "3", 4: "2"}   # from the answer key screenshot

a_doc = fitz.open()
a_page = a_doc.new_page(width=595, height=842)
a_page.insert_text((50, 50), "Answer Key", fontsize=14, fontname="hebo")
a_page.insert_text((50, 80), "The table below gives the correct answer for each question.", fontsize=10)
a_page.insert_text((50, 110), "Q.No  Correct Answers", fontsize=11, fontname="hebo")
y = 130
for q_num, ans in ANSWERS.items():
    a_page.insert_text((50, y), f"{q_num}  {ans}", fontsize=11)
    y += 18

a_path = os.path.join(os.path.dirname(__file__), "_test_answers.pdf")
a_doc.save(a_path)
a_doc.close()
print(f"  Answers PDF created:   {a_path}  ({len(ANSWERS)} answers)")


# ── 3. Test: extract_question_texts_from_pdf ────────────────────────────────
print()
print("=" * 60)
print("2. TEST: extract_question_texts_from_pdf  (PyMuPDF)")
print("=" * 60)

from src.pdf_utils import extract_question_texts_from_pdf

pdf_texts = extract_question_texts_from_pdf(q_path)
print(f"  Questions detected: {len(pdf_texts)}")

all_ok = True
for q_num, (_, body) in enumerate(QUESTIONS, 1):
    extracted = pdf_texts.get(q_num, "")
    found_label = f"Q: {q_num}" in extracted
    found_body  = body.split('\n')[0][:20] in extracted
    ok = bool(extracted) and found_label
    if not ok:
        all_ok = False
    print(f"  [{'OK ' if ok else 'FAIL'}] Q{q_num}: {len(extracted)} chars extracted  "
          f"| label={'yes' if found_label else 'NO'}")

print(f"\n  Result: {'ALL DETECTED' if all_ok else 'SOME MISSING'}")


# ── 4. Test: answer parsing  ─────────────────────────────────────────────────
print()
print("=" * 60)
print("3. TEST: parse_answers  (answer key format)")
print("=" * 60)

from src.pdf_processor import PDFProcessor
processor = PDFProcessor(q_path, a_path)
answers_text = processor.extract_text_from_pdf(a_path)
answers_list = processor.parse_answers(answers_text)

print(f"  Raw extracted text from answer PDF:")
for line in answers_text.strip().split('\n'):
    if line.strip():
        print(f"    | {line}")

print(f"\n  Parsed answers: {answers_list}")
expected = ['3', '2', '3', '2']
ok = answers_list[:4] == expected
print(f"  Expected:       {expected}")
print(f"  Result: {'PASS' if ok else 'FAIL'}")


# ── 5. Full pipeline: build_question_mapping ─────────────────────────────────
print()
print("=" * 60)
print("4. TEST: full pipeline  (crop + map + pymupdf-first transcription)")
print("=" * 60)

import tempfile
from src.pdf_utils import crop_questions_from_pdf, extract_figures_from_pdf, build_question_mapping
from src.helpers import sanitize, latex_to_unicode

work_dir  = tempfile.mkdtemp()
q_dir     = os.path.join(work_dir, "questions")
fig_dir   = os.path.join(work_dir, "figures")
os.makedirs(q_dir);  os.makedirs(fig_dir)

crop_by_qnum = crop_questions_from_pdf(q_path, q_dir)
fig_data     = extract_figures_from_pdf(q_path, fig_dir)
mapping      = build_question_mapping(q_path, a_path, fig_data)

print(f"  Crops generated:  {len(crop_by_qnum)}")
print(f"  Mapping entries:  {len(mapping)}")

# Run _is_useful_text against each extracted block
from api import _is_useful_text, _extraction_summary

print()
print("  Per-question PyMuPDF text quality check:")
for entry in mapping:
    q_num = entry["question_num"]
    text  = pdf_texts.get(q_num, "")
    useful = _is_useful_text(text)
    answer = entry.get("answer", "N/A")
    print(f"    Q{q_num}: {len(text):>4} chars | useful={str(useful):<5} | answer={answer}")


# ── 6. Simulate _transcribe_all_parallel (PyMuPDF path, no API call) ─────────
print()
print("=" * 60)
print("5. SIMULATED TRANSCRIPTION  (PyMuPDF path — no API call needed)")
print("=" * 60)

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
        q_text = "[vision fallback would fire here]"

    results.append({
        "question_num":  str(q_num),
        "question_text": q_text,
        "answers":       sanitize(latex_to_unicode(entry.get("answer", "N/A") or "N/A")),
        "figures":       ", ".join(os.path.basename(p) for p in figs),
        "source":        source,
    })

summary = _extraction_summary(results)
print(f"  Extraction summary header: {summary}\n")

for r in results:
    print(DIVIDER)
    print(f"  Q{r['question_num']}  [{r['source']}]  answer={r['answers']}")
    wrapped = textwrap.fill(r['question_text'].replace('\n', ' '), 54,
                            initial_indent="  ", subsequent_indent="  ")
    print(wrapped)

print(DIVIDER)
vision_count = sum(1 for r in results if r['source'].startswith('vision'))
pdf_count    = sum(1 for r in results if r['source'] == 'pymupdf')
print(f"\n  PyMuPDF: {pdf_count}  |  Vision fallback needed: {vision_count}")
print(f"  {'ALL GOOD - no vision API calls needed for this PDF type!' if vision_count == 0 else 'Some questions need vision.'}")

# Cleanup
import shutil
shutil.rmtree(work_dir)
os.remove(q_path)
os.remove(a_path)

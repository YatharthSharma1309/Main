"""
End-to-end pipeline test using existing question crops in questions/
and a simulated answer key matching the PDF format shown.
"""

import os, sys, textwrap, io
# Force UTF-8 output so math symbols (sqrt, etc.) don't crash on Windows terminals
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))

# ─── 1. Pattern fix smoke-test ──────────────────────────────────────────────
print("=" * 60)
print("1. QUESTION PATTERN TEST")
print("=" * 60)

import re
from src.pdf_utils import _Q_PATTERN, _q_num

samples = [
    ("Q: 1 Let p be a prime number",      1,  True),
    ("Q: 14 The prime factorisation",     14, True),
    ("Q: 5 Two representations",           5,  True),
    ("Q.1. some question",                 1,  True),
    ("1. some question",                   1,  True),
    ("1) some question",                   1,  True),
    ("Q1. some question",                  1,  True),
    ("Real Numbers  CLASS 10",             0,  False),  # header – should NOT match
    ("Answer Key",                         0,  False),
]

all_ok = True
for text, expected_num, should_match in samples:
    m = _Q_PATTERN.match(text)
    matched = m is not None
    num = _q_num(m) if m else None
    ok = (matched == should_match) and (not should_match or num == expected_num)
    status = "OK " if ok else "FAIL"
    if not ok:
        all_ok = False
    print(f"  [{status}] {'MATCH q='+str(num) if matched else 'NO MATCH':15}  <- {text[:50]}")

print(f"\n  Pattern test: {'ALL PASSED' if all_ok else 'FAILURES FOUND'}\n")


# ─── 2. Answer parser test ───────────────────────────────────────────────────
print("=" * 60)
print("2. ANSWER PARSER TEST")
print("=" * 60)

from src.pdf_processor import PDFProcessor
p = PDFProcessor("", "")

# Simulated extractions of your answer key PDF (both layout variants)
same_line_table = """
Math  Real Numbers  CLASS 10  Answer Key

The table below gives the correct answer for each multiple-choice question in this test.

Q.No  Correct Answers
1  3
2  2
3  3
4  2
"""

alt_line_table = """
Math  Real Numbers  CLASS 10  Answer Key
Q.No
Correct Answers
1
3
2
2
3
3
4
2
"""

jee_format = """
1. (3)
2. (2)
3. (3)
4. (2)
"""

for label, text in [("Same-line table", same_line_table),
                    ("Alt-line table",  alt_line_table),
                    ("JEE format",      jee_format)]:
    answers = p.parse_answers(text)
    # Strip parentheses for comparison — "(3)" and "3" are the same answer
    stripped = [a.strip("()") for a in answers[:4]]
    ok = stripped == ['3', '2', '3', '2']
    print(f"  [{'OK ' if ok else 'FAIL'}] {label:20} -> {answers[:4]}")

print()


# ─── 3. Vision transcription on existing crops ──────────────────────────────
print("=" * 60)
print("3. VISION TRANSCRIPTION  (model=haiku, crops in questions/)")
print("=" * 60)

from src.vision import call_vision

crops_dir = os.path.join(os.path.dirname(__file__), "questions")
crop_files = sorted(f for f in os.listdir(crops_dir) if f.endswith(".png"))

if not crop_files:
    print("  No crop files found in questions/. Upload a PDF first.")
    sys.exit(1)

print(f"  Found {len(crop_files)} crop(s). Transcribing with Claude Haiku...\n")

results = []
for fname in crop_files:
    path = os.path.join(crops_dir, fname)
    try:
        text = call_vision(path, figure_count=0, model="haiku")
        results.append((fname, text, None))
    except Exception as e:
        results.append((fname, None, str(e)))

# ─── 4. Print results table ──────────────────────────────────────────────────
print("=" * 60)
print("4. RESULTS")
print("=" * 60)

for fname, text, err in results:
    print(f"\n{'-'*55}")
    print(f"  Crop : {fname}")
    if err:
        print(f"  ERROR: {err}")
    else:
        wrapped = textwrap.fill(text.replace('\n', ' '), width=54,
                                initial_indent="  ", subsequent_indent="  ")
        print(f"  Text : {wrapped[2:]}")

print(f"\n{'-'*55}")
print(f"\nDone. {sum(1 for _,t,_ in results if t)} / {len(results)} questions transcribed successfully.")

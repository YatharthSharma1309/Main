"""End-to-end extraction test for a 14-question PDF.

Usage:
    TEST_PDF_PATH="C:\\path\\to\\14q.pdf" pytest tests/test_14q_pdf.py -v

Set TEST_PDF_PATH to the path of the uploaded 14-question PDF before running.
The test is automatically skipped when the env var is not set or the file is missing.
"""
import io
import os
import zipfile

import openpyxl
import pytest
import requests

BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:5000")
PDF_PATH = os.environ.get("TEST_PDF_PATH", "")


@pytest.mark.skipif(
    not PDF_PATH or not os.path.exists(PDF_PATH),
    reason="TEST_PDF_PATH env var not set or file does not exist",
)
def test_14_question_extraction():
    with open(PDF_PATH, "rb") as f:
        resp = requests.post(
            f"{BASE_URL}/api/extract-single",
            files={"pdf": (os.path.basename(PDF_PATH), f, "application/pdf")},
            data={"model": "haiku"},
            timeout=300,
        )
    assert resp.status_code == 200, f"API returned {resp.status_code}: {resp.text[:500]}"
    assert resp.headers.get("Content-Type", "").startswith("application/zip"), \
        "Expected ZIP response"

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert "extraction_results.xlsx" in zf.namelist(), \
        f"extraction_results.xlsx not in ZIP. Files: {zf.namelist()}"

    wb = openpyxl.load_workbook(io.BytesIO(zf.read("extraction_results.xlsx")))
    ws = wb["Questions"]
    rows = [row for row in ws.iter_rows(min_row=2, values_only=True) if any(c for c in row)]

    assert len(rows) == 14, f"Expected 14 data rows, got {len(rows)}"

    problems = []
    for row in rows:
        q_num      = row[0]
        q_text     = row[1]
        q_image    = row[2]
        validation = row[6]

        if validation and "Missing" in str(validation):
            problems.append(f"Q{q_num}: {validation}")

        if q_image:
            assert "/" in str(q_image), \
                f"Q{q_num} question_image should be ZIP-relative (e.g. 'questions/q_1.png'), got: {q_image!r}"

        assert q_text and len(str(q_text).strip()) > 5, \
            f"Q{q_num} has empty or very short question text: {q_text!r}"

    assert not problems, "Some questions failed validation:\n" + "\n".join(problems)

    # Verify all image paths in the Excel actually exist in the ZIP
    zip_files = set(zf.namelist())
    for row in rows:
        q_num   = row[0]
        q_image = str(row[2] or "").strip()
        if q_image:
            assert q_image in zip_files, \
                f"Q{q_num} image path '{q_image}' referenced in Excel but not found in ZIP"

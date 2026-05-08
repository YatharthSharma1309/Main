import os
import json
import base64
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
load_dotenv()
from difflib import SequenceMatcher
from flask import Flask, request, send_file, jsonify
from werkzeug.utils import secure_filename
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
import pandas as pd
import requests as _http
from rapidfuzz import fuzz as _fuzz
from src.pdf_processor import PDFProcessor
from src.quickstart import parse_pdf
from src.helpers import (
    sanitize,
    # clean_question,       # only used by /api/clean-excel (disabled)
    # call_search_api,      # only used by /api/evaluate*   (disabled)
    check_correctness,
    # build_evaluation_excel,  # only used by /api/evaluate* (disabled)
    build_validation_excel,
    latex_to_unicode, FIGURE_URL_RE, FIG_FONT, inline_fig_labels,
)
from src.pdf_utils import (
    extract_figures_from_pdf,
    build_question_mapping, crop_questions_from_pdf,
    extract_figures_per_question,
    extract_question_texts_from_pdf,
    pdf_pages_to_png, save_page_crops, detect_layout_fitz,
    extract_figures_from_pages, map_figures_to_questions_on_pages,
    build_pdf_map, crop_from_map,
)
from src.vision import call_vision, _MODEL_ALIASES
from src.mathpix import call_mathpix
from src.page_classifier import classify_page_with_gpt

app = Flask(__name__)

UPLOAD_FOLDER = tempfile.gettempdir()
ALLOWED_EXTENSIONS = {'pdf'}
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE


_VISION_MAX_WORKERS = 8

_VLM_VALIDATE_URL   = "http://localhost:11434/api/chat"
_VLM_VALIDATE_MODEL = "qwen2.5vl:7b"

_VLM_COMPARE_PROMPT = (
    "You are a precise exam-question validator.\n\n"
    "The image shows a question cropped from the original exam PDF.\n"
    "Below is the text that was transcribed for this question:\n\n"
    "TRANSCRIPTION:\n{excel_text}\n\n"
    "FIGURE PLACEHOLDERS: The transcription may contain tokens like [Figure 1], "
    "[Figure 2], etc. Each token represents an embedded visual element (figure, "
    "diagram, graph, or image-based answer option) at that position in the question. "
    "A placeholder is correct if a visual element appears at the corresponding "
    "position in the image, and the numbering follows reading order (top-to-bottom).\n\n"
    "Decide whether the transcription is an accurate and complete representation "
    "of the question in the image.\n\n"
    "Evaluate:\n"
    "1. Is the question stem word-for-word correct (wording, numbers, math, units)?\n"
    "2. Are all answer choices present and correctly transcribed?\n"
    "3. Is mathematical notation (fractions, exponents, symbols) accurately captured?\n"
    "4. Are [Figure N] placeholders present wherever a visual appears, in the right positions?\n\n"
    "If the transcription is not a perfect match, list each specific discrepancy in the "
    "'issues' array. Each issue must be concrete and quote the conflicting text, e.g. "
    "\"PDF says '4 m/s²' but transcription says '4 m/s'\", "
    "\"Answer choice (3) is missing\", "
    "\"[Figure 1] placeholder missing before the diagram\". "
    "If there are no issues, return an empty array.\n\n"
    'Return ONLY valid JSON with no surrounding text:\n'
    '{{"match": true/false, "issues": ["specific discrepancy 1", "..."], "confidence": 0.0-1.0, "figure_count": N}}\n'
    'where figure_count is the number of distinct visual elements (figures, diagrams, graphs) '
    'visible in the image (not in the transcription).'
)


def _vlm_compare_question(image_path: str, excel_text: str) -> dict:
    """Send the PDF question crop + Excel transcription to a VLM and get a match verdict."""
    prompt = _VLM_COMPARE_PROMPT.format(excel_text=excel_text.strip() or "(empty)")
    try:
        with open(image_path, "rb") as fh:
            image_b64 = base64.b64encode(fh.read()).decode()
        payload = {
            "model": _VLM_VALIDATE_MODEL,
            "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
            "stream": False,
            "options": {"temperature": 0, "num_predict": 512},
        }
        resp = _http.post(_VLM_VALIDATE_URL, json=payload, timeout=120)
        resp.raise_for_status()
        raw = resp.json()["message"]["content"].strip()
        return json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
    except Exception as exc:
        return {"match": False, "issues": [f"VLM error: {exc}"], "confidence": 0.0, "error": True}



def _normalise_cols(df: pd.DataFrame) -> dict:
    """Return {normalised_name: original_column_name} for all columns."""
    return {
        c.strip().lower().replace(" ", "_").replace("#", "num"): c
        for c in df.columns
    }


def _pick_col(norm: dict, aliases: list):
    for a in aliases:
        if a in norm:
            return norm[a]
    return None


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def validate_request():
    if 'questions_pdf' not in request.files or 'answers_pdf' not in request.files:
        return False, "Missing required files: 'questions_pdf' and 'answers_pdf'"
    questions_file = request.files['questions_pdf']
    answers_file = request.files['answers_pdf']
    if questions_file.filename == '' or answers_file.filename == '':
        return False, "File names cannot be empty"
    if not (allowed_file(questions_file.filename) and allowed_file(answers_file.filename)):
        return False, "Only PDF files are allowed"
    return True, None


# ── Error handlers ────────────────────────────────────────────────────────────

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"error": f"File too large. Maximum file size is {MAX_FILE_SIZE // (1024 * 1024)}MB"}), 413

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error"}), 500


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/', methods=['GET'])
def root():
    frontend_build = os.path.join(os.path.dirname(__file__), 'frontend', 'dist', 'index.html')
    if os.path.exists(frontend_build):
        return send_file(frontend_build)
    return jsonify({
        "service": "QA-PDF-Extractor-API",
        "status": "running",
        "note": "Open http://localhost:3000 for the UI, or call /api/* endpoints directly.",
        "endpoints": ["/health", "/api/extract-single", "/api/extract", "/api/pdf-to-images",
                      "/api/validate", "/api/extract-mathpix", "/api/general-purpose-extraction"],
    }), 200


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "healthy", "service": "QA-PDF-Extractor-API", "version": "1.0.0"}), 200


@app.route('/api/extract', methods=['POST'])
def extract_qa():
    questions_path = None
    answers_path = None
    try:
        is_valid, error_msg = validate_request()
        if not is_valid:
            return jsonify({"error": error_msg}), 400

        questions_file = request.files['questions_pdf']
        answers_file   = request.files['answers_pdf']

        questions_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(questions_file.filename))
        answers_path   = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(answers_file.filename))

        questions_file.save(questions_path)
        answers_file.save(answers_path)

        questions_md   = parse_pdf(questions_path)["markdown"]
        processor      = PDFProcessor(questions_path, answers_path)
        questions_list = processor.parse_questions(questions_md)
        answers_dict   = processor.parse_answers(processor.extract_text_from_pdf(answers_path))

        if not questions_list:
            return jsonify({"error": "No questions could be extracted from the PDF"}), 422

        output_excel = os.path.join(app.config['UPLOAD_FOLDER'], 'extracted_qa.xlsx')
        wb = Workbook()
        ws = wb.active
        ws.title = "Q&A"

        max_figs = max((len(FIGURE_URL_RE.findall(q)) for q in questions_list), default=0)
        ans_col  = 3 + max_figs
        header   = ["Question #", "Question"] + [f"Figure {n}" for n in range(1, max_figs + 1)] + ["Correct Answer"]
        ws.append(header)
        header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True)
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        for idx, question in enumerate(questions_list, start=1):
            answer = answers_dict.get(idx, "N/A")
            urls   = FIGURE_URL_RE.findall(question)
            q_text = latex_to_unicode(sanitize(inline_fig_labels(question)))

            ws.append([idx, q_text] + [None] * max_figs + [sanitize(answer)])
            row = ws.max_row
            ws.cell(row, 1).alignment = Alignment(horizontal="center", vertical="top")
            ws.cell(row, 2).alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            for n, url in enumerate(urls):
                fig_cell           = ws.cell(row, 3 + n)
                fig_cell.value     = f"View Figure {n + 1}"
                fig_cell.hyperlink = url
                fig_cell.font      = FIG_FONT
                fig_cell.alignment = Alignment(horizontal="center", vertical="top")
            ws.cell(row, ans_col).alignment = Alignment(horizontal="center", vertical="center")

        ws.column_dimensions['A'].width = 12
        ws.column_dimensions['B'].width = 60
        for n in range(max_figs):
            ws.column_dimensions[get_column_letter(3 + n)].width = 15
        ws.column_dimensions[get_column_letter(ans_col)].width = 18
        wb.save(output_excel)

        return send_file(
            output_excel,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'qa_extract_{len(questions_list)}q.xlsx',
        )

    except FileNotFoundError as e:
        return jsonify({"error": f"File not found: {str(e)}"}), 404
    except Exception as e:
        return jsonify({"error": f"Processing error: {str(e)}"}), 500
    finally:
        for path in (questions_path, answers_path):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


# ── DISABLED: Evaluate (PDFs) ─────────────────────────────────────────────────
# @app.route('/api/evaluate', methods=['POST'])
# def evaluate_qa():
#     questions_path = None
#     answers_path = None
#     try:
#         is_valid, error_msg = validate_request()
#         if not is_valid:
#             return jsonify({"error": error_msg}), 400
#
#         agent_id        = request.form.get("agent_id", "524829a7-ad2d-4bd4-b094-3a8ef5b62a9e")
#         deployment_slug = request.form.get("deployment_slug", "test123")
#
#         questions_file = request.files['questions_pdf']
#         answers_file   = request.files['answers_pdf']
#
#         questions_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(questions_file.filename))
#         answers_path   = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(answers_file.filename))
#
#         questions_file.save(questions_path)
#         answers_file.save(answers_path)
#
#         processor      = PDFProcessor(questions_path, answers_path)
#         questions_list = processor.parse_questions(processor.extract_text_from_pdf(questions_path))
#         answers_list   = processor.parse_answers(processor.extract_text_from_pdf(answers_path))
#
#         if not questions_list:
#             return jsonify({"error": "No questions could be parsed from the PDF"}), 422
#
#         api_responses, statuses = [], []
#         for idx, question in enumerate(questions_list):
#             correct_answer = answers_list[idx] if idx < len(answers_list) else "N/A"
#             api_resp = call_search_api(question, agent_id, deployment_slug)
#             api_responses.append(api_resp)
#             statuses.append(check_correctness(api_resp, correct_answer))
#
#         output_excel = os.path.join(app.config['UPLOAD_FOLDER'], 'evaluation_results.xlsx')
#         build_evaluation_excel(
#             questions_list,
#             [answers_list[i] if i < len(answers_list) else "N/A" for i in range(len(questions_list))],
#             api_responses,
#             statuses,
#             output_excel,
#         )
#
#         return send_file(
#             output_excel,
#             mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
#             as_attachment=True,
#             download_name=f'evaluation_{len(statuses)}q.xlsx',
#         )
#
#     except Exception as e:
#         return jsonify({"error": f"Processing error: {str(e)}"}), 500
#     finally:
#         for path in (questions_path, answers_path):
#             try:
#                 if path and os.path.exists(path):
#                     os.remove(path)
#             except Exception:
#                 pass


# ── DISABLED: Evaluate (Excel) ────────────────────────────────────────────────
# @app.route('/api/evaluate-excel', methods=['POST'])
# def evaluate_from_excel():
#     excel_path = None
#     try:
#         if 'qa_excel' not in request.files:
#             return jsonify({"error": "Missing required file: 'qa_excel'"}), 400
#
#         excel_file = request.files['qa_excel']
#         if excel_file.filename == '':
#             return jsonify({"error": "File name cannot be empty"}), 400
#         if not excel_file.filename.lower().endswith(('.xlsx', '.xls')):
#             return jsonify({"error": "Only Excel files (.xlsx) are accepted"}), 400
#
#         agent_id        = request.form.get("agent_id", "524829a7-ad2d-4bd4-b094-3a8ef5b62a9e")
#         deployment_slug = request.form.get("deployment_slug", "test123")
#
#         excel_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(excel_file.filename))
#         excel_file.save(excel_path)
#
#         ws_in = load_workbook(excel_path).active
#         rows = [r for r in ws_in.iter_rows(min_row=2, values_only=True) if r[0] is not None]
#
#         if not rows:
#             return jsonify({"error": "No data rows found in the Excel file"}), 422
#
#         questions_list = [str(r[1]) if r[1] is not None else "" for r in rows]
#         answers_list   = [str(r[2]) if r[2] is not None else "N/A" for r in rows]
#
#         api_responses, statuses = [], []
#         for question, correct_answer in zip(questions_list, answers_list):
#             api_resp = call_search_api(question, agent_id, deployment_slug)
#             api_responses.append(api_resp)
#             statuses.append(check_correctness(api_resp, correct_answer))
#
#         output_excel = os.path.join(app.config['UPLOAD_FOLDER'], 'evaluation_from_excel.xlsx')
#         build_evaluation_excel(questions_list, answers_list, api_responses, statuses, output_excel)
#
#         return send_file(
#             output_excel,
#             mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
#             as_attachment=True,
#             download_name=f'evaluation_{len(statuses)}q.xlsx',
#         )
#
#     except Exception as e:
#         return jsonify({"error": f"Processing error: {str(e)}"}), 500
#     finally:
#         try:
#             if excel_path and os.path.exists(excel_path):
#                 os.remove(excel_path)
#         except Exception:
#             pass


# ── DISABLED: Clean Excel ─────────────────────────────────────────────────────
# @app.route('/api/clean-excel', methods=['POST'])
# def clean_excel():
#     excel_path = None
#     try:
#         if 'qa_excel' not in request.files:
#             return jsonify({"error": "Missing required file: 'qa_excel'"}), 400
#
#         excel_file = request.files['qa_excel']
#         if excel_file.filename == '':
#             return jsonify({"error": "File name cannot be empty"}), 400
#         if not excel_file.filename.lower().endswith(('.xlsx', '.xls')):
#             return jsonify({"error": "Only Excel files (.xlsx) are accepted"}), 400
#
#         excel_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(excel_file.filename))
#         excel_file.save(excel_path)
#
#         wb = load_workbook(excel_path)
#         ws = wb.active
#         for row in ws.iter_rows(min_row=2):
#             cell = row[1]  # column B — Question
#             if cell.value:
#                 cell.value = sanitize(clean_question(str(cell.value)))
#
#         output_path = os.path.join(app.config['UPLOAD_FOLDER'], 'cleaned_qa.xlsx')
#         wb.save(output_path)
#
#         return send_file(
#             output_path,
#             mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
#             as_attachment=True,
#             download_name='cleaned_qa.xlsx',
#         )
#
#     except Exception as e:
#         return jsonify({"error": f"Processing error: {str(e)}"}), 500
#     finally:
#         try:
#             if excel_path and os.path.exists(excel_path):
#                 os.remove(excel_path)
#         except Exception:
#             pass


def _render_all_pages(pdf_path: str, output_dir: str, scale: float = 2.0) -> list:
    """Render every page of a PDF to a PNG at the given scale.

    Returns [(page_idx, img_path, page_w_pts, page_h_pts, img_h_px), ...].
    img_h_px is the rendered pixel height (= page_h_pts * scale).
    """
    import fitz
    mat = fitz.Matrix(scale, scale)
    doc = fitz.open(pdf_path)
    pages = []
    for page_idx, page in enumerate(doc):
        pix  = page.get_pixmap(matrix=mat)
        path = os.path.join(output_dir, f'_pg{page_idx:03d}.png')
        pix.save(path)
        pages.append((page_idx, path, page.rect.width, page.rect.height, pix.height))
    doc.close()
    return pages


def _classify_pages_vision(page_records: list, model: str) -> dict:
    """Classify each rendered page image as 'questions', 'answers', or 'other'.

    page_records: list of (page_idx, img_path, ...) from _render_all_pages.
    Returns {page_idx: classification_string}.
    """
    from src.claude_vision import classify_page_claude

    def _classify(record):
        page_idx, img_path = record[0], record[1]
        try:
            return page_idx, classify_page_claude(img_path, model)
        except Exception:
            return page_idx, "other"

    with ThreadPoolExecutor(max_workers=_VISION_MAX_WORKERS) as executor:
        futures = [executor.submit(_classify, r) for r in page_records]
    return dict(f.result() for f in futures)


def _crop_questions_vision(pdf_path: str, output_dir: str, model: str) -> tuple:
    """Vision-based question cropping + answer extraction for scanned PDFs.

    Renders ALL pages, classifies each via Claude, then:
    - question / other pages → extract individual question positions and crop
    - answers pages          → extract answers by question number

    Returns (crops, answers_dict) where:
        crops        = {q_num: crop_path}
        answers_dict = {q_num: answer_text}
    """
    import fitz
    from src.claude_vision import (
        extract_questions_from_page_claude,
        extract_answers_from_page_claude,
    )

    resolved_model = _MODEL_ALIASES.get(model, model)
    SCALE = 2.0

    page_records    = _render_all_pages(pdf_path, output_dir, scale=SCALE)
    classifications = _classify_pages_vision(page_records, resolved_model)

    mat  = fitz.Matrix(SCALE, SCALE)
    doc  = fitz.open(pdf_path)
    crops        = {}
    answers_dict = {}

    for page_idx, img_path, page_w, page_h, img_h_px in page_records:
        label = classifications.get(page_idx, "other")

        # Answer pages: extract answers then discard the image.
        if label == "answers":
            try:
                answers_dict.update(extract_answers_from_page_claude(img_path, resolved_model))
            except Exception:
                pass
            try:
                os.remove(img_path)
            except Exception:
                pass
            continue

        # Question / other pages: attempt question extraction.
        # "other" pages are retried because marks like [1][2][3] beside questions
        # can cause the classifier to mislabel a question page as "other".
        page = doc[page_idx]

        try:
            entries = extract_questions_from_page_claude(img_path, resolved_model)
        except Exception:
            entries = []
        finally:
            try:
                os.remove(img_path)
            except Exception:
                pass

        # No questions on an "other"-labelled page → silently skip.
        if not entries and label != "questions":
            continue

        if not entries:
            # Fallback for confirmed question pages: keep full page as one crop.
            pix = page.get_pixmap(matrix=mat)
            out = os.path.join(output_dir, f'question_{page_idx + 1:03d}.png')
            pix.save(out)
            crops[page_idx + 1] = out
            continue

        entries_sorted = sorted(entries, key=lambda e: e["y_px"])
        PADDING_PX = 15
        FOOTER_PX  = 60

        for i, entry in enumerate(entries_sorted):
            q_num  = entry["question_num"]
            top_px = max(0, entry["y_px"] - PADDING_PX)
            bot_px = (entries_sorted[i + 1]["y_px"] + PADDING_PX
                      if i + 1 < len(entries_sorted) else img_h_px - FOOTER_PX)
            bot_px = min(img_h_px, bot_px)

            if bot_px - top_px < 20:
                continue

            y0    = top_px / SCALE
            y1    = bot_px / SCALE
            clip  = fitz.Rect(0, y0, page_w, y1)
            q_pix = page.get_pixmap(matrix=mat, clip=clip)
            out   = os.path.join(output_dir, f'question_{q_num:03d}.png')
            q_pix.save(out)
            crops[q_num] = out

    doc.close()
    return crops, answers_dict


# Multi-word phrases that are unambiguous heading markers for answer/solution sections.
_ANSWER_PAGE_KEYWORDS = {
    "answer key", "answer sheet", "answer book",
    "mark scheme", "marking scheme",
    "model answer", "model solution",
    "worked solution", "worked example",
    "solution key", "solutions to",
    "correct answer", "correct answers",
    "hints and solutions", "hints & solutions",
    "answers and solutions",
}

# Single words that, when they appear as the first non-empty line of a page
# (i.e. as a heading), signal an answer/solution section.
_ANSWER_HEADING_WORDS = {
    "answers", "answer", "solutions", "solution",
    "hints", "key",
}

_MCQ_ANSWER_TABLE_KEYWORDS = {
    "correct answer", "correct answers", "answer key", "answer sheet",
    "q.no", "q. no", "question no", "question number",
}

_MARKING_SCHEME_KEYWORDS = {
    "award marks", "should award", "marking scheme", "mark scheme",
    "teacher should", "marks awarded", "award full marks",
}


def _is_answer_key_page(page) -> bool:
    """Return True when a page is an answer/solution page rather than a question page."""
    text = page.get_text("text").lower()
    if not text.strip():
        return False

    first_500 = text[:500]

    # Multi-word phrases anywhere in the first 500 characters
    if any(kw in first_500 for kw in _ANSWER_PAGE_KEYWORDS):
        return True

    # Single-word headings: the very first non-empty line of the page
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return first_line in _ANSWER_HEADING_WORDS


def _is_mcq_answer_table_page(page_text_lower: str) -> bool:
    """True when the page likely contains a simple Q-number → answer table."""
    is_mcq    = any(kw in page_text_lower for kw in _MCQ_ANSWER_TABLE_KEYWORDS)
    is_scheme = any(kw in page_text_lower for kw in _MARKING_SCHEME_KEYWORDS)
    return is_mcq and not is_scheme


def _detect_chapter_boundaries(pdf_path: str) -> list:
    """Return [(page_idx, chapter_num), ...] for each chapter start, sorted by page_idx.

    Detects 'Chapter - N' markers from extractable PDF text (chapter title pages
    always have selectable text even when question content is image-based).
    """
    import fitz as _fitz
    _re_chap = _re.compile(r'Chapter\s*[-–]\s*(\d+)', _re.IGNORECASE)
    doc  = _fitz.open(pdf_path)
    seen: set = set()
    out:  list = []
    for i, page in enumerate(doc):
        m = _re_chap.search(page.get_text())
        if m:
            ch = int(m.group(1))
            if ch not in seen:
                seen.add(ch)
                out.append((i, ch))
    doc.close()
    return out


def _chapter_of(page_idx: int, boundaries: list) -> int:
    """Return the chapter number for page_idx given a sorted boundaries list."""
    ch = 1
    for pg, ch_num in boundaries:
        if pg <= page_idx:
            ch = ch_num
        else:
            break
    return ch


def _vision_pipeline_for_scanned_pdf(pdf_path: str, questions_dir: str, model: str) -> tuple:
    """Full vision-based Q&A extraction for PDFs where PyMuPDF can't read the text.

    Renders ALL pages to images, classifies each page via Claude as 'questions',
    'answers', or 'other', then:
    - question pages  → extract all questions + y-positions
    - answer pages    → extract full answers (single-word, multi-line, or with figures)
    - other pages     → skip

    Each question is linked to the full-page PNG it was found on (stored under
    pages/ in the output ZIP by _render_full_pages), since y_px estimates from
    vision models are not accurate enough for per-question sub-crops.

    Returns (result_list, crop_by_qnum) where crop_by_qnum maps q_num → full-page PNG.
    """
    from src.claude_vision import (
        extract_questions_from_page_claude,
        extract_answers_from_page_claude,
    )

    resolved_model = _MODEL_ALIASES.get(model, model)

    # Pre-scan for chapter boundaries (e.g. "Chapter - 1", "Chapter - 2").
    # When found, question numbers are prefixed "Ch{N}-Q{M}" to avoid collisions.
    chapter_boundaries = _detect_chapter_boundaries(pdf_path)
    multi_chapter      = len(chapter_boundaries) > 1

    # Step 1: render all pages (1× scale keeps image ≤ 1568 px tall — no API resize)
    page_records    = _render_all_pages(pdf_path, questions_dir, scale=1.0)

    # Step 2: classify all pages in parallel
    classifications = _classify_pages_vision(page_records, resolved_model)

    q_records = [r for r in page_records if classifications.get(r[0]) in ("questions", "other")]
    a_records = [r for r in page_records if classifications.get(r[0]) == "answers"]

    # Step 3a: extract answers — key by "Ch{N}-{q_num}" when multi-chapter.
    # Images are NOT deleted here; Step 3c reuses them for question extraction.
    answers_dict: dict = {}
    for record in a_records:
        page_idx, img_path = record[0], record[1]
        ch = _chapter_of(page_idx, chapter_boundaries)
        try:
            raw = extract_answers_from_page_claude(img_path, resolved_model)
            for q_num, ans in raw.items():
                key = f"Ch{ch}-Q{q_num}" if multi_chapter else q_num
                answers_dict[key] = ans
        except Exception:
            pass

    # Step 3b: extract questions from question/other pages (parallel, with retry).
    # If ALL extracted entries look like rubrics the page was misclassified — also run
    # answer extraction before deleting the image so the answers aren't lost.
    def _extract_q(record) -> tuple:
        import time
        page_idx, img_path = record[0], record[1]
        ch = _chapter_of(page_idx, chapter_boundaries)
        entries = []
        for attempt in range(3):
            try:
                entries = extract_questions_from_page_claude(img_path, resolved_model)
                break
            except Exception:
                if attempt < 2:
                    time.sleep(2 ** attempt)   # 1s, 2s
        for e in entries:
            e["_page_idx"] = page_idx
            e["_chapter"]  = ch
        # Dual extraction: when every entry looks like a rubric the page is actually
        # an answer page; run answer extraction before the image is deleted.
        extra_answers: dict = {}
        if entries and all(_is_rubric(e.get("question_text", "")) for e in entries):
            try:
                raw_ans = extract_answers_from_page_claude(img_path, resolved_model)
                for q_num, ans in raw_ans.items():
                    key = f"Ch{ch}-Q{q_num}" if multi_chapter else q_num
                    extra_answers[key] = ans
            except Exception:
                pass
        try:
            os.remove(img_path)
        except Exception:
            pass
        return entries, extra_answers

    with ThreadPoolExecutor(max_workers=_VISION_MAX_WORKERS) as executor:
        futures = [executor.submit(_extract_q, r) for r in q_records]
    all_questions = []
    for f in futures:
        q_entries, extra_ans = f.result()
        all_questions.extend(q_entries)
        for key, ans in extra_ans.items():
            if key not in answers_dict:
                answers_dict[key] = ans

    # Step 3c: also run question extraction on answer-classified pages.
    # Some exam layouts put the question stem and its rubric on the same page; the
    # classifier labels the whole page "answers" and the question gets missed.
    # We keep only non-rubric entries to avoid re-adding solution text as questions.
    with ThreadPoolExecutor(max_workers=_VISION_MAX_WORKERS) as executor:
        a_futures = [executor.submit(_extract_q, r) for r in a_records]
    for f in a_futures:
        q_entries, extra_ans = f.result()
        real_qs = [e for e in q_entries if not _is_rubric(e.get("question_text", ""))]
        all_questions.extend(real_qs)
        for key, ans in extra_ans.items():
            if key not in answers_dict:
                answers_dict[key] = ans

    # Sort by (chapter, question_num) so output is in reading order.
    all_questions.sort(key=lambda q: (q.get("_chapter", 1), q["question_num"]))

    # Deduplicate: the same question number can appear on both the question page and the
    # answer/rubric page (which sometimes gets misclassified as a question page).
    # Keep the non-rubric entry; if its answer is missing, borrow from rubric text.
    from collections import defaultdict as _defaultdict
    _grp: dict = _defaultdict(list)
    for _q in all_questions:
        _grp[(int(_q.get("_chapter", 1)), int(_q["question_num"]))].append(_q)

    _deduped = []
    for (_ch_k, _qn_k), _g in sorted(_grp.items()):
        if len(_g) == 1:
            _deduped.append(_g[0])
            continue
        _real = [q for q in _g if not _is_rubric(q.get("question_text", ""))]
        _rubs = [q for q in _g if _is_rubric(q.get("question_text", ""))]
        _best = _real[0] if _real else _g[0]
        # Rescue answer from rubric when answer is missing
        _ans_k = f"Ch{_ch_k}-Q{_qn_k}" if multi_chapter else _qn_k
        if str(answers_dict.get(_ans_k, "N/A")) in ("N/A", "nan", "", "None"):
            for _r in _rubs:
                _ext = _ans_from_rubric(_r.get("question_text", ""))
                if _ext:
                    answers_dict[_ans_k] = _ext
                    break
        _deduped.append(_best)

    all_questions = _deduped

    # Build result + crop map (each question → full page PNG under pages/)
    crop_by_qnum: dict = {}
    result = []
    for q in all_questions:
        q_num    = q["question_num"]
        ch       = q.get("_chapter", 1)
        page_idx = q.get("_page_idx", 0)
        q_id     = f"Ch{ch}-Q{q_num}" if multi_chapter else str(q_num)
        ans_key  = f"Ch{ch}-Q{q_num}" if multi_chapter else q_num
        answer   = answers_dict.get(ans_key, "N/A")
        # Page PNG path matches _render_full_pages naming: page_001.png, page_002.png, …
        page_png = os.path.join(questions_dir, f"page_{page_idx + 1:03d}.png")
        crop_by_qnum[q_id] = page_png
        result.append({
            "question_num":   q_id,
            "question_text":  sanitize(latex_to_unicode(q["question_text"])),
            "question_image": f"pages/page_{page_idx + 1:03d}.png",
            "figures":        "",
            "answers":        sanitize(latex_to_unicode(str(answer))),
            "source":         "vision (no extractable text)",
            # Private fields used by _crop_questions_by_separators; stripped before Excel.
            "_page_idx":      page_idx,
            "_y_px":          q.get("y_px", 0),
        })
    return result, crop_by_qnum


def _prepare_work_dirs(base_dir: str) -> tuple:
    import uuid
    # Unique per-request subdirectory avoids file collisions under concurrent requests.
    req_id        = uuid.uuid4().hex[:12]
    questions_dir = os.path.join(base_dir, 'questions', req_id)
    figures_dir   = os.path.join(base_dir, 'figures',   req_id)
    os.makedirs(questions_dir, exist_ok=True)
    os.makedirs(figures_dir,   exist_ok=True)
    return questions_dir, figures_dir


def _run_pdf_pipeline(questions_path: str, answers_path: str,
                      questions_dir: str, figures_dir: str) -> tuple:
    pdf_map      = build_pdf_map(questions_path, answers_path)
    crop_by_qnum = crop_from_map(questions_path, questions_dir, pdf_map)
    fig_data     = extract_figures_from_pdf(questions_path, figures_dir)
    mapping      = build_question_mapping(questions_path, answers_path, fig_data)
    return crop_by_qnum, mapping, fig_data


def _extraction_summary(result: list) -> str:
    """Return a compact header value describing extraction counts and validation stats."""
    pdf_count    = sum(1 for r in result if r.get("source") == "pymupdf")
    vision_count = sum(1 for r in result if r.get("source", "").startswith("vision"))
    reasons      = list({r["source"] for r in result if r.get("source", "").startswith("vision")})
    reason_str   = "; ".join(reasons) if reasons else ""
    ok_count     = sum(1 for r in result if r.get("validation") == "OK")
    no_ans       = sum(1 for r in result if r.get("validation") == "Missing Answer")
    return (f"pdf:{pdf_count},vision:{vision_count},reasons:{reason_str}"
            f"|ok:{ok_count},missing_answer:{no_ans}")


def _is_useful_text(text: str) -> bool:
    """True when the string has enough readable content to skip vision."""
    if not text or len(text.strip()) < 15:
        return False
    printable = sum(1 for c in text if c.isprintable() and not c.isspace())
    return printable / len(text) > 0.6


import re as _re
_RUBRIC_LEAD_RE = _re.compile(
    r'^\s*(Writes that|Finds |Calculates|Uses the|Shows that|Proves that|'
    r'Teacher should award|Assumes|Identifies|Substitutes|Solves|Expands|'
    r'Draws a|Justif|Hence|Therefore|Note:.*mark)'
    r'|\[\s*\d+\.?\d*\s*\]',   # mark allocations like [0.5] [1] [2]
    _re.IGNORECASE | _re.MULTILINE,
)
_ANS_KEY_RE = _re.compile(r'\[Answer Key\s*[-–]\s*Correct answer:\s*(\S+?)\]', _re.IGNORECASE)


def _is_rubric(text: str) -> bool:
    """True when extracted text looks like a teacher solution rubric, not a question."""
    return bool(_RUBRIC_LEAD_RE.search(str(text)[:400]))


def _ans_from_rubric(text: str) -> str | None:
    """Extract MCQ answer from '[Answer Key - Correct answer: N]' pattern."""
    m = _ANS_KEY_RE.search(str(text))
    return m.group(1) if m else None


def _fig_zip_path(abs_path: str) -> str:
    """Return the ZIP-relative path for a figure file (e.g. 'figures/figure_001.png')."""
    return f"figures/{os.path.basename(abs_path)}"


def _transcribe_entry(entry: dict, crop_by_qnum: dict, model: str,
                      pdf_texts: dict = None) -> dict:
    q_num = entry["question_num"]
    figs  = entry.get("figure") or []

    crop_path  = crop_by_qnum.get(q_num)
    crop_name  = os.path.basename(crop_path) if crop_path else ""
    crop_zip   = f"questions/{crop_name}" if crop_name else ""

    # Try PyMuPDF embedded text first
    if pdf_texts:
        raw = pdf_texts.get(q_num, "")
        if _is_useful_text(raw):
            return {
                "question_num":   str(q_num),
                "question_text":  sanitize(latex_to_unicode(raw)),
                "question_image": crop_zip,
                "figures":        ", ".join(_fig_zip_path(p) for p in figs),
                "answers":        sanitize(latex_to_unicode(entry.get("answer", "N/A") or "N/A")),
                "source":         "pymupdf",
            }

    # Fall back to Claude Haiku vision
    reason = "no embedded text" if not (pdf_texts and pdf_texts.get(q_num)) else "text too short/garbled"
    if crop_path and os.path.exists(crop_path):
        try:
            q_text = call_vision(crop_path, figure_count=len(figs), model=model)
            source = f"vision ({reason})"
        except Exception as exc:
            q_text = f"[vision error: {exc}]"
            source = f"vision error ({reason})"
    else:
        q_text = ""
        source = f"missing crop ({reason})"

    return {
        "question_num":   str(q_num),
        "question_text":  sanitize(latex_to_unicode(q_text)),
        "question_image": crop_zip,
        "figures":        ", ".join(_fig_zip_path(p) for p in figs),
        "answers":        sanitize(latex_to_unicode(entry.get("answer", "N/A") or "N/A")),
        "source":         source,
    }


def _transcribe_all_parallel(mapping: list, crop_by_qnum: dict, model: str,
                              pdf_texts: dict = None) -> list:
    with ThreadPoolExecutor(max_workers=_VISION_MAX_WORKERS) as executor:
        futures = [
            executor.submit(_transcribe_entry, entry, crop_by_qnum, model, pdf_texts)
            for entry in mapping
        ]
    return [f.result() for f in futures]


_VAL_FILLS = {
    "OK":                 PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid"),
    "Missing Answer":     PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid"),
    "Missing Text":       PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid"),
    "Missing Both":       PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"),
    "Bad Image Path":     PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid"),
    "Invalid MCQ Answer": PatternFill(start_color="FFCCCC", end_color="FFCCCC", fill_type="solid"),
}
_VAL_FONTS = {
    "OK":                 Font(color="006100", bold=True),
    "Missing Answer":     Font(color="9C6500", bold=True),
    "Missing Text":       Font(color="974706", bold=True),
    "Missing Both":       Font(color="9C0006", bold=True),
    "Bad Image Path":     Font(color="595959", bold=True),
    "Invalid MCQ Answer": Font(color="CC0000", bold=True),
}

_MCQ_OPT_RE = _re.compile(r'(?:^|\n)\s*(?:\(?)[A-D]\)?[\.\)]\s', _re.IGNORECASE)
_MCQ_ANS_RE = _re.compile(r'^[A-D\d]$', _re.IGNORECASE)


def _validate_extraction(result: list, zip_manifest: set = None) -> dict:
    """Check each result entry for question text, answer completeness, image paths, and MCQ answers.

    Adds a 'validation' field to every entry in-place.
    Returns counts dict.
    """
    counts = {"total": len(result), "ok": 0,
              "missing_answer": 0, "missing_text": 0, "missing_both": 0,
              "bad_image_path": 0, "invalid_mcq_answer": 0, "sequence_gaps": []}
    for entry in result:
        text     = str(entry.get("question_text", "")).strip()
        ans      = str(entry.get("answers",       "")).strip()
        img_path = str(entry.get("question_image", "")).strip()
        has_text = bool(text) and text.lower() not in ("nan", "none", "") and len(text) > 5
        has_ans  = bool(ans)  and ans.lower()  not in ("nan", "none", "n/a", "")

        # Image path check: if we have a manifest, verify the path is in it
        if img_path and zip_manifest is not None and img_path not in zip_manifest:
            entry["validation"] = "Bad Image Path"
            counts["bad_image_path"] += 1
            continue

        # MCQ answer check: if question has A)/B)/C)/D) options, answer must be a single letter/digit
        is_mcq = bool(_MCQ_OPT_RE.search(text))
        if is_mcq and has_ans and not _MCQ_ANS_RE.match(ans):
            entry["validation"] = "Invalid MCQ Answer"
            counts["invalid_mcq_answer"] += 1
            continue

        if has_text and has_ans:
            entry["validation"] = "OK";             counts["ok"]             += 1
        elif has_text:
            entry["validation"] = "Missing Answer"; counts["missing_answer"] += 1
        elif has_ans:
            entry["validation"] = "Missing Text";   counts["missing_text"]   += 1
        else:
            entry["validation"] = "Missing Both";   counts["missing_both"]   += 1

    # Sequential gap check
    nums = []
    for e in result:
        try:
            nums.append(int(e["question_num"]))
        except (ValueError, KeyError, TypeError):
            pass
    nums.sort()
    counts["sequence_gaps"] = [
        nums[i + 1] - nums[i]
        for i in range(len(nums) - 1)
        if nums[i + 1] - nums[i] > 1
    ]
    return counts


def _write_questions_excel(result: list, output_path: str) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Questions"

    ws.append(["question_num", "question_text", "question_image",
               "figures", "answers", "source", "validation"])
    header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    _MULTI_NL = _re.compile(r'\n{3,}')

    def _clean_text(text) -> str:
        """Collapse 3+ consecutive newlines to 2 and strip leading/trailing whitespace."""
        return _MULTI_NL.sub('\n\n', str(text).strip())

    vision_fill = PatternFill(start_color="FFF3CD", end_color="FFF3CD", fill_type="solid")
    for entry in result:
        source     = entry.get("source", "")
        validation = entry.get("validation", "")
        q_text     = _clean_text(entry.get("question_text", ""))
        ws.append([
            entry["question_num"],
            q_text,
            entry.get("question_image", ""),
            entry.get("figures", ""),
            entry["answers"],
            source,
            validation,
        ])
        row = ws.max_row

        # Cap row height: 15 pt per line, minimum 20 pt, maximum 150 pt.
        # Prevents extremely tall rows when question text has many newlines.
        line_count = q_text.count('\n') + 1
        ws.row_dimensions[row].height = max(20, min(line_count * 15, 150))

        ws.cell(row, 1).alignment = Alignment(horizontal="center", vertical="top")
        ws.cell(row, 2).alignment = Alignment(horizontal="left",   vertical="top", wrap_text=True)
        ws.cell(row, 3).alignment = Alignment(horizontal="center", vertical="center")
        ws.cell(row, 4).alignment = Alignment(horizontal="left",   vertical="top", wrap_text=True)
        ws.cell(row, 5).alignment = Alignment(horizontal="center", vertical="center")
        ws.cell(row, 6).alignment = Alignment(horizontal="left",   vertical="center")
        ws.cell(row, 7).alignment = Alignment(horizontal="center", vertical="center")
        if source.startswith("vision"):
            for col in range(1, 8):
                ws.cell(row, col).fill = vision_fill
        if validation in _VAL_FILLS:
            ws.cell(row, 7).fill = _VAL_FILLS[validation]
            ws.cell(row, 7).font = _VAL_FONTS[validation]

    ws.column_dimensions['A'].width = 14
    ws.column_dimensions['B'].width = 70
    ws.column_dimensions['C'].width = 28
    ws.column_dimensions['D'].width = 35
    ws.column_dimensions['E'].width = 18
    ws.column_dimensions['F'].width = 28
    ws.column_dimensions['G'].width = 18

    # ── Summary sheet ─────────────────────────────────────────────────────────
    sv = wb.create_sheet("Validation Summary")
    sv_header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
    sv_header_font = Font(color="FFFFFF", bold=True)

    sv.append(["Metric", "Count", "Percentage"])
    for cell in sv[1]:
        cell.fill = sv_header_fill
        cell.font = sv_header_font
        cell.alignment = Alignment(horizontal="center")

    total = len(result)
    pct = lambda n: f"{round(n / total * 100, 1)}%" if total else "0%"

    ok_count  = sum(1 for e in result if e.get("validation") == "OK")
    ma_count  = sum(1 for e in result if e.get("validation") == "Missing Answer")
    mt_count  = sum(1 for e in result if e.get("validation") == "Missing Text")
    mb_count  = sum(1 for e in result if e.get("validation") == "Missing Both")
    bip_count = sum(1 for e in result if e.get("validation") == "Bad Image Path")
    imq_count = sum(1 for e in result if e.get("validation") == "Invalid MCQ Answer")

    rows = [
        ("Total questions",      total,     "100%"),
        ("OK (text + answer)",   ok_count,  pct(ok_count)),
        ("Missing Answer",       ma_count,  pct(ma_count)),
        ("Missing Text",         mt_count,  pct(mt_count)),
        ("Missing Both",         mb_count,  pct(mb_count)),
        ("Bad Image Path",       bip_count, pct(bip_count)),
        ("Invalid MCQ Answer",   imq_count, pct(imq_count)),
    ]
    fill_map = {
        "OK (text + answer)": _VAL_FILLS["OK"],
        "Missing Answer":     _VAL_FILLS["Missing Answer"],
        "Missing Text":       _VAL_FILLS["Missing Text"],
        "Missing Both":       _VAL_FILLS["Missing Both"],
        "Bad Image Path":     _VAL_FILLS["Bad Image Path"],
        "Invalid MCQ Answer": _VAL_FILLS["Invalid MCQ Answer"],
    }
    for label, count, pct_str in rows:
        sv.append([label, count, pct_str])
        row_idx = sv.max_row
        if label in fill_map:
            for col in range(1, 4):
                sv.cell(row_idx, col).fill = fill_map[label]
        for col in range(1, 4):
            sv.cell(row_idx, col).alignment = Alignment(horizontal="center" if col > 1 else "left")

    sv.column_dimensions['A'].width = 26
    sv.column_dimensions['B'].width = 10
    sv.column_dimensions['C'].width = 12

    # ── List of problem questions ──────────────────────────────────────────────
    problems = [e for e in result if e.get("validation") != "OK"]
    if problems:
        sv.append([])
        sv.append(["Problem Questions", "Validation Status"])
        hdr_row = sv.max_row
        for col in (1, 2):
            sv.cell(hdr_row, col).fill = sv_header_fill
            sv.cell(hdr_row, col).font = sv_header_font
            sv.cell(hdr_row, col).alignment = Alignment(horizontal="center")
        for e in problems:
            sv.append([e["question_num"], e.get("validation", "")])
            row_idx = sv.max_row
            v = e.get("validation", "")
            if v in _VAL_FILLS:
                sv.cell(row_idx, 2).fill = _VAL_FILLS[v]
                sv.cell(row_idx, 2).font = _VAL_FONTS[v]
            sv.cell(row_idx, 2).alignment = Alignment(horizontal="center")

    wb.save(output_path)


def _render_full_pages(pdf_path: str, output_dir: str) -> list[str]:
    """Render every page of pdf_path to a PNG at 150 dpi. Returns list of file paths."""
    import fitz as _fitz
    _mat = _fitz.Matrix(150 / 72, 150 / 72)
    doc   = _fitz.open(pdf_path)
    paths = []
    for i, page in enumerate(doc):
        out = os.path.join(output_dir, f"page_{i + 1:03d}.png")
        page.get_pixmap(matrix=_mat).save(out)
        paths.append(out)
    doc.close()
    return paths


def _detect_separator_lines(img_path: str,
                             dark_threshold: int = 100,
                             min_dark_frac: float = 0.50) -> list:
    """Return y-pixel positions of horizontal separator lines in a page PNG.

    A separator is a contiguous band of rows where >= min_dark_frac of pixels
    are darker than dark_threshold.  Returns centres of those bands.
    """
    import numpy as _np
    from PIL import Image as _PILImage
    img = _np.array(_PILImage.open(img_path).convert('L'))
    h, w = img.shape
    dark_frac = (img < dark_threshold).sum(axis=1) / w
    lines = []
    in_line = False
    line_start = 0
    for y in range(h):
        if dark_frac[y] >= min_dark_frac:
            if not in_line:
                in_line = True
                line_start = y
        else:
            if in_line:
                in_line = False
                lines.append((line_start + y) // 2)
    if in_line:
        lines.append((line_start + h) // 2)
    return lines


def _crop_question_by_separators(page_img_path: str, y_top: int, y_bot: int,
                                  sep_lines: list, out_path: str,
                                  pad: int = 6) -> bool:
    """Crop a question region using the nearest separator lines as boundaries.

    y_top: approximate y of the question label in full-res pixels.
    y_bot: approximate y of the NEXT question label (or page height).
    sep_lines: sorted separator y-positions detected in the full-res page image.
    Returns True when a usable crop was saved.
    """
    from PIL import Image as _PILImage
    img = _PILImage.open(page_img_path)
    h = img.height

    # Top boundary: last separator that sits at or just above y_top
    above = [l for l in sep_lines if l <= y_top + 30]
    top = (max(above) + pad) if above else max(0, y_top - pad)

    # Bottom boundary: first separator at or just below y_bot
    below = [l for l in sep_lines if l >= y_bot - 30]
    bottom = (min(below) - pad) if below else min(h, y_bot + pad)

    if bottom - top < 30:
        return False

    img.crop((0, top, img.width, bottom)).save(out_path)
    return True


def _crop_questions_by_separators(result: list, crop_by_qnum: dict,
                                   questions_dir: str) -> None:
    """Post-process vision results: replace full-page refs with per-question crops.

    Must be called AFTER _render_full_pages has saved page_NNN.png files.
    Modifies result entries in-place; adds per-question crops to crop_by_qnum.
    _page_idx / _y_px fields must be present in each result entry.
    """
    # Scale factor: _render_all_pages uses scale=1.0 (72 dpi equivalent);
    # _render_full_pages uses 150 dpi → multiply by 150/72.
    _SCALE = 150 / 72

    from collections import defaultdict as _dd
    by_page: dict = _dd(list)
    for entry in result:
        if "_page_idx" in entry:
            by_page[entry["_page_idx"]].append(entry)

    for page_idx, entries in by_page.items():
        page_png = os.path.join(questions_dir, f"page_{page_idx + 1:03d}.png")
        if not os.path.exists(page_png):
            continue

        sep_lines = _detect_separator_lines(page_png)
        if not sep_lines:
            continue  # no separator lines found — keep full-page reference

        from PIL import Image as _PILImage
        page_h = _PILImage.open(page_png).height

        entries.sort(key=lambda e: e.get("_y_px", 0))

        for i, entry in enumerate(entries):
            y_top = int(entry.get("_y_px", 0) * _SCALE)
            y_bot = int(entries[i + 1].get("_y_px", 0) * _SCALE) if i + 1 < len(entries) else page_h

            q_id     = entry["question_num"]
            out_path = os.path.join(questions_dir, f"q_{q_id}.png")

            ok = _crop_question_by_separators(page_png, y_top, y_bot, sep_lines, out_path)
            if ok:
                entry["question_image"] = f"questions/q_{q_id}.png"
                crop_by_qnum[q_id]      = out_path


@app.route('/api/pdf-to-images', methods=['POST'])
def pdf_to_images():
    import zipfile
    questions_path = None
    answers_path   = None
    excel_path     = None
    try:
        questions_file = request.files.get('questions_pdf')
        if not questions_file or questions_file.filename == '':
            return jsonify({"error": "Missing questions PDF"}), 400

        questions_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(questions_file.filename))
        questions_file.save(questions_path)

        answers_file = request.files.get('answers_pdf')
        if answers_file and answers_file.filename:
            answers_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(answers_file.filename))
            answers_file.save(answers_path)

        model = request.form.get("model", "haiku")
        questions_dir, _ = _prepare_work_dirs(os.getcwd())

        # Detect and crop questions — map-first for text PDFs, vision fallback for scanned
        pdf_texts = extract_question_texts_from_pdf(questions_path)
        answers_dict_vision: dict = {}
        if pdf_texts:
            # Build the Q↔A map upfront so boundaries and answers are known before cropping
            _answers_path_for_map = answers_path or questions_path
            pdf_map      = build_pdf_map(questions_path, _answers_path_for_map)
            crop_by_qnum = crop_from_map(questions_path, questions_dir, pdf_map)
            map_by_qnum  = {e["question_num"]: e for e in pdf_map}
        else:
            crop_by_qnum, answers_dict_vision = _crop_questions_vision(questions_path, questions_dir, model)
            pdf_texts   = {}
            pdf_map     = []
            map_by_qnum = {}

        # Build mapping for the transcription pipeline.
        # Text path: answers come from the pre-built map (already paired with questions).
        # Vision path: answers come from vision extraction or N/A.
        mapping = [
            {
                "question_num": q_num,
                "figure":       None,
                "answer": (
                    map_by_qnum[q_num]["answer"]
                    if q_num in map_by_qnum
                    else sanitize(latex_to_unicode(str(answers_dict_vision.get(q_num, "N/A"))))
                ),
            }
            for q_num in sorted(crop_by_qnum.keys())
        ]

        # Transcribe: uses embedded PDF text when available (zero extra tokens);
        # falls back to Claude vision only for scanned/unreadable questions.
        result = _transcribe_all_parallel(mapping, crop_by_qnum, model, pdf_texts)

        # Bundle: full page images + question crops + Excel into one ZIP
        page_pngs = _render_full_pages(questions_path, questions_dir)

        # Pre-compute manifest for validator
        zip_manifest: set = set()
        for p in page_pngs:
            if os.path.exists(p):
                zip_manifest.add(f"pages/{os.path.basename(p)}")
        for q_num in sorted(crop_by_qnum.keys()):
            cp = crop_by_qnum[q_num]
            if os.path.exists(cp):
                zip_manifest.add(f"questions/{os.path.basename(cp)}")

        _validate_extraction(result, zip_manifest)

        excel_path = os.path.join(app.config['UPLOAD_FOLDER'], 'extraction_results.xlsx')
        _write_questions_excel(result, excel_path)

        zip_path = os.path.join(app.config['UPLOAD_FOLDER'], 'question_crops.zip')
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for p in page_pngs:
                if os.path.exists(p):
                    zf.write(p, f"pages/{os.path.basename(p)}")
            for q_num in sorted(crop_by_qnum.keys()):
                path = crop_by_qnum[q_num]
                if os.path.exists(path):
                    zf.write(path, f"questions/{os.path.basename(path)}")
            zf.write(excel_path, 'extraction_results.xlsx')

        response = send_file(
            zip_path,
            mimetype='application/zip',
            as_attachment=True,
            download_name='question_crops.zip',
        )
        response.headers['X-Extraction-Summary'] = _extraction_summary(result)
        return response

    except Exception as e:
        return jsonify({"error": f"Processing error: {str(e)}"}), 500
    finally:
        for path in (questions_path, answers_path, excel_path):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


@app.route('/api/extract-single', methods=['POST'])
def extract_single():
    import zipfile
    pdf_path    = None
    excel_path  = None
    zip_path    = None
    try:
        if 'pdf' not in request.files or request.files['pdf'].filename == '':
            return jsonify({"error": "Missing required file: 'pdf'"}), 400
        pdf_file = request.files['pdf']
        if not allowed_file(pdf_file.filename):
            return jsonify({"error": "Only PDF files are allowed"}), 400

        model = request.form.get("model", "haiku")
        pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(pdf_file.filename))
        pdf_file.save(pdf_path)

        questions_dir, figures_dir = _prepare_work_dirs(os.getcwd())
        crop_by_qnum, mapping, fig_data = _run_pdf_pipeline(pdf_path, pdf_path, questions_dir, figures_dir)

        vision_path = not bool(mapping)
        if vision_path:
            # Scanned PDF: vision extracts Q text + links each question to its full page PNG.
            result, crop_by_qnum = _vision_pipeline_for_scanned_pdf(pdf_path, questions_dir, model)
        else:
            pdf_texts = extract_question_texts_from_pdf(pdf_path)
            result = _transcribe_all_parallel(mapping, crop_by_qnum, model, pdf_texts)

        # Render full-res pages (150 dpi) for the ZIP and use them to refine
        # question crops using separator lines (both vision and text paths).
        page_pngs = _render_full_pages(pdf_path, questions_dir)
        _crop_questions_by_separators(result, crop_by_qnum, questions_dir)

        # Strip pipeline-internal fields before writing Excel.
        for _e in result:
            _e.pop("_page_idx", None)
            _e.pop("_y_px", None)

        # Pre-compute zip_manifest from known paths so validation can check image paths.
        zip_manifest: set = set()
        for p in page_pngs:
            if os.path.exists(p):
                zip_manifest.add(f"pages/{os.path.basename(p)}")
        for q_num in sorted(crop_by_qnum.keys()):
            cp = crop_by_qnum[q_num]
            if os.path.exists(cp):
                zip_manifest.add(f"questions/{os.path.basename(cp)}")
        if not vision_path:
            for _, _, fig_abs in (fig_data or []):
                if os.path.exists(fig_abs):
                    zip_manifest.add(_fig_zip_path(fig_abs))

        _validate_extraction(result, zip_manifest)
        excel_path = os.path.join(app.config['UPLOAD_FOLDER'], 'single_pdf_output.xlsx')
        _write_questions_excel(result, excel_path)

        # Bundle: full page images + per-question crops + figures + Excel.
        zip_path = os.path.join(app.config['UPLOAD_FOLDER'], 'single_pdf_output.zip')
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for p in page_pngs:
                if os.path.exists(p):
                    zf.write(p, f"pages/{os.path.basename(p)}")
            for q_num in sorted(crop_by_qnum.keys()):
                crop_path = crop_by_qnum[q_num]
                if os.path.exists(crop_path):
                    zf.write(crop_path, f"questions/{os.path.basename(crop_path)}")
            if not vision_path:
                for _, _, fig_abs in (fig_data or []):
                    if os.path.exists(fig_abs):
                        zf.write(fig_abs, _fig_zip_path(fig_abs))
            zf.write(excel_path, 'extraction_results.xlsx')

        response = send_file(
            zip_path,
            mimetype='application/zip',
            as_attachment=True,
            download_name='single_pdf_output.zip',
        )
        response.headers['X-Extraction-Summary'] = _extraction_summary(result)
        return response
    except Exception as e:
        return jsonify({"error": f"Processing error: {str(e)}"}), 500
    finally:
        for path in (pdf_path, excel_path):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


def _transcribe_entry_mathpix(entry: dict, crop_by_qnum: dict, model: str) -> dict:
    q_num     = entry["question_num"]
    crop_path = crop_by_qnum.get(q_num)
    crop_name = os.path.basename(crop_path) if crop_path else ""
    crop_zip  = f"questions/{crop_name}" if crop_name else ""
    figs = entry.get("figure") or []
    if crop_path and os.path.exists(crop_path):
        try:
            q_text = call_mathpix(crop_path, model=model)
        except Exception as exc:
            q_text = f"[mathpix error: {exc}]"
    else:
        q_text = ""
    return {
        "question_num":   str(q_num),
        "question_text":  sanitize(latex_to_unicode(q_text)),
        "question_image": crop_zip,
        "figures":        ", ".join(_fig_zip_path(p) for p in figs),
        "answers":        sanitize(latex_to_unicode(entry.get("answer", "N/A") or "N/A")),
        "source":         "mathpix",
    }


def _transcribe_all_mathpix_parallel(mapping: list, crop_by_qnum: dict, model: str) -> list:
    with ThreadPoolExecutor(max_workers=_VISION_MAX_WORKERS) as executor:
        futures = [
            executor.submit(_transcribe_entry_mathpix, entry, crop_by_qnum, model)
            for entry in mapping
        ]
    return [f.result() for f in futures]


@app.route('/api/extract-mathpix', methods=['POST'])
def extract_mathpix():
    questions_path = None
    answers_path = None
    try:
        is_valid, error_msg = validate_request()
        if not is_valid:
            return jsonify({"error": error_msg}), 400

        model = request.form.get("model", "text")

        questions_file = request.files['questions_pdf']
        answers_file   = request.files['answers_pdf']

        questions_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(questions_file.filename))
        answers_path   = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(answers_file.filename))
        questions_file.save(questions_path)
        answers_file.save(answers_path)

        questions_dir, figures_dir = _prepare_work_dirs(os.getcwd())
        crop_by_qnum, mapping, fig_data = _run_pdf_pipeline(
            questions_path, answers_path, questions_dir, figures_dir,
        )
        result = _transcribe_all_mathpix_parallel(mapping, crop_by_qnum, model)

        output_excel = os.path.join(app.config['UPLOAD_FOLDER'], 'mathpix_output.xlsx')
        _write_questions_excel(result, output_excel)

        return send_file(
            output_excel,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name='mathpix_output.xlsx',
        )

    except RuntimeError as e:
        return jsonify({"error": str(e)}), 501
    except Exception as e:
        return jsonify({"error": f"Processing error: {str(e)}"}), 500
    finally:
        for path in (questions_path, answers_path):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


@app.route('/api/validate', methods=['POST'])
def validate_qa():
    """Validate an Excel Q&A sheet against questions_pdf and answers_pdf.

    Inputs (multipart/form-data):
        questions_pdf  — PDF of exam questions (source of truth)
        answers_pdf    — PDF of answer key  (source of truth)
        excel          — .xlsx/.xls with columns: question_number, question_text, answer

    Returns an Excel workbook with validation results per question.
    """
    questions_path = answers_path = excel_path = None
    upload_folder = app.config['UPLOAD_FOLDER']
    try:
        # ── Input validation ──────────────────────────────────────────────────
        for field in ('questions_pdf', 'answers_pdf', 'excel'):
            if field not in request.files:
                return jsonify({"error": f"Missing required field: '{field}'"}), 400

        questions_file = request.files['questions_pdf']
        answers_file   = request.files['answers_pdf']
        excel_file     = request.files['excel']

        if not (allowed_file(questions_file.filename) and allowed_file(answers_file.filename)):
            return jsonify({"error": "questions_pdf and answers_pdf must be PDF files"}), 400
        if not excel_file.filename.lower().endswith(('.xlsx', '.xls')):
            return jsonify({"error": "excel must be an .xlsx or .xls file"}), 400

        # ── Save uploads ──────────────────────────────────────────────────────
        questions_path = os.path.join(upload_folder, secure_filename(questions_file.filename))
        answers_path   = os.path.join(upload_folder, secure_filename(answers_file.filename))
        excel_path     = os.path.join(upload_folder, secure_filename(excel_file.filename))

        questions_file.save(questions_path)
        answers_file.save(answers_path)
        excel_file.save(excel_path)

        # ── Crop question images + extract figures per question ───────────────
        base_dir         = os.getcwd()
        questions_dir    = os.path.join(base_dir, 'questions')
        check_images_dir = os.path.join(base_dir, 'check_images')
        os.makedirs(questions_dir, exist_ok=True)
        os.makedirs(check_images_dir, exist_ok=True)

        crop_by_qnum     = crop_questions_from_pdf(questions_path, questions_dir)
        q_figures_by_num = extract_figures_per_question(questions_path, check_images_dir)

        if not crop_by_qnum:
            return jsonify({"error": "No questions could be detected in questions_pdf"}), 422

        # ── Parse answers from answers PDF keyed by question number ──────────
        processor    = PDFProcessor(questions_path, answers_path)
        answers_dict = processor.parse_answers(processor.extract_text_from_pdf(answers_path))

        # ── Load Excel ────────────────────────────────────────────────────────
        df   = pd.read_excel(excel_path)
        norm = _normalise_cols(df)

        q_num_col  = _pick_col(norm, ['question_number', 'question_num', 'q_num', 'qnum', 'num'])
        q_text_col = _pick_col(norm, ['question_text', 'question', 'q_text', 'qtext'])
        ans_col    = _pick_col(norm, ['answer', 'correct_answer', 'answers'])
        fig_col    = _pick_col(norm, ['figures', 'figure', 'figure_names'])

        if not q_num_col or not q_text_col:
            return jsonify({"error": "Excel must have question_number and question_text columns"}), 422

        excel_by_qnum: dict = {}
        for _, row in df.iterrows():
            raw_num = row.get(q_num_col)
            if raw_num is None:
                continue
            try:
                q_num = int(raw_num)
            except (ValueError, TypeError):
                continue
            excel_by_qnum[q_num] = {
                "question_text": "" if pd.isna(row[q_text_col]) else str(row[q_text_col]),
                "answer":        ("" if pd.isna(row[ans_col]) else str(row[ans_col])) if ans_col else "",
                "figures":       ("" if pd.isna(row[fig_col]) else str(row[fig_col])) if fig_col else "",
            }

        # ── VLM: for each Excel question, compare crop image + check figures ──
        results = []

        for q_num in sorted(excel_by_qnum.keys()):
            exc_entry  = excel_by_qnum[q_num]
            excel_q    = exc_entry["question_text"]
            excel_a    = exc_entry["answer"]
            excel_figs = exc_entry["figures"]

            pdf_a          = sanitize(latex_to_unicode(answers_dict.get(q_num, "N/A")))
            crop_path      = crop_by_qnum.get(q_num)
            extracted_figs = q_figures_by_num.get(q_num, [])

            if crop_path and os.path.exists(crop_path):
                vlm_result      = _vlm_compare_question(crop_path, excel_q)
                match_type      = "VLM"
                match_score     = round(vlm_result.get("confidence", 0.0) * 100)
                q_match         = None if vlm_result.get("error") else vlm_result.get("match", False)
                issues          = vlm_result.get("issues") or []
                reason          = "; ".join(issues) if issues else ""
                image_fig_count = 0 if vlm_result.get("error") else int(vlm_result.get("figure_count", 0))
            else:
                match_type      = "No Image"
                match_score     = 0
                q_match         = None
                reason          = "No question image available"
                image_fig_count = 0

            # Figure match: compare extracted figure count vs Excel figure entries
            excel_fig_names = [n.strip() for n in excel_figs.split(",") if n.strip()]
            extracted_count = len(extracted_figs)
            figures_match   = extracted_count == len(excel_fig_names)

            if not figures_match:
                fig_reason = (
                    f"Figure mismatch: image has {extracted_count} figure(s), "
                    f"Excel lists {len(excel_fig_names)}"
                )
                reason = f"{reason}; {fig_reason}" if reason else fig_reason

            ans_sim = _fuzz.ratio(pdf_a.strip(), excel_a.strip())
            if q_match is None:
                status = "Manual Review"
            elif not q_match:
                status = "Incorrect"
            elif not excel_a:
                status = "Manual Review"
            elif ans_sim >= 80:
                status = "Correct"
            else:
                status = "Incorrect"

            results.append({
                "q_num":             q_num,
                "excel_question":    excel_q,
                "pdf_answer":        pdf_a,
                "excel_answer":      excel_a,
                "match_type":        match_type,
                "match_score":       match_score,
                "status":            status,
                "reason":            reason,
                "image_fig_count":   image_fig_count,
                "validated_figures": ", ".join(os.path.basename(p) for p in extracted_figs),
                "excel_figures":     excel_figs,
                "figures_match":     figures_match,
            })

        if not results:
            return jsonify({"error": "No matching questions found between Excel and PDF"}), 422

        output_excel = os.path.join(upload_folder, 'validation_output.xlsx')
        build_validation_excel(results, output_excel)

        return send_file(
            output_excel,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'validation_{len(results)}q.xlsx',
        )

    except Exception as e:
        return jsonify({"error": f"Processing error: {str(e)}"}), 500
    finally:
        for path in (questions_path, answers_path, excel_path):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


@app.route('/api/general-purpose-extraction', methods=['POST'])
def general_purpose_extraction():
    """Classify each page of an uploaded PDF as theory, questions, solutions, or misc."""
    pdf_path = None
    tmp_dir = None
    try:
        if 'pdf' not in request.files:
            return jsonify({"error": "Missing required file: pdf"}), 400

        pdf_file = request.files['pdf']
        if not pdf_file.filename or not pdf_file.filename.lower().endswith('.pdf'):
            return jsonify({"error": "Uploaded file must be a PDF"}), 400

        pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(pdf_file.filename))
        pdf_file.save(pdf_path)

        tmp_dir = tempfile.mkdtemp()
        page_images = pdf_pages_to_png(pdf_path, tmp_dir, prefix="page")

        results = []
        for page_num, image_path in enumerate(page_images, start=1):
            classification = classify_page_with_gpt(image_path)
            page_type = classification.get("page_type")

            entry = {
                "page": page_num,
                "page_type": page_type,
                "confidence": classification.get("confidence"),
                "reason": classification.get("reason"),
                "layout": None,
            }

            if page_type in ("questions", "solutions"):
                layout = detect_layout_fitz(pdf_path, page_num - 1)
                layout_type = layout.get("layout", "single_column")
                entry["layout"] = {
                    "type": layout_type,
                    "columns": layout.get("columns"),
                    "confidence": layout.get("confidence"),
                    "reason": layout.get("reason"),
                }
                saved = save_page_crops(
                    pdf_path, page_num - 1, layout_type, page_type, base_dir=os.getcwd()
                )
                entry["saved_images"] = saved

            results.append(entry)

        # ── Figure extraction from questions and solutions pages only ──────────
        figures_base = os.path.join(os.getcwd(), "figures")

        q_indices = [p["page"] - 1 for p in results if p["page_type"] == "questions"]
        s_indices = [p["page"] - 1 for p in results if p["page_type"] == "solutions"]

        q_fig_data = extract_figures_from_pages(
            pdf_path, q_indices, os.path.join(figures_base, "questions")
        )
        q_figure_map = map_figures_to_questions_on_pages(pdf_path, q_indices, q_fig_data)

        s_fig_data = extract_figures_from_pages(
            pdf_path, s_indices, os.path.join(figures_base, "solutions")
        )
        s_figure_map = map_figures_to_questions_on_pages(pdf_path, s_indices, s_fig_data)

        return jsonify({
            "total_pages": len(results),
            "pages": results,
            "figure_mapping": {
                "questions": {str(k): v for k, v in q_figure_map.items()},
                "solutions":  {str(k): v for k, v in s_figure_map.items()},
            },
        }), 200

    except Exception as e:
        return jsonify({"error": f"Processing error: {str(e)}"}), 500


if __name__ == '__main__':
    app.run(debug=True, host='localhost', port=5000)

import os
import json
import base64
import tempfile
from concurrent.futures import ThreadPoolExecutor
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
)
from src.vision import call_vision
from src.mathpix import call_mathpix

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
        answers_list   = processor.parse_answers(processor.extract_text_from_pdf(answers_path))

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
            answer = answers_list[idx - 1] if idx - 1 < len(answers_list) else "N/A"
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


def _prepare_work_dirs(base_dir: str) -> tuple:
    questions_dir = os.path.join(base_dir, 'questions')
    figures_dir   = os.path.join(base_dir, 'figures')
    os.makedirs(questions_dir, exist_ok=True)
    os.makedirs(figures_dir, exist_ok=True)
    return questions_dir, figures_dir


def _run_pdf_pipeline(questions_path: str, answers_path: str,
                      questions_dir: str, figures_dir: str) -> tuple:
    crop_by_qnum = crop_questions_from_pdf(questions_path, questions_dir)
    fig_data     = extract_figures_from_pdf(questions_path, figures_dir)
    mapping      = build_question_mapping(questions_path, answers_path, fig_data)
    return crop_by_qnum, mapping


def _transcribe_entry(entry: dict, crop_by_qnum: dict, model: str) -> dict:
    crop_path = crop_by_qnum.get(entry["question_num"])
    figs = entry.get("figure") or []
    if crop_path and os.path.exists(crop_path):
        try:
            q_text = call_vision(crop_path, figure_count=len(figs), model=model)
        except Exception as exc:
            q_text = f"[vision error: {exc}]"
    else:
        q_text = ""
    return {
        "question_num":  str(entry["question_num"]),
        "question_text": sanitize(latex_to_unicode(q_text)),
        "answers":       sanitize(latex_to_unicode(entry.get("answer", "N/A") or "N/A")),
        "figures":       ", ".join(os.path.basename(p) for p in figs),
    }


def _transcribe_all_parallel(mapping: list, crop_by_qnum: dict, model: str) -> list:
    with ThreadPoolExecutor(max_workers=_VISION_MAX_WORKERS) as executor:
        futures = [executor.submit(_transcribe_entry, entry, crop_by_qnum, model) for entry in mapping]
    return [f.result() for f in futures]


def _write_questions_excel(result: list, output_path: str) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Questions"

    ws.append(["question_num", "question_text", "figures", "answers"])
    header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for entry in result:
        ws.append([entry["question_num"], entry["question_text"], entry["figures"], entry["answers"]])
        row = ws.max_row
        ws.cell(row, 1).alignment = Alignment(horizontal="center", vertical="top")
        ws.cell(row, 2).alignment = Alignment(horizontal="left",   vertical="top", wrap_text=True)
        ws.cell(row, 3).alignment = Alignment(horizontal="left",   vertical="top", wrap_text=True)
        ws.cell(row, 4).alignment = Alignment(horizontal="center", vertical="center")

    ws.column_dimensions['A'].width = 14
    ws.column_dimensions['B'].width = 70
    ws.column_dimensions['C'].width = 35
    ws.column_dimensions['D'].width = 18
    wb.save(output_path)


@app.route('/api/pdf-to-images', methods=['POST'])
def pdf_to_images():
    questions_path = None
    answers_path = None
    try:
        is_valid, error_msg = validate_request()
        if not is_valid:
            return jsonify({"error": error_msg}), 400

        model          = request.form.get("model", "qwen2.5vl:7b")
        questions_file = request.files['questions_pdf']
        answers_file   = request.files['answers_pdf']

        questions_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(questions_file.filename))
        answers_path   = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(answers_file.filename))
        questions_file.save(questions_path)
        answers_file.save(answers_path)

        questions_dir, figures_dir = _prepare_work_dirs(os.getcwd())
        crop_by_qnum, mapping = _run_pdf_pipeline(
            questions_path, answers_path, questions_dir, figures_dir,
        )
        result = _transcribe_all_parallel(mapping, crop_by_qnum, model)

        output_excel = os.path.join(app.config['UPLOAD_FOLDER'], 'questions_output.xlsx')
        _write_questions_excel(result, output_excel)

        return send_file(
            output_excel,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name='questions_output.xlsx',
        )

    except Exception as e:
        return jsonify({"error": f"Processing error: {str(e)}"}), 500
    finally:
        for path in (questions_path, answers_path):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


@app.route('/api/extract-single', methods=['POST'])
def extract_single():
    pdf_path = None
    try:
        if 'pdf' not in request.files or request.files['pdf'].filename == '':
            return jsonify({"error": "Missing required file: 'pdf'"}), 400
        pdf_file = request.files['pdf']
        if not allowed_file(pdf_file.filename):
            return jsonify({"error": "Only PDF files are allowed"}), 400

        model = request.form.get("model", "sonnet")
        pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(pdf_file.filename))
        pdf_file.save(pdf_path)

        questions_dir, figures_dir = _prepare_work_dirs(os.getcwd())
        crop_by_qnum, mapping = _run_pdf_pipeline(pdf_path, pdf_path, questions_dir, figures_dir)
        result = _transcribe_all_parallel(mapping, crop_by_qnum, model)

        output_excel = os.path.join(app.config['UPLOAD_FOLDER'], 'single_pdf_output.xlsx')
        _write_questions_excel(result, output_excel)

        return send_file(
            output_excel,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name='single_pdf_output.xlsx',
        )
    except Exception as e:
        return jsonify({"error": f"Processing error: {str(e)}"}), 500
    finally:
        try:
            if pdf_path and os.path.exists(pdf_path):
                os.remove(pdf_path)
        except Exception:
            pass


def _transcribe_entry_mathpix(entry: dict, crop_by_qnum: dict, model: str) -> dict:
    crop_path = crop_by_qnum.get(entry["question_num"])
    figs = entry.get("figure") or []
    if crop_path and os.path.exists(crop_path):
        try:
            q_text = call_mathpix(crop_path, model=model)
        except Exception as exc:
            q_text = f"[mathpix error: {exc}]"
    else:
        q_text = ""
    return {
        "question_num":  str(entry["question_num"]),
        "question_text": sanitize(latex_to_unicode(q_text)),
        "answers":       sanitize(latex_to_unicode(entry.get("answer", "N/A") or "N/A")),
        "figures":       ", ".join(os.path.basename(p) for p in figs),
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
        crop_by_qnum, mapping = _run_pdf_pipeline(
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

        # ── Parse answers from answers PDF (index-based: Q1 → index 0) ───────
        processor    = PDFProcessor(questions_path, answers_path)
        answers_list = processor.parse_answers(processor.extract_text_from_pdf(answers_path))

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

            pdf_a          = sanitize(latex_to_unicode(
                answers_list[q_num - 1] if (q_num - 1) < len(answers_list) else "N/A"
            ))
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


if __name__ == '__main__':
    app.run(debug=True, host='localhost', port=5000)

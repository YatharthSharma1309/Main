# QA PDF Extractor

A full-stack application that extracts questions and answers from exam PDFs using vision AI models (Claude, GPT-4o, Ollama), outputs a structured Excel file with per-question image crops, and validates results. Built with **Flask** (backend) + **React/Vite** (frontend).

---

## Table of Contents

1. [What It Does](#what-it-does)
2. [Project Structure](#project-structure)
3. [Setup & Installation](#setup--installation)
4. [Environment Variables](#environment-variables)
5. [Running the App](#running-the-app)
6. [Extraction Pipeline (How It Works)](#extraction-pipeline-how-it-works)
7. [API Endpoints](#api-endpoints)
8. [Output Format (Excel + ZIP)](#output-format-excel--zip)
9. [Vision Model Options](#vision-model-options)
10. [Frontend Modes](#frontend-modes)
11. [Testing](#testing)
12. [Key Source Files](#key-source-files)
13. [Troubleshooting](#troubleshooting)

---

## What It Does

Given one or two exam PDFs (questions + answer key), the system:

1. Detects every question by its numbered marker (e.g. `Q1.`, `1)`, `Question 1`)
2. Crops each question to its own PNG image, respecting separator lines between questions
3. Extracts the question text — using embedded PDF text where available, falling back to a vision model (Claude Haiku by default) for scanned pages
4. Matches each question's answer from the answer key by **question number** (not position)
5. Outputs a downloadable ZIP containing:
   - `extraction_results.xlsx` — one row per question with text, answer, image path, and validation status
   - `questions/q_N.png` — per-question image crops
   - `pages/page_NNN.png` — full-page renders (150 dpi)
   - `figures/figure_NNN.ext` — embedded figures from the PDF

---

## Project Structure

```
QuestionAnswerTesting/
├── api.py                        # Flask server — all HTTP routes + extraction orchestration
├── src/
│   ├── pdf_utils.py              # PDF cropping, question boundary detection, figure extraction
│   ├── pdf_processor.py          # PDF text extraction + answer key parsing (returns dict by Q num)
│   ├── claude_vision.py          # Claude vision: question/answer/page-classification prompts
│   ├── gpt_vision.py             # GPT-4o vision backend
│   ├── vision.py                 # Model router + shared vision prompt templates
│   ├── mathpix.py                # Mathpix OCR backend
│   ├── quickstart.py             # LandingAI PDF parsing client
│   ├── page_classifier.py        # GPT-based page type classifier
│   └── helpers.py                # Utilities: sanitize, latex_to_unicode, Excel builders
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── PDFUploader.jsx   # Main upload UI + mode selector (default: single-pdf)
│   │   │   └── modes/
│   │   │       ├── SinglePdf.jsx           # Single PDF extraction (auto-detects text vs scanned)
│   │   │       ├── PdfExtractor.jsx        # Two-PDF mode (questions + answers separately)
│   │   │       ├── PdfToImages.jsx         # Crop questions to images + Excel
│   │   │       ├── MathpixExtractor.jsx    # Mathpix OCR extraction
│   │   │       ├── ValidateQA.jsx          # Validate an existing Excel against PDFs
│   │   │       ├── ExcelProcessor.jsx      # Process existing Excel Q&A sheet
│   │   │       ├── GeneralPurposeExtraction.jsx  # Theory/questions/solutions classifier
│   │   │       ├── GptExtractor.jsx        # GPT extraction (disabled)
│   │   │       └── PdfEvaluator.jsx        # External search evaluation (disabled)
│   │   └── App.jsx
│   ├── vite.config.js            # Dev server + proxy: /api/* → localhost:5000
│   └── package.json
├── tests/
│   ├── ui.spec.js                # Playwright UI + API health tests
│   ├── test_14q_pdf.py           # End-to-end pytest: upload 14-question PDF, verify Excel
│   └── test_qa_system.py         # Unit tests for extraction logic
├── playwright.config.js          # Playwright configuration
├── package.json                  # Root-level Playwright dev dependency
├── CLAUDE.md                     # AI assistant codebase guide
├── .env                          # API keys (not committed)
└── requirements.txt              # Python dependencies
```

---

## Setup & Installation

### Prerequisites

- Python 3.10+
- Node.js 18+
- (Optional) [Ollama](https://ollama.ai) running locally for the default local model

### Backend

```bash
# Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS/Linux

# Install Python dependencies
pip install -r requirements.txt
```

### Frontend

```bash
cd frontend
npm install
```

### Playwright (for UI tests)

```bash
# From the project root
npm install
npx playwright install chromium
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in your keys:

```env
ANTHROPIC_API_KEY=sk-ant-...      # Required for Claude Haiku / Sonnet
OPENAI_API_KEY=sk-...             # Required for GPT-4o / GPT-4o-mini
MATHPIX_APP_ID=...                # Optional — Mathpix OCR
MATHPIX_APP_KEY=...               # Optional — Mathpix OCR
LANDINGAI_API_KEY=...             # Optional — LandingAI /api/extract route
```

| Key | Required for |
|-----|-------------|
| `ANTHROPIC_API_KEY` | `model=haiku`, `model=sonnet`, all Claude vision |
| `OPENAI_API_KEY` | `model=gpt-4o`, `model=gpt-4o-mini` |
| `MATHPIX_APP_ID` + `MATHPIX_APP_KEY` | `/api/extract-mathpix` |
| `LANDINGAI_API_KEY` | `/api/extract` (LandingAI route) |

For the local Ollama backend (`qwen2.5vl:7b`), no API key is needed — just run `ollama serve`.

---

## Running the App

### Backend (Flask API — port 5000)

```bash
python api.py
```

### Frontend (React/Vite — port 3000)

```bash
cd frontend
npm run dev
```

Then open **http://localhost:3000** in your browser. All `/api/*` requests from the frontend are automatically proxied to `localhost:5000`.

---

## Extraction Pipeline (How It Works)

### Text-based PDFs (embedded text readable)

```
Upload PDF
    │
    ▼
_detect_q_pattern()          ← auto-selects best numbered regex from 9 candidates
    │                           (Question 1, Q: 1, Q1., 1., 1), etc.)
    ▼
build_pdf_map()              ← detects question boundaries (y-positions per page)
    │                           answers matched by question NUMBER from answer key dict
    ▼
crop_from_map()              ← crops each question to PNG
    │                           _content_bottom() stops at separator lines
    │                           _stitch_vertical() handles questions spanning pages
    ▼
extract_question_texts_from_pdf()   ← reads embedded PDF text per question region
    │
    ▼
_transcribe_all_parallel()   ← uses embedded text when readable (pymupdf source)
    │                           falls back to Claude vision for garbled/scanned regions
    ▼
_render_full_pages()         ← 150 dpi page PNGs for ZIP
    │
    ▼
_crop_questions_by_separators()  ← refines crops using horizontal separator lines
    │
    ▼
_validate_extraction()       ← checks completeness + MCQ answer format + image paths
    │
    ▼
_write_questions_excel()     ← one row per question, 7 columns
    │
    ▼
ZIP (pages/ + questions/ + figures/ + Excel)
```

### Scanned PDFs (no embedded text → full vision pipeline)

```
Upload PDF
    │
    ▼
_render_all_pages()          ← 1× scale PNG per page
    │
    ▼
_classify_pages_vision()     ← Claude classifies each page:
    │                           "questions" / "answers" / "other"
    ▼
extract_questions_from_page_claude()   ← per question: num, y_px, full text (MCQ inline)
extract_answers_from_page_claude()     ← per page: {q_num: answer_text}
    │
    ▼
Deduplication + chapter-aware Q numbering (Ch1-Q1, Ch2-Q1, etc.)
    │
    ▼
_render_full_pages()  →  _crop_questions_by_separators()
    │                      ← detects separator lines; crops question sub-regions from page PNGs
    ▼
_validate_extraction()  →  _write_questions_excel()  →  ZIP
```

### Answer key matching

- `PDFProcessor.parse_answers()` returns `dict[int, str]` keyed by **question number**
- All lookups use `.get(q_num, "N/A")` — skipped question numbers never cause mis-matches
- Multi-chapter scanned PDFs prefix keys as `Ch{N}-Q{M}` to avoid collisions

---

## API Endpoints

| Method | Path | Description | Returns |
|--------|------|-------------|---------|
| `GET` | `/health` | Health check | JSON |
| `GET` | `/` | Frontend (if built) or JSON status | HTML / JSON |
| `POST` | `/api/extract-single` | **Main endpoint** — single PDF, auto-detects text vs scanned | ZIP |
| `POST` | `/api/pdf-to-images` | Two-PDF mode (questions + optional answers PDF) | ZIP |
| `POST` | `/api/extract` | LandingAI-based extraction → Excel (two PDFs) | Excel |
| `POST` | `/api/extract-mathpix` | Mathpix OCR per question crop → Excel | Excel |
| `POST` | `/api/validate` | Validate Excel against PDFs using VLM comparison | Excel |
| `POST` | `/api/general-purpose-extraction` | Classify pages + extract figures | ZIP |

### `/api/extract-single` (recommended)

```
POST /api/extract-single
Content-Type: multipart/form-data

pdf     : <file>       required — single PDF (questions + answers in same file,
                                   or questions-only for scanned)
model   : string       optional — vision model (default: haiku)
```

Returns a ZIP. Also sets response header:
```
X-Extraction-Summary: pdf:N,vision:M,reasons:...|ok:K,missing_answer:J
```

### `/api/pdf-to-images`

```
POST /api/pdf-to-images
Content-Type: multipart/form-data

questions_pdf  : <file>   required
answers_pdf    : <file>   optional — if omitted, answers come from questions PDF
model          : string   optional (default: haiku)
```

### `/api/validate`

```
POST /api/validate
Content-Type: multipart/form-data

questions_pdf  : <file>   required — source-of-truth questions PDF
answers_pdf    : <file>   required — source-of-truth answer key PDF
excel          : <file>   required — .xlsx with columns: question_number, question_text, answer
```

---

## Output Format (Excel + ZIP)

### ZIP structure

```
extraction_results.zip
├── extraction_results.xlsx     ← main output
├── questions/
│   ├── q_1.png                 ← per-question crop (separator-refined when lines detected)
│   ├── q_2.png
│   └── ...
├── pages/
│   ├── page_001.png            ← full page at 150 dpi
│   └── ...
└── figures/
    ├── figure_001.png          ← embedded figures extracted from PDF
    └── ...
```

### Excel columns (`extraction_results.xlsx`, sheet: `Questions`)

| Column | Description |
|--------|-------------|
| `question_num` | Question number as printed (`1`, `14`, `Ch2-Q3` for multi-chapter) |
| `question_text` | Full extracted text — MCQ options inline (A) … B) … C) … D) …) |
| `question_image` | ZIP-relative path to the question crop, e.g. `questions/q_1.png` |
| `figures` | Comma-separated ZIP-relative paths to embedded figures, e.g. `figures/figure_001.png` |
| `answers` | Extracted answer — single letter for MCQ (A/B/C/D), full text for open-ended |
| `source` | How the text was obtained: `pymupdf`, `vision (no embedded text)`, `vision error (...)`, `missing crop (...)` |
| `validation` | Quality status (see below) |

### Validation states

| Status | Meaning | Cell colour |
|--------|---------|-------------|
| `OK` | Question text and answer both present | Green |
| `Missing Answer` | Text found, no answer | Yellow |
| `Missing Text` | Answer found, no question text | Orange |
| `Missing Both` | Neither text nor answer | Red |
| `Bad Image Path` | `question_image` path not present in ZIP | Grey |
| `Invalid MCQ Answer` | MCQ question but answer is not a single letter/digit | Light red |

A **Validation Summary** sheet is also included with counts, percentages, and a list of problem questions.

---

## Vision Model Options

| `model` value | Backend | Notes |
|---------------|---------|-------|
| `haiku` | Claude Haiku (`claude-haiku-4-5-20251001`) | Default — fast, low cost |
| `sonnet` | Claude Sonnet (`claude-sonnet-4-6`) | Higher accuracy for complex math |
| `gpt-4o` | OpenAI GPT-4o | Best OpenAI vision quality |
| `gpt-4o-mini` | OpenAI GPT-4o mini | Faster, cheaper OpenAI option |
| *(anything else)* | Ollama local | Calls `localhost:11434` with `qwen2.5vl:7b` |

The model router lives in `src/vision.py::call_vision` and `_MODEL_ALIASES`.

---

## Frontend Modes

Open **http://localhost:3000** and choose a mode from the tab bar:

| Mode | Component | What it does |
|------|-----------|-------------|
| **Single PDF** | `SinglePdf.jsx` | Upload one PDF; auto-detects text vs scanned; returns ZIP with Excel + crops |
| **PDF Extractor** | `PdfExtractor.jsx` | Upload separate questions + answers PDFs |
| **PDF to Images** | `PdfToImages.jsx` | Crops questions to images + runs vision extraction |
| **Mathpix** | `MathpixExtractor.jsx` | Uses Mathpix OCR for math-heavy PDFs |
| **Validate Q&A** | `ValidateQA.jsx` | Upload an Excel + PDFs; VLM compares each crop to its transcription |
| **General Purpose** | `GeneralPurposeExtraction.jsx` | Classifies pages (theory/questions/solutions) + extracts figures |

---

## Testing

### Unit tests

```bash
python -m pytest tests/test_qa_system.py -v
```

### Playwright UI tests (requires both servers running)

```bash
# Terminal 1
python api.py

# Terminal 2
cd frontend && npm run dev

# Terminal 3
npx playwright test tests/ui.spec.js
```

### End-to-end extraction test (14-question PDF)

```bash
# Windows PowerShell
$env:TEST_PDF_PATH = "C:\path\to\your\14_question_exam.pdf"
python -m pytest tests/test_14q_pdf.py -v

# macOS / Linux
TEST_PDF_PATH="/path/to/your/14_question_exam.pdf" pytest tests/test_14q_pdf.py -v
```

The test verifies:
- Exactly 14 data rows in the Excel
- No "Missing" in the validation column for any row
- All `question_image` values are ZIP-relative paths (contain `/`)
- Every image path referenced in Excel actually exists inside the ZIP

Set `API_BASE_URL` env var to override the server address (default: `http://localhost:5000`).

---

## Key Source Files

| File | Key functions | Purpose |
|------|--------------|---------|
| `api.py` | `_run_pdf_pipeline`, `_vision_pipeline_for_scanned_pdf`, `_transcribe_entry`, `_validate_extraction`, `_write_questions_excel` | All routes + pipeline orchestration |
| `src/pdf_utils.py` | `build_pdf_map`, `crop_from_map`, `_content_bottom`, `_detect_q_pattern`, `_stitch_vertical`, `_get_separator_ys` | PDF boundary detection + cropping |
| `src/pdf_processor.py` | `parse_answers` → `dict[int,str]`, `parse_questions` | Answer key parsing (5 formats) |
| `src/claude_vision.py` | `extract_questions_from_page_claude`, `extract_answers_from_page_claude`, `classify_page_claude` | Claude vision prompts |
| `src/vision.py` | `call_vision`, `_MODEL_ALIASES` | Model router + shared prompt templates |
| `src/helpers.py` | `sanitize`, `latex_to_unicode`, `build_validation_excel` | Text cleaning + Excel utilities |

---

## Troubleshooting

### No questions detected

- Check the question numbering format matches one of: `Question 1`, `Q: 1`, `Q1.`, `1.`, `1)`, `(1)`, `[1]`
- For scanned PDFs with no embedded text, the vision pipeline is used automatically
- Run with `model=sonnet` for better accuracy on complex layouts

### Answers all "N/A"

- Ensure the answer key PDF has a parseable format: table (`1   3`), JEE-style (`1. (2)`), or `Answer: A` lines
- `parse_answers()` tries 5 formats in order; check `src/pdf_processor.py` if your format isn't matched

### Questions bleeding into each other in crops

- The cropper uses fitz drawing objects to detect separator lines
- If your PDF uses images (not vector lines) as separators, the separator detection may miss them
- Try `_detect_separator_lines()` in `api.py` which works on rasterised images

### Vision model errors

- `ANTHROPIC_API_KEY` not set → set it in `.env`
- Rate limits → reduce `_VISION_MAX_WORKERS` in `api.py` (default: 8)
- Large PDFs → the 500 MB upload limit is set by `MAX_FILE_SIZE` in `api.py`

### MCQ answers flagged as "Invalid MCQ Answer"

- The validator expects a single letter (A–D) or digit for MCQ questions
- If the answer key uses full option text (e.g. "Option A: 42"), the answer extraction prompt may need tuning in `src/claude_vision.py::_ANSWER_EXTRACT_PROMPT`

### Frontend shows blank or wrong mode on load

- Default mode is `single-pdf` (set in `PDFUploader.jsx` line 15)
- If you see no tab highlighted, clear browser cache or check `ModeSelector.jsx` for valid mode IDs

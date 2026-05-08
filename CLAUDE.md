# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Backend (Flask API):**
```bash
python api.py                          # runs on http://localhost:5000
```

**Frontend (React/Vite):**
```bash
cd frontend && npm run dev             # runs on http://localhost:3000
```

**Tests:**
```bash
npx playwright test tests/ui.spec.js   # Playwright UI + API tests (run both servers first)
python -m pytest tests/test_qa_system.py
```

**Environment:** copy `.env` and fill in keys before running. `ANTHROPIC_API_KEY` is required for Claude vision models; `OPENAI_API_KEY` for GPT models; Mathpix and LandingAI keys are optional.

---

## Architecture

### Request flow

All HTTP routes live in `api.py`. There are two main extraction pipelines:

**Text-based PDFs** (`/api/pdf-to-images`, `/api/extract-single`)
1. `src/pdf_utils.py::crop_questions_from_pdf` — detects question start positions via `_detect_q_pattern` (auto-selects the best numbered regex from `_CANDIDATE_PATTERNS` by scanning the first 6 pages), then crops each question's region using `_content_bottom` to tighten the bottom boundary around actual content (text blocks + embedded images).
2. `src/pdf_utils.py::extract_question_texts_from_pdf` — reads embedded PDF text for each question region; returns `{}` when the PDF is scanned (no selectable text), which triggers the vision fallback.
3. `api.py::_transcribe_all_parallel` — for each question, uses embedded text when readable (`_is_useful_text`), otherwise sends the crop image to the vision model via `src/vision.py::call_vision`.

**Scanned PDFs** (no embedded text → vision pipeline)
1. `api.py::_vision_pipeline_for_scanned_pdf` — renders every page to PNG at 2× scale.
2. `api.py::_classify_pages_vision` → `src/claude_vision.py::classify_page_claude` — classifies each page as `questions`, `answers`, or `other`.
3. `src/claude_vision.py::extract_questions_from_page_claude` — returns `[{question_num, question_text, y_px}]` per page.
4. `src/claude_vision.py::extract_answers_from_page_claude` — returns `{q_num: answer_text}` per page.

### Vision backend routing (`src/vision.py::call_vision`)

Model aliases are resolved in `_MODEL_ALIASES`:
- `haiku` / `sonnet` → `src/claude_vision.py::call_vision_model_claude`
- `gpt-4o` / `gpt-4o-mini` → `src/gpt_vision.py::call_vision_model_gpt`
- anything else (default) → Ollama at `localhost:11434` (`qwen2.5vl:7b`)

The same `_VISION_PROMPT_TEMPLATE` and figure-placeholder rules (`_FIGURE_RULE_PRESENT` / `_FIGURE_RULE_ABSENT`) are shared by all three backends via import from `src/vision.py`.

### Key functions to know

| Function | File | Purpose |
|---|---|---|
| `crop_questions_from_pdf` | `src/pdf_utils.py` | Main crop logic — returns `{q_num: png_path}` |
| `_content_bottom` | `src/pdf_utils.py` | Finds tight lower boundary of a question (text + image blocks) |
| `_detect_q_pattern` | `src/pdf_utils.py` | Auto-detects question numbering format |
| `build_question_mapping` | `src/pdf_utils.py` | Matches figures to questions by (page, y) position |
| `_transcribe_entry` | `api.py` | Per-question: text → vision fallback |
| `_vision_pipeline_for_scanned_pdf` | `api.py` | Full vision path for scanned PDFs |
| `classify_page_claude` | `src/claude_vision.py` | Page type classifier |
| `extract_questions_from_page_claude` | `src/claude_vision.py` | Full-page question extractor |

### Frontend

React + Vite at `frontend/`. All `/api/*` and `/health` requests are proxied to `localhost:5000` via `frontend/vite.config.js`. Extraction modes are separate components under `frontend/src/components/modes/`.

### Output formats

`/api/pdf-to-images` and `/api/extract-single` return a ZIP containing:
- `question_NNN.png` — one crop per question
- `extraction_results.xlsx` — columns: `question_num`, `question_text`, `figures`, `answers`, `source`

The `source` column records how each question was obtained (`pymupdf`, `vision (no embedded text)`, `vision error`, etc.). Vision-sourced rows are highlighted yellow in the Excel output.

`X-Extraction-Summary` response header contains a compact `pdf:N,vision:M,reasons:…` summary without downloading the file.

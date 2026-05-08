import os
import re
import fitz  # PyMuPDF
from src.pdf_processor import PDFProcessor

# Fallback pattern covering the most common formats.
_Q_PATTERN = re.compile(
    r'^\s*Question\s+(\d+)'
    r'|^\s*Q\s*[:.]\s*(\d+)'
    r'|^\s*(?:Q\.?\s*)?(\d+)[.)]\s',
    re.IGNORECASE,
)

# Ordered from most-specific to least-specific so the scorer picks the
# tightest pattern that still gives a consecutive run of question numbers.
_CANDIDATE_PATTERNS = [
    re.compile(r'^\s*Question\s+(\d+)', re.IGNORECASE),       # Question 1
    re.compile(r'^\s*Q\s*[:.]\s*(\d+)', re.IGNORECASE),       # Q: 1  Q. 1
    re.compile(r'^\s*Q\.?\s*(\d+)\s*[.):\s]', re.IGNORECASE), # Q1. Q1) Q1:
    re.compile(r'^\s*Q(\d+)\b', re.IGNORECASE),                # Q1  Q2
    re.compile(r'^\s*\((\d+)\)\s*'),                           # (1) (2)
    re.compile(r'^\s*\[(\d+)\]\s*'),                           # [1] [2]
    re.compile(r'^\s*(\d+)\.\s'),                              # 1.  2.
    re.compile(r'^\s*(\d+)\)\s'),                              # 1)  2)
    re.compile(r'^\s*(\d+)\s'),                                # 1   2  (broad)
]


def _q_num(m) -> int:
    """Return the captured question number from a match object (any group count)."""
    return int(next(g for g in m.groups() if g is not None))


def _detect_q_pattern(doc) -> re.Pattern:
    """Scan the first few pages of an open fitz.Document and return the compiled
    regex that best matches the PDF's question-numbering format.

    Strategy: each candidate is tested against every text-block first-line in the
    first 6 pages.  The pattern that produces the longest consecutive run of
    question numbers starting from 1 (or 2) wins.  Falls back to _Q_PATTERN when
    no candidate beats a minimum run of 2.
    """
    first_lines = []
    for page_idx in range(min(6, len(doc))):
        page = doc[page_idx]
        blocks = sorted(page.get_text("blocks"), key=lambda b: (b[1], b[0]))
        for block in blocks:
            if block[6] != 0:
                continue
            line = block[4].strip().split('\n')[0]
            if line:
                first_lines.append(line)

    best_pattern = _Q_PATTERN
    best_score = 1  # must beat the fallback threshold

    for pat in _CANDIDATE_PATTERNS:
        nums = set()
        for line in first_lines:
            m = pat.match(line)
            if m:
                try:
                    nums.add(int(m.group(1)))
                except (IndexError, ValueError):
                    pass

        if not nums:
            continue

        # Longest consecutive run starting at 1 or 2
        run = 0
        for start in (1, 2):
            r, expected = 0, start
            for n in sorted(nums):
                if n == expected:
                    r += 1
                    expected += 1
            run = max(run, r)

        if run > best_score:
            best_score = run
            best_pattern = pat

    return best_pattern
_DPI = 150
_MAT = fitz.Matrix(_DPI / 72, _DPI / 72)


def _stitch_vertical(pixmaps: list, out_path: str) -> None:
    """Vertically concatenate PyMuPDF Pixmap objects and save as a single PNG."""
    from PIL import Image as _PILImage
    import io
    pil_imgs = [_PILImage.open(io.BytesIO(px.tobytes("png"))) for px in pixmaps]
    total_w = max(img.width for img in pil_imgs)
    total_h = sum(img.height for img in pil_imgs)
    canvas = _PILImage.new("RGB", (total_w, total_h), "white")
    y_off = 0
    for img in pil_imgs:
        canvas.paste(img, (0, y_off))
        y_off += img.height
    canvas.save(out_path)


def pdf_pages_to_png(pdf_path: str, output_dir: str, prefix: str) -> list:
    """Render every page of a PDF to a PNG file and return the list of saved paths."""
    doc = fitz.open(pdf_path)
    paths = []
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=_MAT)
        out_path = os.path.join(output_dir, f'{prefix}_page_{i + 1:03d}.png')
        pix.save(out_path)
        paths.append(out_path)
    doc.close()
    return paths


def extract_figures_from_pdf(pdf_path: str, output_dir: str) -> list:
    """Extract every embedded image from the PDF and save to output_dir.

    Returns a list of (page_idx, y_top, saved_path) for position-based
    question assignment.
    """
    doc = fitz.open(pdf_path)
    results = []
    seen_xrefs = set()
    fig_idx = 1
    for page_idx, page in enumerate(doc):
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            rects = page.get_image_rects(xref)
            y_top = rects[0].y0 if rects else 0.0
            base_image = doc.extract_image(xref)
            ext = base_image["ext"]
            out_path = os.path.join(output_dir, f'figure_{fig_idx:03d}.{ext}')
            with open(out_path, 'wb') as f:
                f.write(base_image["image"])
            results.append((page_idx, y_top, out_path))
            fig_idx += 1
    doc.close()
    return results


def build_question_mapping(questions_path: str, answers_path: str, fig_data: list) -> list:
    """Return [{"question_num": N, "figure": [...] | None, "answer": "..."}, ...].

    fig_data: list of (page_idx, y_top, path) from extract_figures_from_pdf.
    Figures are matched to questions by comparing their (page, y) position
    against the question markers detected in the questions PDF.
    """
    processor = PDFProcessor(questions_path, answers_path)
    answers_list = processor.parse_answers(processor.extract_text_from_pdf(answers_path))

    doc = fitz.open(questions_path)
    pattern = _detect_q_pattern(doc)
    markers = []          # [(q_num, page_idx, y_top), ...] in reading order
    expected_num = None
    for page_idx, page in enumerate(doc):
        blocks = sorted(page.get_text("blocks"), key=lambda b: (b[1], b[0]))
        for block in blocks:
            if block[6] != 0:
                continue
            first_line = block[4].strip().split('\n')[0]
            m = pattern.match(first_line)
            if not m:
                continue
            num = _q_num(m)
            if expected_num is None or num == expected_num:
                markers.append((num, page_idx, block[1]))
                expected_num = num + 1
    doc.close()

    def _find_question(img_page: int, img_y: float):
        """Last marker whose start is at or before (img_page, img_y).
        Falls back to Q1 for figures that appear above the first question marker."""
        result = None
        for q_num, q_page, q_y in markers:
            if q_page < img_page or (q_page == img_page and q_y <= img_y):
                result = q_num
            else:
                break
        if result is None and markers:
            result = markers[0][0]
        return result

    q_figures: dict = {q_num: [] for q_num, _, _ in markers}
    for img_page, img_y, path in sorted(fig_data, key=lambda x: (x[0], x[1])):
        q_num = _find_question(img_page, img_y)
        if q_num is not None and q_num in q_figures:
            q_figures[q_num].append(path)

    mapping = []
    for i, (q_num, _, _) in enumerate(markers):
        figs = q_figures.get(q_num, [])
        mapping.append({
            "question_num": q_num,
            "figure":       figs if figs else None,
            "answer":       answers_list[q_num - 1] if q_num - 1 < len(answers_list) else "N/A",
        })
    return mapping


def build_pdf_map(questions_path: str, answers_path: str) -> list:
    """Detect all question boundaries and pair with answers before image generation.

    Returns [{"question_num", "start_page", "start_y", "end_page", "end_y",
               "spans_pages", "answer"}, ...] in reading order.
    """
    doc = fitz.open(questions_path)
    pattern = _detect_q_pattern(doc)
    markers = []
    expected_num = None
    for page_idx, page in enumerate(doc):
        blocks = sorted(page.get_text("blocks"), key=lambda b: (b[1], b[0]))
        for block in blocks:
            if block[6] != 0:
                continue
            first_line = block[4].strip().split('\n')[0]
            m = pattern.match(first_line)
            if not m:
                continue
            num = _q_num(m)
            if expected_num is None or num == expected_num:
                markers.append((num, page_idx, block[1]))
                expected_num = num + 1

    last_page_idx = len(doc) - 1
    last_page_h   = doc[last_page_idx].rect.height
    doc.close()

    processor    = PDFProcessor(questions_path, answers_path)
    answers_list = processor.parse_answers(processor.extract_text_from_pdf(answers_path))

    pdf_map = []
    for q_idx, (q_num, page_idx, y_top) in enumerate(markers):
        if q_idx + 1 < len(markers):
            _, next_page_idx, next_y = markers[q_idx + 1]
        else:
            next_page_idx = last_page_idx
            next_y        = last_page_h

        pdf_map.append({
            "question_num": q_num,
            "start_page":   page_idx,
            "start_y":      y_top,
            "end_page":     next_page_idx,
            "end_y":        next_y,
            "spans_pages":  next_page_idx != page_idx,
            "answer":       answers_list[q_num - 1] if q_num - 1 < len(answers_list) else "N/A",
        })
    return pdf_map


def crop_from_map(pdf_path: str, output_dir: str, pdf_map: list) -> dict:
    """Crop each question to its own PNG using a pre-built boundary map.

    Returns {q_num: png_path}. Reuses _content_bottom and _stitch_vertical.
    """
    doc   = fitz.open(pdf_path)
    crops = {}

    for entry in pdf_map:
        q_num         = entry["question_num"]
        page_idx      = entry["start_page"]
        y_top         = entry["start_y"]
        next_page_idx = entry["end_page"]
        next_y        = entry["end_y"]
        out_path      = os.path.join(output_dir, f'question_{q_num:03d}.png')
        page_a        = doc[page_idx]

        if not entry["spans_pages"]:
            # ── Same-page ────────────────────────────────────────────────────
            raw_bottom = _content_bottom(page_a, y_top, next_y)
            y0 = max(0.0, y_top - 5)
            y1 = min(page_a.rect.height, raw_bottom + 5)
            if y1 - y0 < 1:
                continue
            pix = page_a.get_pixmap(matrix=_MAT,
                                    clip=fitz.Rect(0, y0, page_a.rect.width, y1))
            pix.save(out_path)
        else:
            # ── Cross-page: stitch segments ──────────────────────────────────
            segments = []

            raw_bot_a = _content_bottom(page_a, y_top, page_a.rect.height)
            y0_a = max(0.0, y_top - 5)
            y1_a = min(page_a.rect.height, raw_bot_a + 5)
            if y1_a - y0_a >= 1:
                segments.append(page_a.get_pixmap(
                    matrix=_MAT,
                    clip=fitz.Rect(0, y0_a, page_a.rect.width, y1_a),
                ))

            for mid_idx in range(page_idx + 1, next_page_idx):
                segments.append(doc[mid_idx].get_pixmap(matrix=_MAT))

            page_c    = doc[next_page_idx]
            raw_bot_c = _content_bottom(page_c, 0.0, next_y)
            y1_c      = min(next_y, raw_bot_c + 5)
            if y1_c >= 1:
                segments.append(page_c.get_pixmap(
                    matrix=_MAT,
                    clip=fitz.Rect(0, 0, page_c.rect.width, y1_c),
                ))

            if not segments:
                continue
            _stitch_vertical(segments, out_path)

        crops[q_num] = out_path

    doc.close()
    return crops


def extract_figures_per_question(pdf_path: str, output_base_dir: str) -> dict:
    """Extract embedded images for each question's region in the PDF.

    Saves each figure to output_base_dir/question_num_{q_num:03d}/figure_{n:03d}.ext
    Returns {q_num: [list of saved figure paths]}.
    """
    doc = fitz.open(pdf_path)
    pattern = _detect_q_pattern(doc)
    markers = []
    expected_num = None

    for page_idx, page in enumerate(doc):
        blocks = sorted(page.get_text("blocks"), key=lambda b: (b[1], b[0]))
        for block in blocks:
            if block[6] != 0:
                continue
            first_line = block[4].strip().split('\n')[0]
            m = pattern.match(first_line)
            if not m:
                continue
            num = _q_num(m)
            if expected_num is None or num == expected_num:
                markers.append((num, page_idx, block[1]))
                expected_num = num + 1

    if not markers:
        doc.close()
        return {}

    q_ranges = {}
    for q_idx, (q_num, page_idx, y_top) in enumerate(markers):
        page_rect = doc[page_idx].rect
        if q_idx + 1 < len(markers):
            next_q_num, next_page_idx, next_y = markers[q_idx + 1]
            y_bottom = next_y if next_page_idx == page_idx else page_rect.height
        else:
            y_bottom = page_rect.height
        y0 = 0.0 if q_idx == 0 else max(0.0, y_top - 5)
        q_ranges[q_num] = (page_idx, y0, y_bottom)

    q_figures: dict = {q_num: [] for q_num in q_ranges}
    seen_xrefs = set()
    fig_idx = 1

    for page_idx, page in enumerate(doc):
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            rects = page.get_image_rects(xref)
            if not rects:
                continue
            img_y = rects[0].y0

            assigned_q = None
            for q_num, (q_page, y0, y_bottom) in q_ranges.items():
                if q_page == page_idx and y0 <= img_y < y_bottom:
                    assigned_q = q_num
                    break
            if assigned_q is None:
                continue

            base_image = doc.extract_image(xref)
            ext = base_image["ext"]
            q_dir = os.path.join(output_base_dir, f'question_num_{assigned_q:03d}')
            os.makedirs(q_dir, exist_ok=True)
            out_path = os.path.join(q_dir, f'figure_{fig_idx:03d}.{ext}')
            with open(out_path, 'wb') as f:
                f.write(base_image["image"])
            q_figures[assigned_q].append(out_path)
            fig_idx += 1

    doc.close()
    return q_figures


def extract_question_texts_from_pdf(pdf_path: str) -> dict:
    """Extract question text directly from PDF using PyMuPDF text blocks.

    Returns {q_num: text}.  Returns an empty dict when the PDF is scanned
    (no embedded text), so callers can fall back to a vision model.
    """
    doc = fitz.open(pdf_path)
    pattern = _detect_q_pattern(doc)
    markers = []
    expected_num = None

    for page_idx, page in enumerate(doc):
        blocks = sorted(page.get_text("blocks"), key=lambda b: (b[1], b[0]))
        for block in blocks:
            if block[6] != 0:
                continue
            first_line = block[4].strip().split('\n')[0]
            m = pattern.match(first_line)
            if not m:
                continue
            num = _q_num(m)
            if expected_num is None or num == expected_num:
                markers.append((num, page_idx, block[1]))
                expected_num = num + 1

    if not markers:
        doc.close()
        return {}

    texts = {}
    for q_idx, (q_num, page_idx, y_top) in enumerate(markers):
        page_a = doc[page_idx]

        if q_idx + 1 < len(markers):
            _, next_page_idx, next_y = markers[q_idx + 1]
        else:
            next_page_idx = len(doc) - 1
            next_y = doc[next_page_idx].rect.height

        if next_page_idx == page_idx:
            # ── Same-page case ────────────────────────────────────────────────
            y0 = max(0.0, y_top - 2)
            raw_bottom = _content_bottom(page_a, y_top, next_y)
            y1 = min(page_a.rect.height, raw_bottom + 2)
            if y1 - y0 < 1:
                continue
            text = page_a.get_text("text", clip=fitz.Rect(0.0, y0, page_a.rect.width, y1)).strip()
            if text:
                texts[q_num] = text
        else:
            # ── Cross-page case: collect text across multiple pages ────────────
            parts = []

            # Part A: from question marker to bottom of starting page
            y0_a = max(0.0, y_top - 2)
            raw_bot_a = _content_bottom(page_a, y_top, page_a.rect.height)
            y1_a = min(page_a.rect.height, raw_bot_a + 2)
            if y1_a - y0_a >= 1:
                t = page_a.get_text("text", clip=fitz.Rect(0.0, y0_a, page_a.rect.width, y1_a)).strip()
                if t:
                    parts.append(t)

            # Part B: full intermediate pages
            for mid_idx in range(page_idx + 1, next_page_idx):
                t = doc[mid_idx].get_text("text").strip()
                if t:
                    parts.append(t)

            # Part C: top of next question's page up to (not including) next question
            page_c = doc[next_page_idx]
            raw_bot_c = _content_bottom(page_c, 0.0, next_y)
            y1_c = min(next_y, raw_bot_c + 2)
            if y1_c >= 1:
                t = page_c.get_text("text", clip=fitz.Rect(0.0, 0.0, page_c.rect.width, y1_c)).strip()
                if t:
                    parts.append(t)

            text = "\n".join(parts)
            if text:
                texts[q_num] = text

    doc.close()
    return texts


def _content_bottom(page, y_start: float, y_limit: float) -> float:
    """Return the y-bottom of the last content block (text or image) whose top
    falls in [y_start, y_limit).  Returns y_start when no blocks are found."""
    bottom = y_start
    for block in page.get_text("blocks"):
        if block[1] < y_start or block[1] >= y_limit:
            continue
        bottom = max(bottom, block[3])
    # Also include embedded XObject images (e.g. image-based answer options)
    # that may not appear as blocks in the text extraction pipeline.
    for img_info in page.get_images(full=True):
        xref = img_info[0]
        for rect in page.get_image_rects(xref):
            if rect.y0 < y_start or rect.y0 >= y_limit:
                continue
            bottom = max(bottom, rect.y1)
    return bottom


def crop_questions_from_pdf(pdf_path: str, output_dir: str) -> dict:
    """Detect question boundaries via sequential numbered patterns and crop each
    question region to its own PNG file.

    Returns {q_num: path} so callers always look up by question number, not index.
    Falls back to {page_num: path} per page when no markers are found.
    """
    doc = fitz.open(pdf_path)
    pattern = _detect_q_pattern(doc)
    markers = []   # [(q_num, page_idx, y_top), ...]
    expected_num = None

    for page_idx, page in enumerate(doc):
        blocks = sorted(page.get_text("blocks"), key=lambda b: (b[1], b[0]))
        for block in blocks:
            if block[6] != 0:
                continue
            first_line = block[4].strip().split('\n')[0]
            m = pattern.match(first_line)
            if not m:
                continue
            num = _q_num(m)
            if expected_num is None or num == expected_num:
                markers.append((num, page_idx, block[1]))
                expected_num = num + 1

    crops = {}

    if not markers:
        for page_idx, page in enumerate(doc):
            pix = page.get_pixmap(matrix=_MAT)
            out_path = os.path.join(output_dir, f'question_{page_idx + 1:03d}.png')
            pix.save(out_path)
            crops[page_idx + 1] = out_path
        doc.close()
        return crops

    for q_idx, (q_num, page_idx, y_top) in enumerate(markers):
        page_a = doc[page_idx]

        if q_idx + 1 < len(markers):
            _, next_page_idx, next_y = markers[q_idx + 1]
        else:
            next_page_idx = len(doc) - 1
            next_y = doc[next_page_idx].rect.height

        out_path = os.path.join(output_dir, f'question_{q_num:03d}.png')

        if next_page_idx == page_idx:
            # ── Same-page case ────────────────────────────────────────────────
            raw_bottom = _content_bottom(page_a, y_top, next_y)
            y0 = max(0.0, y_top - 5)
            y1 = min(page_a.rect.height, raw_bottom + 5)
            if y1 - y0 < 1:
                continue
            pix = page_a.get_pixmap(matrix=_MAT, clip=fitz.Rect(0, y0, page_a.rect.width, y1))
            pix.save(out_path)
        else:
            # ── Cross-page case: stitch segments from multiple pages ───────────
            segments = []

            # Segment A: from question marker to bottom of its starting page
            raw_bot_a = _content_bottom(page_a, y_top, page_a.rect.height)
            y0_a = max(0.0, y_top - 5)
            y1_a = min(page_a.rect.height, raw_bot_a + 5)
            if y1_a - y0_a >= 1:
                segments.append(page_a.get_pixmap(
                    matrix=_MAT,
                    clip=fitz.Rect(0, y0_a, page_a.rect.width, y1_a),
                ))

            # Segment B: any full intermediate pages
            for mid_idx in range(page_idx + 1, next_page_idx):
                segments.append(doc[mid_idx].get_pixmap(matrix=_MAT))

            # Segment C: top of next question's page up to (but not including) next question.
            # Bounded strictly by next_y so no content from question N+1 leaks in.
            page_c = doc[next_page_idx]
            raw_bot_c = _content_bottom(page_c, 0.0, next_y)
            y1_c = min(next_y, raw_bot_c + 5)
            if y1_c >= 1:
                segments.append(page_c.get_pixmap(
                    matrix=_MAT,
                    clip=fitz.Rect(0, 0, page_c.rect.width, y1_c),
                ))

            if not segments:
                continue
            _stitch_vertical(segments, out_path)

        crops[q_num] = out_path

    doc.close()
    return crops


def save_page_crops(pdf_path: str, page_index: int, layout_type: str,
                    page_type: str, base_dir: str = ".") -> list:
    """Render and save a page (or its left/right halves for multi-column) to disk.

    Files are written to <base_dir>/questions/ or <base_dir>/solutions/.
    Returns a list of saved absolute paths.
    """
    target_dir = os.path.join(base_dir, page_type)
    os.makedirs(target_dir, exist_ok=True)

    doc = fitz.open(pdf_path)
    page = doc[page_index]
    rect = page.rect
    page_num = page_index + 1
    saved = []

    if layout_type == "multi_column":
        mid_x = rect.width / 2
        halves = [
            ("left",  fitz.Rect(0,     0, mid_x,      rect.height)),
            ("right", fitz.Rect(mid_x, 0, rect.width, rect.height)),
        ]
        for side, clip in halves:
            pix = page.get_pixmap(matrix=_MAT, clip=clip)
            path = os.path.join(target_dir, f"page_{page_num:03d}_{side}.png")
            pix.save(path)
            saved.append(os.path.abspath(path))
    else:
        pix = page.get_pixmap(matrix=_MAT)
        path = os.path.join(target_dir, f"page_{page_num:03d}.png")
        pix.save(path)
        saved.append(os.path.abspath(path))

    doc.close()
    return saved


def extract_figures_from_pages(pdf_path: str, page_indices: list, output_dir: str) -> list:
    """Extract embedded images from a specific subset of pages.

    Same return format as extract_figures_from_pdf — list of (page_idx, y_top, saved_path) —
    but only processes the pages in page_indices (0-based).
    """
    os.makedirs(output_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    results = []
    seen_xrefs = set()
    fig_idx = 1

    for page_idx in sorted(set(page_indices)):
        if page_idx >= len(doc):
            continue
        page = doc[page_idx]
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            rects = page.get_image_rects(xref)
            y_top = rects[0].y0 if rects else 0.0
            base_image = doc.extract_image(xref)
            ext = base_image["ext"]
            out_path = os.path.join(output_dir, f"figure_{fig_idx:03d}.{ext}")
            with open(out_path, "wb") as f:
                f.write(base_image["image"])
            results.append((page_idx, y_top, out_path))
            fig_idx += 1

    doc.close()
    return results


def map_figures_to_questions_on_pages(pdf_path: str, page_indices: list, fig_data: list) -> dict:
    """Map figure paths (from extract_figures_from_pages) to question numbers.

    Scans only the specified pages for numbered question markers, then assigns
    each figure to the nearest preceding marker by (page, y) position.
    Returns {q_num: [list_of_figure_paths]}.  Empty dict if no markers found.
    """
    if not page_indices or not fig_data:
        return {}

    doc = fitz.open(pdf_path)
    pattern = _detect_q_pattern(doc)
    markers = []
    expected_num = None

    for page_idx in sorted(set(page_indices)):
        if page_idx >= len(doc):
            continue
        page = doc[page_idx]
        blocks = sorted(page.get_text("blocks"), key=lambda b: (b[1], b[0]))
        for block in blocks:
            if block[6] != 0:
                continue
            first_line = block[4].strip().split('\n')[0]
            m = pattern.match(first_line)
            if not m:
                continue
            num = _q_num(m)
            if expected_num is None or num == expected_num:
                markers.append((num, page_idx, block[1]))
                expected_num = num + 1

    doc.close()

    if not markers:
        return {}

    def _find_nearest(img_page: int, img_y: float):
        result = None
        for q_num, q_page, q_y in markers:
            if q_page < img_page or (q_page == img_page and q_y <= img_y):
                result = q_num
            else:
                break
        return result if result is not None else markers[0][0]

    q_figures: dict = {q_num: [] for q_num, _, _ in markers}
    for img_page, img_y, path in sorted(fig_data, key=lambda x: (x[0], x[1])):
        q_num = _find_nearest(img_page, img_y)
        if q_num in q_figures:
            q_figures[q_num].append(path)

    return q_figures


def detect_layout_fitz(pdf_path: str, page_index: int) -> dict:
    """Detect single vs multi-column layout using PyMuPDF text block positions.

    Skips the top 25 % of the page so full-width titles/headers don't pollute
    the column signal.  Returns a dict with: layout, columns, confidence, reason.
    """
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    page_width = page.rect.width
    page_height = page.rect.height

    # blocks: (x0, y0, x1, y1, text, block_no, block_type)  block_type 0 = text
    blocks = page.get_text("blocks")
    doc.close()

    # Keep only text blocks in the content body (below the top-25 % header band)
    content_blocks = [
        b for b in blocks
        if b[6] == 0
        and b[1] > page_height * 0.25
        and len(b[4].strip()) > 10
    ]

    if len(content_blocks) < 4:
        return {
            "layout": "single_column",
            "columns": 1,
            "confidence": 0.5,
            "reason": "Too few content blocks to determine layout",
        }

    # A right-column block starts past 35 % of the page width
    col_threshold = page_width * 0.35
    left_blocks  = [b for b in content_blocks if b[0] <= col_threshold]
    right_blocks = [b for b in content_blocks if b[0] >  col_threshold]

    if len(right_blocks) >= 3 and len(left_blocks) >= 3:
        return {
            "layout": "multi_column",
            "columns": 2,
            "confidence": 0.92,
            "reason": (
                f"{len(left_blocks)} blocks in left column, "
                f"{len(right_blocks)} blocks in right column"
            ),
        }

    return {
        "layout": "single_column",
        "columns": 1,
        "confidence": 0.92,
        "reason": f"All {len(content_blocks)} content blocks start in the left region",
    }

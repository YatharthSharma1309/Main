import os
import re
import fitz  # PyMuPDF
from src.pdf_processor import PDFProcessor

_Q_PATTERN = re.compile(r'^\s*(?:Q\.?\s*)?(\d+)[.)]\s', re.IGNORECASE)
_DPI = 150
_MAT = fitz.Matrix(_DPI / 72, _DPI / 72)


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
    markers = []          # [(q_num, page_idx, y_top), ...] in reading order
    expected_num = None
    for page_idx, page in enumerate(doc):
        blocks = sorted(page.get_text("blocks"), key=lambda b: (b[1], b[0]))
        for block in blocks:
            if block[6] != 0:
                continue
            first_line = block[4].strip().split('\n')[0]
            m = _Q_PATTERN.match(first_line)
            if not m:
                continue
            num = int(m.group(1))
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
            "answer":       answers_list[i] if i < len(answers_list) else "N/A",
        })
    return mapping


def extract_figures_per_question(pdf_path: str, output_base_dir: str) -> dict:
    """Extract embedded images for each question's region in the PDF.

    Saves each figure to output_base_dir/question_num_{q_num:03d}/figure_{n:03d}.ext
    Returns {q_num: [list of saved figure paths]}.
    """
    doc = fitz.open(pdf_path)
    markers = []
    expected_num = None

    for page_idx, page in enumerate(doc):
        blocks = sorted(page.get_text("blocks"), key=lambda b: (b[1], b[0]))
        for block in blocks:
            if block[6] != 0:
                continue
            first_line = block[4].strip().split('\n')[0]
            m = _Q_PATTERN.match(first_line)
            if not m:
                continue
            num = int(m.group(1))
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


def _content_bottom(page, y_start: float, y_limit: float) -> float:
    """Return the y-bottom of the last content block (text or image) whose top
    falls in [y_start, y_limit).  Returns y_start when no blocks are found."""
    bottom = y_start
    for block in page.get_text("blocks"):
        if block[1] < y_start or block[1] >= y_limit:
            continue
        bottom = max(bottom, block[3])
    return bottom


def crop_questions_from_pdf(pdf_path: str, output_dir: str) -> dict:
    """Detect question boundaries via sequential numbered patterns and crop each
    question region to its own PNG file.

    Returns {q_num: path} so callers always look up by question number, not index.
    Falls back to {page_num: path} per page when no markers are found.
    """
    doc = fitz.open(pdf_path)
    markers = []   # [(q_num, page_idx, y_top), ...]
    expected_num = None

    for page_idx, page in enumerate(doc):
        blocks = sorted(page.get_text("blocks"), key=lambda b: (b[1], b[0]))
        for block in blocks:
            if block[6] != 0:
                continue
            first_line = block[4].strip().split('\n')[0]
            m = _Q_PATTERN.match(first_line)
            if not m:
                continue
            num = int(m.group(1))
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
        page = doc[page_idx]
        page_rect = page.rect

        # y_limit: hard upper boundary — the start of the next question (or page bottom).
        # Used only to scope the content search; the actual crop bottom is tighter.
        if q_idx + 1 < len(markers):
            next_q_num, next_page_idx, next_y = markers[q_idx + 1]
            y_limit = next_y if next_page_idx == page_idx else page_rect.height
        else:
            y_limit = page_rect.height

        x0 = 0.0
        y0 = max(0.0, y_top - 5)
        x1 = page_rect.width

        # Tighten the bottom to the last content block (text or image) so the
        # crop contains only the question + options, not the gap before the next question.
        raw_bottom = _content_bottom(page, y_top, y_limit)
        y1 = min(page_rect.height, raw_bottom + 5)

        if y1 - y0 < 1 or x1 - x0 < 1:
            continue

        clip = fitz.Rect(x0, y0, x1, y1)
        pix = page.get_pixmap(matrix=_MAT, clip=clip)
        out_path = os.path.join(output_dir, f'question_{q_num:03d}.png')
        pix.save(out_path)
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
            m = _Q_PATTERN.match(first_line)
            if not m:
                continue
            num = int(m.group(1))
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

import json
import base64
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env file

_CLASSIFICATION_PROMPT = (
    "You are a document-page classifier for academic PDFs.\n\n"
    "Classify this page into exactly one of the following categories:\n"
    "- theory: Explanatory content — definitions, concepts, worked examples with full solutions shown inline, or lecture notes.\n"
    "- questions: A page containing exam or practice problems/questions that a student is expected to solve. "
    "Answer choices (MCQ) count as questions.\n"
    "- solutions: A page containing answers or solutions to previously posed questions "
    "(e.g. answer keys, solution walkthroughs).\n"
    "- misc: Anything that doesn't fit the above — cover pages, table of contents, "
    "instructions, blank pages, index, bibliography.\n\n"
    "Return ONLY a JSON object with no surrounding text:\n"
    '{"page_type": "<theory|questions|solutions|misc>", "confidence": <0.0-1.0>, "reason": "<one short sentence>"}'
)


def classify_page_with_gpt(image_path: str, model: str = "gpt-4o-mini") -> dict:
    """Classify a single PDF page image using GPT-4o-mini.

    Returns a dict with keys: page_type, confidence, reason.
    """
    with open(image_path, "rb") as f:
        image_b64 = base64.standard_b64encode(f.read()).decode("utf-8")

    client = OpenAI()
    response = client.chat.completions.create(
        model=model,
        max_tokens=128,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                },
                {"type": "text", "text": _CLASSIFICATION_PROMPT},
            ],
        }],
    )

    raw = response.choices[0].message.content.strip()
    return json.loads(raw)


_LAYOUT_PROMPT = (
    "You are a document layout analyser for academic PDFs.\n\n"
    "Look at the page and determine how the main content is arranged horizontally.\n\n"
    "Rules:\n"
    "- single_column: All text/content runs in one continuous column that spans most of the page width.\n"
    "- multi_column: Content is split into two or more side-by-side vertical columns "
    "(e.g. two-column exam paper, newspaper-style layout, answer grid).\n\n"
    "Count only the primary content columns, not headers/footers or page-number lines.\n\n"
    "Return ONLY a JSON object with no surrounding text:\n"
    '{"layout": "<single_column|multi_column>", "columns": <integer number of columns>, '
    '"confidence": <0.0-1.0>, "reason": "<one short sentence>"}'
)


def detect_layout_with_gpt(image_path: str, model: str = "gpt-4o-mini") -> dict:
    """Detect whether a page is single-column or multi-column using GPT-4o-mini.

    Returns a dict with keys: layout, columns, confidence, reason.
    Only meaningful for questions/solutions pages.
    """
    with open(image_path, "rb") as f:
        image_b64 = base64.standard_b64encode(f.read()).decode("utf-8")

    client = OpenAI()
    response = client.chat.completions.create(
        model=model,
        max_tokens=128,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                },
                {"type": "text", "text": _LAYOUT_PROMPT},
            ],
        }],
    )

    raw = response.choices[0].message.content.strip()
    return json.loads(raw)

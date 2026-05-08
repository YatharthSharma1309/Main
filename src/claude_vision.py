import re
import base64
import anthropic

from src.vision import _VISION_PROMPT_TEMPLATE, _FIGURE_RULE_PRESENT, _FIGURE_RULE_ABSENT


# Built at call time with actual image height
_PAGE_EXTRACT_PROMPT_TEMPLATE = """\
You are an expert exam question extractor.

This image shows a page from an exam paper (image height: {image_height} pixels, \
top = 0, bottom = {image_height}).

Questions may be numbered in any format — for example:
  "Q: 1", "Q.1", "Q1.", "1.", "1)", "(1)", "Question 1", or plain "1".

CRITICAL RULE: Use the EXACT question number printed on the page. \
Do NOT renumber questions starting from 1. \
For example, if this page shows Q: 8, Q: 9, Q: 10, output QUESTION 8, QUESTION 9, QUESTION 10.

For each question on this page:
1. Read the question number exactly as printed (e.g. if you see "Q: 11", use 11).
2. Give the y-pixel coordinate of the TOP of that question's label, measured from \
the very top of the image.
3. Extract the COMPLETE question text including all sub-parts, answer options \
(A/B/C/D or 1/2/3/4), marks in brackets, and any instructions.
4. Write math in plain Unicode — fractions as (a)/(b), exponents as ^N, \
square roots as sqrt(x). No LaTeX, no backslashes.
5. If a figure, diagram, graph, or image appears anywhere inside the question or \
its options, write [Figure] at that exact position.

Output ONLY in this exact format — one block per question, nothing else:

QUESTION 8 (y=95):
[complete text of question 8]

QUESTION 9 (y=580):
[complete text of question 9]

Use the EXACT question number from the page and the actual pixel y-coordinate.\
"""

_PAGE_CLASSIFY_PROMPT = """\
You are classifying a single page from an exam document.

A page may have a logo, header, or branding at the top — ignore those and focus \
on the main body content.

Classify the page as exactly one of:
- "questions" — the main body contains numbered exam questions for students to answer. \
  Mark allocations like "[2]" or "(2 marks)" after a question stem are normal and do NOT \
  make it an answers page.
- "answers"   — the main body contains any of: an answer key table, correct-answer list, \
  worked solutions, model answers, OR a marking rubric where solution steps are written as \
  "Writes that …", "Finds …", "Calculates …", "Solves …", "Assumes …", "Identifies …", \
  "Draws …", "Hence …", "Therefore …", or similar, each followed by a mark allocation \
  in square brackets like [0.5] or [1]. A page is "answers" even if question numbers appear \
  alongside the rubric steps.
- "other"     — the page is purely a cover, title page, blank page, syllabus, \
  table of contents, or instructions with no actual questions or answers

When in doubt between "questions" and "other", choose "questions" if you can see \
any numbered items that look like exam questions.
When in doubt between "questions" and "answers", choose "answers" if the lines \
describe solution steps with mark allocations.

Output ONLY the single word: questions, answers, or other\
"""

_PAGE_ANSWER_PROMPT = """\
This page contains answers or solutions to exam questions.

For each question that has an answer on this page:
1. Identify the question number.
2. Extract the answer following these rules by page type:
   a) MCQ answer key (table of "Q. No | Answer"): extract just the option number or letter.
   b) Short-answer key: extract the value or brief answer.
   c) Rubric / marking scheme (lines like "Writes that …", "Finds …", "Calculates …"):
      extract ONLY the final answer or key result — the specific value, measurement, or
      conclusion the student must reach. Do NOT copy the full rubric steps.
      Example: if the rubric says "Finds the length of RP as RS + PS = 9 + 9 = 18 cm. [0.5]",
      extract "18 cm".
      Example: if it says "Writes that the two triangles are similar by AA criterion.",
      extract "Similar by AA criterion".
3. If the answer includes a diagram write [Figure].
4. Write math in plain Unicode — fractions as (a)/(b), exponents as ^N, sqrt(x). No LaTeX.
5. Do NOT include mark allocations like "(2 marks)" or "[1]".

Output ONLY in this exact format — one block per answer, nothing else:

ANSWER 1:
[answer for question 1]

ANSWER 2:
[answer for question 2]

Use the actual question number from the page.\
"""


def classify_page_claude(image_path: str,
                         model: str = "claude-haiku-4-5-20251001") -> str:
    """Classify a page image as 'questions', 'answers', or 'other'.

    Returns exactly one of those three strings.
    """
    with open(image_path, "rb") as f:
        image_b64 = base64.standard_b64encode(f.read()).decode("utf-8")

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=16,
        temperature=0,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
                {"type": "text", "text": _PAGE_CLASSIFY_PROMPT},
            ],
        }],
    )
    raw = message.content[0].text.strip().lower()
    if "answer" in raw or "solution" in raw:
        return "answers"
    if "question" in raw:
        return "questions"
    return "other"


def extract_questions_from_page_claude(image_path: str,
                                       model: str = "claude-haiku-4-5-20251001") -> list:
    """Extract all questions from a full-page exam image using Claude vision.

    Returns list of {"question_num": int, "question_text": str, "y_px": int}.
    y_px is the pixel y-coordinate of the question label from the top of the image.
    """
    from PIL import Image as _PILImage
    with _PILImage.open(image_path) as _img:
        _img_h = _img.size[1]

    with open(image_path, "rb") as f:
        image_b64 = base64.standard_b64encode(f.read()).decode("utf-8")

    prompt = _PAGE_EXTRACT_PROMPT_TEMPLATE.format(image_height=_img_h)

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=4096,
        temperature=0,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    raw = message.content[0].text.strip()

    entries = []
    pattern = re.compile(
        r'QUESTION\s+(\d+)\s*\(y\s*=\s*(\d+)\s*\)\s*:\s*\n(.*?)(?=\nQUESTION\s+\d+\s*\(y\s*=|$)',
        re.DOTALL,
    )
    for m in pattern.finditer(raw):
        entries.append({
            "question_num":  int(m.group(1)),
            "y_px":          int(m.group(2)),
            "question_text": m.group(3).strip(),
        })

    # Validate: y_px should increase with question_num.
    # If not monotonically increasing, re-sort by y_px and reassign to match
    # the expected question numbers from the text content.
    if len(entries) >= 2:
        by_y = sorted(entries, key=lambda e: e["y_px"])
        by_q = sorted(entries, key=lambda e: e["question_num"])
        if any(by_y[i]["question_num"] != by_q[i]["question_num"] for i in range(len(by_y))):
            for entry, positioned in zip(by_q, by_y):
                entry["y_px"] = positioned["y_px"]

    return entries


def extract_answers_from_page_claude(image_path: str,
                                     model: str = "claude-haiku-4-5-20251001") -> dict:
    """Extract answers from an answer/solution page image using Claude vision.

    Handles all answer formats: single letter/number, full sentences, multi-line
    worked solutions, and answers that include figures.

    Returns {q_num: answer_text} where answer_text may be multi-line.
    """
    with open(image_path, "rb") as f:
        image_b64 = base64.standard_b64encode(f.read()).decode("utf-8")

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=4096,
        temperature=0,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
                {"type": "text", "text": _PAGE_ANSWER_PROMPT},
            ],
        }],
    )
    raw = message.content[0].text.strip()

    result = {}
    pattern = re.compile(
        r'ANSWER\s+(\d+)\s*:\s*\n(.*?)(?=\nANSWER\s+\d+\s*:|$)',
        re.DOTALL,
    )
    for m in pattern.finditer(raw):
        answer_text = m.group(2).strip()
        if answer_text:
            result[int(m.group(1))] = answer_text
    return result


def call_vision_model_claude(image_path: str, figure_count: int = 0,
                             model: str = "claude-haiku-4-5-20251001") -> str:
    """Send a question-crop image to Anthropic Claude and return the extracted text.

    Reads ANTHROPIC_API_KEY from the environment.
    Supported models: claude-haiku-4-5-20251001, claude-sonnet-4-6
    """
    rule = _FIGURE_RULE_PRESENT.format(n=figure_count) if figure_count > 0 else _FIGURE_RULE_ABSENT
    prompt = _VISION_PROMPT_TEMPLATE.format(figure_instruction=rule)

    with open(image_path, "rb") as f:
        image_b64 = base64.standard_b64encode(f.read()).decode("utf-8")

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=2048,
        temperature=0,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": image_b64},
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return message.content[0].text.strip()

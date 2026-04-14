import re


def normalize_student_code(value) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return ""

    text = text.replace("\u00A0", " ")
    text = re.sub(r"\s+", "", text)

    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]

    return text.upper()

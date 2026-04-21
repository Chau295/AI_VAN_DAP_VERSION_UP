from __future__ import annotations

import re


ACADEMIC_YEAR_PATTERN = re.compile(r"^\d{4}-\d{4}$")
ACADEMIC_YEAR_ERROR_MESSAGE = "Năm học phải đúng định dạng xxxx-xxxx và chỉ được chứa chữ số."


def is_valid_academic_year(value: str) -> bool:
    return bool(ACADEMIC_YEAR_PATTERN.fullmatch(str(value or "")))


def sanitize_academic_year_for_display(value: str) -> str:
    cleaned = str(value or "").strip()
    return cleaned if is_valid_academic_year(cleaned) else ""

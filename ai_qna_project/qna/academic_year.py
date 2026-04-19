from __future__ import annotations

import re


ACADEMIC_YEAR_PATTERN = re.compile(r"^\d{4}-\d{4}$")
ACADEMIC_YEAR_ERROR_MESSAGE = "Năm học phải đúng định dạng xxxx-xxxx và chỉ được chứa chữ số."


def is_valid_academic_year(value: str) -> bool:
    return bool(ACADEMIC_YEAR_PATTERN.fullmatch(str(value or "")))

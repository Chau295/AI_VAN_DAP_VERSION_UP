from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.cache import cache
from django.utils import timezone

LECTURER_HIDDEN_VIOLATION_TYPES = {"NO_FACE"}
POST_END_SUBMISSION_GRACE_SECONDS = 15
EXAM_GRADING_STATE_TIMEOUT_SECONDS = 60 * 60
EXAM_GRADING_STATUS_PENDING = "PENDING"
EXAM_GRADING_STATUS_PROGRESS = "PROGRESS"
EXAM_GRADING_STATUS_RESULT = "RESULT"
EXAM_GRADING_STATUS_ERROR = "ERROR"


def build_exam_grading_cache_key(session_id, question_id) -> str:
    return f"exam_grading:{int(session_id)}:{int(question_id)}"


def get_exam_grading_state(session_id, question_id) -> dict[str, Any] | None:
    if not session_id or not question_id:
        return None
    return cache.get(build_exam_grading_cache_key(session_id, question_id))


def clear_exam_grading_state(session_id, question_id) -> None:
    if not session_id or not question_id:
        return
    cache.delete(build_exam_grading_cache_key(session_id, question_id))


def store_exam_grading_state(
    session_id,
    question_id,
    *,
    status: str,
    progress: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error_message: str = "",
) -> dict[str, Any]:
    state = {
        "status": status,
        "session_id": int(session_id),
        "question_id": int(question_id),
        "progress": progress or {},
        "result": result or {},
        "error_message": error_message or "",
        "updated_at": timezone.now().isoformat(),
    }
    cache.set(
        build_exam_grading_cache_key(session_id, question_id),
        state,
        timeout=EXAM_GRADING_STATE_TIMEOUT_SECONDS,
    )
    return state


def is_exam_group_open(exam_group, *, now=None) -> bool:
    if not exam_group or exam_group.status in {"CANCELLED", "DRAFT"}:
        return False

    current_time = now or timezone.now()
    start_at = getattr(exam_group, "start_at", None)
    end_at = getattr(exam_group, "end_at", None)
    if not start_at or not end_at:
        return False

    return start_at <= current_time <= end_at


def is_exam_group_submission_window_open(
    exam_group,
    *,
    allow_post_end_grace=False,
    now=None,
) -> bool:
    current_time = now or timezone.now()
    if is_exam_group_open(exam_group, now=current_time):
        return True

    if not allow_post_end_grace or not exam_group or exam_group.status in {"CANCELLED", "DRAFT"}:
        return False

    end_at = getattr(exam_group, "end_at", None)
    if not end_at:
        return False

    grace_deadline = end_at + timedelta(seconds=POST_END_SUBMISSION_GRACE_SECONDS)
    return end_at < current_time <= grace_deadline


def should_mark_lecturer_cheating_flag(violation_type: str | None) -> bool:
    return (violation_type or "") not in LECTURER_HIDDEN_VIOLATION_TYPES

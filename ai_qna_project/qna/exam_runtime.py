from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

LECTURER_HIDDEN_VIOLATION_TYPES = {"NO_FACE"}
POST_END_SUBMISSION_GRACE_SECONDS = 15


def is_exam_group_open(exam_group, *, now=None) -> bool:
    if not exam_group or exam_group.status == "CANCELLED":
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

    if not allow_post_end_grace or not exam_group or exam_group.status == "CANCELLED":
        return False

    end_at = getattr(exam_group, "end_at", None)
    if not end_at:
        return False

    grace_deadline = end_at + timedelta(seconds=POST_END_SUBMISSION_GRACE_SECONDS)
    return end_at < current_time <= grace_deadline


def should_mark_lecturer_cheating_flag(violation_type: str | None) -> bool:
    return (violation_type or "") not in LECTURER_HIDDEN_VIOLATION_TYPES

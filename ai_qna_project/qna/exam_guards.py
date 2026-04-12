from __future__ import annotations

from datetime import timedelta

from django.db import models
from django.db.models import Q
from django.utils import timezone

from .models import ExamSessionGroup, Subject


def is_exam_group_active(exam_group: ExamSessionGroup | None, now=None) -> bool:
    if not exam_group or exam_group.status == "CANCELLED":
        return False

    current_time = now or timezone.now()
    start_at = getattr(exam_group, "start_at", None)
    end_at = getattr(exam_group, "end_at", None)
    if not start_at or not end_at:
        return False

    return start_at <= current_time <= end_at


def subject_has_active_exam_group(subject: Subject, now=None) -> bool:
    current_time = now or timezone.now()
    
    return ExamSessionGroup.objects.filter(
        subject_id=subject,
        exam_date__lte=current_time,
    ).exclude(
        status="CANCELLED"
    ).filter(
        exam_date__gte=current_time - models.F('duration_minutes') * timedelta(minutes=1)
    ).exists()

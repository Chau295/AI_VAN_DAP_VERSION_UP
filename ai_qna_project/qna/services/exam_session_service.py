from django.utils import timezone


SESSION_DRAFT = "DRAFT"
SESSION_ONGOING = "ONGOING"
SESSION_COMPLETED = "COMPLETED"
SESSION_CANCELLED = "CANCELLED"


def get_exam_group_computed_status(group, now=None):
    """
    Trạng thái thực tế tính theo thời gian.
    Không phụ thuộc hoàn toàn vào field status lưu DB.
    """
    if not group:
        return SESSION_DRAFT

    current = now or timezone.now()

    if getattr(group, "status", None) == SESSION_CANCELLED:
        return SESSION_CANCELLED

    start_at = getattr(group, "start_at", None) or getattr(group, "exam_date", None)
    end_at = getattr(group, "end_at", None)

    if not end_at and start_at:
        duration = getattr(group, "duration_minutes", 0) or 0
        end_at = start_at + timezone.timedelta(minutes=duration)

    if not start_at or not end_at:
        return getattr(group, "status", None) or SESSION_DRAFT

    if current < start_at:
        return SESSION_DRAFT

    if start_at <= current <= end_at:
        return SESSION_ONGOING

    return SESSION_COMPLETED


def is_exam_group_ongoing(group, now=None):
    return get_exam_group_computed_status(group, now=now) == SESSION_ONGOING


def is_exam_group_completed(group, now=None):
    return get_exam_group_computed_status(group, now=now) == SESSION_COMPLETED
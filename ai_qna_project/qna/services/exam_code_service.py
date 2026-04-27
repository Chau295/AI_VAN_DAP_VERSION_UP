from .exam_session_service import is_exam_group_ongoing


def is_exam_code_locked_by_ongoing_session(exam_code, now=None):
    """
    Mã đề bị khóa nếu đang được dùng trong phòng/ca thi đang diễn ra.
    """
    if not exam_code:
        return False

    rooms = getattr(exam_code, "session_rooms", None)
    if rooms is None:
        return False

    for room in rooms.select_related("exam_session_group_id").all():
        group = getattr(room, "exam_session_group_id", None)
        if is_exam_group_ongoing(group, now=now):
            return True

    return False


def get_exam_code_display_status(exam_code, now=None):
    if is_exam_code_locked_by_ongoing_session(exam_code, now=now):
        return "Đang dùng"

    if getattr(exam_code, "is_approved", False):
        return "Đã duyệt"

    return "Chưa duyệt"
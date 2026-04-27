def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def compute_session_final_score(results):
    """
    results: queryset/list ExamResult.
    Trả về tổng điểm đã làm tròn.
    """
    total = 0.0
    for result in results:
        total += safe_float(getattr(result, "score", 0.0))
    return round(total, 2)
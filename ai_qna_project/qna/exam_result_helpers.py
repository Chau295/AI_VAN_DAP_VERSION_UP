# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from django.db.models import Count, Max, Q
from django.utils import timezone
from base64 import b64encode
from .exam_runtime import LECTURER_HIDDEN_VIOLATION_TYPES
from .models import (
    DifficultyLevel,
    ExamCode,
    ExamCodeQuestion,
    ExamResult,
    ExamSession,
    ExamSessionRoom,
    ExamSet,
    Question,
)

TEN_POINT_SCALE = 10.0

def _with_lecturer_visible_violation_count(queryset):
    return queryset.annotate(
        visible_violation_count=Count(
            "violation_images",
            filter=~Q(violation_images__violation_type__in=LECTURER_HIDDEN_VIOLATION_TYPES),
            distinct=True,
        )
    )


def _has_lecturer_visible_violation(session):
    visible_count = getattr(session, "visible_violation_count", None)
    if visible_count is not None:
        return visible_count > 0
    return session.violation_images.exclude(violation_type__in=LECTURER_HIDDEN_VIOLATION_TYPES).exists()

def _session_has_exam_results(session) -> bool:
    cached_count = getattr(session, "answered_question_count", None)
    if cached_count is not None:
        return int(cached_count) > 0

    results = getattr(session, "results", None)
    if results is not None and hasattr(results, "exists"):
        return results.exists()

    if getattr(session, "id", None):
        return _get_answered_question_count(session) > 0

    return False


def _student_has_started_exam(session):
    if not session:
        return False

    session_has_started_attempt = getattr(session, "has_started_attempt", None)
    if callable(session_has_started_attempt):
        return bool(session_has_started_attempt())

    if _is_session_finalized(session):
        return True

    return bool(
        getattr(session, "started_at", None)
        or getattr(session, "session_status", "") == "STARTED"
        or _session_has_exam_results(session)
    )


def _is_exam_group_closed_for_status(exam_group) -> bool:
    return bool(exam_group and getattr(exam_group, "computed_status", None) in {"COMPLETED", "CANCELLED"})


def _resolve_exam_session_status(session, *, allow_in_progress=True) -> str | None:
    if not session:
        return None

    resolve_display_status = getattr(session, "resolve_display_status", None)
    if callable(resolve_display_status):
        return resolve_display_status(allow_in_progress=allow_in_progress)

    if _is_session_finalized(session):
        return "COMPLETED"

    exam_group = getattr(session, "exam_session_group_id", None) or getattr(session, "exam_group", None)
    has_started = _student_has_started_exam(session)

    if has_started:
        if _is_exam_group_closed_for_status(exam_group) or not allow_in_progress:
            return "COMPLETED"
        return "IN_PROGRESS"

    if _is_exam_group_closed_for_status(exam_group):
        return "ABSENT"

    return "IN_PROGRESS"

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_score_scale_value(value: float | None, *, min_decimals: int = 0) -> str:
    if value is None:
        return ""

    normalized = round(_safe_float(value), 2)
    whole, fraction = f"{normalized:.2f}".split(".", 1)
    while len(fraction) > min_decimals and fraction.endswith("0"):
        fraction = fraction[:-1]
    return f"{whole}.{fraction}" if fraction else whole


def _format_question_score_suffix(value: float | None) -> str:
    if value is None:
        return ""
    return f"/ {_format_score_scale_value(value, min_decimals=1)}"


def _format_total_score_suffix(value: float | None, *, compact: bool = False) -> str:
    if value is None:
        return ""
    prefix = "/" if compact else "/ "
    return f"{prefix}{_format_score_scale_value(value, min_decimals=0)}"

def _get_room_assignment_for_user(exam_group, user):
    if not exam_group or not user:
        return None, None

    config = exam_group.configuration_data or {}
    room_configs = config.get("rooms") or []
    if not isinstance(room_configs, list) or not room_configs:
        from .lecturer_session_management import _legacy_room_configuration

        room_configs = _legacy_room_configuration(exam_group)

    for room_cfg in room_configs:
        for assignment in room_cfg.get("assignments") or []:
            if str(assignment.get("linked_user_id")) == str(user.id):
                return room_cfg, assignment
    return None, None

def _get_session_assignment_context(session: ExamSession):
    cached = getattr(session, "_assignment_context_cache", None)
    if cached is not None:
        return cached

    exam_group = getattr(session, "exam_group", None)
    user = getattr(session, "user", None)
    context = _get_room_assignment_for_user(exam_group, user) if exam_group and user else (None, None)
    setattr(session, "_assignment_context_cache", context)
    return context


def _get_session_assigned_exam_code_id(session: ExamSession) -> int | None:
    cached = getattr(session, "_assigned_exam_code_id_cache", None)
    if cached is not None:
        return cached

    _, assignment = _get_session_assignment_context(session)
    exam_code_id = assignment.get("exam_code_id") if assignment else None
    try:
        normalized = int(exam_code_id) if exam_code_id is not None else None
    except (TypeError, ValueError):
        normalized = None

    setattr(session, "_assigned_exam_code_id_cache", normalized)
    return normalized


def _get_session_exam_blueprint(session: ExamSession) -> list[ExamCodeQuestion]:
    cached = getattr(session, "_exam_blueprint_cache", None)
    if cached is not None:
        return cached

    exam_code_id = _get_session_assigned_exam_code_id(session)
    if exam_code_id is None:
        blueprint: list[ExamCodeQuestion] = []
    else:
        blueprint = list(
            ExamCodeQuestion.objects.filter(exam_code_id=exam_code_id)
            .select_related("question_id", "exam_code_id__exam_set_id")
            .order_by("display_order", "exam_code_question_id")
        )

    setattr(session, "_exam_blueprint_cache", blueprint)
    return blueprint


def _get_session_total_question_count(session: ExamSession) -> int:
    blueprint = _get_session_exam_blueprint(session)
    if blueprint:
        return len(blueprint)

    question_count = session.questions.count()
    return question_count or 3


def _get_session_raw_total_score(session: ExamSession) -> float | None:
    if hasattr(session, "_raw_total_score_cache"):
        return getattr(session, "_raw_total_score_cache")

    exam_code_id = _get_session_assigned_exam_code_id(session)
    if exam_code_id is None:
        raw_total_score = None
    else:
        raw_total_score = round(sum(float(item.score or 0) for item in _get_session_exam_blueprint(session)), 2)

    setattr(session, "_raw_total_score_cache", raw_total_score)
    return raw_total_score


def _get_session_assigned_exam_set(session: ExamSession) -> ExamSet | None:
    if hasattr(session, "_assigned_exam_set_cache"):
        return getattr(session, "_assigned_exam_set_cache")

    exam_set = None
    blueprint = _get_session_exam_blueprint(session)
    if blueprint:
        exam_set = getattr(getattr(blueprint[0], "exam_code_id", None), "exam_set_id", None)

    if exam_set is None:
        room_cfg, _ = _get_session_assignment_context(session)
        exam_set_id = _normalize_optional_int((room_cfg or {}).get("exam_set_id"))
        if exam_set_id is not None:
            exam_set = ExamSet.objects.filter(pk=exam_set_id).first()

    if exam_set is None:
        exam_group = getattr(session, "exam_group", None)
        user = getattr(session, "user", None)
        if exam_group and user:
            session_room = (
                ExamSessionRoom.objects.filter(exam_session_group_id=exam_group, students=user)
                .select_related("exam_set_id")
                .order_by("display_order", "exam_session_room_id")
                .first()
            )
            exam_set = getattr(session_room, "exam_set_id", None)

    setattr(session, "_assigned_exam_set_cache", exam_set)
    return exam_set


def _get_session_total_score_scale(session: ExamSession) -> tuple[float, bool]:
    raw_total_score = _get_session_raw_total_score(session)
    if raw_total_score is not None:
        return round(_safe_float(raw_total_score), 2), True

    exam_set = _get_session_assigned_exam_set(session)
    if exam_set is not None:
        return round(_safe_float(exam_set.total_score), 2), True

    return TEN_POINT_SCALE, False


def _get_exam_set_question_max_score(exam_set: ExamSet | None, question: Question | None) -> float | None:
    if not exam_set or not question:
        return None

    score_map = {
        DifficultyLevel.EASY: exam_set.easy_score,
        DifficultyLevel.MEDIUM: exam_set.medium_score,
        DifficultyLevel.HARD: exam_set.hard_score,
    }
    score_value = score_map.get(getattr(question, "difficulty", None))
    if score_value is None:
        return None
    return round(_safe_float(score_value), 2)


def _resolve_session_question_max_score(
        session: ExamSession,
        question: Question | None,
        raw_question_score: float | None,
) -> tuple[float, bool]:
    if raw_question_score is not None:
        return round(_safe_float(raw_question_score), 2), True

    derived_score = _get_exam_set_question_max_score(_get_session_assigned_exam_set(session), question)
    if derived_score is not None:
        return derived_score, True

    return TEN_POINT_SCALE, False


def _scale_session_question_score(
        raw_score: float | None,
        question_max_score: float | None,
        *,
        has_actual_question_max: bool,
) -> float:
    normalized_score = min(max(_safe_float(raw_score), 0.0), TEN_POINT_SCALE)
    if not has_actual_question_max or question_max_score is None:
        return round(normalized_score, 2)

    # Worker hiện lưu điểm từng câu theo thang 10, nên khi có max điểm thật của câu
    # thì phải scale về đúng thang điểm của câu trước khi render/tính tổng.
    question_cap = max(_safe_float(question_max_score), 0.0)
    scaled_score = (normalized_score / TEN_POINT_SCALE) * question_cap
    return round(min(scaled_score, question_cap), 2)


def _attach_session_score_meta(session: ExamSession):
    total_scale_value, has_actual_total_scale = _get_session_total_score_scale(session)
    score_value = round(_safe_float(session.final_score), 2)
    session.raw_total_score = total_scale_value if has_actual_total_scale else None
    session.total_score_scale = total_scale_value
    session.score_scale_suffix = _format_total_score_suffix(total_scale_value, compact=True)
    session.final_display_score = score_value
    session.final_display_score_suffix = _format_total_score_suffix(total_scale_value)
    session.show_non_ten_scale_warning = False
    session.score_scale_warning = ""
    session.rendered_final_score = score_value
    return session

def _get_answered_question_count(row) -> int:
    cached = getattr(row, "answered_question_count", None)
    if cached is not None:
        return int(cached)

    if not getattr(row, "id", None):
        return 0

    answered_count = ExamResult.objects.filter(exam_session_id=row).count()
    row.answered_question_count = answered_count
    return answered_count


def _build_session_question_details(session: ExamSession) -> list[SimpleNamespace]:
    if hasattr(session, "_question_details_cache"):
        return getattr(session, "_question_details_cache")

    results = list(
        ExamResult.objects.filter(exam_session_id=session)
        .select_related("question_id")
        .order_by("answered_at", "exam_result_id")
    )
    result_map = {result.question_id_id: result for result in results}

    question_details: list[SimpleNamespace] = []
    seen_question_ids = set()

    def build_question_detail(order: int, question: Question, result: ExamResult | None, raw_question_score: float | None):
        question_max_score, has_actual_question_max = _resolve_session_question_max_score(
            session,
            question,
            raw_question_score,
        )
        raw_result_score = _safe_float(getattr(result, "score", 0.0) if result else 0.0)
        display_score_value = _scale_session_question_score(
            raw_result_score,
            question_max_score,
            has_actual_question_max=has_actual_question_max,
        )
        return SimpleNamespace(
            order=order,
            question=question,
            result=result,
            is_answered=bool(result),
            status_label="Đã trả lời" if result else "Chưa trả lời",
            raw_question_score=raw_question_score,
            raw_result_score=raw_result_score,
            question_max_score=question_max_score,
            has_actual_question_max=has_actual_question_max,
            display_score_value=display_score_value,
            display_score_suffix=_format_question_score_suffix(question_max_score),
        )

    blueprint = _get_session_exam_blueprint(session)
    if blueprint:
        for index, item in enumerate(blueprint, start=1):
            question = item.question_id
            result = result_map.get(question.pk)
            question_details.append(build_question_detail(index, question, result, _safe_float(item.score)))
            seen_question_ids.add(question.pk)
    else:
        for index, question in enumerate(session.questions.all().order_by("question_id"), start=1):
            result = result_map.get(question.pk)
            question_details.append(build_question_detail(index, question, result, None))
            seen_question_ids.add(question.pk)

    for result in results:
        if result.question_id_id in seen_question_ids:
            continue
        question_details.append(build_question_detail(len(question_details) + 1, result.question_id, result, None))

    setattr(session, "_question_details_cache", question_details)
    return question_details

def _compute_scores(session: ExamSession) -> tuple[float, float]:
    """
    Tính điểm dựa trên cùng dữ liệu question_details dùng để render cho sinh viên và giảng viên.
    Câu chưa trả lời được tính là 0 điểm.
    Trả về: (main_avg, final_total)
    """
    question_details = _build_session_question_details(session)
    total_main_questions = len(question_details)

    if total_main_questions <= 0:
        return 0.0, 0.0

    _, has_actual_total_scale = _get_session_total_score_scale(session)
    if has_actual_total_scale:
        final_total = round(sum(_safe_float(item.display_score_value) for item in question_details), 2)
    else:
        total_answer_score = sum(_safe_float(getattr(item.result, "score", 0.0)) for item in question_details)
        final_total = round(total_answer_score / float(total_main_questions), 2)

    main_avg = round(final_total / float(total_main_questions), 4)
    return main_avg, final_total

def _describe_violation_type(violation_type: str | None) -> str:
    return {
        "TWO_FACES": "Phát hiện nhiều khuôn mặt trong khung hình",
        "CAMERA_OFF": "Camera bị tắt hoặc gián đoạn quá lâu",
        "NO_FACE": "Không phát hiện khuôn mặt trong khung hình",
    }.get((violation_type or "").strip().upper(), "Cảnh báo giám sát")


def _format_duration_display(total_seconds: int | None) -> str:
    if total_seconds is None:
        return "-"

    total_seconds = max(0, int(total_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if hours:
        parts.append(f"{hours} giờ")
    if minutes:
        parts.append(f"{minutes} phút")
    if seconds or not parts:
        parts.append(f"{seconds} giây")
    return " ".join(parts)


def _get_session_duration_seconds(session: ExamSession) -> int | None:
    started_at = getattr(session, "started_at", None) or getattr(session, "created_at", None)
    if not started_at:
        return None

    latest_answered_at = getattr(session, "latest_answered_at", None)
    if latest_answered_at is None:
        latest_answered_at = ExamResult.objects.filter(exam_session_id=session).aggregate(latest=Max("answered_at"))["latest"]

    end_at = getattr(session, "completed_at", None) or latest_answered_at

    if end_at is None:
        return None

    return max(0, int((end_at - started_at).total_seconds()))


def _get_session_latest_answered_at(session: ExamSession):
    latest_answered_at = getattr(session, "latest_answered_at", None)
    if latest_answered_at is not None:
        return latest_answered_at
    return ExamResult.objects.filter(exam_session_id=session).aggregate(latest=Max("answered_at"))["latest"]


def _derive_session_completion_time(session: ExamSession):
    started_at = getattr(session, "started_at", None) or getattr(session, "created_at", None)
    latest_answered_at = _get_session_latest_answered_at(session)
    exam_group = getattr(session, "exam_group", None)
    now = timezone.now()

    if latest_answered_at is not None:
        completed_at = latest_answered_at
    elif exam_group and getattr(exam_group, "end_at", None) and now >= exam_group.end_at:
        completed_at = exam_group.end_at
    else:
        completed_at = now

    if started_at and completed_at < started_at:
        completed_at = started_at
    return completed_at


def _attach_session_duration_meta(session: ExamSession):
    session.actual_duration_seconds = _get_session_duration_seconds(session)
    session.actual_duration_display = _format_duration_display(session.actual_duration_seconds)
    return session


def _attach_session_assignment_meta(session: ExamSession):
    room_cfg, _ = _get_session_assignment_context(session)
    session.room_display = (room_cfg or {}).get("room_name") or "-"

    exam_code_id = _get_session_assigned_exam_code_id(session)
    exam_code = ExamCode.objects.filter(pk=exam_code_id).only("code_name",
                                                              "code_number").first() if exam_code_id else None
    if exam_code:
        session.exam_code_display = exam_code.code_name or str(exam_code.code_number or exam_code.pk)
    else:
        session.exam_code_display = "-"

    return session


def _is_session_finalized(session: ExamSession) -> bool:
    return bool(
        session.session_status == "COMPLETED"
        or session.completed_at is not None
    )

def _build_image_data_url(blob, mime):
    if not blob or not mime:
        return None
    try:
        return f"data:{mime};base64,{b64encode(blob).decode('ascii')}"
    except Exception:
        return None
# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
from base64 import b64encode
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import (
    ExamCodeQuestion,
    ExamResult,
    ExamSession,
    Subject,
    UserProfile,
)
from .permissions import student_required
from .exam_result_helpers import (
    _safe_float,
    _compute_scores,
    _get_room_assignment_for_user,
    _get_session_exam_blueprint,
    _attach_session_score_meta,
    _attach_session_duration_meta,
    _build_session_question_details,
    _resolve_exam_session_status,
)
from .student_exam_helpers import (
    _get_student_current_room_for_subject,
    _get_student_display_room_for_subject,
    _get_student_exam_access_context,
    _deny_if_exam_closed,
    _student_exam_password_session_key,
    _store_student_room_password_access,
    _student_has_room_password_access,
    _get_student_exam_entry_payload,
    _clear_student_exam_entry_payload,
    _get_student_exam_session,
    _student_exam_reentry_state,
    _redirect_locked_student_exam,
    _sync_student_subjects_from_rosters,
    _ensure_student_absent_sessions_for_history,
)

LIST_PAGE_SIZE = 8
PASSWORD_ATTEMPT_LIMIT = 5
PASSWORD_ATTEMPT_WINDOW = 300


def _room_password_attempts_key(user_id: int, subject_code: str) -> str:
    return f"room_pwd_attempts:{user_id}:{subject_code}"


def _get_avatar_data_url(profile: UserProfile) -> str:
    if profile and profile.profile_image_blob:
        try:
            return (
                f"data:{profile.profile_image_mime or 'image/jpeg'};base64,"
                f"{b64encode(profile.profile_image_blob).decode('ascii')}"
            )
        except Exception:
            pass
    return static("images/logo.jpg")


def _attach_review_status_meta(session: ExamSession) -> None:
    resolved_status = _resolve_exam_session_status(session)
    session.review_status = resolved_status

    if resolved_status == "COMPLETED":
        session.review_status_label = "Đã hoàn thành"
        session.review_status_class = "status-completed"
        session.can_view_detail = True
        session.can_display_score = True
    elif resolved_status == "ABSENT":
        session.review_status_label = "Vắng thi"
        session.review_status_class = "status-absent"
        session.can_view_detail = bool(getattr(session, "id", None))

        # Vắng thi vẫn hiển thị điểm 0/10
        session.can_display_score = True
        if getattr(session, "final_score", None) is None:
            session.final_score = 0
    else:
        session.review_status_label = "Đang thực hiện"
        session.review_status_class = "status-in-progress"
        session.can_view_detail = False
        session.can_display_score = False

    session.status_label = session.review_status_label
    session.status_class = session.review_status_class


def _attach_history_status_meta(session: ExamSession) -> None:
    resolved_status = _resolve_exam_session_status(session, allow_in_progress=True)
    session.history_status = resolved_status or "IN_PROGRESS"

    if session.history_status == "ABSENT":
        session.history_status_label = "Vắng thi"
        session.history_status_class = "status-absent"
    elif session.history_status == "IN_PROGRESS":
        session.history_status_label = "Đang thực hiện"
        session.history_status_class = "status-in-progress"
    else:
        session.history_status_label = "Đã hoàn thành"
        session.history_status_class = "status-completed"


def _get_session_appeal_deadline(session: ExamSession):
    completed_at = getattr(session, "completed_at", None)
    if not completed_at:
        return None
    return completed_at + timedelta(days=7)


def _is_session_appeal_open(session: ExamSession) -> bool:
    deadline = _get_session_appeal_deadline(session)
    if not deadline:
        return False
    return timezone.now() <= deadline


def _build_exam_page_existing_results(session: ExamSession) -> list[dict]:
    existing_results = []
    for item in _build_session_question_details(session):
        if not item.result:
            continue
        existing_results.append(
            {
                "question_id": item.question.id,
                "result_id": item.result.id,
                "score": round(_safe_float(item.result.score), 2),
                "display_score": round(_safe_float(item.display_score_value), 2),
                "question_max_score": round(_safe_float(item.question_max_score), 2),
                "feedback": item.result.feedback or "",
                "transcript": item.result.transcript or "",
                "audio_url": item.result.audio_file.url if item.result.audio_file else "",
            }
        )
    return existing_results

@login_required
def dashboard_view(request: HttpRequest) -> HttpResponse:
    profile, _ = UserProfile.objects.get_or_create(user_id=request.user)
    if profile.is_lecturer:
        return redirect("qna:lecturer_dashboard")

    # Tự đồng bộ lại dữ liệu quyền môn theo roster:
    # - thêm môn còn thiếu
    # - gỡ môn đã stale sau khi roster bị xóa
    _sync_student_subjects_from_rosters(request.user)
    profile.refresh_from_db()

    subjects = profile.subjects_enrolled.all().order_by("name")
    subject_cards = []

    for subject in subjects:
        current_room = _get_student_current_room_for_subject(request.user, subject)
        display_room = current_room or _get_student_display_room_for_subject(request.user, subject)
        active_group = display_room.exam_session_group_id if display_room else None
        group_status = active_group.computed_status if active_group else None
        existing_session = _get_student_exam_session(request.user, subject, active_group) if active_group else None
        reentry_state = _student_exam_reentry_state(existing_session)
        can_enter_exam = bool(current_room) and reentry_state in {None, "STARTED"}

        if reentry_state == "COMPLETED":
            status_label = "Đã hoàn thành ca thi"
            entry_button_label = "Không thể vào lại"
        elif reentry_state == "STARTED":
            status_label = "Đang thực hiện ca thi"
            entry_button_label = "Tiếp tục thi"
        else:
            status_label = (
                "Đang trong ca thi"
                if group_status == "ONGOING"
                else "Chưa đến giờ thi"
                if group_status == "SCHEDULED"
                else "Chưa mở ca thi"
                if group_status == "DRAFT"
                else "Ca thi đã hủy"
                if group_status == "CANCELLED"
                else "Ngoài thời gian thi"
                if active_group
                else "Chưa có ca thi"
            )
            entry_button_label = "Vào thi" if can_enter_exam else "Chưa thể vào thi"

        subject_cards.append(
            {
                "subject": subject,
                "active_room": display_room,
                "current_room": current_room,
                "active_group": active_group,
                "can_enter_exam": can_enter_exam,
                "status_label": status_label,
                "entry_lock_state": reentry_state or "",
                "entry_button_label": entry_button_label,
            }
        )

    paginator = Paginator(subject_cards, LIST_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    recent_sessions = (
        ExamSession.objects.filter(user_id=request.user)
        .select_related("subject_id")
        .order_by("-created_at")[:8]
    )

    return render(
        request,
        "qna/student/dashboard.html",
        {
            "subjects": subjects,
            "subject_cards": list(page_obj.object_list),
            "page_obj": page_obj,
            "recent_sessions": recent_sessions,
            "full_name": profile.full_name or request.user.first_name or request.user.username,
            "server_now_ts": int(timezone.now().timestamp()),
        },
    )


@login_required
@student_required
def history_view(request: HttpRequest) -> HttpResponse:
    _ensure_student_absent_sessions_for_history(request.user)

    sessions_qs = (
        ExamSession.objects.filter(user_id=request.user)
        .select_related("subject_id", "exam_session_group_id")
        .order_by("-created_at")
    )

    paginator = Paginator(sessions_qs, LIST_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    sessions = list(page_obj.object_list)
    for session in sessions:
        main_avg, final_total = _compute_scores(session)
        session.main_avg = main_avg
        if session.final_score != final_total:
            session.final_score = final_total
        _attach_session_score_meta(session)
        _attach_history_status_meta(session)

    return render(
        request,
        "qna/student/history.html",
        {
            "sessions": sessions,
            "page_obj": page_obj,
        },
    )


@login_required
@student_required
def history_detail_view(request: HttpRequest, session_id: int) -> HttpResponse:
    session = get_object_or_404(
        ExamSession.objects.select_related("subject_id", "user_id", "exam_session_group_id"),
        pk=session_id,
        user_id=request.user,
    )

    main_avg, final_total = _compute_scores(session)
    if session.final_score != final_total:
        session.final_score = final_total

    _attach_session_score_meta(session)
    _attach_session_duration_meta(session)
    _attach_review_status_meta(session)

    if getattr(session, "review_status", None) == "IN_PROGRESS":
        messages.info(request, "Phiên thi đang diễn ra. Hệ thống chuyển bạn trở lại màn hình làm bài.")
        return redirect("qna:exam_page", subject_code=session.subject.subject_code)

    question_details = _build_session_question_details(session)

    return render(
        request,
        "qna/student/history_detail.html",
        {
            "session": session,
            "question_details": question_details,
            "appeal_deadline": _get_session_appeal_deadline(session),
            "appeal_open": _is_session_appeal_open(session),
        },
    )


@login_required
@student_required
def pre_exam_verification_view(request: HttpRequest, subject_code: str) -> HttpResponse:
    subject = get_object_or_404(Subject, subject_code=subject_code)
    active_room, display_room, exam_group = _get_student_exam_access_context(request.user, subject)
    blocked = _deny_if_exam_closed(request, exam_group, subject_code)
    if blocked:
        return blocked
    if not active_room:
        messages.error(request, "Bạn chưa được phân vào phòng thi của môn này.")
        return redirect("qna:dashboard")

    existing_session = _get_student_exam_session(request.user, subject, exam_group)
    locked_redirect = _redirect_locked_student_exam(request, existing_session)
    if locked_redirect:
        return locked_redirect
    if not _student_has_room_password_access(request, exam_group, active_room):
        messages.error(request, "Bạn cần xác thực mật khẩu phòng thi trước.")
        return redirect("qna:exam_password", subject_code=subject_code)

    return render(
        request,
        "qna/student/pre_exam_verification.html",
        {
            "subject": subject,
            "subject_code": subject_code,
        },
    )


@login_required
@student_required
def exam_password_view(request, subject_code):
    subject = get_object_or_404(Subject, subject_code=subject_code)
    active_room, display_room, exam_group = _get_student_exam_access_context(request.user, subject)
    blocked = _deny_if_exam_closed(request, exam_group, subject_code)
    if blocked:
        return blocked
    if not active_room:
        messages.error(request, "Bạn chưa được phân vào phòng thi của môn này.")
        return redirect("qna:dashboard")

    existing_session = _get_student_exam_session(request.user, subject, exam_group)
    locked_redirect = _redirect_locked_student_exam(request, existing_session)
    if locked_redirect:
        return locked_redirect

    if not (active_room.room_password or "").strip():
        _store_student_room_password_access(request, exam_group, active_room)
        return redirect("qna:pre_exam_verification", subject_code=subject_code)
    return render(
        request,
        "qna/student/exam_password.html",
        {"subject": subject, "subject_code": subject_code, "active_room": active_room, "exam_group": exam_group},
    )


@login_required
@student_required
def verify_exam_password(request, subject_code):
    if request.method != "POST":
        return redirect("qna:exam_password", subject_code=subject_code)

    def _safe_cache_get(key, default=0):
        try:
            return cache.get(key, default)
        except Exception:
            return default

    def _safe_cache_set(key, value, timeout):
        try:
            cache.set(key, value, timeout)
        except Exception:
            pass

    def _safe_cache_delete(key):
        try:
            cache.delete(key)
        except Exception:
            pass

    subject = get_object_or_404(Subject, subject_code=subject_code)
    active_room, display_room, exam_group = _get_student_exam_access_context(request.user, subject)

    blocked = _deny_if_exam_closed(request, exam_group, subject_code)
    if blocked:
        return blocked

    if not active_room:
        messages.error(request, "Bạn chưa được phân vào phòng thi của môn này.")
        return redirect("qna:dashboard")

    existing_session = _get_student_exam_session(request.user, subject, exam_group)
    locked_redirect = _redirect_locked_student_exam(request, existing_session)
    if locked_redirect:
        return locked_redirect

    attempts_key = _room_password_attempts_key(request.user.pk, subject_code)
    attempts = _safe_cache_get(attempts_key, 0)

    if attempts >= PASSWORD_ATTEMPT_LIMIT:
        messages.error(request, "Bạn đã nhập sai quá nhiều lần. Vui lòng thử lại sau 5 phút.")
        return redirect("qna:exam_password", subject_code=subject_code)

    password = (request.POST.get("password") or "").strip()
    room_password = (active_room.room_password or "").strip()

    if not room_password:
        _store_student_room_password_access(request, exam_group, active_room)
        return redirect("qna:pre_exam_verification", subject_code=subject_code)

    if password == room_password:
        _safe_cache_delete(attempts_key)
        _store_student_room_password_access(request, exam_group, active_room)
        return redirect("qna:pre_exam_verification", subject_code=subject_code)

    _safe_cache_set(attempts_key, attempts + 1, PASSWORD_ATTEMPT_WINDOW)
    messages.error(request, "Mật khẩu không đúng. Vui lòng thử lại.")
    return redirect("qna:exam_password", subject_code=subject_code)


@login_required
@student_required
def exam_page_view(request: HttpRequest, subject_code: str) -> HttpResponse:
    subject = get_object_or_404(Subject, subject_code=subject_code)

    active_room, display_room, exam_group = _get_student_exam_access_context(request.user, subject)

    blocked = _deny_if_exam_closed(request, exam_group, subject_code)
    if blocked:
        return blocked
    if not active_room:
        messages.error(request, f"Bạn chưa được xếp vào phòng thi nào của môn {subject.name}.")
        return redirect("qna:dashboard")

    exam_code_id = None
    room_cfg, assignment = _get_room_assignment_for_user(exam_group, request.user)
    if assignment:
        exam_code_id = assignment.get("exam_code_id")

    main_questions = []
    if exam_code_id:
        from .models import ExamCodeQuestion
        ecqs = ExamCodeQuestion.objects.filter(exam_code_id=exam_code_id).select_related('question_id').order_by(
            'display_order')
        main_questions = [ecq.question_id for ecq in ecqs]

    existing_session = _get_student_exam_session(request.user, subject, exam_group)
    reentry_state = _student_exam_reentry_state(existing_session)
    locked_redirect = _redirect_locked_student_exam(request, existing_session)
    if locked_redirect:
        return locked_redirect
    resume_started_session = reentry_state == "STARTED"

    if resume_started_session:
        session = existing_session
    else:
        if not _student_has_room_password_access(request, exam_group, active_room):
            messages.error(request, "Bạn cần xác thực mật khẩu phòng thi trước.")
            return redirect("qna:exam_password", subject_code=subject_code)

        entry_payload = _get_student_exam_entry_payload(request, exam_group)
        if not entry_payload:
            messages.error(request, "Bạn cần hoàn thành xác thực trước khi vào thi.")
            return redirect("qna:pre_exam_verification", subject_code=subject_code)
        if str(entry_payload.get("room_id")) != str(active_room.pk):
            _clear_student_exam_entry_payload(request, exam_group)
            messages.error(request, "Thông tin xác thực không còn hợp lệ. Vui lòng xác thực lại.")
            return redirect("qna:pre_exam_verification", subject_code=subject_code)

        try:
            face_image_blob = base64.b64decode(entry_payload["face_image_b64"])
        except (KeyError, TypeError, ValueError, base64.binascii.Error):
            _clear_student_exam_entry_payload(request, exam_group)
            messages.error(request, "Ảnh xác thực không hợp lệ. Vui lòng xác thực lại.")
            return redirect("qna:pre_exam_verification", subject_code=subject_code)

        face_image_mime = entry_payload.get("face_image_mime") or "image/jpeg"
        if existing_session:
            session = existing_session
            session.face_image_blob = face_image_blob
            session.face_image_mime = face_image_mime
            session.verification_status = "ALLOW"
            update_fields = ["face_image_blob", "face_image_mime", "verification_status"]
            if session.started_at is None:
                session.started_at = timezone.now()
                update_fields.append("started_at")
            if session.session_status != "STARTED":
                session.session_status = "STARTED"
                update_fields.append("session_status")
            session.save(update_fields=update_fields)
        else:
            session = ExamSession.objects.create(
                user_id=request.user,
                subject_id=subject,
                exam_session_group_id=exam_group,
                started_at=timezone.now(),
                session_status="STARTED",
                face_image_blob=face_image_blob,
                face_image_mime=face_image_mime,
                verification_status="ALLOW",
            )

    if not session.questions.exists():
        session.questions.set(main_questions)
    blueprint = _get_session_exam_blueprint(session)
    if blueprint:
        main_questions = [item.question_id for item in blueprint]
    else:
        main_questions = list(session.questions.all().order_by("question_id"))

    question_details = _build_session_question_details(session)
    existing_results = _build_exam_page_existing_results(session)
    question_score_map = {
        item.question.id: round(_safe_float(item.question_max_score), 2)
        for item in question_details
    }

    if not resume_started_session:
        _clear_student_exam_entry_payload(request, exam_group)
        request.session.pop(_student_exam_password_session_key(exam_group.pk), None)
    _attach_session_score_meta(session)

    return render(
        request,
        "qna/student/exam.html",
        {
            "subject": subject,
            "selected_questions": main_questions,
            "session": session,
            "duration_minutes": exam_group.duration_minutes if exam_group else 60,
            "server_now_ms": int(timezone.now().timestamp() * 1000),
            "exam_end_ms": int(exam_group.end_at.timestamp() * 1000) if exam_group else 0,
            "raw_total_score": session.raw_total_score or 0,
            "score_scale_suffix": session.score_scale_suffix,
            "show_non_ten_scale_warning": session.show_non_ten_scale_warning,
            "score_scale_warning": session.score_scale_warning,
            "appeal_deadline_ms": 0,
            "existing_results": existing_results,
            "question_score_map": question_score_map,
        },
    )


@login_required
def profile_view(request: HttpRequest) -> HttpResponse:
    try:
        profile = request.user.userprofile
    except UserProfile.DoesNotExist:
        profile = None
    avatar_url = _get_avatar_data_url(profile) if profile else None
    return render(request, "qna/student/profile.html",
                  {"user": request.user, "profile": profile, "avatar_url": avatar_url})


@login_required
@require_POST
def update_profile_image(request: HttpRequest) -> JsonResponse:
    return JsonResponse(
        {"success": False, "error": "Người dùng không có quyền thay đổi hình ảnh."},
        status=403,
    )
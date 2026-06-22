# -*- coding: utf-8 -*-
from __future__ import annotations

from base64 import b64encode

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.utils import timezone

from .exam_runtime import LECTURER_HIDDEN_VIOLATION_TYPES
from .models import (
    ExamCode,
    ExamSession,
    ExamSessionGroup,
    Subject,
    UserProfile,
)
from .permissions import lecturer_required, get_lecturer_subject_or_404


LIST_PAGE_SIZE = 8


def _get_avatar_data_url(profile: UserProfile) -> str:
    if profile.profile_image_blob:
        try:
            return (
                f"data:{profile.profile_image_mime or 'image/jpeg'};base64,"
                f"{b64encode(profile.profile_image_blob).decode('ascii')}"
            )
        except Exception:
            pass
    return static("images/logo.jpg")


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
    return session.violation_images.exclude(
        violation_type__in=LECTURER_HIDDEN_VIOLATION_TYPES
    ).exists()


def _session_has_started_exam(session: ExamSession) -> bool:
    if not session:
        return False

    if getattr(session, "completed_at", None) is not None:
        return True

    if getattr(session, "started_at", None):
        return True

    if getattr(session, "session_status", "") == "STARTED":
        return True

    results = getattr(session, "results", None)
    if results is not None and hasattr(results, "exists"):
        return results.exists()

    return False


def _resolve_dashboard_session_status(session: ExamSession) -> str:
    if not session:
        return "IN_PROGRESS"

    if getattr(session, "session_status", None) == "COMPLETED" or getattr(session, "completed_at", None):
        return "COMPLETED"

    exam_group = getattr(session, "exam_session_group_id", None) or getattr(session, "exam_group", None)
    group_status = getattr(exam_group, "computed_status", None)
    has_started = _session_has_started_exam(session)

    if has_started:
        if group_status in {"COMPLETED", "CANCELLED"}:
            return "COMPLETED"
        return "IN_PROGRESS"

    if group_status in {"COMPLETED", "CANCELLED"}:
        return "ABSENT"

    return "IN_PROGRESS"


def _attach_review_status_meta(session: ExamSession) -> None:
    resolved_status = _resolve_dashboard_session_status(session)
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
        session.can_display_score = False
    else:
        session.review_status_label = "Đang thực hiện"
        session.review_status_class = "status-in-progress"
        session.can_view_detail = False
        session.can_display_score = False

    session.status_label = session.review_status_label
    session.status_class = session.review_status_class


# =========================================================
# Dashboard giảng viên
# =========================================================
@login_required
@lecturer_required
def lecturer_dashboard(request):
    '''Lấy danh sách môn giảng viên dạy'''
    subjects = request.user.userprofile.subjects_taught.annotate(
        #đếm số ca thi đã tạo theo từng môn
        created_exam_count=Count("exam_session_groups", distinct=True),
        #Đếm số sinh viên tham gia theo từng môn
        participating_student_count=Count(
            "student_roster_uploads__students__student_code",
            distinct=True,
        ),
    ).order_by("name")#sắp xếp theo tên
    #Lấy subject_id đang chọn nếu có
    selected_subject_id = (request.GET.get("subject_id") or "").strip()
    recent_page_number = request.GET.get("recent_page")
    #Đếm tổng số môn
    total_subjects = subjects.count()
    total_sessions = ExamSession.objects.filter(subject_id__in=subjects).count()

    if selected_subject_id:
        try:
            selected_subject = subjects.get(pk=selected_subject_id)
        except (Subject.DoesNotExist, ValueError):
            selected_subject = None
    else:
        selected_subject = None

    recent_sessions_qs = (
        ExamSession.objects.filter(subject_id__in=subjects)
        .select_related("subject_id", "user_id", "user_id__userprofile")
        .order_by("-created_at", "-pk")
    )

    recent_paginator = Paginator(recent_sessions_qs, LIST_PAGE_SIZE)
    recent_page_obj = recent_paginator.get_page(recent_page_number)

    context = {
        "subjects": subjects,
        "selected_subject": selected_subject,
        "total_subjects": total_subjects,
        "total_sessions": total_sessions,
        "recent_sessions": recent_page_obj.object_list,
        "recent_page_obj": recent_page_obj,
    }
    return render(request, "qna/lecturer/lecturer_dashboard.html", context)


@login_required
@lecturer_required
def lecturer_subject_list(request):
    return redirect("qna:lecturer_dashboard")


@login_required
@lecturer_required
def lecturer_subject_workspace(request, subject_code):
    '''Lấy môn học theo subject_code, nhưng chỉ nếu giảng viên có quyền dạy môn đó.'''
    subject = get_lecturer_subject_or_404(
        request.user,
        subject_code=subject_code,
    )

    upcoming_page_number = request.GET.get("upcoming_page")
    #Lấy các ExamSession thuộc môn
    sessions_in_subject = ExamSession.objects.filter(subject_id=subject)
    #Đếm tổng số bài thi/session
    total_exams = sessions_in_subject.count()
    #Đếm tổng số bài thi đã hoàn thành
    completed_exams = sessions_in_subject.filter(session_status="COMPLETED").count()
    #đếm số sinh viên đã có phiên thi trong môn
    total_students = sessions_in_subject.values("user_id").distinct().count()
    #đếm số mã đề đã duyệt
    approved_codes = ExamCode.objects.filter(subject_id=subject, is_approved=True).count()
    #Lấy các ca thi sắp tới
    upcoming_groups_qs = (
        ExamSessionGroup.objects.filter(subject_id=subject, exam_date__gte=timezone.now())
        .order_by("exam_date", "pk")
    )
    upcoming_groups_paginator = Paginator(upcoming_groups_qs, LIST_PAGE_SIZE)
    upcoming_groups_page_obj = upcoming_groups_paginator.get_page(upcoming_page_number)

    context = {
        "subject": subject,
        "total_exams": total_exams,
        "completed_exams": completed_exams,
        "total_students": total_students,
        "upcoming_groups": upcoming_groups_page_obj.object_list,
        "upcoming_groups_page_obj": upcoming_groups_page_obj,
        "approved_codes": approved_codes,
    }
    return render(request, "qna/lecturer/lecturer_subject_workspace.html", context)


@login_required
@lecturer_required
def lecturer_subject_dashboard(request, subject_code):
    subject = get_lecturer_subject_or_404(
        request.user,
        subject_code=subject_code,
    )

    recent_page_number = request.GET.get("recent_page")
    sessions_in_subject = ExamSession.objects.filter(subject_id=subject)

    recent_exams_qs = _with_lecturer_visible_violation_count(
        sessions_in_subject.select_related("user_id", "user_id__userprofile")
    ).order_by("-created_at", "-pk")

    recent_exams_paginator = Paginator(recent_exams_qs, LIST_PAGE_SIZE)
    recent_exams_page_obj = recent_exams_paginator.get_page(recent_page_number)

    recent_exams = list(recent_exams_page_obj.object_list)
    for session in recent_exams:
        session.has_violation_warning = _has_lecturer_visible_violation(session)
        _attach_review_status_meta(session)
        session.status_label = session.review_status_label
        session.status_class = session.review_status_class

    context = {
        "subject": subject,
        "total_exams": sessions_in_subject.count(),
        "total_students": sessions_in_subject.values("user_id").distinct().count(),
        "completed_exams": sessions_in_subject.filter(session_status="COMPLETED").count(),
        "recent_exams": recent_exams,
        "recent_exams_page_obj": recent_exams_page_obj,
    }
    return render(request, "qna/lecturer/lecturer_subject_dashboard.html", context)


@login_required
@lecturer_required
def lecturer_profile_view(request: HttpRequest) -> HttpResponse:
    profile = request.user.userprofile
    avatar_url = _get_avatar_data_url(profile)

    context = {
        "user": request.user,
        "profile": profile,
        "avatar_url": avatar_url,
        "subjects_taught": profile.subjects_taught.all().order_by("subject_code"),
    }
    return render(request, "qna/lecturer/lecturer_profile.html", context)
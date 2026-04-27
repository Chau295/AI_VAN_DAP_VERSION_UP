# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any
from uuid import uuid4

from django.contrib import messages
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from .exam_runtime import is_exam_group_open
from .exam_result_helpers import (
    _is_session_finalized,
    _student_has_started_exam,
)
from .models import (
    ExamSession,
    ExamSessionGroup,
    ExamSessionRoom,
    StudentRosterStudent,
    Subject,
    UserProfile,
)


def _get_student_assigned_rooms_for_subject(user, subject):
    return list(
        ExamSessionRoom.objects.filter(
            exam_session_group_id__subject_id=subject,
            students=user,
        )
        .select_related("exam_session_group_id", "exam_set_id")
        .prefetch_related("exam_codes")
        .order_by("exam_session_group_id__exam_date", "display_order", "pk")
    )


def _is_exam_group_open(exam_group):
    return is_exam_group_open(exam_group)


def _get_student_current_room_for_subject(user, subject):
    rooms = _get_student_assigned_rooms_for_subject(user, subject)
    for room in rooms:
        if _is_exam_group_open(room.exam_session_group_id):
            return room
    return None


def _get_student_display_room_for_subject(user, subject):
    rooms = _get_student_assigned_rooms_for_subject(user, subject)
    if not rooms:
        return None

    now = timezone.now()

    current_room = next(
        (room for room in rooms if _is_exam_group_open(room.exam_session_group_id)),
        None,
    )
    if current_room:
        return current_room

    upcoming_rooms = [
        room
        for room in rooms
        if room.exam_session_group_id.computed_status in {"SCHEDULED", "DRAFT"}
        and room.exam_session_group_id.start_at >= now
    ]
    if upcoming_rooms:
        return min(upcoming_rooms, key=lambda room: room.exam_session_group_id.start_at)

    completed_rooms = [
        room
        for room in rooms
        if room.exam_session_group_id.computed_status == "COMPLETED"
    ]
    if completed_rooms:
        return max(completed_rooms, key=lambda room: room.exam_session_group_id.end_at)

    cancelled_rooms = [
        room
        for room in rooms
        if room.exam_session_group_id.computed_status == "CANCELLED"
    ]
    if cancelled_rooms:
        return max(cancelled_rooms, key=lambda room: room.exam_session_group_id.start_at)

    return rooms[0]


def _get_student_active_room_for_subject(user, subject):
    return _get_student_current_room_for_subject(user, subject)


def _deny_if_exam_closed(request, exam_group, subject_code):
    if not exam_group or exam_group.status == "CANCELLED":
        messages.error(request, "Ca thi không khả dụng.")
        return redirect("qna:dashboard")

    now = timezone.now()

    if now < exam_group.start_at:
        messages.error(request, "Chưa đến giờ thi.")
        return redirect("qna:dashboard")

    if now > exam_group.end_at:
        messages.error(request, "Ca thi đã kết thúc.")
        return redirect("qna:dashboard")

    return None


def _get_student_exam_access_context(user, subject):
    current_room = _get_student_current_room_for_subject(user, subject)
    display_room = current_room or _get_student_display_room_for_subject(user, subject)
    exam_group = display_room.exam_session_group_id if display_room else None
    return current_room, display_room, exam_group


def _student_exam_access_denied_message(display_room, *, no_room_message):
    return "Chưa đến giờ hoặc đã hết giờ thi." if display_room else no_room_message


def _student_exam_entry_session_key(exam_group_id):
    return f"exam_entry_session_{exam_group_id}"


def _student_exam_password_session_key(exam_group_id):
    return f"exam_password_session_{exam_group_id}"


def _store_student_room_password_access(
    request: HttpRequest,
    exam_group: ExamSessionGroup | None,
    room: ExamSessionRoom | None,
) -> None:
    if not exam_group or not room:
        return

    request.session[_student_exam_password_session_key(exam_group.pk)] = {
        "room_id": room.pk,
        "verified_at": timezone.now().isoformat(),
    }
    request.session.modified = True


def _student_has_room_password_access(
    request: HttpRequest,
    exam_group: ExamSessionGroup | None,
    room: ExamSessionRoom | None,
) -> bool:
    if not exam_group or not room:
        return False

    if not (room.room_password or "").strip():
        _store_student_room_password_access(request, exam_group, room)
        return True

    payload = request.session.get(_student_exam_password_session_key(exam_group.pk)) or {}
    return str(payload.get("room_id")) == str(room.pk)


def _store_student_exam_entry_payload(
    request: HttpRequest,
    exam_group: ExamSessionGroup | None,
    room: ExamSessionRoom | None,
    *,
    face_image_b64: str,
    face_image_mime: str,
) -> dict[str, Any]:
    payload = {
        "entry_token": uuid4().hex,
        "room_id": getattr(room, "pk", None),
        "face_image_b64": face_image_b64,
        "face_image_mime": face_image_mime or "image/jpeg",
        "verified_at": timezone.now().isoformat(),
    }

    if exam_group:
        request.session[_student_exam_entry_session_key(exam_group.pk)] = payload
        request.session.modified = True

    return payload


def _get_student_exam_entry_payload(
    request: HttpRequest,
    exam_group: ExamSessionGroup | None,
) -> dict[str, Any] | None:
    if not exam_group:
        return None

    payload = request.session.get(_student_exam_entry_session_key(exam_group.pk)) or {}

    if not isinstance(payload, dict):
        return None

    if not payload.get("entry_token") or not payload.get("face_image_b64") or not payload.get("room_id"):
        return None

    return payload


def _clear_student_exam_entry_payload(
    request: HttpRequest,
    exam_group: ExamSessionGroup | None,
) -> None:
    if not exam_group:
        return

    request.session.pop(_student_exam_entry_session_key(exam_group.pk), None)
    request.session.modified = True


def _get_student_exam_session(user, subject, exam_group):
    if not exam_group:
        return None

    return (
        ExamSession.objects.filter(
            user_id=user,
            subject_id=subject,
            exam_session_group_id=exam_group,
        )
        .order_by("-created_at", "-pk")
        .first()
    )


def _student_exam_reentry_state(session: ExamSession | None) -> str | None:
    if not session:
        return None

    if _is_session_finalized(session):
        return "COMPLETED"

    if _student_has_started_exam(session):
        return "STARTED"

    return None


def _redirect_locked_student_exam(
    request: HttpRequest,
    session: ExamSession | None,
) -> HttpResponse | None:
    reentry_state = _student_exam_reentry_state(session)

    if reentry_state == "COMPLETED":
        messages.error(request, "Bài thi này đã hoàn thành.")
        return redirect("qna:history_detail", session_id=session.pk)

    if reentry_state == "STARTED":
        exam_url = reverse("qna:exam_page", args=[session.subject.subject_code])
        if request.path != exam_url:
            return redirect("qna:exam_page", subject_code=session.subject.subject_code)

    return None


def _locked_student_exam_json(session: ExamSession | None) -> JsonResponse | None:
    reentry_state = _student_exam_reentry_state(session)

    if reentry_state == "COMPLETED":
        return JsonResponse(
            {"status": "error", "message": "Bài thi này đã hoàn thành."},
            status=400,
        )

    if reentry_state == "STARTED":
        return JsonResponse(
            {
                "status": "success",
                "resume": True,
                "redirect_url": reverse("qna:exam_page", args=[session.subject.subject_code]),
                "message": "Phiên thi đang diễn ra và có thể tiếp tục.",
            }
        )

    return None


def _ensure_student_enrolled_subject(user, subject):
    if not user or not subject:
        return

    profile, _ = UserProfile.objects.get_or_create(user_id=user)
    if not profile.subjects_enrolled.filter(pk=subject.pk).exists():
        profile.subjects_enrolled.add(subject)


def _revoke_student_enrolled_subject_if_unused(user, subject):
    if not user or not subject:
        return

    profile, _ = UserProfile.objects.get_or_create(user_id=user)

    still_linked = StudentRosterStudent.objects.filter(
        linked_user_id=user,
        student_roster_upload_id__subject_id=subject,
    ).exists()

    if not still_linked:
        profile.subjects_enrolled.remove(subject)


def _sync_roster_enrollments(roster):
    if not roster:
        return 0

    linked_rows = (
        roster.students
        .select_related("linked_user_id")
        .filter(linked_user_id__isnull=False)
    )

    synced_count = 0
    for row in linked_rows:
        profile, _ = UserProfile.objects.get_or_create(user_id=row.linked_user_id)
        before_exists = profile.subjects_enrolled.filter(pk=roster.subject.pk).exists()

        _ensure_student_enrolled_subject(row.linked_user_id, roster.subject)

        after_exists = profile.subjects_enrolled.filter(pk=roster.subject.pk).exists()
        if not before_exists and after_exists:
            synced_count += 1

    return synced_count


def _sync_student_subjects_from_rosters(user):
    if not user:
        return {"added": 0, "removed": 0}

    profile, _ = UserProfile.objects.get_or_create(user_id=user)

    roster_subject_ids = set(
        StudentRosterStudent.objects.filter(linked_user_id=user)
        .values_list("student_roster_upload_id__subject_id", flat=True)
        .distinct()
    )

    enrolled_ids = set(profile.subjects_enrolled.values_list("pk", flat=True))

    missing_ids = roster_subject_ids - enrolled_ids
    stale_ids = enrolled_ids - roster_subject_ids

    if missing_ids:
        profile.subjects_enrolled.add(*Subject.objects.filter(pk__in=missing_ids))

    if stale_ids:
        profile.subjects_enrolled.remove(*Subject.objects.filter(pk__in=stale_ids))

    return {
        "added": len(missing_ids),
        "removed": len(stale_ids),
    }


def _ensure_student_absent_sessions_for_history(user):
    if not user:
        return 0

    created_count = 0
    profile = getattr(user, "userprofile", None)
    subjects = profile.subjects_enrolled.all() if profile else Subject.objects.none()

    for subject in subjects:
        rooms = _get_student_assigned_rooms_for_subject(user, subject)

        for room in rooms:
            exam_group = room.exam_session_group_id
            if not exam_group or exam_group.computed_status not in {"COMPLETED", "CANCELLED"}:
                continue

            existing_session = _get_student_exam_session(user, subject, exam_group)
            if existing_session:
                continue

            absent_session = ExamSession.objects.create(
                user_id=user,
                subject_id=subject,
                exam_session_group_id=exam_group,
                session_status="PENDING",
                final_score=0,
            )

            if getattr(exam_group, "end_at", None):
                ExamSession.objects.filter(pk=absent_session.pk).update(created_at=exam_group.end_at)

            created_count += 1

    return created_count
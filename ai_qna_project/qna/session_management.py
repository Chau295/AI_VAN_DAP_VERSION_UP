from __future__ import annotations

import csv
import io
import json
import random
import re
import string
from datetime import datetime, timedelta
from uuid import uuid4

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from openpyxl import Workbook as OpenpyxlWorkbook

from .models import (
    ExamCode,
    ExamRoom,
    ExamSessionGroup,
    ExamSessionRoom,
    ExamSet,
    ExamSetStatus,
    SemesterChoices,
    StudentRosterStudent,
    StudentRosterUpload,
    Subject,
)
from .student_accounts import normalize_student_code

User = get_user_model()


def _ensure_lecturer(request):
    profile = getattr(request.user, "userprofile", None)
    if not profile or not profile.is_lecturer:
        raise PermissionDenied("KhÃ´ng cÃ³ quyá»n truy cáº­p.")


def _get_lecturer_subjects(request):
    return request.user.userprofile.subjects_taught.all().order_by("name")


def _get_selected_subject_for_lecturer(request, subject_id):
    subjects = _get_lecturer_subjects(request)
    if subject_id:
        return get_object_or_404(subjects, pk=subject_id)
    return subjects.first()


def _student_default_academic_year():
    now = timezone.localtime()
    start_year = now.year if now.month >= 8 else now.year - 1
    return f"{start_year}-{start_year + 1}"


SESSION_STATUS_CHOICES = [
    ("ALL", "Táº¥t cáº£"),
    ("DRAFT", "LÆ°u nhÃ¡p"),
    ("SCHEDULED", "Sáº¯p diá»…n ra"),
    ("ONGOING", "Äang diá»…n ra"),
    ("COMPLETED", "ÄÃ£ káº¿t thÃºc"),
    ("CANCELLED", "ÄÃ£ há»§y"),
]


def _session_safe_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default


def _session_safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _locked_exam_group_mutation_response(exam_group):
    message = (
        "KhÃ´ng thá»ƒ chá»‰nh sá»­a khi ca thi Ä‘ang diá»…n ra."
        if exam_group.is_entry_open
        else "Ca thi nÃ y Ä‘Ã£ khÃ³a chá»‰nh sá»­a."
    )
    return JsonResponse(
        {
            "success": False,
            "status": "FAIL",
            "error": message,
            "message": message,
        },
        status=403,
    )


def _generate_room_password(length=5):
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def _session_get_payload(request):
    if request.content_type and "application/json" in request.content_type:
        try:
            return json.loads(request.body.decode("utf-8"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return request.POST


def _session_status_class(status):
    return {
        "DRAFT": "draft",
        "SCHEDULED": "scheduled",
        "ONGOING": "ongoing",
        "COMPLETED": "completed",
        "CANCELLED": "cancelled",
    }.get(status, "draft")


def _session_total_score_for_code(exam_code):
    total = exam_code.exam_code_questions.aggregate(total=Sum("score")).get("total") or 0
    return _session_safe_float(total)


def _serialize_exam_code_for_session(exam_code):
    total_qs = getattr(exam_code, "total_questions", None)
    if total_qs is None:
        total_qs = exam_code.exam_code_questions.count()

    return {
        "id": exam_code.pk, # Changed to pk
        "code_name": exam_code.code_name,
        "total_questions": total_qs,
        "total_score": _session_total_score_for_code(exam_code),
        "status_label": "ÄÃ£ duyá»‡t" if exam_code.is_approved else "ChÆ°a duyá»‡t",
        "is_approved": exam_code.is_approved,
    }


def _serialize_exam_set_for_session(exam_set):
    approved_codes = [
        _serialize_exam_code_for_session(exam_code)
        for exam_code in exam_set.exam_codes.all()
        if exam_code.is_approved
    ]
    return {
        "id": exam_set.pk, # Changed to pk
        "set_code": f"BD-{exam_set.pk:04d}",
        "name": getattr(exam_set, "display_name", exam_set.name),
        "academic_year": exam_set.academic_year,
        "semester": exam_set.semester,
        "semester_label": getattr(exam_set, "get_semester_display", lambda: exam_set.semester)(),
        "status": exam_set.status,
        "status_label": getattr(exam_set, "get_status_display", lambda: exam_set.status)(),
        "approved_codes_count": len(approved_codes),
        "codes": approved_codes,
    }


def _serialize_roster_for_session(roster):
    students = []
    for row in roster.students.select_related("linked_user_id").all().order_by("row_number"):
        students.append(
            {
                "roster_student_id": row.pk, # Changed to pk
                "student_code": row.student_code,
                "full_name": row.full_name,
                "linked_user_id": row.linked_user_id.pk if row.linked_user_id else None,
            }
        )
    return {
        "id": roster.pk, # Changed to pk
        "title": roster.original_file_name or roster.title,
        "academic_year": roster.academic_year,
        "semester": roster.semester,
        "semester_label": roster.get_semester_display(),
        "student_count": roster.total_students,
        "students": students,
    }


def _build_session_catalog(subject):
    exam_sets = (
        ExamSet.objects.filter(subject_id=subject)
        .prefetch_related("exam_codes__exam_code_questions")
        .order_by("-created_at")
    )
    rosters = (
        StudentRosterUpload.objects.filter(subject_id=subject)
        .prefetch_related("students__linked_user_id")
        .order_by("-created_at")
    )
    room_suggestions = list(
        ExamRoom.objects.all().order_by("room_name").values("room_name", "room_code", "capacity")
    )

    year_options = sorted(
        list(set(es.academic_year for es in exam_sets if es.academic_year)),
        reverse=True
    )

    return {
        "subject": {
            "id": subject.pk, # Changed to pk
            "name": subject.name,
            "subject_code": subject.subject_code,
        },
        "year_options": year_options,
        "semester_options": [{"value": value, "label": label} for value, label in SemesterChoices.choices],
        "exam_sets": [
            serialized
            for serialized in (_serialize_exam_set_for_session(exam_set) for exam_set in exam_sets)
            if serialized["approved_codes_count"] > 0
        ],
        "rosters": [_serialize_roster_for_session(roster) for roster in rosters],
        "room_suggestions": room_suggestions,
    }


def _legacy_room_configuration(exam_group):
    room_configs = []
    group_codes = list(exam_group.exam_codes.all().order_by("code_number", "pk"))
    session_rooms = exam_group.session_rooms.select_related("exam_room_id", "exam_set_id").prefetch_related(
        "exam_codes", "students__userprofile"
    )

    for index, session_room in enumerate(session_rooms, start=1):
        room_codes = list(session_room.exam_codes.all()) or group_codes
        assignments = []
        users = list(session_room.students.select_related("userprofile").all())

        for assignment_index, user in enumerate(users, start=1):
            assigned_code = room_codes[(assignment_index - 1) % len(room_codes)] if room_codes else None
            profile = getattr(user, "userprofile", None)
            assignments.append(
                {
                    "roster_student_id": None,
                    "student_code": (
                        profile.student_id if profile and profile.student_id else user.username
                    ),
                    "full_name": (
                        profile.full_name if profile and profile.full_name else user.username
                    ),
                    "linked_user_id": user.pk, # Changed to pk
                    "exam_code_id": assigned_code.pk if assigned_code else None,
                    "exam_code_name": assigned_code.code_name if assigned_code else "",
                    "display_order": assignment_index,
                }
            )

        room_configs.append(
            {
                "client_id": f"legacy-room-{session_room.pk}",
                "room_name": session_room.display_room_name,
                "student_count": session_room.expected_students or len(assignments),
                "password": session_room.room_password,
                "exam_set_id": session_room.exam_set_id_id,
                "exam_set_name": session_room.exam_set_id.display_name if getattr(session_room.exam_set_id, "display_name", None) else getattr(session_room.exam_set_id, "name", ""),
                "exam_code_ids": [code.pk for code in room_codes],
                "exam_code_names": [code.code_name for code in room_codes],
                "assignments": assignments,
                "display_order": session_room.display_order or index,
            }
        )
    return room_configs


def _build_session_initial_state(subject, exam_group=None):
    if exam_group:
        config = exam_group.configuration_data or {}
        room_configs = config.get("rooms") or _legacy_room_configuration(exam_group)
        roster_configs = config.get("rosters") or []
        start_at = timezone.localtime(exam_group.exam_date)
        end_at = timezone.localtime(exam_group.end_at)
        return {
            "session_id": exam_group.pk, # Changed to pk
            "subject_id": subject.pk, # Changed to pk
            "group_name": exam_group.group_name,
            "academic_year": exam_group.academic_year or _student_default_academic_year(),
            "semester": exam_group.semester or SemesterChoices.HK1,
            "exam_date": start_at.strftime("%Y-%m-%d"),
            "start_time": start_at.strftime("%H:%M"),
            "end_time": end_at.strftime("%H:%M"),
            "duration_minutes": exam_group.duration_minutes,
            "description": exam_group.description or "",
            "exam_password": exam_group.exam_password or "",
            "status": exam_group.status,
            "status_label": exam_group.status_label,
            "start_at_timestamp": int(start_at.timestamp()),
            "end_at_timestamp": int(end_at.timestamp()),
            "room_configs": room_configs,
            "roster_configs": roster_configs,
        }

    now = timezone.localtime()
    default_start = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    default_end = default_start + timedelta(minutes=90)
    return {
        "session_id": None,
        "subject_id": subject.pk, # Changed to pk
        "group_name": "",
        "academic_year": _student_default_academic_year(),
        "semester": SemesterChoices.HK1,
        "exam_date": default_start.strftime("%Y-%m-%d"),
        "start_time": default_start.strftime("%H:%M"),
        "end_time": default_end.strftime("%H:%M"),
        "duration_minutes": 90,
        "description": "",
        "exam_password": "",
        "status": "DRAFT",
        "status_label": "LÆ°u nhÃ¡p",
        "room_configs": [],
        "roster_configs": [],
    }


def _normalize_session_configuration(request, payload, exam_group=None):
    subjects = _get_lecturer_subjects(request)
    subject_id = payload.get("subject_id") or (exam_group.subject_id_id if exam_group else "")
    subject = get_object_or_404(subjects, pk=subject_id)

    errors = []
    group_name = (payload.get("group_name") or "").strip()
    academic_year = (payload.get("academic_year") or "").strip()
    semester = (payload.get("semester") or "").strip() or SemesterChoices.HK1
    description = (payload.get("description") or "").strip()
    exam_password = (payload.get("exam_password") or "").strip()
    exam_date_value = (payload.get("exam_date") or "").strip()
    start_time_value = (payload.get("start_time") or "").strip()
    end_time_value = (payload.get("end_time") or "").strip()
    submit_action = (payload.get("submit_action") or "CONFIRM").strip().upper()
    is_draft = submit_action in {"DRAFT", "SAVE_DRAFT"}

    if not group_name:
        errors.append("Vui lÃ²ng nháº­p tÃªn ká»³ thi.")
    if not academic_year or not re.match(r"^\d{4}-\d{4}$", academic_year):
        errors.append("NÄƒm há»c khÃ´ng há»£p lá»‡.")
    if semester not in {choice[0] for choice in SemesterChoices.choices}:
        errors.append("Há»c ká»³ khÃ´ng há»£p lá»‡.")
    if not exam_date_value:
        errors.append("Vui lÃ²ng chá»n ngÃ y thi.")
    if not start_time_value or not end_time_value:
        errors.append("Vui lÃ²ng nháº­p Ä‘áº§y Ä‘á»§ thá»i gian báº¯t Ä‘áº§u vÃ  káº¿t thÃºc.")

    start_at = None
    end_at = None
    if exam_date_value and start_time_value and end_time_value:
        try:
            start_at = timezone.make_aware(
                datetime.strptime(f"{exam_date_value} {start_time_value}", "%Y-%m-%d %H:%M"),
                timezone.get_current_timezone(),
            )
            end_at = timezone.make_aware(
                datetime.strptime(f"{exam_date_value} {end_time_value}", "%Y-%m-%d %H:%M"),
                timezone.get_current_timezone(),
            )
        except ValueError:
            errors.append("Äá»‹nh dáº¡ng ngÃ y giá» khÃ´ng há»£p lá»‡.")

    duration_minutes = 0
    if start_at and end_at:
        duration_minutes = int((end_at - start_at).total_seconds() // 60)
        if duration_minutes <= 0:
            errors.append("Thá»i gian káº¿t thÃºc pháº£i sau thá»i gian báº¯t Ä‘áº§u.")
        if start_at < timezone.now():
            errors.append("KhÃ´ng thá»ƒ Ä‘áº·t thá»i gian báº¯t Ä‘áº§u nhá» hÆ¡n thá»i Ä‘iá»ƒm hiá»‡n táº¡i.")

    roster_ids = [_session_safe_int(item) for item in payload.get("roster_ids") or [] if _session_safe_int(item)]
    roster_qs = (
        StudentRosterUpload.objects.filter(subject_id=subject, pk__in=roster_ids) # Changed to pk__in
        .prefetch_related("students__linked_user_id")
        .order_by("-created_at")
    )
    roster_students_by_id = {}
    roster_students_by_code = {}
    roster_configs = []
    for roster in roster_qs:
        roster_configs.append(_serialize_roster_for_session(roster))
        for row in roster.students.select_related("linked_user_id").all():
            roster_students_by_id[row.pk] = row
            roster_students_by_code[normalize_student_code(row.student_code)] = row

    if not is_draft and not roster_configs:
        errors.append("Vui lÃ²ng chá»n Ã­t nháº¥t má»™t nguá»“n danh sÃ¡ch sinh viÃªn.")

    rooms_payload = payload.get("rooms") or []
    normalized_rooms = []
    selected_code_ids = set()
    seen_student_codes = set()

    if not isinstance(rooms_payload, list):
        rooms_payload = []

    if not is_draft and not rooms_payload:
        errors.append("Vui lÃ²ng thÃªm Ã­t nháº¥t má»™t phÃ²ng thi.")

    for index, raw_room in enumerate(rooms_payload, start=1):
        room_name = (raw_room.get("room_name") or "").strip()
        student_count = _session_safe_int(raw_room.get("student_count"), 0)
        room_password = (raw_room.get("password") or "").strip().upper()
        exam_set_id = _session_safe_int(raw_room.get("exam_set_id"))
        exam_set = ExamSet.objects.filter(pk=exam_set_id, subject_id=subject).first() if exam_set_id else None

        raw_exam_code_ids = [
            _session_safe_int(item)
            for item in raw_room.get("exam_code_ids") or []
            if _session_safe_int(item)
        ]

        valid_codes = []
        if exam_set:
            valid_codes = list(
                ExamCode.objects.filter(
                    pk__in=raw_exam_code_ids, # Changed to pk__in
                    subject_id=subject,
                    exam_set_id=exam_set,
                    is_approved=True,
                ).order_by("code_number", "pk")
            )

        assignments_payload = raw_room.get("assignments") or []
        normalized_assignments = []

        if not is_draft:
            if not room_name:
                errors.append(f"PhÃ²ng thi dÃ²ng {index} chÆ°a cÃ³ tÃªn phÃ²ng.")
            if student_count <= 0:
                errors.append(f"PhÃ²ng thi dÃ²ng {index} pháº£i cÃ³ sá»‘ lÆ°á»£ng sinh viÃªn lá»›n hÆ¡n 0.")
            if not room_password:
                room_password = _generate_room_password()
            elif len(room_password) != 5:
                errors.append(f"PhÃ²ng thi dÃ²ng {index} pháº£i cÃ³ máº­t kháº©u Ä‘Ãºng 5 kÃ½ tá»±.")
            if not exam_set:
                errors.append(f"PhÃ²ng thi dÃ²ng {index} chÆ°a gáº¯n bá»™ Ä‘á».")
            if not valid_codes:
                errors.append(f"PhÃ²ng thi dÃ²ng {index} chÆ°a chá»n mÃ£ Ä‘á» Ä‘Ã£ duyá»‡t.")

        valid_code_map = {code.pk: code for code in valid_codes}
        if isinstance(assignments_payload, list):
            for assignment_index, raw_assignment in enumerate(assignments_payload, start=1):
                roster_student_id = _session_safe_int(raw_assignment.get("roster_student_id"))
                student_code = normalize_student_code(raw_assignment.get("student_code") or "")
                roster_student = None

                if roster_student_id:
                    roster_student = roster_students_by_id.get(roster_student_id)
                if not roster_student and student_code:
                    roster_student = roster_students_by_code.get(student_code)

                if not roster_student:
                    errors.append(
                        f"Sinh viÃªn dÃ²ng {assignment_index} cá»§a phÃ²ng {room_name or index} khÃ´ng thuá»™c nguá»“n danh sÃ¡ch Ä‘Ã£ chá»n."
                    )
                    continue

                normalized_code = normalize_student_code(roster_student.student_code)
                if normalized_code in seen_student_codes:
                    errors.append(f"Sinh viÃªn {roster_student.student_code} Ä‘ang bá»‹ phÃ¢n trÃ¹ng nhiá»u phÃ²ng.")
                    continue

                exam_code_id = _session_safe_int(raw_assignment.get("exam_code_id"))
                if exam_code_id and exam_code_id not in valid_code_map:
                    errors.append(
                        f"Sinh viÃªn {roster_student.student_code} cá»§a phÃ²ng {room_name or index} Ä‘ang gáº¯n mÃ£ Ä‘á» khÃ´ng há»£p lá»‡."
                    )
                    continue

                seen_student_codes.add(normalized_code)
                linked_user_id = raw_assignment.get("linked_user_id") or (roster_student.linked_user_id.pk if roster_student.linked_user_id else None)
                linked_user_id = _session_safe_int(linked_user_id) or None
                selected_code = valid_code_map.get(exam_code_id)

                normalized_assignments.append(
                    {
                        "roster_student_id": roster_student.pk,
                        "student_code": roster_student.student_code,
                        "full_name": roster_student.full_name,
                        "linked_user_id": linked_user_id,
                        "exam_code_id": selected_code.pk if selected_code else None,
                        "exam_code_name": selected_code.code_name if selected_code else "",
                        "display_order": assignment_index,
                    }
                )

        if not is_draft and len(normalized_assignments) > student_count:
            errors.append(
                f"PhÃ²ng {room_name or index} Ä‘ang cÃ³ {len(normalized_assignments)} sinh viÃªn, vÆ°á»£t quÃ¡ sá»©c chá»©a Ä‘Ã£ cáº¥u hÃ¬nh {student_count}."
            )

        selected_code_ids.update(code.pk for code in valid_codes)
        normalized_rooms.append(
            {
                "client_id": (raw_room.get("client_id") or f"room-{uuid4().hex[:8]}").strip(),
                "room_name": room_name,
                "student_count": student_count,
                "password": room_password,
                "exam_set_id": exam_set.pk if exam_set else None,
                "exam_set_name": getattr(exam_set, "display_name", exam_set.name) if exam_set else "",
                "exam_code_ids": [code.pk for code in valid_codes],
                "exam_code_names": [code.code_name for code in valid_codes],
                "assignments": normalized_assignments,
                "display_order": index,
            }
        )

    if not is_draft:
        selected_student_codes = {
            normalize_student_code(row.student_code)
            for row in roster_students_by_id.values()
            if normalize_student_code(row.student_code)
        }
        missing_student_count = len(selected_student_codes - seen_student_codes)
        if missing_student_count:
            errors.append(
                f"Hiá»‡n cÃ²n {missing_student_count} sinh viÃªn chÆ°a Ä‘Æ°á»£c phÃ¢n bá»• vÃ o phÃ²ng thi. Vui lÃ²ng kiá»ƒm tra láº¡i sá»©c chá»©a vÃ  thao tÃ¡c random."
            )

    if errors:
        return {"errors": errors, "subject": subject}

    return {
        "errors": [],
        "subject": subject,
        "group_name": group_name,
        "academic_year": academic_year,
        "semester": semester,
        "description": description,
        "exam_password": exam_password,
        "exam_date": start_at,
        "duration_minutes": duration_minutes,
        "status": "DRAFT" if is_draft else "SCHEDULED",
        "rosters": roster_configs,
        "rooms": normalized_rooms,
        "selected_code_ids": sorted(selected_code_ids),
        "is_draft": is_draft,
    }


def _save_session_configuration(normalized, user, exam_group=None):
    with transaction.atomic():
        target = exam_group or ExamSessionGroup(subject_id=normalized["subject"], created_by_user_id=user)
        target.subject_id = normalized["subject"]
        target.group_name = normalized["group_name"]
        target.academic_year = normalized["academic_year"]
        target.semester = normalized["semester"]
        target.description = normalized["description"]
        target.exam_date = normalized["exam_date"]
        target.duration_minutes = normalized["duration_minutes"]
        target.exam_password = normalized["exam_password"] or None
        target.status = normalized["status"]
        target.configuration_data = {
            "rosters": normalized["rosters"],
            "rooms": normalized["rooms"],
        }
        target.save()

        if normalized["selected_code_ids"]:
            target.exam_codes.set(
                ExamCode.objects.filter(
                    subject_id=normalized["subject"],
                    pk__in=normalized["selected_code_ids"], # Changed to pk__in
                )
            )
        else:
            target.exam_codes.clear()

        target.session_rooms.all().delete()

        for room in normalized["rooms"]:
            session_room = ExamSessionRoom.objects.create(
                exam_session_group_id=target,
                room_name=room["room_name"],
                expected_students=room["student_count"],
                room_password=room["password"],
                exam_set_id_id=room["exam_set_id"],
                display_order=room["display_order"],
            )
            if room["exam_code_ids"]:
                session_room.exam_codes.set(
                    ExamCode.objects.filter(
                        pk__in=room["exam_code_ids"], # Changed to pk__in
                        subject_id=normalized["subject"],
                    )
                )
            linked_user_ids = [
                assignment["linked_user_id"]
                for assignment in room["assignments"]
                if assignment.get("linked_user_id")
            ]
            if linked_user_ids:
                session_room.students.set(User.objects.filter(pk__in=linked_user_ids)) # Changed to pk__in

    return target


def _exam_group_base_queryset():
    return ExamSessionGroup.objects.select_related("subject_id", "created_by_user_id").prefetch_related(
        "exam_codes__exam_code_questions",
        "exam_sessions",
        "session_rooms__exam_room_id",
        "session_rooms__exam_set_id",
        "session_rooms__exam_codes",
        "session_rooms__students__userprofile",
    )


def _get_lecturer_exam_group_or_404(request, exam_group_id):
    exam_group = get_object_or_404(_exam_group_base_queryset(), pk=exam_group_id)
    if exam_group.subject not in _get_lecturer_subjects(request):
        raise PermissionDenied("Báº¡n khÃ´ng cÃ³ quyá»n truy cáº­p ca thi nÃ y.")
    return exam_group


def _get_exam_group_room_configs(exam_group):
    config = exam_group.configuration_data or {}
    room_configs = config.get("rooms") or _legacy_room_configuration(exam_group)
    if not isinstance(room_configs, list):
        return []
    return sorted(room_configs, key=lambda item: _session_safe_int(item.get("display_order"), 0) or 0)


def _build_exam_group_student_rows(exam_group):
    room_configs = _get_exam_group_room_configs(exam_group)
    config = exam_group.configuration_data or {}
    roster_ids = [
        _session_safe_int(item.get("id"))
        for item in (config.get("rosters") or [])
        if _session_safe_int(item.get("id"))
    ]

    roster_student_ids = set()
    student_codes = set()
    for room in room_configs:
        for assignment in room.get("assignments") or []:
            roster_student_id = _session_safe_int(assignment.get("roster_student_id"))
            if roster_student_id:
                roster_student_ids.add(roster_student_id)
            normalized_code = normalize_student_code(assignment.get("student_code") or "")
            if normalized_code:
                student_codes.add(normalized_code)

    roster_rows = StudentRosterStudent.objects.select_related("linked_user_id", "linked_user_id__userprofile")
    roster_filter = Q()
    if roster_ids:
        roster_filter |= Q(student_roster_upload_id__in=roster_ids)
    if roster_student_ids:
        roster_filter |= Q(pk__in=roster_student_ids) # Changed to pk__in
    if student_codes:
        roster_filter |= Q(student_code__in=student_codes)

    if roster_filter:
        roster_rows = roster_rows.filter(roster_filter, student_roster_upload_id__subject_id=exam_group.subject_id)
    else:
        roster_rows = roster_rows.none()

    roster_rows = roster_rows.order_by("-created_at", "-pk")
    roster_rows_by_id = {}
    roster_rows_by_code = {}
    for row in roster_rows:
        roster_rows_by_id.setdefault(row.pk, row) # Changed to pk
        normalized_code = normalize_student_code(row.student_code)
        if normalized_code and normalized_code not in roster_rows_by_code:
            roster_rows_by_code[normalized_code] = row

    time_label = f"{timezone.localtime(exam_group.start_at):%H:%M}-{timezone.localtime(exam_group.end_at):%H:%M}"
    student_rows = []

    for room_index, room in enumerate(room_configs, start=1):
        room_name = (room.get("room_name") or "").strip() or f"PhÃ²ng {room_index}"
        assignments = room.get("assignments") or []
        for assignment_index, assignment in enumerate(assignments, start=1):
            roster_student_id = _session_safe_int(assignment.get("roster_student_id"))
            normalized_code = normalize_student_code(assignment.get("student_code") or "")
            roster_student = roster_rows_by_id.get(roster_student_id) or roster_rows_by_code.get(normalized_code)
            linked_user = getattr(roster_student, "linked_user_id", None)
            linked_profile = getattr(linked_user, "userprofile", None) if linked_user else None

            student_rows.append(
                {
                    "room_order": _session_safe_int(room.get("display_order"), room_index) or room_index,
                    "assignment_order": _session_safe_int(assignment.get("display_order"), assignment_index) or assignment_index,
                    "student_code": assignment.get("student_code") or (roster_student.student_code if roster_student else ""),
                    "full_name": assignment.get("full_name") or (roster_student.full_name if roster_student else ""),
                    "class_name": (
                        roster_student.class_name
                        if roster_student and roster_student.class_name
                        else (linked_profile.class_name if linked_profile else "")
                    ),
                    "date_of_birth": (
                        roster_student.date_of_birth.strftime("%d/%m/%Y")
                        if roster_student and roster_student.date_of_birth
                        else ""
                    ),
                    "room_name": room_name,
                    "time_label": time_label,
                    "exam_code_name": assignment.get("exam_code_name") or "",
                }
            )

    student_rows.sort(key=lambda item: (item["room_order"], item["assignment_order"], item["student_code"]))
    return student_rows


def _build_exam_group_detail_context(exam_group):
    room_rows = []
    unique_exam_code_ids = set()
    unique_exam_set_ids = set()
    total_expected_students = 0

    for room_index, room in enumerate(_get_exam_group_room_configs(exam_group), start=1):
        room_name = (room.get("room_name") or "").strip() or f"PhÃ²ng {room_index}"
        student_count = _session_safe_int(room.get("student_count"), 0)
        total_expected_students += student_count
        exam_set_id = _session_safe_int(room.get("exam_set_id"))
        if exam_set_id:
            unique_exam_set_ids.add(exam_set_id)

        exam_code_ids = [
            _session_safe_int(item)
            for item in (room.get("exam_code_ids") or [])
            if _session_safe_int(item)
        ]
        unique_exam_code_ids.update(exam_code_ids)

        room_rows.append(
            {
                "room_name": room_name,
                "student_count": student_count,
                "password": room.get("password") or "",
                "exam_set_name": room.get("exam_set_name") or "-",
                "exam_code_names": room.get("exam_code_names") or [],
            }
        )

    student_rows = _build_exam_group_student_rows(exam_group)
    return {
        "room_rows": room_rows,
        "student_rows": student_rows,
        "room_count": len(room_rows),
        "student_count": len(student_rows) or total_expected_students or exam_group.total_students_count,
        "assigned_code_count": len(unique_exam_code_ids),
        "exam_set_count": len(unique_exam_set_ids),
        "status_class": _session_status_class(exam_group.computed_status),
        "can_delete": exam_group.can_modify,
        "can_edit": exam_group.can_modify,
    }


@login_required
def lecturer_create_session_screen(request):
    _ensure_lecturer(request)
    subjects = _get_lecturer_subjects(request)
    session_id = request.GET.get("session_id") or request.GET.get("edit")
    exam_group = None

    if session_id:
        exam_group = get_object_or_404(
            ExamSessionGroup.objects.select_related("subject_id", "created_by_user_id").prefetch_related(
                "exam_codes__exam_code_questions",
                "session_rooms__exam_room_id",
                "session_rooms__exam_set_id",
                "session_rooms__exam_codes",
                "session_rooms__students__userprofile",
            ),
            pk=session_id,
        )
        if exam_group.subject not in subjects:
            raise PermissionDenied("Báº¡n khÃ´ng cÃ³ quyá»n truy cáº­p ca thi nÃ y.")
        selected_subject = exam_group.subject
    else:
        selected_subject = _get_selected_subject_for_lecturer(request, request.GET.get("subject_id"))

    if not selected_subject:
        return redirect("qna:lecturer_dashboard")

    requested_mode = (request.GET.get("mode") or ("edit" if exam_group else "create")).strip().lower()
    if requested_mode not in {"create", "edit", "view"}:
        requested_mode = "edit" if exam_group else "create"
    if exam_group and requested_mode == "edit" and not exam_group.can_modify:
        requested_mode = "view"

    context = {
        "subjects": subjects,
        "selected_subject": selected_subject,
        "exam_group": exam_group,
        "screen_mode": requested_mode,
        "screen_mode_label": {
            "create": "Táº¡o ca thi má»›i",
            "edit": "Chá»‰nh sá»­a ca thi",
            "view": "Chi tiáº¿t ca thi",
        }[requested_mode],
        "catalog_json": mark_safe(json.dumps(_build_session_catalog(selected_subject), ensure_ascii=False)),
        "initial_state_json": mark_safe(
            json.dumps(_build_session_initial_state(selected_subject, exam_group), ensure_ascii=False)
        ),
    }
    return render(request, "qna/lecturer/lecturer_exam_session_wizard.html", context)


@login_required
@require_POST
def lecturer_create_exam_group_screen(request):
    _ensure_lecturer(request)
    normalized = _normalize_session_configuration(request, _session_get_payload(request))
    if normalized["errors"]:
        return JsonResponse(
            {
                "success": False,
                "status": "FAIL",
                "message": normalized["errors"][0],
                "errors": normalized["errors"],
            },
            status=400,
        )

    exam_group = _save_session_configuration(normalized, request.user)
    messages.success(
        request,
        "ÄÃ£ lÆ°u nhÃ¡p ca thi thÃ nh cÃ´ng." if normalized["is_draft"] else "Cáº¥u hÃ¬nh ca thi má»›i thÃ nh cÃ´ng",
    )
    return JsonResponse(
        {
            "success": True,
            "status": "SUCCESS",
            "exam_group_id": exam_group.pk, # Changed to pk
            "redirect_url": reverse("qna:lecturer_exam_sessions_list"),
        }
    )


@login_required
def lecturer_exam_sessions_list(request):
    _ensure_lecturer(request)
    subjects = list(_get_lecturer_subjects(request))
    if not subjects:
        return redirect("qna:lecturer_dashboard")

    subject_id = (request.GET.get("subject_id") or "").strip()
    keyword = (request.GET.get("q") or "").strip().lower()
    status_filter = (request.GET.get("status") or "ALL").strip().upper() or "ALL"
    academic_year_filter = (request.GET.get("academic_year") or "").strip()
    semester_filter = (request.GET.get("semester") or "").strip()

    base_queryset = (
        ExamSessionGroup.objects.filter(subject_id__in=subjects)
        .select_related("subject_id", "created_by_user_id")
        .prefetch_related("exam_codes")
        .order_by("-exam_date", "-pk")
    )

    if subject_id:
        base_queryset = base_queryset.filter(subject_id=subject_id)
    if academic_year_filter:
        base_queryset = base_queryset.filter(academic_year=academic_year_filter)
    if semester_filter:
        base_queryset = base_queryset.filter(semester=semester_filter)

    all_groups = list(base_queryset)
    selected_subject = next((item for item in subjects if str(item.pk) == subject_id), None) # Changed to pk

    summary = {
        "total": len(all_groups),
        "draft": sum(1 for item in all_groups if item.computed_status == "DRAFT"),
        "scheduled": sum(1 for item in all_groups if item.computed_status == "SCHEDULED"),
        "ongoing": sum(1 for item in all_groups if item.computed_status == "ONGOING"),
        "completed": sum(1 for item in all_groups if item.computed_status == "COMPLETED"),
    }

    filtered_groups = all_groups

    if keyword:
        filtered_groups = [
            item
            for item in filtered_groups
            if keyword in item.group_name.lower()
            or keyword in item.subject.name.lower()
            or keyword in item.subject.subject_code.lower()
            or keyword in (item.academic_year or "").lower()
            or keyword in (item.semester or "").lower()
        ]

    if status_filter != "ALL":
        filtered_groups = [item for item in filtered_groups if item.computed_status == status_filter]

    year_options = sorted(
        {
            value
            for value in ExamSessionGroup.objects.filter(subject_id__in=subjects)
            .exclude(academic_year="")
            .values_list("academic_year", flat=True)
        },
        reverse=True,
    )

    paginator = Paginator(filtered_groups, 10)
    page_obj = paginator.get_page(request.GET.get("page"))

    # Add timestamps for realtime updates
    for exam_group in page_obj.object_list:
        exam_group.start_ts = int(exam_group.start_at.timestamp() * 1000) if exam_group.start_at else 0
        exam_group.end_ts = int(exam_group.end_at.timestamp() * 1000) if exam_group.end_at else 0

    context = {
        "subjects": subjects,
        "selected_subject": selected_subject,
        "page_obj": page_obj,
        "summary": summary,
        "filters": {
            "q": request.GET.get("q") or "",
            "status": status_filter,
            "subject_id": subject_id,
            "academic_year": academic_year_filter,
            "semester": semester_filter,
        },
        "status_choices": SESSION_STATUS_CHOICES,
        "semester_choices": SemesterChoices.choices,
        "year_options": year_options,
        "server_now_ms": int(timezone.now().timestamp() * 1000),
    }
    return render(request, "qna/lecturer/lecturer_exam_sessions_list.html", context)


@login_required
def lecturer_exam_session_detail_screen(request, exam_group_id):
    _ensure_lecturer(request)
    exam_group = _get_lecturer_exam_group_or_404(request, exam_group_id)
    detail_context = _build_exam_group_detail_context(exam_group)

    # Add timestamps for realtime updates
    start_ts = int(exam_group.start_at.timestamp() * 1000) if exam_group.start_at else 0
    end_ts = int(exam_group.end_at.timestamp() * 1000) if exam_group.end_at else 0

    context = {
        "subjects": _get_lecturer_subjects(request),
        "selected_subject": exam_group.subject,
        "exam_group": exam_group,
        "detail": detail_context,
        "list_url": reverse("qna:lecturer_exam_sessions_list"),
        "common_list_url": reverse("qna:lecturer_exam_session_common_list_screen", args=[exam_group.pk]),
        "edit_url": f"{reverse('qna:lecturer_create_session_screen')}?session_id={exam_group.pk}&mode=edit",
        "delete_url": reverse("qna:lecturer_delete_exam_group", args=[exam_group.pk]),
        "start_ts": start_ts,
        "end_ts": end_ts,
        "server_now_ts": int(timezone.now().timestamp()),
    }
    return render(request, "qna/lecturer/lecturer_exam_session_detail.html", context)


@login_required
def lecturer_exam_session_common_list_screen(request, exam_group_id):
    _ensure_lecturer(request)
    exam_group = _get_lecturer_exam_group_or_404(request, exam_group_id)

    keyword = (request.GET.get("q") or "").strip().lower()
    room_filter = (request.GET.get("room") or "").strip()

    student_rows = _build_exam_group_student_rows(exam_group)
    room_options = sorted({item["room_name"] for item in student_rows if item["room_name"]})

    if keyword:
        student_rows = [
            item
            for item in student_rows
            if keyword in (item["student_code"] or "").lower()
            or keyword in (item["full_name"] or "").lower()
            or keyword in (item["class_name"] or "").lower()
            or keyword in (item["room_name"] or "").lower()
            or keyword in (item["exam_code_name"] or "").lower()
        ]

    if room_filter:
        student_rows = [item for item in student_rows if item["room_name"] == room_filter]

    paginator = Paginator(student_rows, 5)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        "subjects": _get_lecturer_subjects(request),
        "selected_subject": exam_group.subject,
        "exam_group": exam_group,
        "page_obj": page_obj,
        "filters": {
            "q": request.GET.get("q") or "",
            "room": room_filter,
        },
        "room_options": room_options,
        "list_url": reverse("qna:lecturer_exam_sessions_list"),
        "detail_url": reverse("qna:lecturer_exam_session_detail_screen", args=[exam_group.pk]),
        "export_url": reverse("qna:lecturer_exam_session_common_list_export", args=[exam_group.pk]),
    }
    return render(request, "qna/lecturer/lecturer_exam_session_common_list.html", context)


@login_required
def lecturer_exam_session_common_list_export(request, exam_group_id):
    _ensure_lecturer(request)
    exam_group = _get_lecturer_exam_group_or_404(request, exam_group_id)

    keyword = (request.GET.get("q") or "").strip().lower()
    room_filter = (request.GET.get("room") or "").strip()
    student_rows = _build_exam_group_student_rows(exam_group)

    if keyword:
        student_rows = [
            item
            for item in student_rows
            if keyword in (item["student_code"] or "").lower()
            or keyword in (item["full_name"] or "").lower()
            or keyword in (item["class_name"] or "").lower()
            or keyword in (item["room_name"] or "").lower()
            or keyword in (item["exam_code_name"] or "").lower()
        ]
    if room_filter:
        student_rows = [item for item in student_rows if item["room_name"] == room_filter]

    workbook = OpenpyxlWorkbook()
    worksheet = workbook.active
    worksheet.title = "Danh sÃ¡ch chung"
    worksheet.append(["STT", "MÃ£ sinh viÃªn", "TÃªn", "Lá»›p", "NgÃ y sinh", "PhÃ²ng thi", "Thá»i gian thi", "MÃ£ Ä‘á»"])

    for index, row in enumerate(student_rows, start=1):
        worksheet.append(
            [
                index,
                row["student_code"],
                row["full_name"],
                row["class_name"] or "-",
                row["date_of_birth"] or "-",
                row["room_name"],
                row["time_label"],
                row["exam_code_name"] or "-",
            ]
        )

    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", exam_group.group_name or "ca_thi").strip("_") or "ca_thi"
    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="Danh_sach_chung_{safe_name}.xlsx"'
    workbook.save(response)
    return response


@login_required
@require_POST
def lecturer_delete_exam_group(request, exam_group_id):
    _ensure_lecturer(request)
    exam_group = _get_lecturer_exam_group_or_404(request, exam_group_id)
    detail_url = reverse("qna:lecturer_exam_session_detail_screen", args=[exam_group.pk])

    if not exam_group.can_modify:
        messages.error(
            request,
            "Ca thi Ä‘ang diá»…n ra nÃªn khÃ´ng thá»ƒ xÃ³a."
            if exam_group.is_entry_open
            else "Ca thi nÃ y Ä‘Ã£ khÃ³a chá»‰nh sá»­a nÃªn khÃ´ng thá»ƒ xÃ³a.",
        )
        return redirect(detail_url)

    exam_group.delete()
    messages.success(request, "XÃ³a ca thi thÃ nh cÃ´ng")
    return redirect(reverse("qna:lecturer_exam_sessions_list"))


@login_required
def lecturer_create_exam_session(request, subject_code):
    _ensure_lecturer(request)
    subject = get_object_or_404(_get_lecturer_subjects(request), subject_code=subject_code)
    return redirect(f"{reverse('qna:lecturer_create_session_screen')}?subject_id={subject.pk}")


@login_required
@require_POST
def lecturer_create_exam_group(request, subject_code):
    _ensure_lecturer(request)
    subject = get_object_or_404(_get_lecturer_subjects(request), subject_code=subject_code)
    payload = request.POST.copy()
    payload["subject_id"] = str(subject.pk)
    normalized = _normalize_session_configuration(request, payload)
    if normalized["errors"]:
        return JsonResponse({"success": False, "error": normalized["errors"][0]}, status=400)

    exam_group = _save_session_configuration(normalized, request.user)
    messages.success(request, "ÄÃ£ táº¡o ca thi thÃ nh cÃ´ng.")
    return JsonResponse({"success": True, "exam_group_id": exam_group.pk})


@login_required
@require_POST
def lecturer_update_exam_group(request, exam_group_id):
    _ensure_lecturer(request)
    exam_group = get_object_or_404(ExamSessionGroup.objects.select_related("subject_id"), pk=exam_group_id)
    if exam_group.subject not in _get_lecturer_subjects(request):
        return JsonResponse({"success": False, "message": "KhÃ´ng cÃ³ quyá»n truy cáº­p"}, status=403)
    if not exam_group.can_modify:
        return _locked_exam_group_mutation_response(exam_group)

    normalized = _normalize_session_configuration(request, _session_get_payload(request), exam_group=exam_group)
    if normalized["errors"]:
        return JsonResponse(
            {
                "success": False,
                "status": "FAIL",
                "message": normalized["errors"][0],
                "errors": normalized["errors"],
            },
            status=400,
        )

    updated_group = _save_session_configuration(normalized, request.user, exam_group=exam_group)
    messages.success(
        request,
        "ÄÃ£ lÆ°u nhÃ¡p ca thi thÃ nh cÃ´ng." if normalized["is_draft"] else "Cáº­p nháº­t ca thi thÃ nh cÃ´ng",
    )
    return JsonResponse(
        {
            "success": True,
            "status": "SUCCESS",
            "exam_group_id": updated_group.pk,
            "redirect_url": f"{reverse('qna:lecturer_exam_sessions_list')}?subject_id={updated_group.subject_id_id}",
        }
    )


@login_required
@require_POST
def lecturer_import_students_to_room(request, session_room_id):
    _ensure_lecturer(request)
    session_room = get_object_or_404(
        ExamSessionRoom.objects.select_related("exam_session_group_id__subject_id"),
        pk=session_room_id,
    )
    if session_room.exam_group.subject not in request.user.userprofile.subjects_taught.all():
        return JsonResponse({"success": False, "error": "KhÃ´ng cÃ³ quyá»n truy cáº­p."}, status=403)
    if not session_room.exam_group.can_modify:
        return _locked_exam_group_mutation_response(session_room.exam_group)

    file_obj = request.FILES.get("csv_file")
    if not file_obj:
        return JsonResponse({"success": False, "error": "Thiáº¿u file."}, status=400)

    try:
        decoded = file_obj.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(decoded))
        imported_users = []
        imported_count = 0

        for row in reader:
            username = (row.get("username") or row.get("student_code") or row.get("mssv") or "").strip()
            username = normalize_student_code(username)
            if not username:
                continue

            user = User.objects.filter(username__iexact=username).first()
            if not user:
                continue

            imported_users.append(user)
            imported_count += 1

        if imported_users:
            session_room.students.add(*imported_users)

        messages.success(request, f"ÄÃ£ import {imported_count} sinh viÃªn thÃ nh cÃ´ng.")
        return JsonResponse({"success": True, "imported_count": imported_count})
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@require_POST
def lecturer_random_assign_students(request, exam_group_id):
    _ensure_lecturer(request)
    exam_group = get_object_or_404(
        ExamSessionGroup.objects.prefetch_related("session_rooms__students"),
        pk=exam_group_id,
    )
    if exam_group.subject not in request.user.userprofile.subjects_taught.all():
        return JsonResponse({"success": False, "error": "KhÃ´ng cÃ³ quyá»n truy cáº­p."}, status=403)
    if not exam_group.can_modify:
        return _locked_exam_group_mutation_response(exam_group)

    session_rooms = list(exam_group.session_rooms.all().order_by("display_order", "pk"))
    if not session_rooms:
        return JsonResponse({"success": False, "error": "Ca thi chÆ°a cÃ³ phÃ²ng Ä‘á»ƒ random."}, status=400)

    all_students = []
    for sr in session_rooms:
        all_students.extend(list(sr.students.all()))
        sr.students.clear()

    if not all_students:
        return JsonResponse({"success": False, "error": "KhÃ´ng cÃ³ sinh viÃªn nÃ o Ä‘á»ƒ random."}, status=400)

    random.shuffle(all_students)
    for index, student in enumerate(all_students):
        session_rooms[index % len(session_rooms)].students.add(student)

    messages.success(request, "ÄÃ£ random sinh viÃªn vÃ o cÃ¡c phÃ²ng thi thÃ nh cÃ´ng.")
    return JsonResponse({"success": True})


__all__ = [
    "lecturer_create_exam_group",
    "lecturer_create_exam_group_screen",
    "lecturer_create_exam_session",
    "lecturer_create_session_screen",
    "lecturer_delete_exam_group",
    "lecturer_exam_session_common_list_export",
    "lecturer_exam_session_common_list_screen",
    "lecturer_exam_session_detail_screen",
    "lecturer_exam_sessions_list",
    "lecturer_import_students_to_room",
    "lecturer_random_assign_students",
    "lecturer_update_exam_group",
]


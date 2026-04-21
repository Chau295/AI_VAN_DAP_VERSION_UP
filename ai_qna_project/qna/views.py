# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import csv
import io
import json
import logging
import os
import posixpath
import random
import re
import subprocess
import tempfile
import unicodedata
from base64 import b64encode
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from uuid import uuid4
from django.conf import settings

import fitz  # PyMuPDF
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, Max, Q, Sum
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_GET, require_POST
from docx import Document
from openai import AuthenticationError, OpenAI
from openpyxl import Workbook as OpenpyxlWorkbook
from openpyxl import load_workbook

from .academic_year import (
    ACADEMIC_YEAR_ERROR_MESSAGE,
    is_valid_academic_year,
    sanitize_academic_year_for_display,
)
from .exam_guards import subject_has_active_exam_group
from .exam_runtime import (
    EXAM_GRADING_STATUS_ERROR,
    EXAM_GRADING_STATUS_PENDING,
    EXAM_GRADING_STATUS_PROGRESS,
    EXAM_GRADING_STATUS_RESULT,
    LECTURER_HIDDEN_VIOLATION_TYPES,
    get_exam_grading_state,
    is_exam_group_open,
    is_exam_group_submission_window_open,
    should_mark_lecturer_cheating_flag,
)
from .forms import LecturerManualQuestionForm, LecturerMaterialUploadForm
from .models import (
    DifficultyLevel,
    ExamCode,
    ExamCodeQuestion,
    ExamResult,
    ExamRoom,
    ExamSession,
    ExamSessionGroup,
    ExamSessionRoom,
    ExamSet,
    ExamSetStatus,
    LectureMaterial,
    Question,
    QuestionBank,
    SemesterChoices,
    StudentListUploadStatus,
    StudentRosterAccountStatus,
    StudentRosterStudent,
    StudentRosterUpload,
    Subject,
    UserProfile,
    ViolationImage,
    normalize_question_text,
    resolve_uploaded_audio_extension,
)
from .student_accounts import normalize_student_code

logger = logging.getLogger(__name__)
User = get_user_model()
TEN_POINT_SCALE = 10.0
PASSWORD_ATTEMPT_LIMIT = 5
PASSWORD_ATTEMPT_WINDOW = 300
LIST_PAGE_SIZE = 8


# =========================================================
# COMMON HELPERS
# =========================================================
def _ensure_lecturer(request):
    profile = getattr(request.user, "userprofile", None)
    if not profile or not profile.is_lecturer:
        raise PermissionDenied("Không có quyền truy cập.")


def _get_lecturer_subjects(request):
    return request.user.userprofile.subjects_taught.all().order_by("name")


def _get_selected_subject_for_lecturer(request, subject_id):
    subjects = _get_lecturer_subjects(request)
    if subject_id:
        return get_object_or_404(subjects, pk=subject_id)
    return None


def _json_body(request: HttpRequest) -> Dict[str, Any]:
    try:
        if request.body:
            return json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return {}


def _is_ajax_request(request):
    return (
            request.headers.get("x-requested-with") == "XMLHttpRequest"
            or "application/json" in request.headers.get("accept", "")
    )


def _get_workspace_id(request, payload=None):
    payload = payload or {}
    workspace_id = (
            request.GET.get("workspace_id")
            or request.POST.get("workspace_id")
            or payload.get("workspace_id")
            or ""
    )
    return str(workspace_id).strip()


def _extract_ffmpeg_duration_seconds(metadata_text: str) -> float:
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", metadata_text or "")
    if not match:
        return 0.0

    hours, minutes, seconds = match.groups()
    return (int(hours) * 3600) + (int(minutes) * 60) + float(seconds)


def _probe_audio_duration_seconds(audio_path: str, *, session_id: str, question_id: str) -> float:
    ffprobe_binary = getattr(settings, "FFPROBE_BINARY", "ffprobe")
    ffmpeg_binary = getattr(settings, "FFMPEG_BINARY", "ffmpeg")
    ffprobe_command = [
        ffprobe_binary,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        audio_path,
    ]

    try:
        completed = subprocess.run(ffprobe_command, check=True, capture_output=True, text=True)
        duration = float((completed.stdout or "").strip() or 0)
        logger.info(
            "Validated exam mp3 with ffprobe: session_id=%s question_id=%s output_path=%s duration_seconds=%.3f",
            session_id,
            question_id,
            audio_path,
            duration,
        )
        return duration
    except (subprocess.CalledProcessError, ValueError) as exc:
        stderr = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        logger.warning(
            "FFprobe failed reading exam mp3 metadata, fallback to ffmpeg: session_id=%s question_id=%s output_path=%s stderr=%s",
            session_id,
            question_id,
            audio_path,
            stderr,
        )
    except FileNotFoundError:
        logger.warning(
            "FFprobe binary not found, fallback to ffmpeg: session_id=%s question_id=%s binary=%s",
            session_id,
            question_id,
            ffprobe_binary,
        )

    ffmpeg_command = [ffmpeg_binary, "-hide_banner", "-i", audio_path]
    try:
        completed = subprocess.run(ffmpeg_command, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        logger.error("FFmpeg binary not found while validating exam mp3: %s", ffmpeg_binary)
        raise RuntimeError("ffmpeg_missing") from exc

    metadata_text = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
    duration = _extract_ffmpeg_duration_seconds(metadata_text)
    logger.info(
        "Validated exam mp3 with ffmpeg metadata: session_id=%s question_id=%s output_path=%s duration_seconds=%.3f",
        session_id,
        question_id,
        audio_path,
        duration,
    )
    return duration


def _validate_generated_mp3_file(audio_path: str, *, session_id: str, question_id: str) -> Dict[str, float]:
    if not os.path.exists(audio_path):
        raise RuntimeError("mp3_output_missing")

    output_size = os.path.getsize(audio_path)
    duration_seconds = _probe_audio_duration_seconds(
        audio_path,
        session_id=session_id,
        question_id=question_id,
    )

    logger.info(
        "Converted exam audio output: session_id=%s question_id=%s output_path=%s output_size=%s duration_seconds=%.3f",
        session_id,
        question_id,
        audio_path,
        output_size,
        duration_seconds,
    )

    if output_size <= 0:
        raise RuntimeError("mp3_output_empty")
    if duration_seconds <= 0:
        raise RuntimeError("mp3_output_invalid_duration")

    return {
        "output_size": output_size,
        "duration_seconds": duration_seconds,
    }


def _convert_uploaded_audio_to_mp3(*, audio_file, session_id: str, question_id: str) -> Dict[str, Any]:
    uploaded_file_name = getattr(audio_file, "name", "")
    uploaded_content_type = getattr(audio_file, "content_type", "")
    source_extension = resolve_uploaded_audio_extension(uploaded_file_name, uploaded_content_type)
    ffmpeg_binary = getattr(settings, "FFMPEG_BINARY", "ffmpeg")
    target_file_name = f"audio_{session_id}_{question_id}.mp3"

    with tempfile.TemporaryDirectory(prefix="exam_audio_upload_") as temp_dir:
        source_path = os.path.join(temp_dir, f"source{source_extension}")
        target_path = os.path.join(temp_dir, target_file_name)

        if hasattr(audio_file, "seek"):
            audio_file.seek(0)

        with open(source_path, "wb") as source_handle:
            for chunk in audio_file.chunks():
                source_handle.write(chunk)

        logger.info(
            "Saved temporary exam audio source: session_id=%s question_id=%s source_path=%s content_type=%s",
            session_id,
            question_id,
            source_path,
            uploaded_content_type,
        )

        command = [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            source_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "44100",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "192k",
            target_path,
        ]

        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            logger.error(
                "FFmpeg failed converting exam audio to mp3: session_id=%s question_id=%s stderr=%s",
                session_id,
                question_id,
                exc.stderr,
            )
            raise RuntimeError("ffmpeg_failed") from exc
        except FileNotFoundError as exc:
            logger.error("FFmpeg binary not found for exam audio conversion: %s", ffmpeg_binary)
            raise RuntimeError("ffmpeg_missing") from exc

        validation_meta = _validate_generated_mp3_file(
            target_path,
            session_id=session_id,
            question_id=question_id,
        )

        with open(target_path, "rb") as target_handle:
            mp3_content = target_handle.read()

    if hasattr(audio_file, "seek"):
        audio_file.seek(0)

    if not mp3_content:
        raise RuntimeError("mp3_output_empty")

    return {
        "content": mp3_content,
        "stored_name": target_file_name,
        "source_extension": source_extension,
        "source_content_type": uploaded_content_type,
        "output_size": validation_meta["output_size"],
        "duration_seconds": validation_meta["duration_seconds"],
    }


def _get_audio_upload_error_message(exc: Exception) -> str:
    error_code = str(exc)
    if error_code == "ffmpeg_missing":
        return "Máy chủ chưa sẵn sàng xử lý file ghi âm MP3."
    if error_code in {"mp3_output_missing", "mp3_output_empty", "mp3_output_invalid_duration"}:
        return "File MP3 tạo ra không hợp lệ hoặc không có dữ liệu âm thanh."
    return "Không thể chuyển đổi file ghi âm sang MP3."


def _get_field_file_path(field_file) -> str:
    if not field_file:
        return ""
    try:
        return field_file.path
    except (NotImplementedError, ValueError):
        return ""


def _ensure_exam_result_audio_access(request: HttpRequest, result: ExamResult) -> None:
    profile = getattr(request.user, "userprofile", None)
    session = result.exam_session_id

    if profile and getattr(profile, "is_lecturer", False):
        if not profile.subjects_taught.filter(pk=session.subject_id_id).exists():
            raise PermissionDenied("Bạn không có quyền truy cập file ghi âm này.")
        return

    _ensure_owner(session, request.user)


def _draft_prefix(workspace_id: str) -> str:
    return f"DRAFT_{workspace_id}_" if workspace_id else "DRAFT_"


def _ensure_owner(session: ExamSession, user: User) -> None:
    if getattr(session.user_id, "pk", None) != getattr(user, "pk", None):
        raise PermissionDenied("Bạn không có quyền truy cập phiên thi này.")


def _get_avatar_data_url(profile: UserProfile) -> str:
    if profile.profile_image_blob:
        try:
            return (
                f"data:{profile.profile_image_mime or 'image/jpeg'};base64,"
                f"{b64encode(profile.profile_image_blob).decode('ascii')}"
            )
        except Exception:
            pass
    # Use logo.jpg as fallback avatar if default not found
    return static("images/logo.jpg")

def _build_image_data_url(blob, mime):
    if not blob or not mime:
        return None
    try:
        return f"data:{mime};base64,{b64encode(blob).decode('ascii')}"
    except Exception:
        return None

def _build_image_data_url(blob, mime) -> str | None:
    if not blob or not mime:
        return None
    try:
        return f"data:{mime};base64,{b64encode(blob).decode('ascii')}"
    except Exception:
        return None


def _question_exists_in_subject(subject, question_text, exclude_question_id=None):
    normalized = normalize_question_text(question_text)
    queryset = Question.objects.filter(
        subject_id=subject,
        question_text_normalized=normalized,
        is_exam_clone=False,
    )
    if exclude_question_id:
        queryset = queryset.exclude(pk=exclude_question_id)
    return queryset.exists()


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
        room for room in rooms if room.exam_session_group_id.computed_status == "COMPLETED"
    ]
    if completed_rooms:
        return max(completed_rooms, key=lambda room: room.exam_session_group_id.end_at)

    cancelled_rooms = [
        room for room in rooms if room.exam_session_group_id.computed_status == "CANCELLED"
    ]
    if cancelled_rooms:
        return max(cancelled_rooms, key=lambda room: room.exam_session_group_id.start_at)

    return rooms[0]


def _get_student_active_room_for_subject(user, subject):
    return _get_student_current_room_for_subject(user, subject)


def _is_exam_group_open(exam_group):
    return is_exam_group_open(exam_group)


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


def _subject_has_ongoing_exam_group(subject):
    return subject_has_active_exam_group(subject, now=timezone.now())


def _get_student_exam_access_context(user, subject):
    current_room = _get_student_current_room_for_subject(user, subject)
    display_room = current_room or _get_student_display_room_for_subject(user, subject)
    exam_group = display_room.exam_session_group_id if display_room else None
    return current_room, display_room, exam_group


def _student_exam_access_denied_message(display_room, *, no_room_message):
    return "Chưa đến giờ hoặc đã hết giờ thi." if display_room else no_room_message


def _locked_subject_mutation_response(response_style="status"):
    message = "Không thể chỉnh sửa khi ca thi đang diễn ra."
    if response_style == "legacy":
        return JsonResponse({"success": False, "error": message}, status=403)
    return JsonResponse({"status": "FAIL", "message": message}, status=403)


def _get_room_assignment_for_user(exam_group, user):
    if not exam_group or not user:
        return None, None

    config = exam_group.configuration_data or {}
    room_configs = config.get("rooms") or []
    if not isinstance(room_configs, list) or not room_configs:
        from .session_management import _legacy_room_configuration

        room_configs = _legacy_room_configuration(exam_group)

    for room_cfg in room_configs:
        for assignment in room_cfg.get("assignments") or []:
            if str(assignment.get("linked_user_id")) == str(user.id):
                return room_cfg, assignment
    return None, None


def _student_exam_entry_session_key(exam_group_id):
    return f"exam_entry_session_{exam_group_id}"


def _student_exam_password_session_key(exam_group_id):
    return f"exam_password_session_{exam_group_id}"


def _room_password_attempts_key(user_id: int, subject_code: str) -> str:
    return f"room_pwd_attempts:{user_id}:{subject_code}"


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


def _clear_student_exam_entry_payload(request: HttpRequest, exam_group: ExamSessionGroup | None) -> None:
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


def _student_exam_reentry_state(session: ExamSession | None) -> str | None:
    if not session:
        return None
    if _is_session_finalized(session):
        return "COMPLETED"
    if _student_has_started_exam(session):
        return "STARTED"
    return None


def _redirect_locked_student_exam(request: HttpRequest, session: ExamSession | None) -> HttpResponse | None:
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
        return JsonResponse({"status": "error", "message": "Bài thi này đã hoàn thành."}, status=400)
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


def _is_ten_point_scale(raw_total_score: float | None) -> bool:
    return raw_total_score is not None and abs(float(raw_total_score) - TEN_POINT_SCALE) < 0.001


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


def _build_exam_page_existing_results(session: ExamSession) -> list[dict[str, Any]]:
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


def _sync_session_completion_flags(session: ExamSession) -> None:
    update_fields = []

    if getattr(session, "session_status", None) != "COMPLETED":
        session.session_status = "COMPLETED"
        update_fields.append("session_status")

    if getattr(session, "completed_at", None) is None:
        session.completed_at = _derive_session_completion_time(session)
        update_fields.append("completed_at")

    if update_fields:
        session.save(update_fields=update_fields)


def _finalize_started_exam_session(session: ExamSession | None) -> bool:
    if not session or not _student_has_started_exam(session):
        return False

    if not _is_session_finalized(session):
        _sync_session_completion_flags(session)

    _, final_total = _compute_scores(session)
    final_total = round(float(final_total or 0), 2)
    if session.final_score != final_total:
        session.final_score = final_total
        session.save(update_fields=["final_score"])

    return True

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
# =========================================================
# AUTH / ROOT
# =========================================================
@login_required
def post_login_redirect(request):
    if not hasattr(request.user, "userprofile"):
        UserProfile.objects.create(user_id=request.user)

    if request.user.userprofile.is_lecturer:
        return redirect("qna:lecturer_dashboard")
    return redirect("qna:dashboard")


@login_required
def root_redirect(request):
    return post_login_redirect(request)


# =========================================================
# Dashboard giảng viên
# =========================================================
@login_required
def lecturer_dashboard(request):
    if not request.user.userprofile.is_lecturer:
        return redirect("qna:dashboard")

    subjects = request.user.userprofile.subjects_taught.annotate(
        created_exam_count=Count("exam_session_groups", distinct=True),
        participating_student_count=Count(
            "student_roster_uploads__students__student_code",
            distinct=True,
        ),
    ).order_by("name")

    selected_subject_id = (request.GET.get("subject_id") or "").strip()
    recent_page_number = request.GET.get("recent_page")

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
def lecturer_subject_list(request):
    if not request.user.userprofile.is_lecturer:
        return redirect("qna:dashboard")
    return redirect("qna:lecturer_dashboard")


@login_required
def lecturer_subject_workspace(request, subject_code):
    if not request.user.userprofile.is_lecturer:
        return redirect("qna:dashboard")

    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        subject_code=subject_code,
    )

    upcoming_page_number = request.GET.get("upcoming_page")

    sessions_in_subject = ExamSession.objects.filter(subject_id=subject)
    total_exams = sessions_in_subject.count()
    completed_exams = sessions_in_subject.filter(session_status="COMPLETED").count()
    total_students = sessions_in_subject.values("user_id").distinct().count()

    approved_codes = ExamCode.objects.filter(subject_id=subject, is_approved=True).count()

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
def lecturer_subject_dashboard(request, subject_code):
    if not request.user.userprofile.is_lecturer:
        return redirect("qna:dashboard")

    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
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
def lecturer_profile_view(request: HttpRequest) -> HttpResponse:
    if not request.user.userprofile.is_lecturer:
        return redirect("qna:profile")

    profile = request.user.userprofile
    avatar_url = _get_avatar_data_url(profile)

    context = {
        "user": request.user,
        "profile": profile,
        "avatar_url": avatar_url,
        "subjects_taught": profile.subjects_taught.all().order_by("subject_code"),
    }
    return render(request, "qna/lecturer/lecturer_profile.html", context)


# =========================================================
# Câu hỏi - CRUD cũ
# =========================================================
@login_required
@require_POST
def lecturer_create_question(request):
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({"success": False, "error": "Không có quyền truy cập"}, status=403)

    subject_id = request.POST.get("subject_id")
    question_text = (request.POST.get("question_text") or "").strip()
    difficulty = request.POST.get("difficulty")

    if not all([subject_id, question_text, difficulty]):
        return JsonResponse({"success": False, "error": "Thiếu thông tin bắt buộc"}, status=400)

    subject = get_object_or_404(request.user.userprofile.subjects_taught, pk=subject_id)
    if _subject_has_ongoing_exam_group(subject):
        return _locked_subject_mutation_response(response_style="legacy")

    if _question_exists_in_subject(subject, question_text):
        return JsonResponse(
            {"success": False, "error": "Câu hỏi bị trùng trong môn học này."},
            status=400,
        )

    question = Question.objects.create(
        subject_id=subject,
        question_text=question_text,
        difficulty=difficulty,
        question_id_in_barem=f"Q_{subject.subject_code}_{uuid4().hex[:8]}",
    )

    messages.success(request, "Đã tạo câu hỏi thành công.")
    return JsonResponse({"success": True, "question_id": question.id})


@login_required
@require_POST
def lecturer_update_question(request, question_id):
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({"success": False, "error": "Không có quyền truy cập"}, status=403)

    question = get_object_or_404(Question, pk=question_id)
    if question.is_exam_clone:
        return JsonResponse(
            {"status": "FAIL", "message": "Không thể sửa câu hỏi đang được sử dụng trong mã đề."},
            status=400,
        )
    if question.subject not in request.user.userprofile.subjects_taught.all():
        return JsonResponse({"success": False, "error": "Không có quyền truy cập môn học này"}, status=403)
    if _subject_has_ongoing_exam_group(question.subject):
        return _locked_subject_mutation_response(response_style="legacy")

    question_text = (request.POST.get("question_text") or "").strip()
    difficulty = request.POST.get("difficulty")

    if question_text and _question_exists_in_subject(question.subject, question_text, exclude_question_id=question.id):
        return JsonResponse(
            {"success": False, "error": "Câu hỏi bị trùng trong môn học này."},
            status=400,
        )

    if question_text:
        question.question_text = question_text
    if difficulty:
        question.difficulty = difficulty
    question.save()

    messages.success(request, "Đã cập nhật câu hỏi thành công.")
    return JsonResponse({"success": True})


@login_required
@require_POST
def lecturer_delete_question(request, question_id):
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({"success": False, "error": "Không có quyền truy cập"}, status=403)

    question = get_object_or_404(Question, pk=question_id)
    if question.is_exam_clone:
        return JsonResponse(
            {"status": "FAIL", "message": "Không thể xóa câu hỏi đang được sử dụng trong mã đề."},
            status=400,
        )
    if question.subject not in request.user.userprofile.subjects_taught.all():
        return JsonResponse({"success": False, "error": "Không có quyền truy cập môn học này"}, status=403)
    if _subject_has_ongoing_exam_group(question.subject):
        return _locked_subject_mutation_response(response_style="legacy")

    question.delete()
    messages.success(request, "Đã xóa câu hỏi thành công.")
    return JsonResponse({"success": True})


@login_required
@require_POST
def lecturer_import_questions(request):
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({"success": False, "error": "Không có quyền truy cập"}, status=403)

    subject_id = request.POST.get("subject_id")
    file_obj = request.FILES.get("file")
    if not subject_id or not file_obj:
        return JsonResponse({"success": False, "error": "Thiếu thông tin"}, status=400)

    subject = get_object_or_404(request.user.userprofile.subjects_taught, pk=subject_id)
    if _subject_has_ongoing_exam_group(subject):
        return _locked_subject_mutation_response(response_style="legacy")

    try:
        decoded_file = file_obj.read().decode("utf-8-sig")
        io_string = io.StringIO(decoded_file)
        reader = csv.DictReader(io_string)

        imported_count = 0
        duplicated_count = 0
        invalid_count = 0

        for row in reader:
            question_text = _extract_import_question_text(row)
            difficulty = _normalize_import_difficulty(row.get("difficulty"))
            if not question_text or difficulty is None:
                invalid_count += 1
                continue

            if _question_exists_in_subject(subject, question_text):
                duplicated_count += 1
                continue

            Question.objects.create(
                subject_id=subject,
                question_text=question_text,
                difficulty=difficulty,
                question_id_in_barem=f"IMP_{subject.subject_code}_{uuid4().hex[:8]}",
            )
            imported_count += 1

        messages.success(
            request,
            f"Đã import {imported_count} câu hỏi thành công. Bỏ qua {duplicated_count} câu bị trùng và {invalid_count} dòng không hợp lệ.",
        )
        return JsonResponse(
            {
                "success": True,
                "imported_count": imported_count,
                "duplicated_count": duplicated_count,
                "invalid_count": invalid_count,
            }
        )
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# =========================================================
# QUESTION BANK MANAGEMENT
# =========================================================
QUESTION_JOB_PREFIX = "qna_question_job_"
QUESTION_JOB_TIMEOUT = 60 * 30
QUESTION_JOB_LOCAL_FALLBACK = {}
QUESTION_JOB_LOCAL_FALLBACK_LOCK = Lock()
QUESTION_BANK_DUPLICATE_NAME_MESSAGE = "Tên ngân hàng đã tồn tại trong môn học này."
QUESTION_BANK_NAME_REQUIRED_MESSAGE = "Tên ngân hàng không được để trống."


def _get_lecturer_question_banks(request):
    return QuestionBank.objects.filter(subject_id__in=_get_lecturer_subjects(request)).select_related("subject_id")


def _get_lecturer_question_banks_with_counts(request, subject=None):
    queryset = _get_lecturer_question_banks(request)
    if subject is not None:
        queryset = queryset.filter(subject_id=subject)
    return queryset.annotate(
        question_count_value=Count(
            "questions",
            filter=Q(questions__is_exam_clone=False),
            distinct=True,
        )
    )


def _serialize_question_bank_item(bank, request=None, subject=None):
    subject_obj = subject or getattr(bank, "subject_id", None)
    detail_url = None
    if request is not None and subject_obj is not None:
        detail_url = f"{request.path}?subject_id={subject_obj.pk}&bank_id={bank.pk}&mode=detail&view=bank"

    return {
        "bank_id": str(bank.pk),
        "name": bank.name,
        "bank_name": bank.name,
        "bank_type": "ORAL",
        "created_at": bank.created_at.strftime("%d/%m/%Y") if bank.created_at else "",
        "question_count": int(getattr(bank, "question_count_value", 0) or 0),
        "detail_url": detail_url,
    }


def _normalize_question_bank_name(raw_name):
    return (raw_name or "").strip()


def _question_bank_name_exists(subject, bank_name, exclude_bank_id=None):
    queryset = QuestionBank.objects.filter(subject_id=subject, name__iexact=bank_name)
    if exclude_bank_id is not None:
        queryset = queryset.exclude(pk=exclude_bank_id)
    return queryset.exists()


def _validate_question_bank_name(subject, raw_name, exclude_bank_id=None):
    bank_name = _normalize_question_bank_name(raw_name)
    if not bank_name:
        return "", QUESTION_BANK_NAME_REQUIRED_MESSAGE
    if _question_bank_name_exists(subject, bank_name, exclude_bank_id=exclude_bank_id):
        return bank_name, QUESTION_BANK_DUPLICATE_NAME_MESSAGE
    return bank_name, ""


def _serialize_material(material: LectureMaterial):
    return {
        "document_id": material.id,
        "file_name": material.title,
        "original_file_name": material.filename,
        "upload_time": material.uploaded_at.strftime("%d/%m/%Y"),
        "content_type": material.file_type,
        "extension": material.extension,
        "file_size": 0,
    }


def _serialize_question(question: Question):
    barem_id = question.question_id_in_barem or ""
    if "AI_" in barem_id:
        source_text = "AI tạo tự động"
    elif "IMP_" in barem_id:
        source_text = "Import từ file CSV/Excel"
    else:
        source_text = "Giảng viên thêm thủ công"

    time_str = ""
    if question.created_at:
        local_time = timezone.localtime(question.created_at)
        time_str = local_time.strftime("%H:%M - %d/%m/%Y")

    is_draft = barem_id.startswith("DRAFT_")

    return {
        "question_id": question.id,
        "content": question.question_text,
        "difficulty": question.difficulty,
        "difficulty_label": question.get_difficulty_display(),
        "source": source_text,
        "source_type": "AI" if "AI_" in barem_id else "MANUAL",
        "created_at": time_str,
        "updated_at": "",
        "is_draft": is_draft,
    }


def _extract_import_question_text(row):
    for key in ("question", "question_text", "content", "noi_dung", "cau_hoi"):
        value = (row.get(key) or "").strip()
        if value:
            return value
    return ""


def _normalize_import_difficulty(raw_value):
    value = unicodedata.normalize("NFKD", str(raw_value or "")).encode("ascii", "ignore").decode("ascii")
    normalized = value.strip().upper().replace(" ", "_")
    mapping = {
        "": DifficultyLevel.MEDIUM,
        "EASY": DifficultyLevel.EASY,
        "DE": DifficultyLevel.EASY,
        "MEDIUM": DifficultyLevel.MEDIUM,
        "TRUNG_BINH": DifficultyLevel.MEDIUM,
        "HARD": DifficultyLevel.HARD,
        "KHO": DifficultyLevel.HARD,
    }
    return mapping.get(normalized)


def _build_level_config(total_count, preset):
    total_count = int(total_count)
    if preset == "EASY":
        return {"easy": total_count, "medium": 0, "hard": 0}
    if preset == "MEDIUM":
        return {"easy": 0, "medium": total_count, "hard": 0}
    if preset == "HARD":
        return {"easy": 0, "medium": 0, "hard": total_count}

    easy = total_count // 3
    medium = total_count // 3
    hard = total_count - easy - medium
    return {"easy": easy, "medium": medium, "hard": hard}


def _question_job_cache_key(job_id):
    return f"{QUESTION_JOB_PREFIX}{job_id}"


def _purge_expired_local_question_jobs(now=None):
    now = now or timezone.now()
    with QUESTION_JOB_LOCAL_FALLBACK_LOCK:
        expired_keys = [
            key
            for key, entry in QUESTION_JOB_LOCAL_FALLBACK.items()
            if entry["expires_at"] <= now
        ]
        for key in expired_keys:
            QUESTION_JOB_LOCAL_FALLBACK.pop(key, None)


def _save_question_job(job_id, payload):
    cache_key = _question_job_cache_key(job_id)

    try:
        cache.set(cache_key, payload, timeout=QUESTION_JOB_TIMEOUT)
        with QUESTION_JOB_LOCAL_FALLBACK_LOCK:
            QUESTION_JOB_LOCAL_FALLBACK.pop(cache_key, None)
    except Exception as exc:
        logger.warning(
            "Question job cache unavailable for %s, using local fallback: %s",
            job_id,
            exc,
        )
        _purge_expired_local_question_jobs()
        with QUESTION_JOB_LOCAL_FALLBACK_LOCK:
            QUESTION_JOB_LOCAL_FALLBACK[cache_key] = {
                "payload": payload,
                "expires_at": timezone.now() + timedelta(seconds=QUESTION_JOB_TIMEOUT),
            }


def _get_question_job(job_id):
    cache_key = _question_job_cache_key(job_id)

    try:
        cached_job = cache.get(cache_key)
        if cached_job is not None:
            with QUESTION_JOB_LOCAL_FALLBACK_LOCK:
                QUESTION_JOB_LOCAL_FALLBACK.pop(cache_key, None)
            return cached_job
    except Exception as exc:
        logger.warning(
            "Question job cache read failed for %s, checking local fallback: %s",
            job_id,
            exc,
        )

    _purge_expired_local_question_jobs()
    with QUESTION_JOB_LOCAL_FALLBACK_LOCK:
        fallback_job = QUESTION_JOB_LOCAL_FALLBACK.get(cache_key)
        if fallback_job:
            return fallback_job["payload"]
    return None


# =========================================================
# ĐỌC FILE TÀI LIỆU
# =========================================================
def _read_txt_file(file_path: str) -> str:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        with open(file_path, "r", encoding="latin-1") as f:
            return f.read()
    except Exception as exc:
        logger.exception("Không đọc được TXT: %s", exc)
        return ""


def _read_docx_file(file_path: str) -> str:
    try:
        doc = Document(file_path)
        paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs)
    except Exception as exc:
        logger.exception("Không đọc được DOCX: %s", exc)
        return ""


def _read_pdf_file(file_path: str) -> str:
    try:
        # Sử dụng PyMuPDF (fitz) để đọc text từ PDF, kể cả dạng slide.
        pdf_document = fitz.open(file_path)
        texts = []
        for page_num in range(pdf_document.page_count):
            try:
                page = pdf_document.load_page(page_num)
                # get_text("text") giúp lấy text bảo toàn bộ cục tốt hơn
                page_text = page.get_text("text") or ""
                if page_text.strip():
                    texts.append(page_text.strip())
            except Exception:
                continue
        return "\n".join(texts)
    except Exception as exc:
        logger.exception("Không đọc được PDF: %s", exc)
        return ""


def _extract_text_from_material(material: LectureMaterial) -> str:
    file_path = getattr(material, "file_path", "") or ""
    if not file_path or not os.path.exists(file_path):
        return ""

    ext = Path(file_path).suffix.lower()

    if ext == ".txt":
        return _read_txt_file(file_path)
    if ext == ".docx":
        return _read_docx_file(file_path)
    if ext == ".pdf":
        return _read_pdf_file(file_path)

    return ""


def _truncate_text_for_llm(text: str, max_chars: int = 18000) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def _build_material_context(materials) -> str:
    blocks = []
    for idx, material in enumerate(materials, start=1):
        text = _extract_text_from_material(material)
        text = _truncate_text_for_llm(text, max_chars=12000)

        if text.strip():
            blocks.append(
                f"TÀI LIỆU {idx}: {material.title}\n"
                f"NỘI DUNG:\n{text}"
            )
    return "\n\n" + ("\n\n" + "=" * 80 + "\n\n").join(blocks)


def _normalize_requested_question_counts(total_count: int, level_config: dict) -> dict[str, int]:
    level_config = level_config or {}
    total_count = int(total_count or 0)

    easy_count = max(int(level_config.get("easy", 0) or 0), 0)
    medium_count = max(int(level_config.get("medium", 0) or 0), 0)
    hard_count = max(int(level_config.get("hard", 0) or 0), 0)

    if easy_count + medium_count + hard_count != total_count:
        raise ValueError("Phân bổ độ khó không khớp với tổng số lượng câu hỏi yêu cầu.")

    return {
        "EASY": easy_count,
        "MEDIUM": medium_count,
        "HARD": hard_count,
    }


def _question_generation_level_config(target_counts: dict[str, int]) -> dict[str, int]:
    return {
        "easy": int(target_counts.get("EASY", 0) or 0),
        "medium": int(target_counts.get("MEDIUM", 0) or 0),
        "hard": int(target_counts.get("HARD", 0) or 0),
    }


def _remaining_question_counts(target_counts: dict[str, int], questions) -> dict[str, int]:
    remaining_counts = dict(target_counts)
    for item in questions:
        difficulty = item.get("difficulty")
        if difficulty in remaining_counts and remaining_counts[difficulty] > 0:
            remaining_counts[difficulty] -= 1
    return remaining_counts


def _normalize_ai_generated_questions(raw_questions, excluded_question_keys=None):
    excluded_question_keys = set(excluded_question_keys or [])
    seen_question_keys = set(excluded_question_keys)
    normalized_questions = []

    for item in raw_questions or []:
        if not isinstance(item, dict):
            continue

        content = (item.get("content") or "").strip()
        if not content:
            continue

        content_key = normalize_question_text(content)
        if not content_key or content_key in seen_question_keys:
            continue

        difficulty = (item.get("difficulty") or "MEDIUM").strip().upper()
        if difficulty not in {"EASY", "MEDIUM", "HARD"}:
            difficulty = "MEDIUM"

        seen_question_keys.add(content_key)
        normalized_questions.append(
            {
                "content": content,
                "difficulty": difficulty,
                "source": (item.get("source") or "").strip(),
            }
        )

    return normalized_questions


class QuestionGenerationEmptyError(Exception):
    pass


def _build_question_shortage(remaining_counts: dict[str, int]) -> tuple[dict[str, int], str]:
    shortage = {
        difficulty: int(count or 0)
        for difficulty, count in (remaining_counts or {}).items()
        if int(count or 0) > 0
    }
    shortage_text = ", ".join(f"{difficulty}: {count}" for difficulty, count in shortage.items())
    return shortage, shortage_text


def _build_question_generation_message(
        created_count: int,
        requested_count: int,
        skipped_duplicates: int = 0,
        shortage_text: str = "",
) -> tuple[str, str]:
    shortage_message = ""
    if shortage_text:
        shortage_message = f"AI chỉ tạo được {created_count}/{requested_count} câu."
        shortage_message = f"{shortage_message} Còn thiếu: {shortage_text}."
        message = f"Tạo câu hỏi một phần. {shortage_message}"
    else:
        message = "Tạo câu hỏi thành công."

    if skipped_duplicates > 0:
        message = f"{message} Hệ thống đã tự sinh bù {skipped_duplicates} câu bị trùng."

    return message, shortage_message


def _generate_questions_from_ai(
        subject: Subject,
        materials,
        total_count: int,
        level_config: dict,
        excluded_question_texts=None,
):
    api_key = getattr(settings, "OPENAI_API_KEY", None)
    if not api_key:
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise Exception("Thiếu OPENAI_API_KEY trong settings.py hoặc biến môi trường")

    context_text = _build_material_context(materials)
    if not context_text.strip():
        raise Exception("Không trích xuất được nội dung từ tài liệu tải lên.")

    target_counts = _normalize_requested_question_counts(total_count, level_config)

    client = OpenAI(api_key=api_key)

    excluded_question_texts = [
        (text or "").strip()
        for text in (excluded_question_texts or [])
        if (text or "").strip()
    ]
    excluded_question_keys = {
        normalize_question_text(text)
        for text in excluded_question_texts
        if normalize_question_text(text)
    }

    system_prompt = """
    Bạn là trợ lý tạo câu hỏi vấn đáp cho giảng viên đại học.

    Nhiệm vụ:
    - Chỉ được tạo câu hỏi dựa trên nội dung tài liệu được cung cấp.
    - Không bịa thêm kiến thức ngoài tài liệu.
    - Câu hỏi phải rõ ràng, đúng ngữ cảnh môn học.
    - Câu hỏi dạng vấn đáp, phù hợp để giảng viên hỏi sinh viên trực tiếp.
    - Không được tạo câu hỏi trùng ý nhau quá nhiều.
    - Phân loại đúng mức độ EASY / MEDIUM / HARD.

    Mức độ:
    - EASY: hỏi khái niệm, định nghĩa, trình bày cơ bản
    - MEDIUM: hỏi phân tích, so sánh, giải thích, liên hệ
    - HARD: hỏi vận dụng, đánh giá, tình huống, lập luận sâu

    Đầu ra:
    Trả về JSON hợp lệ theo đúng format:
    {
      "questions": [
        {
          "content": "....",
          "difficulty": "EASY",
          "source": "Tên tài liệu liên quan"
        }
      ]
    }
    Không thêm markdown, không thêm giải thích ngoài JSON.
    Phải tuân thủ chính xác số lượng từng mức độ được yêu cầu.
    Không được trả về câu hỏi trùng lặp hoặc gần như trùng lặp.
    """

    collected_questions = []
    collected_question_keys = set()

    for _ in range(3):
        remaining_counts = _remaining_question_counts(target_counts, collected_questions)
        remaining_total = sum(remaining_counts.values())
        if remaining_total <= 0:
            break

        excluded_items = list(
            dict.fromkeys(excluded_question_texts + [item["content"] for item in collected_questions])
        )[-20:]
        excluded_block = ""
        if excluded_items:
            excluded_block = "\n\nCác câu không được lặp lại:\n" + "\n".join(
                f"- {item}" for item in excluded_items
            )

        user_prompt = f"""
Môn học: {subject.name}
Mã môn: {subject.subject_code}

        Hãy tạo chính xác {remaining_total} câu hỏi vấn đáp mới dựa trên nội dung tài liệu bên dưới.

        Yêu cầu số lượng bắt buộc:
- EASY: {remaining_counts['EASY']}
- MEDIUM: {remaining_counts['MEDIUM']}
- HARD: {remaining_counts['HARD']}

        Yêu cầu chất lượng:
        - Câu hỏi phải bám sát tài liệu
- Câu hỏi ngắn gọn, rõ ràng, có chiều sâu
- Không lặp lại nội dung giữa các câu
- Tổng số phần tử trong mảng questions phải đúng bằng {remaining_total}
- Không được trả về câu hỏi thừa so với từng mức độ yêu cầu{excluded_block}

TÀI LIỆU:
{context_text}
"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw)
        questions = data.get("questions", [])
        if not isinstance(questions, list):
            questions = []

        normalized_questions = _normalize_ai_generated_questions(
            questions,
            excluded_question_keys=excluded_question_keys | collected_question_keys,
        )

        for item in normalized_questions:
            difficulty = item["difficulty"]
            if remaining_counts.get(difficulty, 0) <= 0:
                continue
            collected_questions.append(item)
            collected_question_keys.add(normalize_question_text(item["content"]))
            remaining_counts[difficulty] -= 1

    remaining_counts = _remaining_question_counts(target_counts, collected_questions)
    shortage, shortage_text = _build_question_shortage(remaining_counts)

    return {
        "questions": collected_questions,
        "remaining_counts": remaining_counts,
        "is_partial": bool(shortage),
        "shortage": shortage,
        "shortage_text": shortage_text,
    }


# =========================================================
# QUESTION BANK SCREEN
# =========================================================
@login_required
def lecturer_questions_screen(request):
    _ensure_lecturer(request)

    subjects = _get_lecturer_subjects(request)
    subject_id = request.GET.get("subject_id")
    bank_id = request.GET.get("bank_id")
    page_mode = request.GET.get("mode", "list")
    view_type = request.GET.get("view", "bank")
    workspace_id = _get_workspace_id(request)

    selected_subject = _get_selected_subject_for_lecturer(request, subject_id)
    question_banks = []
    documents = []
    selected_bank_name = "Ngân hàng câu hỏi"

    if selected_subject:
        banks = _get_lecturer_question_banks_with_counts(request, selected_subject).order_by("-created_at")
        question_banks = [
            _serialize_question_bank_item(b, request=request, subject=selected_subject)
            for b in banks
        ]

        if page_mode == "detail":
            if view_type == "generate" and workspace_id:
                documents = LectureMaterial.objects.filter(
                    subject_id=selected_subject,
                    workspace_id=workspace_id,
                ).order_by("-uploaded_at")
            elif view_type == "bank" and bank_id:
                bank_obj = _get_lecturer_question_banks(request).filter(
                    subject_id=selected_subject,
                    pk=bank_id,
                ).first()
                if bank_obj:
                    selected_bank_name = bank_obj.name
                    documents = LectureMaterial.objects.filter(
                        subject_id=selected_subject,
                        question_bank_id=bank_obj,
                    ).order_by("-uploaded_at")

    context = {
        "subjects": subjects,
        "selected_subject": selected_subject,
        "selected_bank_id": bank_id,
        "selected_bank_name": selected_bank_name,
        "page_mode": page_mode,
        "view_type": view_type,
        "workspace_id": workspace_id,
        "question_banks": question_banks,
        "documents": [
            {
                "id": d.id,
                "file_name": d.title,
                "upload_time": d.uploaded_at.strftime("%d/%m/%Y"),
            }
            for d in documents
        ] if page_mode == "detail" else [],
        "upload_entry_url": (
            f"{request.path}?subject_id={selected_subject.id}&mode=detail&view=generate"
            if selected_subject else "#"
        ),
        "has_active_exam": subject_has_active_exam_group(selected_subject) if selected_subject else False,
    }
    return render(request, "qna/lecturer/lecturer_question_management.html", context)


@login_required
def question_bank_list_screen(request):
    return lecturer_questions_screen(request)


@login_required
def question_bank_detail_screen(request):
    if "mode" not in request.GET:
        q = request.GET.copy()
        q["mode"] = "detail"
        return redirect(f"{request.path}?{q.urlencode()}")
    return lecturer_questions_screen(request)


@login_required
@require_GET
def api_get_lecturer_subjects(request):
    _ensure_lecturer(request)
    subjects = _get_lecturer_subjects(request)
    return JsonResponse(
        {
            "status": "SUCCESS",
            "subjects": [{"subject_id": str(item.id), "subject_name": item.name} for item in subjects],
        }
    )


@login_required
@require_GET
def api_get_question_banks(request):
    _ensure_lecturer(request)
    subject_id = request.GET.get("subject_id")
    if not subject_id:
        return JsonResponse({"status": "FAIL", "message": "Thiếu subject_id."}, status=400)

    subject = get_object_or_404(_get_lecturer_subjects(request), pk=subject_id)
    banks = _get_lecturer_question_banks_with_counts(request, subject).order_by("-created_at")

    return JsonResponse(
        {
            "status": "SUCCESS",
            "question_banks": [_serialize_question_bank_item(b) for b in banks],
        }
    )


@login_required
@require_POST
def api_create_question_bank(request):
    _ensure_lecturer(request)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    subject_id = payload.get("subject_id")
    if not subject_id:
        return JsonResponse({"status": "FAIL", "message": "Thiếu subject_id."}, status=400)

    subject = get_object_or_404(_get_lecturer_subjects(request), pk=subject_id)
    if _subject_has_ongoing_exam_group(subject):
        return _locked_subject_mutation_response()
    bank_name, error_message = _validate_question_bank_name(subject, payload.get("name"))
    if error_message:
        return JsonResponse({"status": "FAIL", "message": error_message}, status=400)

    try:
        bank = QuestionBank.objects.create(subject_id=subject, name=bank_name)
    except IntegrityError:
        return JsonResponse(
            {"status": "FAIL", "message": QUESTION_BANK_DUPLICATE_NAME_MESSAGE},
            status=400,
        )

    return JsonResponse(
        {
            "status": "SUCCESS",
            "bank_id": str(bank.id),
            "bank_name": bank.name,
            "message": "Tạo ngân hàng câu hỏi thành công.",
        }
    )


@login_required
@require_POST
def api_update_question_bank(request, bank_id):
    _ensure_lecturer(request)
    bank = get_object_or_404(_get_lecturer_question_banks(request), pk=bank_id)
    if _subject_has_ongoing_exam_group(bank.subject):
        return _locked_subject_mutation_response()

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    bank_name, error_message = _validate_question_bank_name(
        bank.subject,
        payload.get("name"),
        exclude_bank_id=bank.pk,
    )
    if error_message:
        return JsonResponse({"status": "FAIL", "message": error_message}, status=400)

    bank.name = bank_name
    try:
        bank.save(update_fields=["name"])
    except IntegrityError:
        return JsonResponse(
            {"status": "FAIL", "message": QUESTION_BANK_DUPLICATE_NAME_MESSAGE},
            status=400,
        )

    bank_with_count = _get_lecturer_question_banks_with_counts(request).get(pk=bank.pk)
    return JsonResponse(
        {
            "status": "SUCCESS",
            "message": "Cập nhật ngân hàng câu hỏi thành công.",
            "bank": _serialize_question_bank_item(bank_with_count),
        }
    )


@login_required
@require_POST
def api_save_question_bank_questions(request, bank_id):
    _ensure_lecturer(request)
    bank = get_object_or_404(_get_lecturer_question_banks(request), pk=bank_id)
    subject = bank.subject
    if _subject_has_ongoing_exam_group(subject):
        return _locked_subject_mutation_response()

    try:
        payload = json.loads(request.body.decode("utf-8"))
        question_ids = payload.get("question_ids", [])
    except Exception:
        return JsonResponse({"status": "FAIL", "message": "Dữ liệu không hợp lệ."}, status=400)

    workspace_id = _get_workspace_id(request, payload)
    if not workspace_id:
        return JsonResponse({"status": "FAIL", "message": "Thiếu workspace_id."}, status=400)

    draft_prefix = _draft_prefix(workspace_id)

    draft_qs = Question.objects.filter(
        subject_id=subject,
        is_exam_clone=False,
        question_id_in_barem__startswith=draft_prefix,
    )

    selected_drafts = list(draft_qs.filter(pk__in=question_ids).order_by("pk"))
    discarded_qs = draft_qs.exclude(pk__in=question_ids)

    with transaction.atomic():
        for q in selected_drafts:
            q.question_id_in_barem = q.question_id_in_barem.replace(draft_prefix, "SAVED_")
            q.question_bank_id = bank
            q.save(update_fields=["question_id_in_barem", "question_bank_id"])

        discarded_count = discarded_qs.count()
        discarded_qs.delete()

        LectureMaterial.objects.filter(subject_id=subject, workspace_id=workspace_id).update(
            workspace_id="",
            question_bank_id=bank,
        )

    bank_with_count = _get_lecturer_question_banks_with_counts(request).get(pk=bank.pk)

    return JsonResponse(
        {
            "status": "SUCCESS",
            "subject_id": subject.id,
            "message": "Lưu ngân hàng câu hỏi thành công.",
            "saved_count": len(selected_drafts),
            "discarded_count": discarded_count,
            "question_count": int(getattr(bank_with_count, "question_count_value", 0) or 0),
            "bank": _serialize_question_bank_item(bank_with_count),
        }
    )


@login_required
@require_POST
def api_delete_question_bank(request, bank_id):
    _ensure_lecturer(request)
    bank = get_object_or_404(_get_lecturer_question_banks(request), pk=bank_id)
    if _subject_has_ongoing_exam_group(bank.subject):
        return _locked_subject_mutation_response()

    # BUG_015: kiểm tra trước khi xóa
    if bank.exam_sets.exists():
        return JsonResponse(
            {
                "status": "FAIL",
                "message": "Ngân hàng câu hỏi đang được sử dụng trong bộ đề thi, không thể xóa.",
            },
            status=400,
        )

    try:
        # BUG_015: bọc trong atomic để tránh partial delete
        with transaction.atomic():
            Question.objects.filter(question_bank_id=bank, is_exam_clone=True).update(question_bank_id=None)
            Question.objects.filter(question_bank_id=bank, is_exam_clone=False).delete()

            materials = LectureMaterial.objects.filter(question_bank_id=bank)
            for material in materials:
                try:
                    if material.file_path and os.path.exists(material.file_path):
                        os.remove(material.file_path)
                except Exception:
                    pass
            materials.delete()
            bank.delete()
        return JsonResponse({"status": "SUCCESS", "message": "Đã xóa ngân hàng câu hỏi thành công."})
    except Exception as exc:
        return JsonResponse({"status": "FAIL", "message": f"Lỗi: {str(exc)}"}, status=500)


@login_required
@require_POST
def api_material_presign(request):
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({"status": "FAIL", "message": "Không có quyền truy cập."}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    file_name = (payload.get("file_name") or "").strip()
    file_size = int(payload.get("file_size") or 0)

    if not file_name:
        return JsonResponse({"status": "FAIL", "message": "Tên file không hợp lệ."}, status=400)
    if file_size > 50 * 1024 * 1024:
        return JsonResponse({"status": "FAIL", "message": "File vượt quá 50MB."}, status=400)

    file_key = f"lecture_materials/tmp/{uuid4().hex}_{file_name}"
    return JsonResponse(
        {
            "status": "SUCCESS",
            "presigned_url": f"/media/{file_key}",
            "file_key": file_key,
            "message": "Tạo phiên upload thành công.",
        }
    )


@login_required
@require_POST
def api_material_upload_complete(request):
    _ensure_lecturer(request)
    import hashlib

    subject_id = request.POST.get("subject_id")
    bank_id = (request.POST.get("bank_id") or "").strip()
    title = request.POST.get("title") or request.POST.get("file_name")
    file_obj = request.FILES.get("file")

    if not subject_id or not title or not file_obj:
        return JsonResponse({"status": "FAIL", "message": "Thiếu dữ liệu upload."}, status=400)

    subject = get_object_or_404(_get_lecturer_subjects(request), pk=subject_id)
    bank_obj = None
    if bank_id:
        bank_obj = _get_lecturer_question_banks(request).filter(subject_id=subject, pk=bank_id).first()
        if bank_obj is None:
            return JsonResponse({"status": "FAIL", "message": "Ngân hàng câu hỏi không hợp lệ."}, status=404)
    if _subject_has_ongoing_exam_group(subject):
        return _locked_subject_mutation_response()

    file_bytes = file_obj.read()
    if not file_bytes:
        return JsonResponse({"status": "FAIL", "message": "File rỗng hoặc không hợp lệ."}, status=400)

    file_hash = hashlib.md5(file_bytes).hexdigest()
    original_name = file_obj.name.strip()
    workspace_id = request.POST.get("workspace_id", "")

    existing_materials = LectureMaterial.objects.filter(subject_id=subject)
    if workspace_id:
        existing_materials = existing_materials.filter(workspace_id=workspace_id)
    elif bank_obj:
        existing_materials = existing_materials.filter(question_bank_id=bank_obj)
    else:
        existing_materials = existing_materials.filter(workspace_id="", question_bank_id__isnull=True)

    for material in existing_materials:
        existing_path = getattr(material, "file_path", None)
        existing_name = os.path.basename(existing_path) if existing_path else ""
        same_name = existing_name.lower() == original_name.lower()

        same_hash = False
        if existing_path and os.path.exists(existing_path):
            try:
                with open(existing_path, "rb") as f:
                    same_hash = (hashlib.md5(f.read()).hexdigest() == file_hash)
            except Exception:
                pass

        if same_name or same_hash:
            return JsonResponse(
                {
                    "status": "FAIL",
                    "message": f"Tài liệu '{original_name}' đã tồn tại hoặc trùng lặp trong ngân hàng này.",
                },
                status=400,
            )

    upload_dir = os.path.join(settings.MEDIA_ROOT, "lecture_materials", subject.subject_code)
    os.makedirs(upload_dir, exist_ok=True)

    safe_name = original_name
    base_name, extension = os.path.splitext(safe_name)
    file_path = os.path.join(upload_dir, safe_name)
    counter = 1
    while os.path.exists(file_path):
        safe_name = f"{base_name}_{counter}{extension}"
        file_path = os.path.join(upload_dir, safe_name)
        counter += 1

    with open(file_path, "wb+") as destination:
        destination.write(file_bytes)

    material = LectureMaterial.objects.create(
        subject_id=subject,
        question_bank_id=bank_obj,
        title=os.path.splitext(title)[0],
        file_path=file_path,
        file_type=file_obj.content_type or "application/octet-stream",
        workspace_id=workspace_id,
    )

    return JsonResponse(
        {
            "status": "SUCCESS",
            "document_id": str(material.id),
            "file_name": material.title,
            "message": "Upload thành công.",
        }
    )


@login_required
@require_POST
def lecturer_delete_material(request, material_id):
    material = get_object_or_404(LectureMaterial, pk=material_id)

    if not request.user.userprofile.is_lecturer:
        return JsonResponse({"status": "FAIL", "message": "Không có quyền truy cập."}, status=403)

    if material.subject not in request.user.userprofile.subjects_taught.all():
        return JsonResponse({"status": "FAIL", "message": "Bạn không có quyền xóa tài liệu này."}, status=403)
    if _subject_has_ongoing_exam_group(material.subject):
        return _locked_subject_mutation_response()

    try:
        if material.file_path and os.path.exists(material.file_path):
            os.remove(material.file_path)
    except Exception as exc:
        logger.warning("Không thể xóa file vật lý: %s", exc)

    material.delete()
    return JsonResponse({"status": "SUCCESS", "message": "Đã xóa tài liệu thành công."})


@login_required
@require_GET
def api_get_materials(request):
    _ensure_lecturer(request)

    subject_id = request.GET.get("subject_id")
    bank_id = (request.GET.get("bank_id") or "").strip()
    search = (request.GET.get("search") or "").strip()
    workspace_id = (request.GET.get("workspace_id") or "").strip()
    view_type = (request.GET.get("view_type") or "generate").strip()

    if not subject_id:
        return JsonResponse({"status": "FAIL", "message": "Thiếu subject_id."}, status=400)

    subject = get_object_or_404(_get_lecturer_subjects(request), pk=subject_id)

    if view_type == "bank" and bank_id:
        bank_obj = _get_lecturer_question_banks(request).filter(subject_id=subject, pk=bank_id).first()
        if bank_obj is None:
            return JsonResponse({"status": "FAIL", "message": "Ngân hàng câu hỏi không hợp lệ."}, status=404)
        qs = LectureMaterial.objects.filter(subject_id=subject, question_bank_id=bank_obj).order_by("-uploaded_at")
    elif workspace_id:
        qs = LectureMaterial.objects.filter(subject_id=subject, workspace_id=workspace_id).order_by("-uploaded_at")
    else:
        qs = LectureMaterial.objects.none()

    if search:
        qs = qs.filter(title__icontains=search)

    return JsonResponse(
        {
            "status": "SUCCESS",
            "documents": [_serialize_material(item) for item in qs],
            "pagination": {"page": 1, "num_pages": 1, "has_next": False, "has_previous": False},
        }
    )


@login_required
@require_GET
def api_get_questions(request):
    _ensure_lecturer(request)

    subject_id = request.GET.get("bank_id")
    real_bank_id = (request.GET.get("real_bank_id") or "").strip()
    difficulty = request.GET.get("difficulty")
    view_type = request.GET.get("view_type", "bank")
    workspace_id = (request.GET.get("workspace_id") or "").strip()

    if not subject_id:
        return JsonResponse({"status": "FAIL", "message": "Thiếu subject_id."}, status=400)

    subject = get_object_or_404(_get_lecturer_subjects(request), pk=subject_id)
    all_qs = Question.objects.filter(subject_id=subject, is_exam_clone=False)

    if view_type == "generate" and workspace_id:
        filtered_qs = all_qs.filter(question_id_in_barem__startswith=_draft_prefix(workspace_id))
    elif view_type == "bank" and real_bank_id:
        bank_obj = _get_lecturer_question_banks(request).filter(subject_id=subject, pk=real_bank_id).first()
        if bank_obj is None:
            return JsonResponse({"status": "FAIL", "message": "Ngân hàng câu hỏi không hợp lệ."}, status=404)
        if workspace_id:
            filtered_qs = all_qs.filter(
                Q(question_bank_id=bank_obj) | Q(question_id_in_barem__startswith=_draft_prefix(workspace_id))
            )
        else:
            filtered_qs = all_qs.filter(question_bank_id=bank_obj)
    else:
        filtered_qs = Question.objects.none()

    summary_qs = filtered_qs
    if difficulty and difficulty != "ALL":
        filtered_qs = filtered_qs.filter(difficulty=difficulty)

    return JsonResponse(
        {
            "status": "SUCCESS",
            "questions": [_serialize_question(item) for item in filtered_qs.order_by("-pk")],
            "summary": {
                "all": summary_qs.count(),
                "easy": summary_qs.filter(difficulty=DifficultyLevel.EASY).count(),
                "medium": summary_qs.filter(difficulty=DifficultyLevel.MEDIUM).count(),
                "hard": summary_qs.filter(difficulty=DifficultyLevel.HARD).count(),
            },
        }
    )


@login_required
@require_POST
def api_create_manual_question(request):
    _ensure_lecturer(request)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    subject_id = payload.get("subject_id") or payload.get("bank_id")
    real_bank_id = payload.get("real_bank_id")
    workspace_id = _get_workspace_id(request, payload)
    view_type = (payload.get("view_type") or "bank").strip()

    form = LecturerManualQuestionForm(
        {
            "subject_id": subject_id,
            "question_text": payload.get("content") or payload.get("question_text"),
            "difficulty": payload.get("difficulty"),
        }
    )

    if not form.is_valid():
        return JsonResponse({"status": "FAIL", "message": form.errors.as_json()}, status=400)

    subject = get_object_or_404(_get_lecturer_subjects(request), pk=form.cleaned_data["subject_id"])
    question_text = form.cleaned_data["question_text"]
    if _subject_has_ongoing_exam_group(subject):
        return _locked_subject_mutation_response()

    if _question_exists_in_subject(subject, question_text):
        return JsonResponse(
            {"status": "FAIL", "message": "Câu hỏi bị trùng trong môn học này."},
            status=400,
        )

    bank_obj = None
    if view_type == "generate":
        if not workspace_id:
            return JsonResponse({"status": "FAIL", "message": "Thiếu workspace_id."}, status=400)
        question_code = f"{_draft_prefix(workspace_id)}{uuid4().hex[:8]}"
    else:
        if real_bank_id:
            bank_obj = _get_lecturer_question_banks(request).filter(subject_id=subject, pk=real_bank_id).first()
            if bank_obj is None:
                return JsonResponse({"status": "FAIL", "message": "Ngân hàng câu hỏi không hợp lệ."}, status=404)
        question_code = f"MAN_{subject.subject_code}_{uuid4().hex[:8]}"

    try:
        question = Question.objects.create(
            subject_id=subject,
            question_bank_id=bank_obj,
            question_text=question_text,
            difficulty=form.cleaned_data["difficulty"],
            question_id_in_barem=question_code,
        )
    except IntegrityError:
        return JsonResponse(
            {"status": "FAIL", "message": "Câu hỏi bị trùng trong môn học này."},
            status=400,
        )

    return JsonResponse(
        {
            "status": "SUCCESS",
            "question": _serialize_question(question),
            "message": "Thêm câu hỏi thành công.",
        }
    )


@login_required
@require_POST
def api_update_question_bank_question(request, question_id):
    _ensure_lecturer(request)

    question = get_object_or_404(Question, pk=question_id)
    if question.subject not in request.user.userprofile.subjects_taught.all():
        return JsonResponse({"status": "FAIL", "message": "Không có quyền truy cập môn học này."}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    if question.is_exam_clone:
        return JsonResponse(
            {"status": "FAIL", "message": "Không thể sửa câu hỏi đang được sử dụng trong mã đề."},
            status=400,
        )
    if _subject_has_ongoing_exam_group(question.subject):
        return _locked_subject_mutation_response()

    question_text = (payload.get("content") or payload.get("question_text") or "").strip()
    difficulty = payload.get("difficulty")

    # BUG_014: validate length
    if question_text and len(question_text) > 5000:
        return JsonResponse(
            {"status": "FAIL", "message": "Nội dung câu hỏi không được vượt quá 5000 ký tự."},
            status=400,
        )

    if question_text and _question_exists_in_subject(question.subject, question_text, exclude_question_id=question.id):
        return JsonResponse(
            {"status": "FAIL", "message": "Câu hỏi bị trùng trong môn học này."},
            status=400,
        )

    if question_text:
        question.question_text = question_text
    if difficulty in ["EASY", "MEDIUM", "HARD"]:
        question.difficulty = difficulty

    try:
        question.save()
    except IntegrityError:
        return JsonResponse(
            {"status": "FAIL", "message": "Câu hỏi bị trùng trong môn học này."},
            status=400,
        )

    # BUG_016: trả updated_at = thời điểm cập nhật thực tế
    _now_str = timezone.localtime(timezone.now()).strftime("%H:%M - %d/%m/%Y")
    serialized = _serialize_question(question)
    serialized["updated_at"] = _now_str
    return JsonResponse(
        {
            "status": "SUCCESS",
            "question": serialized,
            "message": "Cập nhật câu hỏi thành công.",
        }
    )


@login_required
@require_POST
def api_delete_question_bank_question(request, question_id):
    _ensure_lecturer(request)

    question = get_object_or_404(Question, pk=question_id)
    if question.is_exam_clone:
        return JsonResponse(
            {"status": "FAIL", "message": "Không thể xóa câu hỏi đang được sử dụng trong mã đề."},
            status=400,
        )
    if question.subject not in request.user.userprofile.subjects_taught.all():
        return JsonResponse({"status": "FAIL", "message": "Không có quyền truy cập môn học này."}, status=403)
    if _subject_has_ongoing_exam_group(question.subject):
        return _locked_subject_mutation_response()

    question.delete()
    return JsonResponse({"status": "SUCCESS", "message": "Xóa câu hỏi thành công."})


@login_required
@require_POST
def api_bulk_update_question_level(request):
    _ensure_lecturer(request)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    bank_id = payload.get("bank_id")
    difficulty = payload.get("difficulty")
    question_ids = payload.get("question_ids", [])

    if isinstance(question_ids, str):
        question_ids = json.loads(question_ids)

    subject = get_object_or_404(_get_lecturer_subjects(request), pk=bank_id)
    if _subject_has_ongoing_exam_group(subject):
        return _locked_subject_mutation_response()

    updated = Question.objects.filter(
        subject_id=subject,
        pk__in=question_ids,
        is_exam_clone=False,
    ).update(difficulty=difficulty)

    return JsonResponse(
        {
            "status": "SUCCESS",
            "affected": updated,
            "message": "Thay đổi mức độ thành công.",
        }
    )


@login_required
@require_POST
def api_bulk_delete_questions(request):
    _ensure_lecturer(request)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    bank_id = payload.get("bank_id")
    question_ids = payload.get("question_ids", [])

    if isinstance(question_ids, str):
        question_ids = json.loads(question_ids)

    subject = get_object_or_404(_get_lecturer_subjects(request), pk=bank_id)
    if _subject_has_ongoing_exam_group(subject):
        return _locked_subject_mutation_response()

    qs = Question.objects.filter(subject_id=subject, pk__in=question_ids, is_exam_clone=False)
    deleted_count = qs.count()
    qs.delete()

    return JsonResponse(
        {
            "status": "SUCCESS",
            "affected": deleted_count,
            "message": "Xóa câu hỏi thành công.",
        }
    )


@login_required
@require_POST
def api_generate_questions(request):
    _ensure_lecturer(request)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    subject_id = payload.get("subject_id")
    document_ids = payload.get("document_ids", [])
    total_count = int(payload.get("total_count", 0))
    level_config = payload.get("level_config", {})
    workspace_id = _get_workspace_id(request, payload)

    if isinstance(document_ids, str):
        document_ids = json.loads(document_ids)
    if isinstance(level_config, str):
        level_config = json.loads(level_config)

    if not subject_id:
        return JsonResponse({"status": "FAIL", "message": "Thiếu subject_id."}, status=400)

    subject = get_object_or_404(_get_lecturer_subjects(request), pk=subject_id)
    if _subject_has_ongoing_exam_group(subject):
        return _locked_subject_mutation_response()

    if not workspace_id:
        return JsonResponse(
            {"status": "FAIL", "message": "Thiếu workspace_id cho phiên tạo câu hỏi."},
            status=400,
        )

    if not document_ids:
        return JsonResponse({"status": "FAIL", "message": "Bạn phải chọn ít nhất 1 tài liệu."}, status=400)

    if total_count <= 0 or total_count > 100:
        return JsonResponse({"status": "FAIL", "message": "Số lượng câu hỏi không hợp lệ."}, status=400)

    try:
        requested_counts = _normalize_requested_question_counts(total_count, level_config)
    except ValueError as exc:
        return JsonResponse({"status": "FAIL", "message": str(exc)}, status=400)

    materials = list(
        LectureMaterial.objects.filter(
            subject_id=subject,
            pk__in=document_ids,
        ).order_by("-uploaded_at")
    )

    if not materials:
        return JsonResponse(
            {"status": "FAIL", "message": "Danh sách tài liệu không hợp lệ cho phiên hiện tại."},
            status=400,
        )

    job_id = uuid4().hex

    try:
        skipped_duplicates = 0
        prepared_questions = []
        retry_excluded_texts = []
        known_question_keys = {
            item
            for item in Question.objects.filter(subject_id=subject, is_exam_clone=False).values_list(
                "question_text_normalized",
                flat=True,
            )
            if item
        }
        draft_prefix = _draft_prefix(workspace_id)

        for _ in range(3):
            remaining_counts = _remaining_question_counts(requested_counts, prepared_questions)
            remaining_total = sum(remaining_counts.values())
            if remaining_total <= 0:
                break

            generation_result = _generate_questions_from_ai(
                subject=subject,
                materials=materials,
                total_count=remaining_total,
                level_config=_question_generation_level_config(remaining_counts),
                excluded_question_texts=retry_excluded_texts,
            )
            if isinstance(generation_result, dict):
                ai_questions = generation_result.get("questions", [])
            else:
                ai_questions = generation_result
            if not isinstance(ai_questions, list):
                ai_questions = []

            for item in ai_questions:
                content_key = normalize_question_text(item["content"])
                if not content_key:
                    continue
                if content_key in known_question_keys:
                    skipped_duplicates += 1
                    retry_excluded_texts.append(item["content"])
                    continue

                difficulty = item.get("difficulty")
                if remaining_counts.get(difficulty, 0) <= 0:
                    retry_excluded_texts.append(item["content"])
                    continue

                prepared_questions.append(item)
                known_question_keys.add(content_key)
                remaining_counts[difficulty] -= 1

        remaining_counts = _remaining_question_counts(requested_counts, prepared_questions)
        shortage, shortage_text = _build_question_shortage(remaining_counts)
        if not prepared_questions:
            message = "AI không tạo được câu hỏi hợp lệ từ tài liệu đã chọn."
            if shortage_text:
                message = f"{message} Còn thiếu: {shortage_text}."
            raise QuestionGenerationEmptyError(message)

        created_questions = []
        with transaction.atomic():
            for item in prepared_questions:
                q = Question.objects.create(
                    subject_id=subject,
                    question_text=item["content"],
                    difficulty=item["difficulty"],
                    question_id_in_barem=f"{draft_prefix}{uuid4().hex[:8]}",
                )

                q_data = _serialize_question(q)
                q_data["source"] = item.get("source") or "AI từ tài liệu"
                created_questions.append(q_data)

        summary = {
            "all": len(created_questions),
            "easy": len([q for q in created_questions if q["difficulty"] == "EASY"]),
            "medium": len([q for q in created_questions if q["difficulty"] == "MEDIUM"]),
            "hard": len([q for q in created_questions if q["difficulty"] == "HARD"]),
        }
        is_partial = bool(shortage)
        response_status = "PARTIAL_SUCCESS" if is_partial else "SUCCESS"
        process_status = "PARTIAL_SUCCESS" if is_partial else "COMPLETE"
        message, shortage_message = _build_question_generation_message(
            created_count=len(created_questions),
            requested_count=total_count,
            skipped_duplicates=skipped_duplicates,
            shortage_text=shortage_text,
        )

        _save_question_job(
            job_id,
            {
                "status": process_status,
                "progress": 100,
                "message": message,
                "questions": created_questions,
                "summary": summary,
                "error_message": shortage_message if is_partial else "",
                "shortage": shortage,
                "shortage_message": shortage_message,
                "created_count": len(created_questions),
                "requested_count": total_count,
                "skipped_duplicates": skipped_duplicates,
            },
        )

        return JsonResponse(
            {
                "status": response_status,
                "job_id": job_id,
                "message": message,
                "questions": created_questions,
                "summary": summary,
                "created_count": len(created_questions),
                "requested_count": total_count,
                "shortage": shortage,
                "shortage_message": shortage_message,
                "skipped_duplicates": skipped_duplicates,
            }
        )

    except QuestionGenerationEmptyError as exc:
        message = str(exc)
        logger.warning("Generate questions failed because AI produced no valid questions")
        _save_question_job(
            job_id,
            {
                "status": "FAIL",
                "progress": 100,
                "message": message,
                "questions": [],
                "summary": {},
                "error_message": message,
                "shortage": {},
                "shortage_message": "",
                "created_count": 0,
                "requested_count": total_count,
                "skipped_duplicates": 0,
            },
        )
        return JsonResponse({"status": "FAIL", "message": message}, status=400)
    except AuthenticationError:
        message = "OpenAI API key không hợp lệ hoặc đã hết hiệu lực. Vui lòng kiểm tra lại cấu hình OPENAI_API_KEY."
        logger.warning("Generate questions failed due to OpenAI authentication", exc_info=True)
        _save_question_job(
            job_id,
            {
                "status": "FAIL",
                "progress": 100,
                "message": message,
                "questions": [],
                "summary": {},
                "error_message": message,
                "shortage": {},
                "shortage_message": "",
                "created_count": 0,
                "requested_count": total_count,
                "skipped_duplicates": 0,
            },
        )
        return JsonResponse({"status": "FAIL", "message": message}, status=400)
    except Exception:
        message = "Tạo câu hỏi thất bại. Vui lòng thử lại sau."
        logger.exception("Generate questions direct failed")
        _save_question_job(
            job_id,
            {
                "status": "FAIL",
                "progress": 100,
                "message": message,
                "questions": [],
                "summary": {},
                "error_message": message,
                "shortage": {},
                "shortage_message": "",
                "created_count": 0,
                "requested_count": total_count,
                "skipped_duplicates": 0,
            },
        )
        return JsonResponse({"status": "FAIL", "message": message}, status=500)


@login_required
@require_GET
def api_generate_questions_status(request):
    _ensure_lecturer(request)

    job_id = request.GET.get("job_id")
    if not job_id:
        return JsonResponse({"status": "FAIL", "message": "Thiếu job_id."}, status=400)

    job = _get_question_job(job_id)
    if not job:
        return JsonResponse({"status": "FAIL", "message": "Không tìm thấy job."}, status=404)

    return JsonResponse(
        {
            "status": "SUCCESS",
            "process_status": job.get("status"),
            "progress": job.get("progress", 0),
            "message": job.get("message", ""),
            "questions": job.get("questions", []),
            "summary": job.get("summary", {}),
            "error_message": job.get("error_message", ""),
            "shortage": job.get("shortage", {}),
            "shortage_message": job.get("shortage_message", ""),
            "created_count": job.get("created_count", 0),
            "requested_count": job.get("requested_count", 0),
            "skipped_duplicates": job.get("skipped_duplicates", 0),
        }
    )


# =========================================================
# Material / question screen cũ
# =========================================================
@login_required
def lecturer_question_management(request, subject_code):
    _ensure_lecturer(request)
    subject = get_object_or_404(request.user.userprofile.subjects_taught.all(), subject_code=subject_code)
    query = request.GET.copy()
    query["subject_id"] = str(subject.id)
    query.setdefault("mode", "detail")
    return redirect(f"{reverse('qna:lecturer_questions_screen')}?{query.urlencode()}")


@login_required
@require_POST
def lecturer_upload_material_screen(request):
    return JsonResponse(
        {
            "success": False,
            "error": "Route upload tài liệu cũ đã ngừng hỗ trợ. Hãy dùng API upload mới.",
        },
        status=410,
    )


@login_required
@require_POST
def lecturer_upload_material(request, subject_code):
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({"success": False, "error": "Không có quyền"}, status=403)

    subject = get_object_or_404(request.user.userprofile.subjects_taught, subject_code=subject_code)
    if _subject_has_ongoing_exam_group(subject):
        return _locked_subject_mutation_response(response_style="legacy")
    title = request.POST.get("title")
    file_obj = request.FILES.get("file")
    if not title or not file_obj:
        return JsonResponse({"success": False, "error": "Thiếu thông tin"}, status=400)

    upload_dir = os.path.join(settings.MEDIA_ROOT, "lecture_materials", subject_code)
    os.makedirs(upload_dir, exist_ok=True)
    file_path = os.path.join(upload_dir, file_obj.name)
    with open(file_path, "wb+") as destination:
        for chunk in file_obj.chunks():
            destination.write(chunk)

    material = LectureMaterial.objects.create(
        subject_id=subject,
        title=title,
        file_path=file_path,
        file_type=file_obj.content_type,
    )
    messages.success(request, "Đã tải tài liệu lên thành công.")
    return JsonResponse({"success": True, "material_id": material.id})


@login_required
def lecturer_generate_exam_codes(request, subject_code):
    _ensure_lecturer(request)
    subject = get_object_or_404(request.user.userprofile.subjects_taught, subject_code=subject_code)
    return redirect(f"{reverse('qna:lecturer_generate_codes_screen')}?subject_id={subject.id}")


@login_required
@require_POST
def lecturer_generate_codes_with_ai(request):
    return JsonResponse(
        {
            "success": False,
            "error": (
                "Chức năng sinh mã đề AI kiểu cũ đã bị vô hiệu vì không còn khớp với cấu trúc "
                "mã đề hiện tại. Hãy dùng màn tạo bộ đề và mã đề mới."
            ),
        },
        status=410,
    )


@login_required
@require_POST
def lecturer_update_exam_code_question(request, exam_code_id):
    return JsonResponse(
        {
            "success": False,
            "error": (
                "API chỉnh câu hỏi mã đề kiểu cũ đã bị vô hiệu vì model hiện tại không còn "
                "question_easy/question_medium/question_hard."
            ),
        },
        status=410,
    )


@login_required
@require_POST
def lecturer_edit_exam_code_question(request, exam_code_id):
    return JsonResponse(
        {
            "success": False,
            "error": (
                "API chỉnh câu hỏi mã đề kiểu cũ đã bị vô hiệu vì model hiện tại không còn "
                "question_easy/question_medium/question_hard."
            ),
        },
        status=410,
    )


# =========================================================
# REVIEW / EXPORT
# =========================================================

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


def _attach_review_status_meta(session: ExamSession) -> None:
    """
    Chỉ gán các field tạm thời KHÔNG đụng vào property.
    """
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
        session.can_view_detail = False
        session.can_display_score = False
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


def _apply_review_status(row):
    row.has_violation_warning = bool(getattr(row, "visible_violation_count", 0))
    _attach_review_status_meta(row)


def _can_view_review_detail(row):
    _attach_review_status_meta(row)
    return bool(getattr(row, "can_view_detail", False))


def _build_missing_review_rows(exam_groups, existing_session_keys):
    from .session_management import _build_exam_group_student_rows

    rows = []
    for exam_group in exam_groups:
        if exam_group.computed_status not in {"COMPLETED", "CANCELLED"}:
            continue

        for student_row in _build_exam_group_student_rows(exam_group):
            student_identifier = student_row.get("student_code") or "-"
            student_key = normalize_student_code(student_identifier)
            if student_key and (exam_group.pk, student_key) in existing_session_keys:
                continue

            row = SimpleNamespace(
                id=None,
                exam_group=exam_group,
                session_status="PENDING",
                completed_at=None,
                cheating_flag=False,
                student_identifier=student_identifier,
                student_name=student_row.get("full_name") or student_identifier,
                student_class_name=student_row.get("class_name") or "-",
                exam_date_display=exam_group.exam_date,
                final_score=None,
                created_at=exam_group.end_at,
                visible_violation_count=0,
            )
            _apply_review_status(row)
            row.can_view_detail = _can_view_review_detail(row)
            rows.append(row)

    return rows


@login_required
def lecturer_student_review_screen(request):
    if not request.user.userprofile.is_lecturer:
        return redirect("qna:dashboard")

    subjects = list(request.user.userprofile.subjects_taught.all().order_by("name"))
    selected_subject_id = (request.GET.get("subject_id") or "").strip()
    raw_exam_group_id = (request.GET.get("exam_group_id") or "").strip()
    student_filter = (request.GET.get("student") or "").strip()
    selected_exam_group_id = int(raw_exam_group_id) if raw_exam_group_id.isdigit() else None
    selected_subject = next((subject for subject in subjects if str(subject.pk) == selected_subject_id), None)

    exam_groups = []
    review_rows = []
    page_obj = None

    if selected_subject:
        exam_groups = list(
            ExamSessionGroup.objects.filter(subject_id=selected_subject).order_by("-exam_date", "-pk")
        )
        sessions = _with_lecturer_visible_violation_count(
            ExamSession.objects.filter(
                subject_id=selected_subject,
                exam_session_group_id__isnull=False,
            ).select_related(
                "user_id",
                "user_id__userprofile",
                "exam_session_group_id",
            )
        )

        if selected_exam_group_id:
            sessions = sessions.filter(exam_session_group_id=selected_exam_group_id)

        selected_exam_groups = [
            exam_group for exam_group in exam_groups if exam_group.pk == selected_exam_group_id
        ] if selected_exam_group_id else exam_groups
        existing_session_keys = set()
        sessions = sessions.order_by("-created_at", "-pk")

        for session in sessions:
            main_avg, final_total = _compute_scores(session)
            session.main_avg = main_avg
            if session.final_score != final_total:
                session.final_score = final_total

            _attach_session_score_meta(session)

            profile = getattr(session.user_id, "userprofile", None)

            linked_roster_row = (
                StudentRosterStudent.objects
                .filter(linked_user_id=session.user_id, student_roster_upload_id__subject_id=session.subject_id)
                .order_by("-student_roster_student_id")
                .first()
            )

            session.student_identifier = (
                    (linked_roster_row.student_code if linked_roster_row else None)
                    or getattr(profile, "student_id", None)
                    or session.user_id.username
            )

            session.student_name = (
                    (linked_roster_row.full_name if linked_roster_row else None)
                    or getattr(profile, "full_name", None)
                    or session.user_id.username
            )

            session.student_class_name = (
                    (linked_roster_row.class_name if linked_roster_row else None)
                    or getattr(profile, "class_name", None)
                    or "-"
            )
            session.exam_date_display = (
                session.exam_group.exam_date
                if session.exam_group and session.exam_group.exam_date
                else session.completed_at.date() if session.completed_at
                else session.created_at.date()
            )
            session.has_violation_warning = bool(getattr(session, "visible_violation_count", 0))
            _attach_review_status_meta(session)
            review_rows.append(session)

            if session.exam_group:
                session_key = normalize_student_code(session.student_identifier)
                if session_key:
                    existing_session_keys.add((session.exam_group.pk, session_key))

        review_rows.extend(_build_missing_review_rows(selected_exam_groups, existing_session_keys))

        if student_filter:
            keyword = student_filter.lower()
            review_rows = [
                row for row in review_rows
                if keyword in (row.student_identifier or "").lower()
                or keyword in (row.student_name or "").lower()
                or keyword in (row.student_class_name or "").lower()
                or keyword in ((row.exam_group.group_name if getattr(row, "exam_group", None) else "") or "").lower()
            ]

        default_sort_time = timezone.make_aware(datetime(1970, 1, 1))
        review_rows.sort(
            key=lambda item: getattr(item, "created_at", None) or default_sort_time,
            reverse=True,
        )

    completed_count = 0
    absent_count = 0
    total_score = 0.0
    flagged_count = 0

    for row in review_rows:
        if getattr(row, "has_violation_warning", False):
            flagged_count += 1

        status_label = getattr(row, "review_status_label", "")
        if status_label == "Đã hoàn thành":
            completed_count += 1
            total_score += float(getattr(row, "final_score", 0.0) or 0.0)
        elif status_label == "Vắng thi":
            absent_count += 1

    average_score = round(total_score / completed_count, 1) if completed_count else 0

    sessions_page = []
    if selected_subject:
        paginator = Paginator(review_rows, LIST_PAGE_SIZE)
        page_obj = paginator.get_page(request.GET.get("page"))
        sessions_page = page_obj.object_list

    return render(
        request,
        "qna/lecturer/lecturer_student_review.html",
        {
            "subjects": subjects,
            "selected_subject": selected_subject,
            "subject": selected_subject,
            "exam_groups": exam_groups,
            "selected_exam_group_id": selected_exam_group_id,
            "sessions": sessions_page,
            "page_obj": page_obj,
            "student_filter": student_filter,
            "average_score": average_score,
            "completed_count": completed_count,
            "absent_count": absent_count,
            "flagged_count": flagged_count,
        },
    )


@login_required
def lecturer_student_review(request, subject_code):
    _ensure_lecturer(request)
    subject = get_object_or_404(request.user.userprofile.subjects_taught, subject_code=subject_code)
    query = request.GET.copy()
    query["subject_id"] = str(subject.id)
    return redirect(f"{reverse('qna:lecturer_student_review_screen')}?{query.urlencode()}")


@login_required
def lecturer_session_detail(request, session_id):
    if not request.user.userprofile.is_lecturer:
        return redirect("qna:dashboard")

    session = get_object_or_404(
        ExamSession.objects.select_related(
            "subject_id",
            "user_id",
            "user_id__userprofile",
            "exam_session_group_id",
        ),
        pk=session_id,
    )
    if session.subject not in request.user.userprofile.subjects_taught.all():
        raise PermissionDenied("Bạn không có quyền")

    main_avg, final_total = _compute_scores(session)
    if session.final_score != final_total:
        session.final_score = final_total
    _attach_review_status_meta(session)
    if getattr(session, "review_status", None) == "IN_PROGRESS":
        messages.error(request, "Phiên thi đang diễn ra. Chưa thể xem chi tiết.")
        return redirect("qna:lecturer_student_review_screen")
    _attach_session_score_meta(session)
    _attach_session_duration_meta(session)
    _attach_session_assignment_meta(session)
    question_details = _build_session_question_details(session)

    session.detail_status_label = session.review_status_label
    session.detail_status_class = session.review_status_class

    violation_images = [
        {
            "image_data_url": image.get_image_data_url,
            "timestamp": image.timestamp,
            "violation_description": _describe_violation_type(image.violation_type),
        }
        for image in session.violation_images.exclude(
            violation_type__in=LECTURER_HIDDEN_VIOLATION_TYPES
        ).order_by("-timestamp")
    ]

    face_image_url = _build_image_data_url(
        session.face_image_blob,
        session.face_image_mime,
    )
    id_card_image_url = _build_image_data_url(
        session.id_card_image_blob,
        session.id_card_image_mime,
    )

    return render(
        request,
        "qna/lecturer/lecturer_session_detail.html",
        {
            "session": session,
            "question_details": question_details,
            "main_avg": main_avg,
            "final_total": final_total,
            "violation_images": violation_images,
            "face_image_url": face_image_url,
            "id_card_image_url": id_card_image_url,
        },
    )


@login_required
@require_GET
def serve_student_audio_media(request: HttpRequest, relative_path: str) -> FileResponse:
    raw_relative_path = str(relative_path or "").replace("\\", "/")
    normalized_relative_path = posixpath.normpath(f"/{raw_relative_path}").lstrip("/")
    stored_name = posixpath.normpath(f"student_audio/{normalized_relative_path}")

    if not normalized_relative_path or not stored_name.startswith("student_audio/"):
        raise Http404("Không tìm thấy file ghi âm.")

    result = get_object_or_404(
        ExamResult.objects.select_related(
            "exam_session_id",
            "exam_session_id__subject_id",
            "exam_session_id__user_id",
        ),
        audio_file=stored_name,
    )
    _ensure_exam_result_audio_access(request, result)

    try:
        audio_handle = result.audio_file.open("rb")
    except FileNotFoundError:
        logger.error(
            "Exam audio file missing on disk: result_id=%s stored_name=%s expected_path=%s",
            result.pk,
            result.audio_file.name,
            _get_field_file_path(result.audio_file),
        )
        raise Http404("Không tìm thấy file ghi âm.")

    response = FileResponse(audio_handle, content_type=result.audio_mime_type or "audio/mpeg")
    response["Content-Length"] = str(result.audio_file.size)
    logger.info(
        "Serving exam audio file: user_id=%s result_id=%s stored_name=%s content_type=%s",
        request.user.pk,
        result.pk,
        result.audio_file.name,
        response.get("Content-Type", ""),
    )
    return response


@login_required
def lecturer_export_reports_screen(request):
    return redirect("qna:lecturer_student_review_screen")


@login_required
def lecturer_export_exam_results_screen(request):
    if not request.user.userprofile.is_lecturer:
        return redirect("qna:dashboard")

    subject_id = request.GET.get("subject_id")
    exam_group_id = request.GET.get("exam_group_id")
    subject = get_object_or_404(request.user.userprofile.subjects_taught, pk=subject_id)

    if exam_group_id:
        exam_group = get_object_or_404(ExamSessionGroup, pk=exam_group_id, subject_id=subject)
        sessions = ExamSession.objects.filter(subject_id=subject, exam_session_group_id=exam_group).select_related(
            "user_id",
            "user_id__userprofile",
        )
    else:
        sessions = ExamSession.objects.filter(subject_id=subject).select_related("user_id", "user_id__userprofile")

    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Kết quả thi"

    # YÊU CẦU 1: Loại bỏ cột điểm phụ
    headers = ["STT", "Mã SV", "Họ tên", "Lớp", "Ngày thi", "Điểm tổng", "Cảnh báo gian lận"]
    ws.append(headers)

    header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for idx, session in enumerate(sessions, 1):
        main_avg, final_total = _compute_scores(session)
        profile = session.user.userprofile
        ws.append(
            [
                idx,
                session.user.username,
                profile.full_name,
                profile.class_name,
                session.created_at.strftime("%d/%m/%Y %H:%M"),
                f"{final_total:.2f}",
                "CÓ" if _has_lecturer_visible_violation(session) else "KHÔNG",
            ]
        )

    for column in ws.columns:
        max_length = 0
        column_letter = column[0].column_letter
        for cell in column:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except Exception:
                pass
        ws.column_dimensions[column_letter].width = min(max_length + 2, 50)

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    filename = f"Ket_qua_thi_{subject.subject_code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


@login_required
def lecturer_export_exam_results(request, subject_code):
    if not request.user.userprofile.is_lecturer:
        messages.error(request, "Bạn không có quyền truy cập.")
        return redirect("qna:dashboard")

    subject = get_object_or_404(
        request.user.userprofile.subjects_taught.all(),
        subject_code=subject_code,
    )

    sessions = (
        ExamSession.objects.filter(subject_id=subject, session_status="COMPLETED")
        .select_related("user_id", "exam_session_group_id")
        .order_by("-created_at")
    )

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="ket_qua_{subject.subject_code}.xlsx"'

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Kết quả thi"

    headers = [
        "STT",
        "Họ tên",
        "Username",
        "Mã sinh viên",
        "Lớp",
        "Môn học",
        "Mã môn",
        "Ca thi",
        "Ngày thi",
        "Điểm cuối",
        "Trạng thái hoàn thành",
        "Xác thực khuôn mặt",
        "Cảnh báo gian lận"
    ]
    ws.append(headers)

    for idx, session in enumerate(sessions, start=1):
        profile = getattr(session.user, "userprofile", None)
        ws.append(
            [
                idx,
                profile.full_name if profile and profile.full_name else session.user.get_full_name() or session.user.username,
                session.user.username,
                profile.student_id if profile else "",
                profile.class_name if profile else "",
                subject.name,
                subject.subject_code,
                session.exam_group.group_name if session.exam_group else "",
                session.created_at.strftime("%d/%m/%Y %H:%M") if session.created_at else "",
                session.final_score if session.final_score is not None else "",
                "Đã hoàn thành" if _is_session_finalized(session) else "Chưa hoàn thành",
                session.get_verification_status_display() if hasattr(session,
                                                                     "get_verification_status_display") else session.verification_status,
                "CÓ" if _has_lecturer_visible_violation(session) else "KHÔNG"
            ]
        )

    for column_cells in ws.columns:
        max_length = 0
        column_letter = column_cells[0].column_letter
        for cell in column_cells:
            try:
                cell_length = len(str(cell.value)) if cell.value is not None else 0
                if cell_length > max_length:
                    max_length = cell_length
            except Exception:
                pass
        ws.column_dimensions[column_letter].width = min(max_length + 2, 40)

    wb.save(response)
    return response


@login_required
def lecturer_export_questions_word(request):
    return JsonResponse(
        {
            "status": "FAIL",
            "message": "Chức năng xuất Word đã bị tắt theo yêu cầu mới.",
        },
        status=400,
    )


# =========================================================
# STUDENT SIDE
# =========================================================
class RegistrationForm(forms.Form):
    full_name = forms.CharField(label=mark_safe('Họ và tên <span class="text-red-500">*</span>'), max_length=150)
    username = forms.CharField(label=mark_safe('Tên đăng nhập <span class="text-red-500">*</span>'), max_length=150)
    class_name = forms.CharField(label=mark_safe('Lớp <span class="text-red-500">*</span>'), max_length=100)
    email = forms.EmailField(label="Email", required=False)
    faculty = forms.CharField(label="Khoa", required=False, max_length=150)
    password = forms.CharField(
        label=mark_safe('Mật khẩu <span class="text-red-500">*</span>'),
        widget=forms.PasswordInput(),
    )
    password2 = forms.CharField(
        label=mark_safe('Nhập lại mật khẩu <span class="text-red-500">*</span>'),
        widget=forms.PasswordInput(),
    )

    def clean_username(self):
        username = self.cleaned_data.get("username", "").strip()
        if not username:
            raise ValidationError("Tên đăng nhập là bắt buộc.")
        if User.objects.filter(username=username).exists():
            raise ValidationError("Tên đăng nhập này đã tồn tại.")
        return username

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        password2 = cleaned_data.get("password2")
        if password and password2 and password != password2:
            self.add_error("password2", "Mật khẩu nhập lại không khớp.")
        return cleaned_data

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
def history_view(request: HttpRequest) -> HttpResponse:
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

    if getattr(session, "review_status_label", "") == "Vắng thi":
        messages.warning(request, "Phiên thi vắng thi không có chi tiết để xem.")
        return redirect("qna:history")
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


@login_required
@require_POST
def save_exam_result(request: HttpRequest) -> JsonResponse:
    session_id = request.POST.get("session_id")
    question_id = request.POST.get("question_id")
    audio_file = request.FILES.get("audio_file")
    allow_after_expiry = str(request.POST.get("allow_after_expiry", "")).strip().lower() in {"1", "true", "yes"}

    if not session_id or not question_id:
        return JsonResponse({"status": "error", "message": "Thiếu dữ liệu bắt buộc."}, status=400)
    if not audio_file:
        return JsonResponse({"status": "error", "message": "Không tìm thấy file ghi âm."}, status=400)

    session = get_object_or_404(ExamSession, pk=session_id)
    _ensure_owner(session, request.user)

    if _is_session_finalized(session):
        return JsonResponse({"status": "error", "message": "Phiên thi đã kết thúc."}, status=400)

    if not is_exam_group_submission_window_open(
            session.exam_group,
            allow_post_end_grace=allow_after_expiry,
    ):
        return JsonResponse({"status": "error", "message": "Ngoài thời gian thi."}, status=403)
    if not session.questions.filter(pk=question_id).exists():
        return JsonResponse({"status": "error", "message": "Câu hỏi không thuộc phiên thi này."}, status=400)

    result = ExamResult.objects.filter(exam_session_id=session, question_id_id=question_id).first()
    if not result:
        return JsonResponse({"status": "error", "message": "Chưa có kết quả server để đính kèm file ghi âm."}, status=400)

    uploaded_file_name = getattr(audio_file, "name", "")
    uploaded_content_type = getattr(audio_file, "content_type", "")
    uploaded_size = getattr(audio_file, "size", 0)
    extension = resolve_uploaded_audio_extension(
        uploaded_file_name,
        uploaded_content_type,
    )
    logger.info(
        "Saving exam audio upload: session_id=%s question_id=%s uploaded_file_name=%s content_type=%s size=%s resolved_extension=%s",
        session_id,
        question_id,
        uploaded_file_name,
        uploaded_content_type,
        uploaded_size,
        extension,
    )

    try:
        converted_audio = _convert_uploaded_audio_to_mp3(
            audio_file=audio_file,
            session_id=str(session_id),
            question_id=str(question_id),
        )
    except Exception as exc:
        logger.exception(
            "Failed converting uploaded exam audio to mp3: session_id=%s question_id=%s uploaded_file_name=%s content_type=%s",
            session_id,
            question_id,
            uploaded_file_name,
            uploaded_content_type,
        )
        return JsonResponse(
            {"status": "error", "message": _get_audio_upload_error_message(exc)},
            status=500,
        )

    if result.audio_file:
        result.audio_file.delete(save=False)

    result.audio_file.save(
        converted_audio["stored_name"],
        ContentFile(converted_audio["content"]),
        save=True,
    )

    logger.info(
        "Saved exam audio upload as mp3: session_id=%s question_id=%s source_extension=%s stored_name=%s output_size=%s duration_seconds=%.3f model_audio_name=%s model_audio_path=%s model_audio_url=%s",
        session_id,
        question_id,
        converted_audio["source_extension"],
        converted_audio.get("output_size", 0),
        float(converted_audio.get("duration_seconds") or 0),
        result.audio_file.name if result.audio_file else "",
        _get_field_file_path(result.audio_file),
        result.audio_file.url if result.audio_file else "",
    )

    return JsonResponse(
        {
            "status": "ok",
            "result_id": result.pk,
            "audio_url": result.audio_file.url if result.audio_file else "",
        }
    )


@login_required
@require_GET
def exam_grading_status_view(request: HttpRequest, session_id: int, question_id: int) -> JsonResponse:
    session = get_object_or_404(
        ExamSession.objects.select_related("subject_id", "user_id", "exam_session_group_id"),
        pk=session_id,
    )
    _ensure_owner(session, request.user)

    if not session.questions.filter(pk=question_id).exists():
        return JsonResponse({"status": "error", "message": "Câu hỏi không thuộc phiên thi này."}, status=404)

    grading_state = get_exam_grading_state(session_id, question_id)
    if grading_state:
        return JsonResponse({"status": "success", "grading_state": grading_state})

    result = ExamResult.objects.filter(exam_session_id=session, question_id_id=question_id).first()
    if result:
        result_payload = {
            "type": "main_question_complete",
            "data": {
                "result_id": result.id,
                "score": round(_safe_float(result.score), 2),
                "question_id": int(question_id),
                "transcript": result.transcript or "",
                "feedback": result.feedback or "",
            },
        }
        return JsonResponse(
            {
                "status": "success",
                "grading_state": {
                    "status": EXAM_GRADING_STATUS_RESULT,
                    "session_id": int(session_id),
                    "question_id": int(question_id),
                    "progress": {},
                    "result": result_payload,
                    "error_message": "",
                    "updated_at": result.answered_at.isoformat() if result.answered_at else "",
                },
            }
        )

    return JsonResponse(
        {
            "status": "missing",
            "grading_state": {
                "status": EXAM_GRADING_STATUS_PENDING,
                "session_id": int(session_id),
                "question_id": int(question_id),
                "progress": {},
                "result": {},
                "error_message": "",
                "updated_at": "",
            },
        },
        status=404,
    )


@login_required
@require_POST
def save_violation_image(request: HttpRequest) -> JsonResponse:
    """
    YÊU CẦU 3: API nhận ảnh chụp gian lận từ client và lưu vào DB.
    """
    data = _json_body(request)
    session_id = data.get('session_id')
    image_base64 = data.get('image_base64')
    violation_type = data.get('violation_type', 'TWO_FACES')

    if not all([session_id, image_base64]):
        return JsonResponse({"status": "error", "message": "Thiếu dữ liệu ảnh."}, status=400)

    try:
        session = get_object_or_404(ExamSession, pk=session_id)
        _ensure_owner(session, request.user)

        format_part, imgstr = image_base64.split(";base64,")
        mime = format_part.split(":")[1]

        ViolationImage.objects.create(
            exam_session_id=session,
            image_blob=base64.b64decode(imgstr),
            image_mime=mime,
            violation_type=violation_type
        )

        # Bật cờ cảnh báo gian lận cho phiên thi với các cảnh báo cần hiển thị cho giảng viên.
        if should_mark_lecturer_cheating_flag(violation_type):
            session.cheating_flag = True
            session.save(update_fields=['cheating_flag'])

        return JsonResponse({"status": "success", "message": "Đã ghi nhận gian lận."})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=500)


@login_required
@require_POST
def finalize_session_view(request: HttpRequest, session_id: int) -> JsonResponse:
    session = get_object_or_404(
        ExamSession.objects.select_related("exam_session_group_id", "subject_id", "user_id"),
        pk=session_id,
    )
    _ensure_owner(session, request.user)

    # Idempotent finalize
    already_finalized = _is_session_finalized(session)
    if not already_finalized:
        if not _finalize_started_exam_session(session):
            _sync_session_completion_flags(session)
            _, final_total = _compute_scores(session)
            session.final_score = round(float(final_total or 0), 2)
            session.save(update_fields=["final_score"])

    _attach_session_score_meta(session)
    if "_attach_session_duration_meta" in (globals() or {}):
        _attach_session_duration_meta(session)
    deadline = _get_session_appeal_deadline(session)
    return JsonResponse(
        {
            "status": "success",
            "final_score": round(float(session.final_score or 0), 2),
            "score_scale_suffix": session.score_scale_suffix,
            "show_non_ten_scale_warning": session.show_non_ten_scale_warning,
            "score_scale_warning": session.score_scale_warning,
            "appeal_deadline_ms": int(deadline.timestamp() * 1000) if deadline else 0,
            "actual_duration_display": getattr(session, "actual_duration_display", ""),
            "already_finalized": already_finalized,
        }
    )

@login_required
@require_POST
def verify_student_face(request: HttpRequest) -> JsonResponse:
    face_image_data = request.POST.get("face_image")
    subject_code = request.POST.get("subject_code")
    if not face_image_data:
        return JsonResponse({"status": "error", "message": "Thiếu ảnh khuôn mặt."}, status=400)
    if not subject_code:
        return JsonResponse({"status": "error", "message": "Thiếu mã môn học."}, status=400)

    try:
        subject = get_object_or_404(Subject, subject_code=subject_code)
        active_room, display_room, exam_group = _get_student_exam_access_context(request.user, subject)
        if not active_room or not _is_exam_group_open(exam_group):
            return JsonResponse(
                {
                    "status": "error",
                    "message": _student_exam_access_denied_message(
                        display_room,
                        no_room_message="Bạn chưa được phân vào phòng thi của môn này.",
                    ),
                },
                status=400,
            )
        session = _get_student_exam_session(request.user, subject, exam_group)
        locked_response = _locked_student_exam_json(session)
        if locked_response:
            return locked_response
        if not _student_has_room_password_access(request, exam_group, active_room):
            return JsonResponse(
                {
                    "status": "error",
                    "message": "Bạn cần xác thực mật khẩu phòng thi trước.",
                },
                status=403,
            )

        format_part, imgstr = face_image_data.split(";base64,", 1)
        image_mime = format_part.split(":", 1)[-1] or "image/jpeg"
        base64.b64decode(imgstr)
        entry_payload = _store_student_exam_entry_payload(
            request,
            exam_group,
            active_room,
            face_image_b64=imgstr,
            face_image_mime=image_mime,
        )
        return JsonResponse(
            {
                "status": "success",
                "entry_token": entry_payload["entry_token"],
                "message": "Đã lưu ảnh xác thực thành công.",
            }
        )
    except Exception as e:
        return JsonResponse({"status": "error", "message": f"Lỗi khi xử lý ảnh: {str(e)}"}, status=500)


@login_required
def get_verification_images(request: HttpRequest, session_id: int) -> HttpResponse:
    session = get_object_or_404(
        ExamSession.objects.select_related("subject_id", "user_id", "user_id__userprofile"),
        pk=session_id,
    )

    profile = getattr(request.user, "userprofile", None)
    is_owner = session.user_id == request.user
    can_view_as_lecturer = bool(
        profile
        and profile.is_lecturer
        and session.subject in profile.subjects_taught.all()
    )
    if not (is_owner or can_view_as_lecturer):
        return JsonResponse(
            {"status": "error", "message": "Bạn không có quyền xem ảnh xác thực này."},
            status=403,
        )

    face_data_url = (
        f"data:{session.face_image_mime};base64,{b64encode(session.face_image_blob).decode('ascii')}"
        if session.face_image_blob and session.face_image_mime else None
    )
    id_card_data_url = (
        f"data:{session.id_card_image_mime};base64,{b64encode(session.id_card_image_blob).decode('ascii')}"
        if session.id_card_image_blob and session.id_card_image_mime else None
    )

    return JsonResponse(
        {
            "status": "success",
            "face_image": face_data_url,
            "id_card_image": id_card_data_url,
            "verification_score": session.verification_score,
            "verification_status": session.verification_status,
            "needs_manual_review": session.needs_manual_review,
        }
    )


# =========================================================
# STUDENT LIST MANAGEMENT
# =========================================================
STUDENT_LIST_ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv"}
STUDENT_LIST_MAX_FILE_SIZE = 10 * 1024 * 1024
STUDENT_LIST_REQUIRED_COLUMNS = {
    "student_code": {"mssv", "ma_so_sinh_vien", "student_id", "student_code", "username", "msv"},
    "full_name": {"ho_va_ten", "ho_ten", "full_name", "name", "ten_sinh_vien"},
}
STUDENT_LIST_OPTIONAL_COLUMNS = {
    "gender": {"gioi_tinh", "gender", "sex"},
    "date_of_birth": {"ngay_sinh", "date_of_birth", "dob", "birth_date"},
    "class_name": {"lop", "class", "class_name", "ten_lop"},
    "email": {"email", "mail"},
}


def _student_default_academic_year():
    now = timezone.localtime()
    start_year = now.year if now.month >= 8 else now.year - 1
    return f"{start_year}-{start_year + 1}"


def _student_normalize_header(value):
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "_", value.lower()).strip("_")
    return value


def _student_clean_cell(value):
    if value is None:
        return ""
    return str(value).strip()


def _student_parse_birth(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"]:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _student_normalize_gender(value):
    text = _student_clean_cell(value).lower()
    if text in {"nam", "male", "m"}:
        return "Nam"
    if text in {"nu", "nữ", "female", "f"}:
        return "Nữ"
    return _student_clean_cell(value)


def _student_user_is_eligible(user):
    if user is None:
        return False
    if user.is_staff or user.is_superuser:
        return False
    try:
        return not user.userprofile.is_lecturer
    except UserProfile.DoesNotExist:
        return True


def _student_find_user(student_code):
    student_code = normalize_student_code(student_code)
    if not student_code:
        return None

    user = User.objects.select_related("userprofile").filter(username__iexact=student_code).first()
    if _student_user_is_eligible(user):
        return user

    profile = (
        UserProfile.objects.select_related("user_id")
        .filter(student_id__iexact=student_code, is_lecturer=False)
        .first()
    )
    if profile:
        return profile.user_id

    roster_row = (
        StudentRosterStudent.objects.select_related("linked_user_id")
        .filter(student_code__iexact=student_code, linked_user_id__isnull=False)
        .order_by("-created_at", "-pk")
        .first()
    )
    if roster_row and _student_user_is_eligible(roster_row.linked_user_id):
        return roster_row.linked_user_id

    return None


def _student_validate_file(uploaded_file):
    extension = os.path.splitext(uploaded_file.name or "")[1].lower()
    if extension not in STUDENT_LIST_ALLOWED_EXTENSIONS:
        raise ValidationError("Hệ thống chỉ chấp nhận file Excel (.xlsx, .xls) hoặc CSV.")
    if uploaded_file.size > STUDENT_LIST_MAX_FILE_SIZE:
        raise ValidationError("Dung lượng file vượt quá 10MB. Vui lòng kiểm tra lại.")


def _student_resolve_columns(headers):
    mapping = {}
    normalized = [_student_normalize_header(item) for item in headers]

    for target, aliases in STUDENT_LIST_REQUIRED_COLUMNS.items():
        for index, header in enumerate(normalized):
            if header in aliases:
                mapping[target] = index
                break
        if target not in mapping:
            raise ValidationError(f"Thiếu cột bắt buộc cho danh sách sinh viên: {target}.")

    for target, aliases in STUDENT_LIST_OPTIONAL_COLUMNS.items():
        for index, header in enumerate(normalized):
            if header in aliases:
                mapping[target] = index
                break

    return mapping


def _student_parse_csv_rows(file_bytes):
    text = file_bytes.decode("utf-8-sig", errors="ignore")
    reader = csv.reader(io.StringIO(text))
    return [row for row in reader if any(str(cell).strip() for cell in row)]


def _student_parse_xlsx_rows(file_bytes):
    workbook = load_workbook(io.BytesIO(file_bytes), data_only=True)
    worksheet = workbook.active
    rows = []
    for row in worksheet.iter_rows(values_only=True):
        values = ["" if value is None else value for value in row]
        if any(str(cell).strip() for cell in values):
            rows.append(values)
    return rows


def _student_parse_xls_rows(file_bytes):
    try:
        import pandas as pd
    except Exception as exc:
        raise ValidationError("Muốn đọc file .xls, vui lòng cài thêm xlrd==2.0.1 và pandas.") from exc

    try:
        dataframe = pd.read_excel(io.BytesIO(file_bytes), dtype=str)
    except Exception as exc:
        raise ValidationError(f"Không thể đọc file .xls: {exc}") from exc

    rows = [list(dataframe.columns)]
    rows.extend(dataframe.fillna("").values.tolist())
    return rows


def _student_parse_records(file_name, file_bytes):
    extension = os.path.splitext(file_name or "")[1].lower()
    if extension == ".csv":
        rows = _student_parse_csv_rows(file_bytes)
    elif extension == ".xlsx":
        rows = _student_parse_xlsx_rows(file_bytes)
    elif extension == ".xls":
        rows = _student_parse_xls_rows(file_bytes)
    else:
        raise ValidationError("Định dạng file không hợp lệ.")

    if len(rows) < 2:
        raise ValidationError("File được chọn không có dữ liệu.")

    headers = rows[0]
    column_map = _student_resolve_columns(headers)

    records = []
    seen_codes = set()
    skipped_rows = []

    for row_number, row in enumerate(rows[1:], start=2):
        student_code = normalize_student_code(
            row[column_map["student_code"]] if column_map["student_code"] < len(row) else ""
        )
        full_name = _student_clean_cell(
            row[column_map["full_name"]] if column_map["full_name"] < len(row) else ""
        )

        if not student_code or not full_name:
            skipped_rows.append({"row_number": row_number, "message": f"Dòng {row_number}: thiếu MSSV hoặc họ tên."})
            continue

        if student_code in seen_codes:
            skipped_rows.append(
                {"row_number": row_number, "message": f"Dòng {row_number}: trùng MSSV {student_code} trong cùng file."}
            )
            continue
        seen_codes.add(student_code)

        gender = _student_normalize_gender(
            row[column_map["gender"]]
            if column_map.get("gender") is not None and column_map["gender"] < len(row)
            else ""
        )
        date_of_birth = _student_parse_birth(
            row[column_map["date_of_birth"]]
            if column_map.get("date_of_birth") is not None and column_map["date_of_birth"] < len(row)
            else ""
        )
        class_name = _student_clean_cell(
            row[column_map["class_name"]]
            if column_map.get("class_name") is not None and column_map["class_name"] < len(row)
            else ""
        )
        email = _student_clean_cell(
            row[column_map["email"]]
            if column_map.get("email") is not None and column_map["email"] < len(row)
            else ""
        )

        records.append(
            {
                "student_code": student_code,
                "full_name": full_name,
                "gender": gender,
                "date_of_birth": date_of_birth,
                "class_name": class_name,
                "email": email,
            }
        )

    if not records:
        raise ValidationError("Không tìm thấy sinh viên hợp lệ trong file tải lên.")

    return records, skipped_rows


def _student_create_roster(subject, created_by, academic_year, semester, uploaded_file, mode="new"):
    if not is_valid_academic_year(academic_year):
        raise ValidationError(ACADEMIC_YEAR_ERROR_MESSAGE)

    if mode == "replace":
        existing_rosters = StudentRosterUpload.objects.filter(
            subject_id=subject,
            academic_year=academic_year,
            semester=semester,
        )

        for old_roster in existing_rosters.prefetch_related("students"):
            linked_users = list(
                old_roster.students.filter(linked_user_id__isnull=False)
                .values_list("linked_user_id", flat=True)
                .distinct()
            )

            if old_roster.uploaded_file:
                old_roster.uploaded_file.delete(save=False)
            old_roster.delete()

            for user_id in linked_users:
                user = User.objects.filter(pk=user_id).first()
                if user:
                    _revoke_student_enrolled_subject_if_unused(user, subject)

    _student_validate_file(uploaded_file)
    file_bytes = uploaded_file.read()
    if not file_bytes:
        raise ValidationError("File được chọn không có dữ liệu.")

    records, skipped_rows = _student_parse_records(uploaded_file.name, file_bytes)

    existing_codes = set(
        StudentRosterStudent.objects.filter(
            student_roster_upload_id__subject_id=subject,
            student_roster_upload_id__academic_year=academic_year,
            student_roster_upload_id__semester=semester,
        ).values_list("student_code", flat=True)
    )

    duplicate_codes = set()
    for record in records:
        if record["student_code"] in existing_codes:
            duplicate_codes.add(record["student_code"])

    if duplicate_codes:
        skipped_rows.append({
            "row": None,
            "message": f"Các MSSV sau đã tồn tại trong danh sách khác của môn {subject.name} "
                       f"năm học {academic_year} học kỳ {semester}: {', '.join(sorted(duplicate_codes))}. "
                       "Có thể là import trùng hoặc học lại."
        })

    existing_roster_count = StudentRosterUpload.objects.filter(
        subject_id=subject,
        academic_year=academic_year,
        semester=semester
    ).count()
    if existing_roster_count > 0:
        skipped_rows.append({
            "row": None,
            "message": f"Đã có {existing_roster_count} danh sách sinh viên cho môn {subject.name} "
                       f"năm học {academic_year} học kỳ {semester}. "
                       "Có thể là import trùng hoặc học lại. Hệ thống vẫn tạo danh sách mới."
        })

    roster = StudentRosterUpload(
        subject_id=subject,
        academic_year=academic_year,
        semester=semester,
        title=os.path.splitext(uploaded_file.name)[0],
        original_file_name=uploaded_file.name,
        total_students=len(records),
        created_by_user_id=created_by,
        status=StudentListUploadStatus.PENDING,
    )
    roster.uploaded_file.save(
        f"{uuid4().hex}_{uploaded_file.name}",
        ContentFile(file_bytes),
        save=False,
    )
    roster.save()

    students = []
    for index, item in enumerate(records, start=1):
        linked_user = _student_find_user(item["student_code"])
        students.append(
            StudentRosterStudent(
                student_roster_upload_id=roster,
                row_number=index,
                student_code=item["student_code"],
                full_name=item["full_name"],
                gender=item["gender"],
                date_of_birth=item["date_of_birth"],
                class_name=item["class_name"],
                email=item["email"],
                linked_user_id=linked_user,
                account_created=bool(linked_user),
                account_status=(
                    StudentRosterAccountStatus.EXISTING
                    if linked_user
                    else StudentRosterAccountStatus.PENDING
                ),
            )
        )

    StudentRosterStudent.objects.bulk_create(students)
    roster.refresh_status()

    # Nếu file roster đã map được tới user có sẵn thì cấp quyền môn ngay
    _sync_roster_enrollments(roster)

    roster.import_warnings = skipped_rows
    return roster


def _student_sync_profile(user, student_row):
    profile, _ = UserProfile.objects.get_or_create(user_id=user)
    profile.is_lecturer = False
    profile.full_name = student_row.full_name
    profile.class_name = student_row.class_name
    profile.student_id = normalize_student_code(student_row.student_code)
    profile.save()
    return profile


def _student_provision_account(student_row, default_password):
    user = student_row.linked_user_id if _student_user_is_eligible(student_row.linked_user_id) else None
    if user is None:
        user = _student_find_user(student_row.student_code)

    normalized_code = normalize_student_code(student_row.student_code) or student_row.student_code
    created = False

    if user is None:
        user = User.objects.create_user(
            username=normalized_code,
            password=default_password,
            email=student_row.email or "",
        )
        created = True
    else:
        user_changed = False
        if student_row.email and user.email != student_row.email:
            user.email = student_row.email
            user_changed = True
        if not user.is_active:
            user.is_active = True
            user_changed = True
        if user_changed:
            user.save()

    _student_sync_profile(user, student_row)
    return user, created, False


def _student_roster_queryset(subject):
    return StudentRosterUpload.objects.filter(subject_id=subject).annotate(
        student_count=Count("students", distinct=True),
        created_accounts_total=Count(
            "students",
            filter=Q(students__account_created=True),
            distinct=True,
        ),
        existing_accounts_total=Count(
            "students",
            filter=Q(students__account_status=StudentRosterAccountStatus.EXISTING),
            distinct=True,
        ),
        new_accounts_total=Count(
            "students",
            filter=Q(students__account_status=StudentRosterAccountStatus.CREATED),
            distinct=True,
        ),
    ).order_by("-created_at", "-pk")


def _student_roster_summary(rosters):
    raw_summary = rosters.aggregate(
        total_files=Count("pk"),
        completed_files=Count(
            "pk",
            filter=Q(status=StudentListUploadStatus.CREATED),
            distinct=True,
        ),
        total_students=Sum("student_count"),
        total_created_accounts=Sum("created_accounts_total"),
    )
    total_files = raw_summary.get("total_files") or 0
    completed_files = raw_summary.get("completed_files") or 0
    total_students = raw_summary.get("total_students") or 0
    total_created_accounts = raw_summary.get("total_created_accounts") or 0

    return {
        "total_files": total_files,
        "completed_files": completed_files,
        "pending_files": max(0, total_files - completed_files),
        "total_students": total_students,
        "total_created_accounts": total_created_accounts,
        "pending_accounts": max(0, total_students - total_created_accounts),
        "all_files_completed": bool(total_files and completed_files == total_files),
    }


@login_required
def lecturer_student_list_management(request, subject_code):
    _ensure_lecturer(request)
    subject = get_object_or_404(_get_lecturer_subjects(request), subject_code=subject_code)

    rosters = _student_roster_queryset(subject)
    keyword = (request.GET.get("q") or "").strip()
    academic_year = (request.GET.get("academic_year") or "").strip()
    semester = (request.GET.get("semester") or "").strip()
    status = (request.GET.get("status") or "").strip()

    if keyword:
        rosters = rosters.filter(
            Q(title__icontains=keyword)
            | Q(original_file_name__icontains=keyword)
            | Q(students__student_code__icontains=keyword)
        ).distinct()
    if academic_year and is_valid_academic_year(academic_year):
        rosters = rosters.filter(academic_year=academic_year)
    if semester:
        rosters = rosters.filter(semester=semester)
    if status:
        rosters = rosters.filter(status=status)

    paginator = Paginator(rosters, LIST_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    for roster_item in page_obj.object_list:
        roster_item.safe_academic_year = sanitize_academic_year_for_display(roster_item.academic_year)
    roster_summary = _student_roster_summary(rosters)

    context = {
        "subject": subject,
        "page_obj": page_obj,
        "roster_summary": roster_summary,
        "completed_rosters_preview": rosters.filter(
            status=StudentListUploadStatus.CREATED,
            account_created_at__isnull=False,
        ).order_by("-account_created_at")[:8],
        "filters": {
            "q": keyword,
            "academic_year": academic_year if is_valid_academic_year(academic_year) else "",
            "semester": semester,
            "status": status,
        },
        "academic_year_options": StudentRosterUpload.objects.filter(subject_id=subject)
        .values_list("academic_year", flat=True)
        .distinct()
        .order_by("-academic_year"),
        "semester_options": SemesterChoices.choices,
        "status_options": StudentListUploadStatus.choices,
    }
    context["academic_year_options"] = [
        year for year in context["academic_year_options"] if sanitize_academic_year_for_display(year)
    ]
    return render(request, "qna/lecturer/lecturer_student_list_management.html", context)


@login_required
def lecturer_student_list_template(request, subject_code):
    _ensure_lecturer(request)
    subject = get_object_or_404(_get_lecturer_subjects(request), subject_code=subject_code)

    workbook = OpenpyxlWorkbook()
    worksheet = workbook.active
    worksheet.title = "Danh sách sinh viên"
    worksheet.append(["Mã số sinh viên", "Họ và tên", "Giới tính", "Ngày sinh", "Lớp", "Email"])
    worksheet.append(["SV2024001", "Nguyễn Văn An", "Nam", "12/05/2003", "CNT01", "sv2024001@example.com"])
    worksheet.append(["SV2024002", "Trần Thị Bích", "Nữ", "24/08/2003", "CNT01", "sv2024002@example.com"])

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="Template_Danh_Sach_SV_{subject.subject_code}.xlsx"'
    workbook.save(response)
    return response


@login_required
def lecturer_student_list_detail(request, roster_id):
    _ensure_lecturer(request)
    roster = get_object_or_404(
        StudentRosterUpload.objects.select_related("subject_id", "created_by_user_id"),
        pk=roster_id,
    )
    if roster.subject not in request.user.userprofile.subjects_taught.all():
        raise PermissionDenied("Bạn không có quyền truy cập danh sách này.")

    keyword = (request.GET.get("q") or "").strip()
    students_qs = roster.students.select_related("linked_user_id").order_by("row_number")

    if keyword:
        students_qs = students_qs.filter(
            Q(student_code__icontains=keyword)
            | Q(full_name__icontains=keyword)
            | Q(class_name__icontains=keyword)
        )

    paginator = Paginator(students_qs, 8)
    page_obj = paginator.get_page(request.GET.get("page"))

    roster.refresh_status()
    account_breakdown = roster.students.aggregate(
        existing_accounts=Count("pk", filter=Q(account_status=StudentRosterAccountStatus.EXISTING)),
        new_accounts=Count("pk", filter=Q(account_status=StudentRosterAccountStatus.CREATED)),
        pending_accounts=Count("pk", filter=Q(account_status=StudentRosterAccountStatus.PENDING)),
    )
    create_account_modal = request.session.pop("student_account_modal", None) or {}

    context = {
        "roster": roster,
        "subject": roster.subject,
        "page_obj": page_obj,
        "search_query": keyword,
        "pending_accounts_count": roster.pending_accounts_count,
        "created_accounts_count": roster.created_accounts_count,
        "existing_accounts_count": account_breakdown.get("existing_accounts") or 0,
        "new_accounts_count": account_breakdown.get("new_accounts") or 0,
        "uncreated_accounts_count": account_breakdown.get("pending_accounts") or 0,
        "create_account_modal": {
            "open": bool(create_account_modal.get("open")),
            "error": create_account_modal.get("error", ""),
            "existing_students": create_account_modal.get("existing_students", []),
            "need_skip_confirm": bool(create_account_modal.get("need_skip_confirm")),
        },
        "password_rules": [],
    }
    return render(request, "qna/lecturer/lecturer_student_list_detail.html", context)


def _student_account_modal_error(request, roster, message, existing_students=None, need_skip_confirm=False):
    request.session["student_account_modal"] = {
        "open": True,
        "error": message,
        "existing_students": existing_students or [],
        "need_skip_confirm": need_skip_confirm,
    }
    return redirect("qna:lecturer_student_list_detail", roster_id=roster.id)


def _validate_student_batch_password(password):
    if not str(password or "").strip():
        raise ValidationError("Vui lòng nhập mật khẩu.")


@login_required
@require_POST
def lecturer_student_list_delete(request, roster_id):
    _ensure_lecturer(request)
    roster = get_object_or_404(
        StudentRosterUpload.objects.select_related("subject_id"),
        pk=roster_id,
    )
    if roster.subject not in request.user.userprofile.subjects_taught.all():
        raise PermissionDenied("Bạn không có quyền xóa danh sách này.")

    subject = roster.subject
    subject_code = subject.subject_code

    linked_users = list(
        roster.students.filter(linked_user_id__isnull=False)
        .values_list("linked_user_id", flat=True)
        .distinct()
    )

    if roster.uploaded_file:
        roster.uploaded_file.delete(save=False)
    roster.delete()

    for user_id in linked_users:
        user = User.objects.filter(pk=user_id).first()
        if user:
            _revoke_student_enrolled_subject_if_unused(user, subject)

    messages.success(request, "Xóa danh sách lớp thành công")
    return redirect("qna:lecturer_student_list_management", subject_code=subject_code)


@login_required
def lecturer_student_list_upload(request, subject_code):
    _ensure_lecturer(request)
    subject = get_object_or_404(_get_lecturer_subjects(request), subject_code=subject_code)

    if request.method == "POST":
        academic_year = "" if request.POST.get("academic_year") is None else str(request.POST.get("academic_year"))
        semester = (request.POST.get("semester") or SemesterChoices.HK1).strip()
        if semester not in {choice[0] for choice in SemesterChoices.choices}:
            semester = SemesterChoices.HK1
        mode = request.POST.get("mode", "new")

        files = request.FILES.getlist("files")
        wants_json = _is_ajax_request(request)

        if not is_valid_academic_year(academic_year):
            message = ACADEMIC_YEAR_ERROR_MESSAGE
            if wants_json:
                return JsonResponse({"success": False, "message": message}, status=400)
            messages.error(request, message)
            return redirect("qna:lecturer_student_list_upload", subject_code=subject.subject_code)

        if not files:
            message = "Vui lòng chọn ít nhất 1 file danh sách sinh viên."
            if wants_json:
                return JsonResponse({"success": False, "message": message}, status=400)
            messages.error(request, message)
            return redirect("qna:lecturer_student_list_upload", subject_code=subject.subject_code)

        success_items = []
        errors = []

        for uploaded_file in files:
            try:
                with transaction.atomic():
                    roster = _student_create_roster(
                        subject=subject,
                        created_by=request.user,
                        academic_year=academic_year,
                        semester=semester,
                        uploaded_file=uploaded_file,
                        mode=mode,
                    )
                success_items.append(
                    {
                        "roster_id": roster.id,
                        "file_name": roster.original_file_name,
                        "total_students": roster.total_students,
                        "status": roster.status,
                        "status_label": roster.get_status_display(),
                        "detail_url": f"/lecturer/student-lists/{roster.id}/",
                        "created_at": timezone.localtime(roster.created_at).strftime("%H:%M %d/%m/%Y"),
                        "warnings": getattr(roster, "import_warnings", []),
                    }
                )
            except Exception as exc:
                error_message = " ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
                errors.append({"file_name": uploaded_file.name, "message": error_message})

        if wants_json:
            return JsonResponse(
                {
                    "success": bool(success_items) and not errors,
                    "uploaded_count": len(success_items),
                    "results": success_items,
                    "errors": errors,
                    "message": (
                        f"Tải lên thành công {len(success_items)} file danh sách sinh viên."
                        if success_items
                        else "Tải file thất bại."
                    ),
                },
                status=200 if success_items else 400,
            )

        if success_items:
            messages.success(request, f"Tải lên thành công {len(success_items)} file danh sách sinh viên.")
            for item in success_items:
                warnings = item.get("warnings") or []
                if warnings:
                    preview = "; ".join(warning["message"] for warning in warnings[:5])
                    extra_count = max(0, len(warnings) - 5)
                    extra_suffix = f" (+{extra_count} dòng khác)" if extra_count else ""
                    messages.warning(
                        request,
                        f"{item['file_name']}: bỏ qua {len(warnings)} dòng không hợp lệ hoặc trùng lặp. {preview}{extra_suffix}",
                    )

        for error in errors:
            messages.error(request, f"{error['file_name']}: {error['message']}")

        if success_items:
            return redirect("qna:lecturer_student_list_management", subject_code=subject.subject_code)

    context = {
        "subject": subject,
        "default_academic_year": _student_default_academic_year(),
        "semester_options": SemesterChoices.choices,
        "max_upload_mb": STUDENT_LIST_MAX_FILE_SIZE // (1024 * 1024),
    }
    return render(request, "qna/lecturer/lecturer_student_list_upload.html", context)


@login_required
@require_POST
def lecturer_student_list_create_accounts(request, roster_id):
    _ensure_lecturer(request)
    roster = get_object_or_404(StudentRosterUpload.objects.select_related("subject_id"), pk=roster_id)
    if roster.subject not in request.user.userprofile.subjects_taught.all():
        raise PermissionDenied("Bạn không có quyền thao tác danh sách này.")

    default_password = (request.POST.get("default_password") or "").strip()
    confirm_password = (request.POST.get("confirm_password") or "").strip()
    skip_existing = str(request.POST.get("skip_existing") or "").strip() in {"1", "true", "yes", "on"}

    if not default_password:
        return _student_account_modal_error(request, roster, "Vui lòng nhập mật khẩu.")

    if default_password != confirm_password:
        return _student_account_modal_error(request, roster, "Mật khẩu xác nhận không khớp.")

    try:
        _validate_student_batch_password(default_password)
    except ValidationError as exc:
        return _student_account_modal_error(request, roster, " ".join(exc.messages))

    roster.refresh_status()
    if roster.pending_accounts_count == 0:
        messages.warning(request, "Danh sách này đã được tạo tài khoản")
        return redirect("qna:lecturer_student_list_detail", roster_id=roster.id)

    existing_rows = []
    pending_rows = []

    def _row_is_created_in_roster(student_row):
        return (
            student_row.account_created
            and student_row.account_status == StudentRosterAccountStatus.CREATED
        )

    def _row_has_canonical_existing_account(student_row, existing_user):
        if not student_row.account_created or not existing_user:
            return False
        return normalize_student_code(existing_user.username) == normalize_student_code(student_row.student_code)

    for student_row in roster.students.select_related("linked_user_id").order_by("row_number"):
        if _row_is_created_in_roster(student_row):
            continue
        existing_user = student_row.linked_user_id or _student_find_user(student_row.student_code)
        if existing_user or student_row.account_status == StudentRosterAccountStatus.EXISTING:
            if _row_has_canonical_existing_account(student_row, existing_user):
                continue
            existing_rows.append(
                {
                    "student_code": student_row.student_code,
                    "full_name": student_row.full_name,
                    "class_name": student_row.class_name,
                }
            )
        else:
            pending_rows.append(student_row)

    if existing_rows and not skip_existing:
        return _student_account_modal_error(
            request,
            roster,
            "Một số sinh viên đã có tài khoản. Vui lòng bấm 'Bỏ qua' để tiếp tục tạo cho các sinh viên còn lại.",
            existing_students=existing_rows,
            need_skip_confirm=True,
        )

    created_count = 0
    skipped_existing_count = len(existing_rows)

    try:
        with transaction.atomic():
            for student_row in roster.students.select_related("linked_user_id").order_by("row_number"):
                if _row_is_created_in_roster(student_row):
                    continue

                existing_user = student_row.linked_user_id or _student_find_user(student_row.student_code)
                if existing_user:
                    student_row.linked_user_id = existing_user
                    student_row.account_created = True
                    student_row.account_status = StudentRosterAccountStatus.EXISTING
                    student_row.save(update_fields=["linked_user_id", "account_created", "account_status"])
                    _ensure_student_enrolled_subject(existing_user, roster.subject)

            for student_row in pending_rows:
                user, created, _ = _student_provision_account(student_row, default_password)
                student_row.linked_user_id = user
                student_row.account_created = True
                student_row.account_status = (
                    StudentRosterAccountStatus.CREATED if created else StudentRosterAccountStatus.EXISTING
                )
                student_row.save(update_fields=["linked_user_id", "account_created", "account_status"])

                # Dù là newly created hay do race-condition thành existing, đều phải cấp quyền môn
                _ensure_student_enrolled_subject(user, roster.subject)

                if created:
                    created_count += 1

            related_rosters = StudentRosterUpload.objects.filter(
                subject_id=roster.subject_id,
                original_file_name=roster.original_file_name,
            )
            for related_roster in related_rosters:
                related_roster.refresh_status()

    except Exception:
        logger.exception("Batch create student accounts failed for roster_id=%s", roster.id)
        messages.error(
            request,
            "Lỗi đường truyền khi tạo tài khoản. Hệ thống đã hoàn tác toàn bộ, vui lòng tạo lại.",
        )
        return redirect("qna:lecturer_student_list_detail", roster_id=roster.id)

    if skipped_existing_count:
        request.session["student_account_modal"] = {
            "open": True,
            "error": "",
            "existing_students": existing_rows,
            "need_skip_confirm": False,
        }

    messages.success(
        request,
        f"Tạo tài khoản hoàn tất: {created_count} tài khoản mới, {skipped_existing_count} sinh viên đã có sẵn tài khoản.",
    )
    return redirect("qna:lecturer_student_list_detail", roster_id=roster.id)


# =========================================================
# EXAM MANAGEMENT / SESSION MANAGEMENT
# =========================================================
from .exam_management import (
    lecturer_approve_exam_code,
    lecturer_bulk_approve_exam_codes,
    lecturer_create_exam_set,
    lecturer_create_manual_exam_code,
    lecturer_delete_exam_code,
    lecturer_delete_exam_set,
    lecturer_exam_codes_screen,
    lecturer_exam_set_detail_screen,
    lecturer_generate_codes_screen,
    lecturer_publish_exam_set,
    lecturer_regenerate_exam_code,
    lecturer_save_exam_set_draft,
    lecturer_update_exam_code_content,
)

from .session_management import (
    lecturer_create_exam_group,
    lecturer_create_exam_group_screen,
    lecturer_create_exam_session,
    lecturer_create_session_screen,
    lecturer_delete_exam_group,
    lecturer_exam_session_common_list_export,
    lecturer_exam_session_common_list_screen,
    lecturer_exam_session_detail_screen,
    lecturer_exam_sessions_list,
    lecturer_import_students_to_room,
    lecturer_random_assign_students,
    lecturer_update_exam_group,
)

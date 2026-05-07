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
from .lecturer_student_accounts import normalize_student_code

logger = logging.getLogger(__name__)
User = get_user_model()
TEN_POINT_SCALE = 10.0
PASSWORD_ATTEMPT_LIMIT = 5
PASSWORD_ATTEMPT_WINDOW = 300
LIST_PAGE_SIZE = 8

from .permissions import (
    lecturer_required,
    student_required,
    lecturer_required_json,
    student_required_json,
    get_lecturer_subject_or_404,
    lecturer_can_access_subject,
    user_can_access_exam_session,
    forbid_if_no_exam_session_access,
)
from .exam_result_helpers import (
    _with_lecturer_visible_violation_count,
    _has_lecturer_visible_violation,
    _session_has_exam_results,
    _student_has_started_exam,
    _is_exam_group_closed_for_status,
    _resolve_exam_session_status,
    _safe_float,
    _normalize_optional_int,
    _format_score_scale_value,
    _format_question_score_suffix,
    _format_total_score_suffix,
    _get_room_assignment_for_user,
    _get_session_assignment_context,
    _get_session_assigned_exam_code_id,
    _get_session_exam_blueprint,
    _get_session_total_question_count,
    _get_session_raw_total_score,
    _get_session_assigned_exam_set,
    _get_session_total_score_scale,
    _get_exam_set_question_max_score,
    _resolve_session_question_max_score,
    _scale_session_question_score,
    _attach_session_score_meta,
    _get_answered_question_count,
    _build_session_question_details,
    _compute_scores,
    _describe_violation_type,
    _format_duration_display,
    _get_session_duration_seconds,
    _get_session_latest_answered_at,
    _derive_session_completion_time,
    _attach_session_duration_meta,
    _attach_session_assignment_meta,
    _is_session_finalized,
)
from .student_exam_helpers import (
    _get_student_assigned_rooms_for_subject,
    _get_student_current_room_for_subject,
    _get_student_display_room_for_subject,
    _get_student_exam_access_context,
    _is_exam_group_open,
    _deny_if_exam_closed,
    _student_exam_access_denied_message,
    _student_exam_entry_session_key,
    _student_exam_password_session_key,
    _store_student_room_password_access,
    _student_has_room_password_access,
    _store_student_exam_entry_payload,
    _get_student_exam_entry_payload,
    _clear_student_exam_entry_payload,
    _get_student_exam_session,
    _student_exam_reentry_state,
    _redirect_locked_student_exam,
    _locked_student_exam_json,
    _ensure_student_absent_sessions_for_history,
    _sync_student_subjects_from_rosters,
)
from django.core.files.storage import default_storage
# =========================================================
# COMMON HELPERS
# =========================================================
def _ensure_lecturer(request):
    profile = getattr(request.user, "userprofile", None)
    if not profile or not profile.is_lecturer:
        raise PermissionDenied("Không có quyền truy cập.")




def _json_body(request: HttpRequest) -> Dict[str, Any]:
    try:
        if request.body:
            return json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return {}

def _exam_heartbeat_key(session_id: int) -> str:
    return f"exam_heartbeat:{session_id}"


def _exam_active_sessions_key() -> str:
    return "exam_active_sessions"


def _mark_exam_session_active(session_id: int) -> None:
    try:
        active_ids = cache.get(_exam_active_sessions_key(), set())
        if not isinstance(active_ids, set):
            active_ids = set(active_ids or [])
        active_ids.add(int(session_id))
        cache.set(_exam_active_sessions_key(), active_ids, timeout=24 * 60 * 60)
    except Exception:
        pass


def _unmark_exam_session_active(session_id: int) -> None:
    try:
        active_ids = cache.get(_exam_active_sessions_key(), set())
        if not isinstance(active_ids, set):
            active_ids = set(active_ids or [])
        active_ids.discard(int(session_id))
        cache.set(_exam_active_sessions_key(), active_ids, timeout=24 * 60 * 60)
    except Exception:
        pass

def _is_ajax_request(request):
    return (
            request.headers.get("x-requested-with") == "XMLHttpRequest"
            or "application/json" in request.headers.get("accept", "")
    )




def _extract_ffmpeg_duration_seconds(metadata_text: str) -> float:
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", metadata_text or "")
    if not match:
        return 0.0

    hours, minutes, seconds = match.groups()
    return (int(hours) * 3600) + (int(minutes) * 60) + float(seconds)

def _exam_active_question_key(session_id: int) -> str:
    return f"exam_active_question:{session_id}"


def _exam_recording_meta_key(session_id: int, question_id: int) -> str:
    return f"exam_recording_meta:{session_id}:{question_id}"


def _exam_recording_chunk_dir(session_id: int, question_id: int) -> str:
    return f"exam_recording_drafts/session_{session_id}/question_{question_id}"

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

def _locked_subject_mutation_response(response_style="status"):
    message = "Không thể chỉnh sửa khi ca thi đang diễn ra."
    if response_style == "legacy":
        return JsonResponse({"success": False, "error": message}, status=403)
    return JsonResponse({"status": "FAIL", "message": message}, status=403)


QUESTION_TEXT_MAX_LENGTH = 5000
QUESTION_TEXT_TOO_LONG_MESSAGE = "Nội dung câu hỏi không được vượt quá 5000 ký tự."


def _room_password_attempts_key(user_id: int, subject_code: str) -> str:
    return f"room_pwd_attempts:{user_id}:{subject_code}"

def _is_ten_point_scale(raw_total_score: float | None) -> bool:
    return raw_total_score is not None and abs(float(raw_total_score) - TEN_POINT_SCALE) < 0.001


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

        if session.completed_at is None:
            session.completed_at = timezone.now()
            session.save(update_fields=["completed_at"])

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

@login_required
@require_POST
@student_required_json
def save_exam_recording_chunk_view(request: HttpRequest) -> JsonResponse:
    session_id = request.POST.get("session_id")
    question_id = request.POST.get("question_id")
    chunk_index = request.POST.get("chunk_index")
    audio_mime_type = request.POST.get("audio_mime_type") or "audio/webm"
    audio_chunk = request.FILES.get("audio_chunk")

    if not session_id or not question_id or chunk_index is None:
        return JsonResponse(
            {"status": "error", "message": "Thiếu dữ liệu chunk ghi âm."},
            status=400,
        )

    if not audio_chunk:
        return JsonResponse(
            {"status": "error", "message": "Không tìm thấy chunk ghi âm."},
            status=400,
        )

    try:
        session_id_int = int(session_id)
        question_id_int = int(question_id)
        chunk_index_int = int(chunk_index)
    except (TypeError, ValueError):
        return JsonResponse(
            {"status": "error", "message": "Dữ liệu chunk không hợp lệ."},
            status=400,
        )

    session = get_object_or_404(ExamSession, pk=session_id_int)
    _ensure_owner(session, request.user)

    if _is_session_finalized(session):
        return JsonResponse(
            {"status": "error", "message": "Phiên thi đã kết thúc."},
            status=400,
        )

    if not session.questions.filter(pk=question_id_int).exists():
        return JsonResponse(
            {"status": "error", "message": "Câu hỏi không thuộc phiên thi này."},
            status=400,
        )

    chunk_dir = _exam_recording_chunk_dir(session_id_int, question_id_int)
    chunk_name = f"{chunk_dir}/chunk_{chunk_index_int:06d}.webm"

    # Nếu upload lại cùng index thì xóa bản cũ để tránh lỗi storage không ghi đè.
    if default_storage.exists(chunk_name):
        default_storage.delete(chunk_name)

    saved_path = default_storage.save(chunk_name, ContentFile(audio_chunk.read()))

    now_ts = timezone.now().timestamp()

    cache.set(_exam_heartbeat_key(session_id_int), now_ts, timeout=120)
    _mark_exam_session_active(session_id_int)

    cache.set(
        _exam_active_question_key(session_id_int),
        question_id_int,
        timeout=24 * 60 * 60,
    )

    cache.set(
        _exam_recording_meta_key(session_id_int, question_id_int),
        {
            "session_id": session_id_int,
            "question_id": question_id_int,
            "audio_mime_type": audio_mime_type,
            "latest_chunk_index": chunk_index_int,
            "updated_ts": now_ts,
        },
        timeout=24 * 60 * 60,
    )

    return JsonResponse(
        {
            "status": "success",
            "saved_path": saved_path,
            "chunk_index": chunk_index_int,
        }
    )

@login_required
@require_POST
@student_required_json
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
        converted_audio["stored_name"],
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
@student_required_json
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
        }
    )


@login_required
@require_POST
@student_required_json
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
@student_required_json
def exam_heartbeat_view(request: HttpRequest, session_id: int) -> JsonResponse:
    session = get_object_or_404(
        ExamSession.objects.select_related("user_id"),
        pk=session_id,
    )
    _ensure_owner(session, request.user)

    if _is_session_finalized(session):
        _unmark_exam_session_active(session.pk)
        return JsonResponse({
            "status": "finalized",
            "message": "Phiên thi đã kết thúc.",
        })

    now_ts = timezone.now().timestamp()

    cache.set(_exam_heartbeat_key(session.pk), now_ts, timeout=120)
    _mark_exam_session_active(session.pk)

    return JsonResponse({
        "status": "success",
        "heartbeat_ts": now_ts,
    })

@login_required
@require_POST
@student_required_json
def finalize_session_view(request: HttpRequest, session_id: int) -> JsonResponse:
    session = get_object_or_404(
        ExamSession.objects.select_related("exam_session_group_id", "subject_id", "user_id"),
        pk=session_id,
    )
    _ensure_owner(session, request.user)

    already_finalized = _is_session_finalized(session)

    if not already_finalized:
        if not _finalize_started_exam_session(session):
            _sync_session_completion_flags(session)

            if session.completed_at is None:
                session.completed_at = timezone.now()
                session.save(update_fields=["completed_at"])

            _, final_total = _compute_scores(session)
            final_total = round(float(final_total or 0), 2)

            if session.final_score != final_total:
                session.final_score = final_total
                session.save(update_fields=["final_score"])

    _attach_session_score_meta(session)

    if "_attach_session_duration_meta" in globals():
        _attach_session_duration_meta(session)

    deadline = _get_session_appeal_deadline(session)
    _unmark_exam_session_active(session.pk)
    cache.delete(_exam_heartbeat_key(session.pk))

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
@student_required_json
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

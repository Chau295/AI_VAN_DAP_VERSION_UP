# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import csv
import io
import json
import logging
import os
import random
import re
import unicodedata
from base64 import b64encode
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List
from uuid import uuid4
from django.conf import settings

import fitz  # PyMuPDF
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, Q, Sum
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_GET, require_POST
from docx import Document
from openai import AuthenticationError, OpenAI
from openpyxl import Workbook as OpenpyxlWorkbook
from openpyxl import load_workbook

from .forms import LecturerManualQuestionForm, LecturerMaterialUploadForm
from .models import (
    DifficultyLevel,
    ExamCode,
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
    SupplementaryResult,
    UserProfile,
    normalize_question_text,
)
from .student_accounts import normalize_student_code

logger = logging.getLogger(__name__)
User = get_user_model()


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
    return subjects.first()


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


def _draft_prefix(workspace_id: str) -> str:
    return f"DRAFT_{workspace_id}_" if workspace_id else "DRAFT_"


def _ensure_owner(session: ExamSession, user: User) -> None:
    if session.user_id_id != getattr(user, "id", None):
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
    return static("images/default_avatar.png")


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
# DASHBOARD GIẢNG VIÊN
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

    selected_subject_id = request.GET.get("subject_id")
    total_subjects = subjects.count()
    total_sessions = ExamSession.objects.filter(subject_id__in=subjects).count()

    if selected_subject_id:
        try:
            selected_subject = subjects.get(pk=selected_subject_id)
            sessions = (
                ExamSession.objects.filter(subject_id=selected_subject)
                .select_related("subject_id", "user_id", "user_id__userprofile")
                .order_by("-created_at")[:20]
            )
        except (Subject.DoesNotExist, ValueError):
            selected_subject = None
            sessions = []
    else:
        selected_subject = None
        sessions = (
            ExamSession.objects.filter(subject_id__in=subjects)
            .select_related("subject_id", "user_id", "user_id__userprofile")
            .order_by("-created_at")[:20]
        )

    context = {
        "subjects": subjects,
        "selected_subject": selected_subject,
        "total_subjects": total_subjects,
        "total_sessions": total_sessions,
        "recent_sessions": sessions,
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

    sessions_in_subject = ExamSession.objects.filter(subject_id=subject)
    total_exams = sessions_in_subject.count()
    completed_exams = sessions_in_subject.filter(is_completed=True).count()
    total_students = sessions_in_subject.values("user_id").distinct().count()
    recent_exams = (
        sessions_in_subject.select_related("user_id", "user_id__userprofile")
        .order_by("-created_at")[:10]
    )
    upcoming_groups = (
        ExamSessionGroup.objects.filter(subject_id=subject, exam_date__gte=timezone.now())
        .order_by("exam_date")[:5]
    )
    approved_codes = ExamCode.objects.filter(subject_id=subject, is_approved=True).count()

    context = {
        "subject": subject,
        "total_exams": total_exams,
        "completed_exams": completed_exams,
        "total_students": total_students,
        "recent_exams": recent_exams,
        "upcoming_groups": upcoming_groups,
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
    sessions_in_subject = ExamSession.objects.filter(subject_id=subject)

    context = {
        "subject": subject,
        "total_exams": sessions_in_subject.count(),
        "total_students": sessions_in_subject.values("user_id").distinct().count(),
        "completed_exams": sessions_in_subject.filter(is_completed=True).count(),
        "recent_exams": sessions_in_subject.select_related("user_id", "user_id__userprofile").order_by("-created_at")[:10],
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
# CÂU HỎI - CRUD CŨ
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
        is_supplementary=False,
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

    question.delete()
    messages.success(request, "Đã xoá câu hỏi thành công.")
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

    try:
        decoded_file = file_obj.read().decode("utf-8")
        io_string = io.StringIO(decoded_file)
        reader = csv.DictReader(io_string)

        imported_count = 0
        duplicated_count = 0

        for row in reader:
            question_text = (row.get("question") or "").strip()
            difficulty = (row.get("difficulty") or "MEDIUM").strip()
            if question_text and difficulty in ["EASY", "MEDIUM", "HARD"]:
                if _question_exists_in_subject(subject, question_text):
                    duplicated_count += 1
                    continue

                Question.objects.create(
                    subject_id=subject,
                    question_text=question_text,
                    difficulty=difficulty,
                    question_id_in_barem=f"IMP_{subject.subject_code}_{uuid4().hex[:8]}",
                    is_supplementary=False,
                )
                imported_count += 1

        messages.success(
            request,
            f"Đã import {imported_count} câu hỏi thành công. Bỏ qua {duplicated_count} câu bị trùng.",
        )
        return JsonResponse(
            {
                "success": True,
                "imported_count": imported_count,
                "duplicated_count": duplicated_count,
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


def _get_lecturer_question_banks(request):
    return QuestionBank.objects.filter(subject_id__in=_get_lecturer_subjects(request)).select_related("subject_id")


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
        source_text = "AI Tạo tự động"
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
        # Sử dụng PyMuPDF (fitz) để đọc text từ PDF (kể cả dạng Slide)
        pdf_document = fitz.open(file_path)
        texts = []
        for page_num in range(pdf_document.page_count):
            try:
                page = pdf_document.load_page(page_num)
                # get_text("text") giúp lấy text bảo toàn bố cục tốt hơn
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


def _generate_questions_from_ai(subject: Subject, materials, total_count: int, level_config: dict):
    api_key = getattr(settings, "OPENAI_API_KEY", None)
    if not api_key:
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise Exception("Thiếu OPENAI_API_KEY trong settings.py hoặc biến môi trường")

    context_text = _build_material_context(materials)
    if not context_text.strip():
        raise Exception("Không trích xuất được nội dung từ tài liệu tải lên.")

    easy_count = int(level_config.get("easy", 0))
    medium_count = int(level_config.get("medium", 0))
    hard_count = int(level_config.get("hard", 0))

    client = OpenAI(api_key=api_key)

    system_prompt = """
Bạn là trợ lý tạo câu hỏi vấn đáp cho giảng viên đại học.

NHIỆM VỤ:
- Chỉ được tạo câu hỏi dựa trên nội dung tài liệu được cung cấp.
- Không bịa thêm kiến thức ngoài tài liệu.
- Câu hỏi phải rõ ràng, đúng ngữ cảnh môn học.
- Câu hỏi dạng vấn đáp, phù hợp để giảng viên hỏi sinh viên trực tiếp.
- Không được tạo câu hỏi trùng ý nhau quá nhiều.
- Phân loại đúng mức độ EASY / MEDIUM / HARD.

MỨC ĐỘ:
- EASY: hỏi khái niệm, định nghĩa, trình bày cơ bản
- MEDIUM: hỏi phân tích, so sánh, giải thích, liên hệ
- HARD: hỏi vận dụng, đánh giá, tình huống, lập luận sâu

ĐẦU RA:
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
"""

    user_prompt = f"""
Môn học: {subject.name}
Mã môn: {subject.subject_code}

Hãy tạo tổng cộng {total_count} câu hỏi vấn đáp dựa trên nội dung tài liệu bên dưới.

YÊU CẦU SỐ LƯỢNG:
- EASY: {easy_count}
- MEDIUM: {medium_count}
- HARD: {hard_count}

YÊU CẦU CHẤT LƯỢNG:
- Câu hỏi phải bám sát tài liệu
- Câu hỏi ngắn gọn, rõ ràng, có chiều sâu
- Không lặp lại nội dung giữa các câu

TÀI LIỆU:
{context_text}
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content
    data = json.loads(raw)

    questions = data.get("questions", [])
    if not isinstance(questions, list) or not questions:
        raise Exception("AI không trả về danh sách câu hỏi hợp lệ.")

    normalized = []
    for item in questions:
        content = (item.get("content") or "").strip()
        difficulty = (item.get("difficulty") or "MEDIUM").strip().upper()
        source = (item.get("source") or "").strip()

        if not content:
            continue
        if difficulty not in ["EASY", "MEDIUM", "HARD"]:
            difficulty = "MEDIUM"

        normalized.append(
            {
                "content": content,
                "difficulty": difficulty,
                "source": source,
            }
        )

    if not normalized:
        raise Exception("AI không sinh được câu hỏi hợp lệ.")

    return normalized


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
        banks = _get_lecturer_question_banks(request).filter(subject_id=selected_subject).order_by("-created_at")
        for b in banks:
            question_banks.append(
                {
                    "bank_id": b.id,
                    "name": b.name,
                    "detail_url": f"{request.path}?subject_id={selected_subject.id}&bank_id={b.id}&mode=detail&view=bank",
                    "question_count": Question.objects.filter(question_bank_id=b, is_exam_clone=False).count(),
                }
            )

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
    banks = _get_lecturer_question_banks(request).filter(subject_id=subject).order_by("-created_at")

    return JsonResponse(
        {
            "status": "SUCCESS",
            "question_banks": [
                {
                    "bank_id": str(b.id),
                    "bank_name": b.name,
                    "bank_type": "ORAL",
                    "created_at": b.created_at.strftime("%d/%m/%Y") if b.created_at else "",
                    "question_count": Question.objects.filter(question_bank_id=b, is_exam_clone=False).count(),
                }
                for b in banks
            ],
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
    bank_name = (payload.get("name") or f"Ngân hàng - {subject.subject_code}").strip()

    # THÊM LOGIC CHỐNG TRÙNG TÊN NGÂN HÀNG CÂU HỎI
    if QuestionBank.objects.filter(subject_id=subject, name__iexact=bank_name).exists():
        return JsonResponse({
            "status": "FAIL",
            "message": "Tên ngân hàng này đã tồn tại. Vui lòng chọn tên khác!"
        }, status=400)

    bank = QuestionBank.objects.create(subject_id=subject, name=bank_name)

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
def api_save_question_bank_questions(request, bank_id):
    _ensure_lecturer(request)
    bank = get_object_or_404(_get_lecturer_question_banks(request), pk=bank_id)
    subject = bank.subject

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

    for q in draft_qs.filter(pk__in=question_ids):
        q.question_id_in_barem = q.question_id_in_barem.replace(draft_prefix, "SAVED_")
        q.question_bank_id = bank
        q.save(update_fields=["question_id_in_barem", "question_bank_id"])

    draft_qs.exclude(pk__in=question_ids).delete()

    if workspace_id:
        LectureMaterial.objects.filter(subject_id=subject, workspace_id=workspace_id).update(
            workspace_id="",
            question_bank_id=bank,
        )

    return JsonResponse(
        {
            "status": "SUCCESS",
            "subject_id": subject.id,
            "message": "Lưu ngân hàng câu hỏi thành công.",
        }
    )


@login_required
@require_POST
def api_delete_question_bank(request, bank_id):
    _ensure_lecturer(request)
    bank = get_object_or_404(_get_lecturer_question_banks(request), pk=bank_id)

    try:
        if bank.exam_sets.exists():
            return JsonResponse(
                {
                    "status": "FAIL",
                    "message": "Ngân hàng câu hỏi đang được sử dụng trong bộ đề thi, không thể xóa.",
                },
                status=400,
            )

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
            is_supplementary=False,
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

    question_text = (payload.get("content") or payload.get("question_text") or "").strip()
    difficulty = payload.get("difficulty")

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

    return JsonResponse(
        {
            "status": "SUCCESS",
            "question": _serialize_question(question),
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

    if not workspace_id:
        return JsonResponse(
            {"status": "FAIL", "message": "Thiếu workspace_id cho phiên tạo câu hỏi."},
            status=400,
        )

    if not document_ids:
        return JsonResponse({"status": "FAIL", "message": "Bạn phải chọn ít nhất 1 tài liệu."}, status=400)

    if total_count <= 0 or total_count > 100:
        return JsonResponse({"status": "FAIL", "message": "Số lượng câu hỏi không hợp lệ."}, status=400)

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
        ai_questions = _generate_questions_from_ai(
            subject=subject,
            materials=materials,
            total_count=total_count,
            level_config=level_config,
        )

        created_questions = []
        draft_prefix = _draft_prefix(workspace_id)

        for item in ai_questions:
            if _question_exists_in_subject(subject, item["content"]):
                continue

            q = Question.objects.create(
                subject_id=subject,
                question_text=item["content"],
                difficulty=item["difficulty"],
                question_id_in_barem=f"{draft_prefix}{uuid4().hex[:8]}",
                is_supplementary=False,
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

        _save_question_job(
            job_id,
            {
                "status": "COMPLETE",
                "progress": 100,
                "questions": created_questions,
                "summary": summary,
                "error_message": "",
            },
        )

        return JsonResponse(
            {
                "status": "SUCCESS",
                "job_id": job_id,
                "message": "Đã tạo câu hỏi thành công.",
                "questions": created_questions,
                "summary": summary,
            }
        )

    except AuthenticationError:
        message = "OpenAI API key không hợp lệ hoặc đã hết hiệu lực. Vui lòng kiểm tra lại cấu hình OPENAI_API_KEY."
        logger.warning("Generate questions failed due to OpenAI authentication", exc_info=True)
        _save_question_job(
            job_id,
            {
                "status": "FAIL",
                "progress": 100,
                "questions": [],
                "summary": {},
                "error_message": message,
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
                "questions": [],
                "summary": {},
                "error_message": message,
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
            "questions": job.get("questions", []),
            "summary": job.get("summary", {}),
            "error_message": job.get("error_message", ""),
        }
    )


# =========================================================
# MATERIAL / QUESTION SCREEN CŨ
# =========================================================
@login_required
def lecturer_question_management(request, subject_code):
    if not request.user.userprofile.is_lecturer:
        messages.error(request, "Bạn không có quyền truy cập.")
        return redirect("qna:dashboard")

    subject = get_object_or_404(
        request.user.userprofile.subjects_taught.all(),
        subject_code=subject_code,
    )

    subjects = request.user.userprofile.subjects_taught.all().order_by("name")
    page_mode = request.GET.get("mode", "detail")

    question_banks = [
        {
            "bank_id": subject.id,
            "name": f"Ngân hàng câu hỏi - {subject.subject_code}",
            "detail_url": f"{request.path}?mode=detail",
            "question_count": Question.objects.filter(subject_id=subject, is_exam_clone=False).count(),
        }
    ]

    documents = LectureMaterial.objects.filter(subject_id=subject).order_by("-uploaded_at")

    questions = [
        {
            "id": q.id,
            "content": q.question_text,
            "level": q.difficulty,
            "source_name": "",
            "created_at": "",
            "selected": False,
        }
        for q in Question.objects.filter(subject_id=subject, is_exam_clone=False).order_by("question_id_in_barem", "-pk")
    ]

    context = {
        "subject": subject,
        "subjects": subjects,
        "selected_subject": subject,
        "page_mode": page_mode,
        "question_banks": question_banks,
        "documents": [
            {
                "id": d.id,
                "file_name": d.title,
                "upload_time": d.uploaded_at.strftime("%d/%m/%Y"),
            }
            for d in documents
        ],
        "questions": questions,
        "upload_entry_url": request.path,
    }
    return render(request, "qna/lecturer/lecturer_question_management.html", context)


@login_required
@require_POST
def lecturer_upload_material_screen(request):
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({"success": False, "error": "Không có quyền truy cập"}, status=403)

    subject_id = request.POST.get("subject_id")
    title = request.POST.get("title") or request.POST.get("material_name")
    file_obj = request.FILES.get("file") or request.FILES.get("material_file")

    if not subject_id or not title or not file_obj:
        return JsonResponse({"success": False, "error": "Thiếu thông bắt buộc"}, status=400)

    subject = get_object_or_404(request.user.userprofile.subjects_taught, pk=subject_id)

    upload_dir = os.path.join(settings.MEDIA_ROOT, "lecture_materials", subject.subject_code)
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

    return JsonResponse({"success": True, "material_id": material.id})


@login_required
@require_POST
def lecturer_upload_material(request, subject_code):
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({"success": False, "error": "Không có quyền"}, status=403)

    subject = get_object_or_404(request.user.userprofile.subjects_taught, subject_code=subject_code)
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
    messages.success(request, "Đã upload tài liệu thành công.")
    return JsonResponse({"success": True, "material_id": material.id})


@login_required
def lecturer_generate_exam_codes(request, subject_code):
    if not request.user.userprofile.is_lecturer:
        return redirect("qna:dashboard")
    subject = get_object_or_404(request.user.userprofile.subjects_taught, subject_code=subject_code)
    return render(
        request,
        "qna/lecturer/lecturer_generate_exam_codes.html",
        {
            "subject": subject,
            "materials": LectureMaterial.objects.filter(subject_id=subject).order_by("-uploaded_at"),
            "pending_exam_codes": ExamCode.objects.filter(subject_id=subject, is_approved=False).order_by("-created_at"),
        },
    )


@login_required
@require_POST
def lecturer_generate_codes_with_ai(request):
    return JsonResponse(
        {
            "success": False,
            "error": (
                "Chức năng sinh mã đề AI kiểu cũ đã bị vô hiệu vì không còn khớp với cấu trúc "
                "mã đề hiện tại. Hãy dùng màn tạo bộ đề/mã đề mới."
            ),
        },
        status=400,
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
        status=400,
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
        status=400,
    )


# =========================================================
# REVIEW / EXPORT
# =========================================================
SUPP_MAX_PER_QUESTION = 1.0
SUPP_MAX_COUNT = 2
FINAL_CAP = 7.0


def _compute_scores(session: ExamSession) -> tuple[float, float, float]:
    total_main_questions = session.questions.count() or 3
    main_avg = (
        (ExamResult.objects.filter(exam_session_id=session).aggregate(total=Sum("score"))["total"] or 0.0)
        / total_main_questions
    )

    supp_results_qs = SupplementaryResult.objects.filter(exam_session_id=session)
    best_scores_by_text = {}
    for result in supp_results_qs:
        key = (result.question_text or "").strip()
        if not key:
            continue
        score = float(result.score or 0.0)
        if score > SUPP_MAX_PER_QUESTION:
            score /= 10.0
        current_score = max(0.0, min(score, SUPP_MAX_PER_QUESTION))
        if key not in best_scores_by_text or current_score > best_scores_by_text[key]:
            best_scores_by_text[key] = current_score

    unique_supp_scores = sorted(list(best_scores_by_text.values()), reverse=True)
    supp_sum = sum(unique_supp_scores[:SUPP_MAX_COUNT])
    final_total = min(FINAL_CAP, main_avg + supp_sum) if supp_sum > 0 else min(10.0, main_avg)

    return main_avg, supp_sum, final_total


def _dedupe_supp_for_display(qs: SupplementaryResult) -> List[SupplementaryResult]:
    best_by_text = {}
    for result in qs:
        key = (result.question_text or "").strip()
        if not key:
            continue
        cleaned_result = result
        score = float(cleaned_result.score or 0.0)
        if score > SUPP_MAX_PER_QUESTION:
            cleaned_result.score = score / 10.0
        if best_by_text.get(key) is None or float(cleaned_result.score or 0) > float(best_by_text.get(key).score or 0):
            best_by_text[key] = cleaned_result
    return sorted(list(best_by_text.values()), key=lambda x: float(x.score or 0), reverse=True)[:SUPP_MAX_COUNT]


@login_required
def lecturer_student_review_screen(request):
    if not request.user.userprofile.is_lecturer:
        return redirect("qna:dashboard")

    subjects = request.user.userprofile.subjects_taught.all()
    selected_subject_id = request.GET.get("subject_id")
    exam_group_id = request.GET.get("exam_group_id")
    student_filter = (request.GET.get("student") or "").strip()

    if not selected_subject_id:
        selected_subject = subjects.first()
    else:
        try:
            selected_subject = subjects.get(pk=selected_subject_id)
        except (Subject.DoesNotExist, ValueError):
            selected_subject = subjects.first()

    exam_groups = ExamSessionGroup.objects.filter(subject_id=selected_subject).order_by("-exam_date")
    sessions = ExamSession.objects.filter(subject_id=selected_subject).select_related("user_id", "user_id__userprofile")

    if exam_group_id:
        sessions = sessions.filter(exam_session_group_id=exam_group_id)
    if student_filter:
        sessions = sessions.filter(user_id__username__icontains=student_filter)

    sessions = sessions.order_by("-created_at")
    for session in sessions:
        main_avg, supp_sum, final_total = _compute_scores(session)
        session.main_avg = main_avg
        session.supp_sum = supp_sum
        session.calculated_final_score = final_total

    return render(
        request,
        "qna/lecturer/lecturer_student_review.html",
        {
            "subjects": subjects,
            "selected_subject": selected_subject,
            "subject": selected_subject,
            "exam_groups": exam_groups,
            "selected_exam_group_id": int(exam_group_id) if exam_group_id else None,
            "sessions": sessions,
            "student_filter": student_filter,
        },
    )


@login_required
def lecturer_student_review(request, subject_code):
    if not request.user.userprofile.is_lecturer:
        return redirect("qna:dashboard")
    subject = get_object_or_404(request.user.userprofile.subjects_taught, subject_code=subject_code)
    return render(
        request,
        "qna/lecturer/lecturer_student_review.html",
        {
            "subject": subject,
            "sessions": (
                ExamSession.objects.filter(subject_id=subject)
                .select_related("user_id", "user_id__userprofile")
                .prefetch_related("results__question_id")
                .order_by("-created_at")
            ),
        },
    )


@login_required
def lecturer_session_detail(request, session_id):
    if not request.user.userprofile.is_lecturer:
        return redirect("qna:dashboard")

    session = get_object_or_404(
        ExamSession.objects.select_related("subject_id", "user_id", "user_id__userprofile"),
        pk=session_id,
    )
    if session.subject not in request.user.userprofile.subjects_taught.all():
        raise PermissionDenied("Bạn không có quyền")

    main_avg, supp_sum, final_total = _compute_scores(session)
    return render(
        request,
        "qna/lecturer/lecturer_session_detail.html",
        {
            "session": session,
            "results": ExamResult.objects.filter(exam_session_id=session).select_related("question_id").order_by("question_id"),
            "supp_results": _dedupe_supp_for_display(SupplementaryResult.objects.filter(exam_session_id=session)),
            "main_avg": main_avg,
            "supp_sum": supp_sum,
            "final_total": final_total,
        },
    )


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

    headers = ["STT", "Mã SV", "Họ tên", "Lớp", "Ngày thi", "Điểm chính", "Điểm phụ", "Điểm tổng"]
    ws.append(headers)

    header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for idx, session in enumerate(sessions, 1):
        main_avg, supp_sum, final_total = _compute_scores(session)
        profile = session.user.userprofile
        ws.append(
            [
                idx,
                session.user.username,
                profile.full_name,
                profile.class_name,
                session.created_at.strftime("%d/%m/%Y %H:%M"),
                f"{main_avg:.2f}",
                f"{supp_sum:.2f}",
                f"{final_total:.2f}",
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
        ExamSession.objects.filter(subject_id=subject, is_completed=True)
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
                "Đã hoàn thành" if session.is_completed else "Chưa hoàn thành",
                session.get_verification_status_display() if hasattr(session, "get_verification_status_display") else session.verification_status,
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
        password, password2 = cleaned_data.get("password"), cleaned_data.get("password2")
        if password and password2 and password != password2:
            self.add_error("password2", "Mật khẩu nhập lại không khớp.")
        if password:
            try:
                validate_password(password, user=User(username=cleaned_data.get("username")))
            except ValidationError as e:
                self.add_error("password", e)
        return cleaned_data


@login_required
def dashboard_view(request: HttpRequest) -> HttpResponse:
    profile, _ = UserProfile.objects.get_or_create(user_id=request.user)
    if profile.is_lecturer:
        return redirect("qna:lecturer_dashboard")

    subjects = Subject.objects.all().order_by("name")
    recent_sessions = ExamSession.objects.filter(user_id=request.user).select_related("subject_id").order_by("-created_at")[:5]
    return render(
        request,
        "qna/student/dashboard.html",
        {
            "subjects": subjects,
            "recent_sessions": recent_sessions,
            "full_name": profile.full_name or request.user.first_name,
        },
    )


@login_required
def history_view(request: HttpRequest) -> HttpResponse:
    sessions = ExamSession.objects.filter(user_id=request.user).select_related("subject_id").order_by("-created_at")
    for s in sessions:
        main_avg, supp_sum, final_total = _compute_scores(s)
        s.main_avg = main_avg
        s.supp_sum = supp_sum
        s.calculated_final_score = final_total
    return render(request, "qna/student/history.html", {"sessions": sessions})


@login_required
def history_detail_view(request: HttpRequest, session_id: int) -> HttpResponse:
    session = get_object_or_404(ExamSession.objects.select_related("subject_id", "user_id"), pk=session_id)
    _ensure_owner(session, request.user)
    main_results = ExamResult.objects.filter(exam_session_id=session).select_related("question_id").order_by("question_id")
    supp_results_display = _dedupe_supp_for_display(SupplementaryResult.objects.filter(exam_session_id=session))
    main_avg, supp_sum, final_total = _compute_scores(session)
    return render(
        request,
        "qna/student/history_detail.html",
        {
            "session": session,
            "results": main_results,
            "supp_results": supp_results_display,
            "main_avg": main_avg,
            "supp_sum": supp_sum,
            "final_total": final_total,
        },
    )


@login_required
def pre_exam_verification_view(request: HttpRequest, subject_code: str) -> HttpResponse:
    return render(
        request,
        "qna/student/pre_exam_verification.html",
        {
            "subject": get_object_or_404(Subject, subject_code=subject_code),
            "subject_code": subject_code,
        },
    )


@login_required
def exam_password_view(request, subject_code):
    subject = get_object_or_404(Subject, subject_code=subject_code)
    if not subject.exam_password:
        return redirect("qna:pre_exam_verification", subject_code=subject_code)
    return render(request, "qna/student/exam_password.html", {"subject": subject, "subject_code": subject_code})


@login_required
def verify_exam_password(request, subject_code):
    if request.method == "POST":
        subject = get_object_or_404(Subject, subject_code=subject_code)
        password = (request.POST.get("password") or "").strip()
        if not subject.exam_password or password == subject.exam_password:
            return redirect("qna:pre_exam_verification", subject_code=subject_code)
        messages.error(request, "Mật khẩu không đúng. Vui lòng thử lại.")
        return redirect("qna:exam_password", subject_code=subject_code)
    return redirect("qna:exam_password", subject_code=subject_code)


@login_required
def exam_view(request: HttpRequest, subject_code: str) -> HttpResponse:
    subject = get_object_or_404(Subject, subject_code=subject_code)
    main_questions = list(
        Question.objects.filter(subject_id=subject, is_supplementary=False, is_exam_clone=False).order_by("?")[:3]
    )
    if not main_questions:
        messages.error(request, f"Môn {subject.name} chưa có câu hỏi. Vui lòng liên hệ quản trị viên.")
        return redirect("qna:dashboard")

    session = ExamSession.objects.create(user_id=request.user, subject_id=subject)
    session.questions.set(main_questions)
    barem = [
        {"id": q.id, "question": q.question_text}
        for q in Question.objects.filter(subject_id=subject, is_supplementary=False, is_exam_clone=False).exclude(
            pk__in=[q.id for q in main_questions]
        )
    ]
    return render(
        request,
        "qna/student/exam.html",
        {
            "subject": subject,
            "selected_questions": main_questions,
            "session": session,
            "barem_json": json.dumps(barem, ensure_ascii=False),
        },
    )


@login_required
def profile_view(request: HttpRequest) -> HttpResponse:
    try:
        profile = request.user.userprofile
    except UserProfile.DoesNotExist:
        profile = None
    return render(request, "qna/student/profile.html", {"user": request.user, "profile": profile})


@login_required
@require_POST
def update_profile_image(request: HttpRequest) -> JsonResponse:
    file_obj = request.FILES.get("profile_image")
    if not file_obj:
        return JsonResponse({"success": False, "error": "Không tìm thấy file ảnh."}, status=400)
    if file_obj.size > 5 * 1024 * 1024:
        return JsonResponse({"success": False, "error": "Kích thước ảnh không được vượt quá 5MB."}, status=400)

    content = file_obj.read()
    mime = file_obj.content_type
    profile, _ = UserProfile.objects.get_or_create(user_id=request.user)
    profile.profile_image_blob = content
    profile.profile_image_mime = mime
    profile.save()
    return JsonResponse(
        {
            "success": True,
            "image_data_url": f"data:{mime};base64,{b64encode(content).decode('ascii')}",
        }
    )


@login_required
@require_POST
def save_exam_result(request: HttpRequest) -> JsonResponse:
    data = _json_body(request)
    if not all([data.get("session_id"), data.get("question_id"), data.get("score") is not None]):
        return HttpResponseBadRequest("Thiếu các tham số bắt buộc (session_id, question_id, score).")

    session = get_object_or_404(ExamSession, pk=data.get("session_id"))
    _ensure_owner(session, request.user)
    question = get_object_or_404(Question, pk=data.get("question_id"), is_supplementary=False)

    result, created = ExamResult.objects.update_or_create(
        exam_session_id=session,
        question_id=question,
        defaults={
            "transcript": data.get("transcript", ""),
            "score": float(data.get("score")),
            "feedback": data.get("feedback"),
            "analysis": data.get("analysis"),
            "answered_at": timezone.now(),
        },
    )
    return JsonResponse({"status": "ok", "created": created, "result_id": result.id})


@login_required
@require_POST
def get_supplementary_for_session(request: HttpRequest, session_id: int) -> JsonResponse:
    session = get_object_or_404(ExamSession.objects.select_related("subject_id"), pk=session_id)
    _ensure_owner(session, request.user)
    supp_pool = list(Question.objects.filter(subject_id=session.subject_id, is_supplementary=True))
    random.shuffle(supp_pool)
    return JsonResponse(
        {"status": "ok", "items": [{"id": q.id, "question": q.question_text} for q in supp_pool[:SUPP_MAX_COUNT]]}
    )


@login_required
@require_POST
def save_supplementary_result(request: HttpRequest) -> JsonResponse:
    data = _json_body(request)
    if not all([data.get("session_id"), (data.get("question_text") or "").strip(), data.get("score") is not None]):
        return HttpResponseBadRequest("Thiếu tham số.")

    session = get_object_or_404(ExamSession, pk=data.get("session_id"))
    _ensure_owner(session, request.user)
    if SupplementaryResult.objects.filter(exam_session_id=session).count() >= SUPP_MAX_COUNT:
        return JsonResponse(
            {"status": "error", "message": f"Đã đạt số lượng câu hỏi phụ tối đa ({SUPP_MAX_COUNT})."},
            status=400,
        )

    try:
        max_score_val = float(data.get("max_score", 10.0))
        max_score_val = 10.0 if max_score_val <= 0 else max_score_val
    except (TypeError, ValueError):
        return HttpResponseBadRequest("Giá trị điểm không hợp lệ.")

    final_score = max(
        0.0,
        min((float(data.get("score")) / max_score_val) * SUPP_MAX_PER_QUESTION, SUPP_MAX_PER_QUESTION),
    )
    sr = SupplementaryResult.objects.create(
        exam_session_id=session,
        question_text=(data.get("question_text") or "").strip(),
        transcript=data.get("transcript", ""),
        score=final_score,
        max_score=SUPP_MAX_PER_QUESTION,
        feedback=data.get("feedback"),
        analysis=data.get("analysis"),
    )
    main_avg, supp_sum, final_total = _compute_scores(session)
    return JsonResponse(
        {
            "status": "ok",
            "supplementary_result_id": sr.id,
            "main_avg": main_avg,
            "supp_sum": supp_sum,
            "final_total": final_total,
        }
    )


@login_required
@require_POST
def finalize_session_view(request: HttpRequest, session_id: int) -> JsonResponse:
    session = get_object_or_404(ExamSession, pk=session_id)
    _ensure_owner(session, request.user)
    _, _, total_score = _compute_scores(session)
    session.is_completed = True
    session.completed_at = timezone.now()
    session.final_score = total_score
    session.save(update_fields=["is_completed", "completed_at", "final_score"])
    return JsonResponse({"status": "success", "final_score": session.final_score})


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
        format_part, imgstr = face_image_data.split(";base64,")
        session = ExamSession.objects.create(
            user_id=request.user,
            subject_id=subject,
            face_image_blob=base64.b64decode(imgstr),
            face_image_mime=f"image/{format_part.split('/')[-1]}",
            verification_status="ALLOW",
        )
        return JsonResponse(
            {
                "status": "success",
                "session_id": session.id,
                "message": "Đã lưu ảnh xác thực thành công.",
            }
        )
    except Exception as e:
        return JsonResponse({"status": "error", "message": f"Lỗi khi xử lý ảnh: {str(e)}"}, status=500)


@login_required
def get_verification_images(request: HttpRequest, session_id: int) -> HttpResponse:
    session = get_object_or_404(ExamSession, pk=session_id)
    if not (request.user.userprofile.is_lecturer or session.user_id == request.user):
        raise PermissionDenied("Bạn không có quyền xem ảnh xác thực này.")

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


def _student_create_roster(subject, created_by, academic_year, semester, uploaded_file):
    _student_validate_file(uploaded_file)
    file_bytes = uploaded_file.read()
    if not file_bytes:
        raise ValidationError("File được chọn không có dữ liệu.")

    records, skipped_rows = _student_parse_records(uploaded_file.name, file_bytes)

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
    )


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
    if academic_year:
        rosters = rosters.filter(academic_year=academic_year)
    if semester:
        rosters = rosters.filter(semester=semester)
    if status:
        rosters = rosters.filter(status=status)

    paginator = Paginator(rosters.order_by("-created_at"), 10)
    page_obj = paginator.get_page(request.GET.get("page"))
    roster_summary = _student_roster_summary(rosters)

    context = {
        "subject": subject,
        "page_obj": page_obj,
        "roster_summary": roster_summary,
        "completed_rosters_preview": rosters.filter(
            status=StudentListUploadStatus.CREATED,
            account_created_at__isnull=False,
        ).order_by("-account_created_at")[:3],
        "filters": {
            "q": keyword,
            "academic_year": academic_year,
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
        "password_rules": [
            "Tối thiểu 8 ký tự.",
            "Không được chỉ gồm toàn chữ số.",
        ],
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
    if len(password) < 8:
        raise ValidationError("Mật khẩu phải có ít nhất 8 ký tự.")
    if password.isdigit():
        raise ValidationError("Mật khẩu không được chỉ gồm toàn chữ số.")


@login_required
@require_POST
def lecturer_student_list_delete(request, roster_id):
    _ensure_lecturer(request)
    roster = get_object_or_404(StudentRosterUpload.objects.select_related("subject_id"), pk=roster_id)
    if roster.subject not in request.user.userprofile.subjects_taught.all():
        raise PermissionDenied("Bạn không có quyền xóa danh sách này.")

    subject_code = roster.subject.subject_code
    if roster.uploaded_file:
        roster.uploaded_file.delete(save=False)
    roster.delete()
    messages.success(request, "Xóa danh sách lớp thành công")
    return redirect("qna:lecturer_student_list_management", subject_code=subject_code)


@login_required
def lecturer_student_list_upload(request, subject_code):
    _ensure_lecturer(request)
    subject = get_object_or_404(_get_lecturer_subjects(request), subject_code=subject_code)

    if request.method == "POST":
        academic_year = (request.POST.get("academic_year") or "").strip() or _student_default_academic_year()
        semester = (request.POST.get("semester") or SemesterChoices.HK1).strip()
        if semester not in {choice[0] for choice in SemesterChoices.choices}:
            semester = SemesterChoices.HK1

        files = request.FILES.getlist("files")
        wants_json = _is_ajax_request(request)

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

    for student_row in roster.students.select_related("linked_user_id").order_by("row_number"):
        existing_user = _student_find_user(student_row.student_code)
        if existing_user:
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
                existing_user = _student_find_user(student_row.student_code)
                if existing_user:
                    student_row.linked_user_id = existing_user
                    student_row.account_created = True
                    student_row.account_status = StudentRosterAccountStatus.EXISTING
                    student_row.save(update_fields=["linked_user_id", "account_created", "account_status"])

            for student_row in pending_rows:
                user, created, _ = _student_provision_account(student_row, default_password)
                student_row.linked_user_id = user
                student_row.account_created = True
                student_row.account_status = (
                    StudentRosterAccountStatus.CREATED if created else StudentRosterAccountStatus.EXISTING
                )
                student_row.save(update_fields=["linked_user_id", "account_created", "account_status"])
                if created:
                    created_count += 1

            roster.refresh_status()

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
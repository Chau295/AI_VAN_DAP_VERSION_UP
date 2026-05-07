# -*- coding: utf-8 -*-
from __future__ import annotations
from time import perf_counter
import csv
import io
import json
import logging
import os
import re
import tempfile
import unicodedata
from datetime import timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Dict
from uuid import uuid4

import fitz
from docx import Document
from openai import AuthenticationError, OpenAI

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from .services.ai_usage_service import record_ai_usage, summarize_ai_usage
from .exam_guards import subject_has_active_exam_group
from .forms import LecturerManualQuestionForm
from .models import (
    DifficultyLevel,
    ExamCode,
    LectureMaterial,
    Question,
    QuestionBank,
    Subject,
    normalize_question_text,
)
from .permissions import (
    lecturer_required,
    lecturer_required_json,
    get_lecturer_subject_or_404,
    lecturer_can_access_subject,
)

logger = logging.getLogger(__name__)
LIST_PAGE_SIZE = 8
QUESTION_TEXT_MAX_LENGTH = 5000
QUESTION_TEXT_TOO_LONG_MESSAGE = "Nội dung câu hỏi không được vượt quá 5000 ký tự."

# =========================================================
# Câu hỏi - CRUD cũ
# =========================================================
@login_required
@require_POST
@lecturer_required_json
def lecturer_create_question(request):

    subject_id = request.POST.get("subject_id")
    question_text = (request.POST.get("question_text") or "").strip()
    difficulty = request.POST.get("difficulty")

    if not all([subject_id, question_text, difficulty]):
        return JsonResponse({"success": False, "error": "Thiếu thông tin bắt buộc"}, status=400)

    try:
        _validate_question_text_length(question_text)
    except ValueError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    subject = get_lecturer_subject_or_404(
        request.user,
        subject_id=subject_id,
    )

    if _question_exists_in_subject(subject, question_text):
        return JsonResponse(
            {"success": False, "error": "Câu hỏi bị trùng trong môn học này."},
            status=400,
        )

    question = Question.objects.create(
        subject_id=subject,
        question_text=question_text,
        difficulty=difficulty,
        is_draft=False,
    )

    messages.success(request, "Đã tạo câu hỏi thành công.")
    return JsonResponse({"success": True, "question_id": question.id})
@login_required
@require_POST
@lecturer_required_json
def lecturer_update_question(request, question_id):
    question = get_object_or_404(
        Question.objects.select_related("subject_id"),
        pk=question_id,
    )
    if question.is_exam_clone:
        return JsonResponse(
            {"status": "FAIL", "message": "Không thể sửa câu hỏi đang được sử dụng trong mã đề."},
            status=400,
        )
    if not lecturer_can_access_subject(request.user, question.subject):
        return JsonResponse(
            {"success": False, "error": "Không có quyền truy cập môn học này"},
            status=403,
        )

    question_text = (request.POST.get("question_text") or "").strip()
    difficulty = request.POST.get("difficulty")

    try:
        _validate_question_text_length(question_text)
    except ValueError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

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
@lecturer_required_json
def lecturer_delete_question(request, question_id):
    question = get_object_or_404(
        Question.objects.select_related("subject_id"),
        pk=question_id,
    )
    if question.is_exam_clone:
        return JsonResponse(
            {"status": "FAIL", "message": "Không thể xóa câu hỏi đang được sử dụng trong mã đề."},
            status=400,
        )
    if not lecturer_can_access_subject(request.user, question.subject):
        return JsonResponse(
            {"success": False, "error": "Không có quyền truy cập môn học này"},
            status=403,
        )

    question.delete()
    messages.success(request, "Đã xóa câu hỏi thành công.")
    return JsonResponse({"success": True})


@login_required
@require_POST
@lecturer_required_json
def lecturer_import_questions(request):

    subject_id = request.POST.get("subject_id")
    file_obj = request.FILES.get("file")
    if not subject_id or not file_obj:
        return JsonResponse({"success": False, "error": "Thiếu thông tin"}, status=400)

    subject = get_lecturer_subject_or_404(
        request.user,
        subject_id=subject_id,
    )

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
                is_draft=False,
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
# QUESTION BANK SCREEN
# =========================================================
@login_required
@lecturer_required
def lecturer_questions_screen(request):

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
        "has_active_exam": False,
    }
    return render(request, "qna/lecturer/lecturer_question_management.html", context)

@login_required
@lecturer_required
def question_bank_list_screen(request):
    return lecturer_questions_screen(request)


@login_required
@lecturer_required
def question_bank_detail_screen(request):
    if "mode" not in request.GET:
        q = request.GET.copy()
        q["mode"] = "detail"
        return redirect(f"{request.path}?{q.urlencode()}")
    return lecturer_questions_screen(request)


@login_required
@require_GET
@lecturer_required_json
def api_get_lecturer_subjects(request):
    subjects = _get_lecturer_subjects(request)
    return JsonResponse(
        {
            "status": "SUCCESS",
            "subjects": [{"subject_id": str(item.id), "subject_name": item.name} for item in subjects],
        }
    )


@login_required
@require_GET
@lecturer_required_json
def api_get_question_banks(request):
    subject_id = request.GET.get("subject_id")
    if not subject_id:
        return JsonResponse({"status": "FAIL", "message": "Thiếu subject_id."}, status=400)

    subject = get_lecturer_subject_or_404(
        request.user,
        subject_id=subject_id,
    )
    banks = _get_lecturer_question_banks_with_counts(request, subject).order_by("-created_at")

    return JsonResponse(
        {
            "status": "SUCCESS",
            "question_banks": [_serialize_question_bank_item(b) for b in banks],
        }
    )


@login_required
@require_POST
@lecturer_required_json
def api_create_question_bank(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    subject_id = payload.get("subject_id")
    if not subject_id:
        return JsonResponse({"status": "FAIL", "message": "Thiếu subject_id."}, status=400)

    subject = get_lecturer_subject_or_404(
        request.user,
        subject_id=subject_id,
    )
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
@lecturer_required_json
def api_update_question_bank(request, bank_id):
    bank = get_object_or_404(_get_lecturer_question_banks(request), pk=bank_id)

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
@lecturer_required_json
def api_save_question_bank_questions(request, bank_id):
    bank = get_object_or_404(_get_lecturer_question_banks(request), pk=bank_id)
    subject = bank.subject

    try:
        payload = json.loads(request.body.decode("utf-8"))
        question_ids = payload.get("question_ids", [])
        pending_edits = payload.get("pending_edits", [])
        if isinstance(pending_edits, str):
            pending_edits = json.loads(pending_edits)
        if not isinstance(pending_edits, list):
            pending_edits = []
    except Exception:
        return JsonResponse({"status": "FAIL", "message": "Dữ liệu không hợp lệ."}, status=400)

    workspace_id = _get_workspace_id(request, payload)

    clean_question_ids = []
    for item in question_ids:
        try:
            clean_question_ids.append(int(item))
        except (TypeError, ValueError):
            continue

    clean_pending_edits = []

    for item in pending_edits:
        if not isinstance(item, dict):
            continue

        try:
            qid = int(item.get("question_id"))
        except (TypeError, ValueError):
            continue

        content = (item.get("content") or "").strip()
        difficulty = item.get("difficulty")

        if not content:
            return JsonResponse(
                {"status": "FAIL", "message": "Nội dung câu hỏi không được để trống."},
                status=400,
            )

        try:
            _validate_question_text_length(content)
        except ValueError as exc:
            return JsonResponse({"status": "FAIL", "message": str(exc)}, status=400)

        if difficulty not in ["EASY", "MEDIUM", "HARD"]:
            difficulty = "MEDIUM"

        clean_pending_edits.append(
            {
                "question_id": qid,
                "content": content,
                "difficulty": difficulty,
            }
        )

    if not clean_question_ids and not clean_pending_edits:
        return JsonResponse(
            {"status": "FAIL", "message": "Không có thay đổi nào để cập nhật."},
            status=400,
        )

    with transaction.atomic():
        for item in clean_pending_edits:
            target_question = Question.objects.select_for_update().filter(
                pk=item["question_id"],
                subject_id=subject,
                is_exam_clone=False,
            ).first()

            if target_question is None:
                continue

            if target_question.is_draft:
                source_question_id = _parse_edit_draft_source_id_from_text(target_question.question_text)
                exclude_ids = [target_question.pk]
                if source_question_id:
                    exclude_ids.append(source_question_id)
            else:
                source_question_id = None
                exclude_ids = [target_question.pk]

            if _question_exists_in_subject_excluding_ids(
                subject,
                item["content"],
                exclude_ids=exclude_ids,
            ):
                return JsonResponse(
                    {"status": "FAIL", "message": "Câu hỏi bị trùng trong môn học này."},
                    status=400,
                )

            if target_question.is_draft and source_question_id:
                target_question.question_text = _append_draft_edit_text_marker(
                    item["content"],
                    source_question_id,
                    workspace_id,
                )
            elif target_question.is_draft:
                target_question.question_text = _append_draft_workspace_marker(
                    item["content"],
                    workspace_id,
                )
            else:
                target_question.question_text = item["content"]

            target_question.difficulty = item["difficulty"]
            target_question.save()

        draft_qs = Question.objects.filter(
            subject_id=subject,
            is_exam_clone=False,
            is_draft=True,
            question_bank_id__isnull=True,
        )
        draft_qs = _filter_drafts_for_workspace(draft_qs, workspace_id)

        selected_drafts = list(draft_qs.filter(pk__in=clean_question_ids).order_by("pk"))
        discarded_qs = draft_qs.exclude(pk__in=clean_question_ids)

        saved_count = 0

        for q in selected_drafts:
            source_question_id = _parse_edit_draft_source_id_from_text(q.question_text)
            clean_question_text = _strip_draft_edit_text_marker(q.question_text)

            if source_question_id:
                original_question = Question.objects.select_for_update().filter(
                    pk=source_question_id,
                    subject_id=subject,
                    question_bank_id=bank,
                    is_exam_clone=False,
                    is_draft=False,
                ).first()

                if original_question:
                    if _question_exists_in_subject_excluding_ids(
                        subject,
                        clean_question_text,
                        exclude_ids=[original_question.pk, q.pk],
                    ):
                        return JsonResponse(
                            {"status": "FAIL", "message": "Câu hỏi bị trùng trong môn học này."},
                            status=400,
                        )

                    original_question.question_text = clean_question_text
                    original_question.difficulty = q.difficulty
                    original_question.save()

                    q.delete()
                    saved_count += 1
                    continue

            if _question_exists_in_subject_excluding_ids(
                subject,
                clean_question_text,
                exclude_ids=[q.pk],
            ):
                return JsonResponse(
                    {"status": "FAIL", "message": "Câu hỏi bị trùng trong môn học này."},
                    status=400,
                )

            q.question_text = clean_question_text
            q.question_bank_id = bank
            q.is_draft = False
            q.save(update_fields=["question_text", "question_bank_id", "is_draft", "question_text_normalized"])
            saved_count += 1

        discarded_count = discarded_qs.count()
        discarded_qs.delete()

        if workspace_id:
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
            "saved_count": saved_count,
            "discarded_count": discarded_count,
            "question_count": int(getattr(bank_with_count, "question_count_value", 0) or 0),
            "bank": _serialize_question_bank_item(bank_with_count),
        }
    )
@login_required
@require_POST
@lecturer_required_json
def api_delete_question_bank(request, bank_id):
    bank = get_object_or_404(_get_lecturer_question_banks(request), pk=bank_id)

    # Cho phép xóa ngân hàng dù đang dùng trong bộ đề;
    # frontend hiển thị popup xác nhận trước khi gọi API này.
    is_used_in_exam_sets = bank.exam_sets.exists()

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
@require_GET
@lecturer_required_json
def api_check_question_bank_usage(request, bank_id):
    bank = get_object_or_404(_get_lecturer_question_banks(request), pk=bank_id)
    is_used = bank.exam_sets.exists()
    exam_set_count = bank.exam_sets.count()
    return JsonResponse({
        "status": "SUCCESS",
        "is_used_in_exam_sets": is_used,
        "exam_set_count": exam_set_count,
    })


@login_required
@require_POST
@lecturer_required_json
def api_material_presign(request):

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
@lecturer_required_json
def api_material_upload_complete(request):
    import hashlib

    subject_id = request.POST.get("subject_id")
    bank_id = (request.POST.get("bank_id") or "").strip()
    title = request.POST.get("title") or request.POST.get("file_name")
    file_obj = request.FILES.get("file")

    if not subject_id or not title or not file_obj:
        return JsonResponse({"status": "FAIL", "message": "Thiếu dữ liệu upload."}, status=400)

    subject = get_lecturer_subject_or_404(
        request.user,
        subject_id=subject_id,
    )
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
@lecturer_required_json
def lecturer_delete_material(request, material_id):
    material = get_object_or_404(
        LectureMaterial.objects.select_related("subject_id"),
        pk=material_id,
    )

    if not lecturer_can_access_subject(request.user, material.subject):
        return JsonResponse(
            {"status": "FAIL", "message": "Bạn không có quyền xóa tài liệu này."},
            status=403,
        )

    try:
        if material.file_path and os.path.exists(material.file_path):
            os.remove(material.file_path)
    except Exception as exc:
        logger.warning("Không thể xóa file vật lý: %s", exc)

    material.delete()
    return JsonResponse({"status": "SUCCESS", "message": "Đã xóa tài liệu thành công."})


@login_required
@require_GET
@lecturer_required_json
def api_get_materials(request):

    subject_id = request.GET.get("subject_id")
    bank_id = (request.GET.get("bank_id") or "").strip()
    search = (request.GET.get("search") or "").strip()
    workspace_id = (request.GET.get("workspace_id") or "").strip()
    view_type = (request.GET.get("view_type") or "generate").strip()

    if not subject_id:
        return JsonResponse({"status": "FAIL", "message": "Thiếu subject_id."}, status=400)

    subject = get_lecturer_subject_or_404(
        request.user,
        subject_id=subject_id,
    )

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
@lecturer_required_json
def api_get_questions(request):

    subject_id = request.GET.get("bank_id") or request.GET.get("subject_id")
    real_bank_id = (request.GET.get("real_bank_id") or "").strip()
    difficulty = request.GET.get("difficulty")
    view_type = request.GET.get("view_type", "bank")
    workspace_id = (request.GET.get("workspace_id") or "").strip()

    if not subject_id:
        return JsonResponse({"status": "FAIL", "message": "Thiếu subject_id."}, status=400)

    subject = get_lecturer_subject_or_404(
        request.user,
        subject_id=subject_id,
    )

    all_qs = Question.objects.filter(
        subject_id=subject,
        is_exam_clone=False,
    )

    if view_type == "generate" and workspace_id:
        filtered_qs = _filter_drafts_for_workspace(
            all_qs.filter(
                is_draft=True,
                question_bank_id__isnull=True,
            ),
            workspace_id,
        )

    elif view_type == "bank" and real_bank_id:
        bank_obj = _get_lecturer_question_banks(request).filter(
            subject_id=subject,
            pk=real_bank_id,
        ).first()

        if bank_obj is None:
            return JsonResponse(
                {"status": "FAIL", "message": "Ngân hàng câu hỏi không hợp lệ."},
                status=404,
            )

        official_bank_qs = all_qs.filter(
            question_bank_id=bank_obj,
            is_draft=False,
        )

        workspace_draft_qs = _filter_drafts_for_workspace(
            all_qs.filter(
                is_draft=True,
                question_bank_id__isnull=True,
            ),
            workspace_id,
        )
        filtered_qs = (official_bank_qs | workspace_draft_qs).distinct()

    else:
        filtered_qs = Question.objects.none()

    display_questions = list(filtered_qs.order_by("-pk"))

    if view_type == "generate":
        display_questions = [
            item for item in display_questions
            if not _parse_edit_draft_source_id_from_text(item.question_text)
        ]

    if view_type == "bank":
        edit_source_ids = {
            source_id
            for source_id in (
                _parse_edit_draft_source_id_from_text(item.question_text)
                for item in display_questions
                if item.is_draft
            )
            if source_id
        }

        display_questions = [
            item
            for item in display_questions
            if not (not item.is_draft and item.pk in edit_source_ids)
        ]

    if difficulty and difficulty != "ALL":
        response_questions = [
            item for item in display_questions
            if item.difficulty == difficulty
        ]
    else:
        response_questions = display_questions

    return JsonResponse(
        {
            "status": "SUCCESS",
            "questions": [_serialize_question(item) for item in response_questions],
            "summary": {
                "all": len(display_questions),
                "easy": sum(1 for item in display_questions if item.difficulty == DifficultyLevel.EASY),
                "medium": sum(1 for item in display_questions if item.difficulty == DifficultyLevel.MEDIUM),
                "hard": sum(1 for item in display_questions if item.difficulty == DifficultyLevel.HARD),
            },
        }
    )
@login_required
@require_POST
@lecturer_required_json
def api_create_manual_question(request):

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    subject_id = payload.get("subject_id") or payload.get("bank_id")
    real_bank_id = payload.get("real_bank_id")
    workspace_id = _get_workspace_id(request, payload)
    view_type = (payload.get("view_type") or "bank").strip()
    question_text = (payload.get("content") or payload.get("question_text") or "").strip()

    try:
        _validate_question_text_length(question_text)
    except ValueError as exc:
        return JsonResponse({"status": "FAIL", "message": str(exc)}, status=400)

    form = LecturerManualQuestionForm(
        {
            "subject_id": subject_id,
            "question_text": question_text,
            "difficulty": payload.get("difficulty"),
        }
    )

    if not form.is_valid():
        return JsonResponse({"status": "FAIL", "message": _first_form_error_message(form)}, status=400)

    subject = get_lecturer_subject_or_404(
        request.user,
        subject_id=form.cleaned_data["subject_id"],
    )
    question_text = form.cleaned_data["question_text"]

    if _question_exists_in_subject(subject, question_text):
        return JsonResponse(
            {"status": "FAIL", "message": "Câu hỏi bị trùng trong môn học này."},
            status=400,
        )

    bank_obj = None
    is_draft_question = view_type in ["bank", "generate"]

    if is_draft_question and not workspace_id:
        return JsonResponse(
            {"status": "FAIL", "message": "Thiếu workspace_id để tạo câu hỏi nháp."},
            status=400,
        )

    if not is_draft_question and real_bank_id:
        bank_obj = _get_lecturer_question_banks(request).filter(
            subject_id=subject,
            pk=real_bank_id,
        ).first()
        if bank_obj is None:
            return JsonResponse({"status": "FAIL", "message": "Ngân hàng câu hỏi không hợp lệ."}, status=404)

    stored_question_text = (
        _append_draft_workspace_marker(question_text, workspace_id)
        if is_draft_question
        else question_text
    )

    try:
        question = Question.objects.create(
            subject_id=subject,
            question_bank_id=None if is_draft_question else bank_obj,
            question_text=stored_question_text,
            difficulty=form.cleaned_data["difficulty"],
            is_draft=is_draft_question,
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
@lecturer_required_json
def api_update_question_bank_question(request, question_id):
    question = get_object_or_404(
        Question.objects.select_related("subject_id", "question_bank_id"),
        pk=question_id,
    )

    if not lecturer_can_access_subject(request.user, question.subject):
        return JsonResponse(
            {"status": "FAIL", "message": "Không có quyền truy cập môn học này."},
            status=403,
        )

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    if question.is_exam_clone:
        return JsonResponse(
            {"status": "FAIL", "message": "Không thể sửa câu hỏi đang được sử dụng trong mã đề."},
            status=400,
        )

    view_type = (payload.get("view_type") or "bank").strip()
    workspace_id = _get_workspace_id(request, payload)
    question_text = (payload.get("content") or payload.get("question_text") or "").strip()
    difficulty = payload.get("difficulty")

    if difficulty not in ["EASY", "MEDIUM", "HARD"]:
        difficulty = question.difficulty

    if question_text:
        try:
            _validate_question_text_length(question_text)
        except ValueError as exc:
            return JsonResponse(
                {"status": "FAIL", "message": str(exc)},
                status=400,
            )

    is_draft = _is_draft_question(question)

    # Nếu đang sửa một draft thì cập nhật ngay trên draft đó.
    # Trường hợp chỉ đổi độ khó, giữ nguyên nội dung câu hỏi hiện tại.
    if is_draft:
        current_text = _strip_draft_edit_text_marker(question.question_text)
        next_text = _strip_draft_edit_text_marker(question_text) if question_text else current_text

        source_question_id = _parse_edit_draft_source_id_from_text(question.question_text)

        exclude_ids = [question.pk]
        if source_question_id:
            exclude_ids.append(source_question_id)

        if next_text and _question_exists_in_subject_excluding_ids(
            question.subject,
            next_text,
            exclude_ids=exclude_ids,
        ):
            return JsonResponse(
                {"status": "FAIL", "message": "Câu hỏi bị trùng trong môn học này."},
                status=400,
            )

        if source_question_id:
            question.question_text = _append_draft_edit_text_marker(
                next_text,
                source_question_id,
                workspace_id,
            )
        else:
            question.question_text = _append_draft_workspace_marker(
                next_text,
                workspace_id,
            )

        question.difficulty = difficulty

        try:
            question.save()
        except IntegrityError:
            return JsonResponse(
                {"status": "FAIL", "message": "Câu hỏi bị trùng trong môn học này."},
                status=400,
            )

        serialized = _serialize_question(question)
        serialized["updated_at"] = timezone.localtime(timezone.now()).strftime("%H:%M - %d/%m/%Y")

        return JsonResponse(
            {
                "status": "SUCCESS",
                "question": serialized,
                "message": "Đã cập nhật bản nháp câu hỏi.",
            }
        )

    # Nếu đang sửa câu hỏi chính thức trong ngân hàng:
    # không cập nhật trực tiếp câu gốc, mà tạo/cập nhật một draft màu vàng.
    if view_type == "bank":
        if not workspace_id:
            return JsonResponse(
                {"status": "FAIL", "message": "Thiếu workspace_id để tạo bản nháp chỉnh sửa."},
                status=400,
            )

        current_text = _strip_draft_edit_text_marker(question.question_text)
        next_text = _strip_draft_edit_text_marker(question_text) if question_text else current_text

        if not next_text:
            return JsonResponse(
                {"status": "FAIL", "message": "Nội dung câu hỏi không được để trống."},
                status=400,
            )

        # Chỉ kiểm tra trùng khi nội dung thực sự thay đổi.
        content_changed = normalize_question_text(next_text) != normalize_question_text(current_text)
        if content_changed and _question_exists_in_subject_excluding_ids(
            question.subject,
            next_text,
            exclude_ids=[question.pk],
        ):
            return JsonResponse(
                {"status": "FAIL", "message": "Câu hỏi bị trùng trong môn học này."},
                status=400,
            )

        draft_question = Question.objects.filter(
            subject_id=question.subject,
            is_exam_clone=False,
            is_draft=True,
            question_bank_id__isnull=True,
            question_text__contains=f"[DRAFT_EDIT:{question.pk}]",
        )
        draft_question = _filter_drafts_for_workspace(draft_question, workspace_id).first()

        if draft_question is None:
            draft_question = Question(
                subject_id=question.subject,
                question_bank_id=None,
                is_draft=True,
            )

        draft_question.question_text = _append_draft_edit_text_marker(
            next_text,
            question.pk,
            workspace_id,
        )
        draft_question.difficulty = difficulty

        try:
            draft_question.save()
        except IntegrityError:
            return JsonResponse(
                {"status": "FAIL", "message": "Câu hỏi bị trùng trong môn học này."},
                status=400,
            )

        serialized = _serialize_question(draft_question)
        serialized["updated_at"] = timezone.localtime(timezone.now()).strftime("%H:%M - %d/%m/%Y")

        return JsonResponse(
            {
                "status": "SUCCESS",
                "question": serialized,
                "message": "Đã lưu bản nháp chỉnh sửa. Bấm Cập nhật vào ngân hàng để lưu chính thức.",
            }
        )

    # Nhánh ngoài màn ngân hàng: giữ hành vi cập nhật trực tiếp.
    next_text = question_text or question.question_text

    if next_text and _question_exists_in_subject_excluding_ids(
        question.subject,
        next_text,
        exclude_ids=[question.pk],
    ):
        return JsonResponse(
            {"status": "FAIL", "message": "Câu hỏi bị trùng trong môn học này."},
            status=400,
        )

    if question_text:
        question.question_text = question_text
    question.difficulty = difficulty

    try:
        question.save()
    except IntegrityError:
        return JsonResponse(
            {"status": "FAIL", "message": "Câu hỏi bị trùng trong môn học này."},
            status=400,
        )

    serialized = _serialize_question(question)
    serialized["updated_at"] = timezone.localtime(timezone.now()).strftime("%H:%M - %d/%m/%Y")

    return JsonResponse(
        {
            "status": "SUCCESS",
            "question": serialized,
            "message": "Cập nhật câu hỏi thành công.",
        }
    )

@login_required
@require_POST
@lecturer_required_json
def api_delete_question_bank_question(request, question_id):
    question = get_object_or_404(
        Question.objects.select_related("subject_id"),
        pk=question_id,
    )
    if question.is_exam_clone:
        return JsonResponse(
            {"status": "FAIL", "message": "Không thể xóa câu hỏi đang được sử dụng trong mã đề."},
            status=400,
        )
    if not lecturer_can_access_subject(request.user, question.subject):
        return JsonResponse(
            {"status": "FAIL", "message": "Không có quyền truy cập môn học này."},
            status=403,
        )

    question.delete()
    return JsonResponse({"status": "SUCCESS", "message": "Xóa câu hỏi thành công."})


@login_required
@require_POST
@lecturer_required_json
def api_bulk_update_question_level(request):

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    bank_id = payload.get("bank_id")
    real_bank_id = payload.get("real_bank_id")
    workspace_id = _get_workspace_id(request, payload)
    view_type = (payload.get("view_type") or "bank").strip()
    difficulty = payload.get("difficulty")
    question_ids = payload.get("question_ids", [])

    if isinstance(question_ids, str):
        question_ids = json.loads(question_ids)

    if difficulty not in ["EASY", "MEDIUM", "HARD"]:
        return JsonResponse(
            {"status": "FAIL", "message": "Mức độ câu hỏi không hợp lệ."},
            status=400,
        )

    subject = get_lecturer_subject_or_404(
        request.user,
        subject_id=bank_id,
    )

    clean_question_ids = []
    for item in question_ids:
        try:
            clean_question_ids.append(int(item))
        except (TypeError, ValueError):
            continue

    if not clean_question_ids:
        return JsonResponse(
            {"status": "FAIL", "message": "Bạn chưa chọn câu hỏi nào."},
            status=400,
        )

    # Trong màn ngân hàng: đổi độ khó cũng phải tạo/cập nhật draft,
    # không cập nhật trực tiếp câu hỏi gốc.
    if view_type == "bank":
        if not workspace_id:
            return JsonResponse(
                {"status": "FAIL", "message": "Thiếu workspace_id để tạo bản nháp chỉnh sửa."},
                status=400,
            )

        bank_obj = None
        if real_bank_id:
            bank_obj = _get_lecturer_question_banks(request).filter(
                subject_id=subject,
                pk=real_bank_id,
            ).first()

        created_or_updated = 0

        with transaction.atomic():
            selected_questions = list(
                Question.objects.select_for_update().filter(
                    subject_id=subject,
                    pk__in=clean_question_ids,
                    is_exam_clone=False,
                )
            )

            for question in selected_questions:
                # Nếu câu đang chọn đã là draft thì chỉ đổi độ khó trên chính draft đó.
                if question.is_draft:
                    question.difficulty = difficulty
                    question.save(update_fields=["difficulty"])
                    created_or_updated += 1
                    continue

                # Nếu câu chính thức thì tạo/cập nhật draft chỉnh sửa.
                draft_question = Question.objects.filter(
                    subject_id=question.subject,
                    is_exam_clone=False,
                    is_draft=True,
                    question_bank_id__isnull=True,
                    question_text__contains=f"[DRAFT_EDIT:{question.pk}]",
                )
                draft_question = _filter_drafts_for_workspace(draft_question, workspace_id).first()

                if draft_question is None:
                    draft_question = Question(
                        subject_id=question.subject,
                        question_bank_id=None,
                        is_draft=True,
                    )

                draft_question.question_text = _append_draft_edit_text_marker(
                    _strip_draft_edit_text_marker(question.question_text),
                    question.pk,
                    workspace_id,
                )
                draft_question.difficulty = difficulty
                draft_question.save()
                created_or_updated += 1

        return JsonResponse(
            {
                "status": "SUCCESS",
                "affected": created_or_updated,
                "message": "Đã tạo bản nháp thay đổi mức độ. Bấm Cập nhật vào ngân hàng để lưu chính thức.",
            }
        )

    # Ngoài màn ngân hàng thì giữ cập nhật trực tiếp.
    updated = Question.objects.filter(
        subject_id=subject,
        pk__in=clean_question_ids,
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
@lecturer_required_json
def api_bulk_delete_questions(request):

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    bank_id = payload.get("bank_id")
    question_ids = payload.get("question_ids", [])

    if isinstance(question_ids, str):
        question_ids = json.loads(question_ids)

    subject = get_lecturer_subject_or_404(
        request.user,
        subject_id=bank_id,
    )

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
@lecturer_required_json
def api_generate_questions(request):

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

    subject = get_lecturer_subject_or_404(
        request.user,
        subject_id=subject_id,
    )

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
    generation_profile = _get_ai_generation_profile(total_count)
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
    usage_group_id = f"question_generation_{job_id}"

    try:
        skipped_duplicates = 0
        prepared_questions = []
        retry_excluded_texts = []
        known_question_keys = {
            normalize_question_text(_strip_draft_edit_text_marker(item))
            for item in Question.objects.filter(subject_id=subject, is_exam_clone=False).values_list(
                "question_text",
                flat=True,
            )
            if item
        }

        no_progress_count = 0

        for batch_index in range(generation_profile["max_api_calls"]):
            before_count = len(prepared_questions)

            remaining_counts = _remaining_question_counts(requested_counts, prepared_questions)
            remaining_total = sum(remaining_counts.values())
            if remaining_total <= 0:
                break

            batch_counts = _build_batch_question_counts(
                remaining_counts,
                batch_size=generation_profile["batch_size"],
            )
            batch_total = sum(batch_counts.values())

            if batch_total <= 0:
                break

            excluded_for_next_call = retry_excluded_texts + [
                item["content"]
                for item in prepared_questions
                if item.get("content")
            ]

            generation_result = _generate_questions_from_ai(
                subject=subject,
                materials=materials,
                total_count=batch_total,
                level_config=_question_generation_level_config(batch_counts),
                excluded_question_texts=excluded_for_next_call,
                batch_index=batch_index,
                generation_profile=generation_profile,
                usage_group_id=usage_group_id,
                usage_feature="QUESTION_GENERATION",
                usage_step_name=f"batch_{batch_index + 1}",
                user=request.user,
            )

            if isinstance(generation_result, dict):
                ai_questions = generation_result.get("questions", [])
            else:
                ai_questions = generation_result

            if not isinstance(ai_questions, list):
                ai_questions = []

            for item in ai_questions:
                content = (item.get("content") or "").strip()
                if not content:
                    continue

                content_key = normalize_question_text(content)
                if not content_key:
                    continue

                if content_key in known_question_keys:
                    skipped_duplicates += 1
                    retry_excluded_texts.append(content)
                    continue

                difficulty = item.get("difficulty")
                if remaining_counts.get(difficulty, 0) <= 0:
                    retry_excluded_texts.append(content)
                    continue

                prepared_questions.append(item)
                known_question_keys.add(content_key)
                remaining_counts[difficulty] -= 1

            if len(retry_excluded_texts) > 60:
                retry_excluded_texts = retry_excluded_texts[-60:]

            if len(prepared_questions) == before_count:
                no_progress_count += 1
            else:
                no_progress_count = 0

            if no_progress_count >= generation_profile["no_progress_limit"]:
                break

        # =====================================================
        # BÙ CUỐI: nếu batch chính còn thiếu ít câu, gọi AI thêm
        # một request nhỏ để lấy đủ số lượng yêu cầu.
        # =====================================================
        remaining_counts = _remaining_question_counts(requested_counts, prepared_questions)
        remaining_total = sum(remaining_counts.values())

        if remaining_total > 0:
            refill_profile = _get_ai_refill_generation_profile(
                remaining_total=remaining_total,
                base_profile=generation_profile,
            )

            for refill_index in range(refill_profile["max_api_calls"]):
                before_count = len(prepared_questions)

                remaining_counts = _remaining_question_counts(requested_counts, prepared_questions)
                remaining_total = sum(remaining_counts.values())

                if remaining_total <= 0:
                    break

                batch_counts = _build_batch_question_counts(
                    remaining_counts,
                    batch_size=refill_profile["batch_size"],
                )
                batch_total = sum(batch_counts.values())

                if batch_total <= 0:
                    break

                excluded_for_next_call = retry_excluded_texts + [
                    item["content"]
                    for item in prepared_questions
                    if item.get("content")
                ]

                generation_result = _generate_questions_from_ai(
                    subject=subject,
                    materials=materials,
                    total_count=batch_total,
                    level_config=_question_generation_level_config(batch_counts),
                    excluded_question_texts=excluded_for_next_call,
                    batch_index=generation_profile["max_api_calls"] + refill_index,
                    generation_profile=refill_profile,
                    usage_group_id=usage_group_id,
                    usage_feature="QUESTION_GENERATION_REFILL",
                    usage_step_name=f"refill_{refill_index + 1}",
                    user=request.user,
                )

                if isinstance(generation_result, dict):
                    ai_questions = generation_result.get("questions", [])
                else:
                    ai_questions = generation_result

                if not isinstance(ai_questions, list):
                    ai_questions = []

                for item in ai_questions:
                    content = (item.get("content") or "").strip()
                    if not content:
                        continue

                    content_key = normalize_question_text(content)
                    if not content_key:
                        continue

                    if content_key in known_question_keys:
                        skipped_duplicates += 1
                        retry_excluded_texts.append(content)
                        continue

                    difficulty = item.get("difficulty")
                    if remaining_counts.get(difficulty, 0) <= 0:
                        retry_excluded_texts.append(content)
                        continue

                    prepared_questions.append(item)
                    known_question_keys.add(content_key)
                    remaining_counts[difficulty] -= 1

                if len(retry_excluded_texts) > 60:
                    retry_excluded_texts = retry_excluded_texts[-60:]

                # Nếu lượt bù không thêm được câu nào thì dừng,
                # tránh gọi AI vô ích.
                if len(prepared_questions) == before_count:
                    break

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
                    question_text=_append_draft_workspace_marker(item["content"], workspace_id),
                    difficulty=item["difficulty"],
                    is_draft=True,
                    question_bank_id=None,
                )

                q_data = _serialize_question(q)
                q_data["source"] = item.get("source") or "AI từ tài liệu"
                q_data["source_type"] = "AI"
                created_questions.append(q_data)

        summary = {
            "all": len(created_questions),
            "easy": len([q for q in created_questions if q["difficulty"] == "EASY"]),
            "medium": len([q for q in created_questions if q["difficulty"] == "MEDIUM"]),
            "hard": len([q for q in created_questions if q["difficulty"] == "HARD"]),
        }
        token_usage = summarize_ai_usage(usage_group_id)
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
                "token_usage": token_usage,
                "error_message": shortage_message if is_partial else "",
                "shortage": shortage,
                "shortage_message": "",
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
                "token_usage": token_usage,
                "created_count": len(created_questions),
                "requested_count": total_count,
                "shortage": shortage,
                "shortage_message": "",
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
@lecturer_required_json
def api_generate_questions_status(request):

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
@lecturer_required
def lecturer_question_management(request, subject_code):
    subject = get_lecturer_subject_or_404(
        request.user,
        subject_code=subject_code,
    )
    query = request.GET.copy()
    query["subject_id"] = str(subject.id)
    query.setdefault("mode", "detail")
    return redirect(f"{reverse('qna:lecturer_questions_screen')}?{query.urlencode()}")


@login_required
@require_POST
@lecturer_required_json
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
@lecturer_required_json
def lecturer_upload_material(request, subject_code):
    subject = get_lecturer_subject_or_404(
        request.user,
        subject_code=subject_code,
    )
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
@lecturer_required
def lecturer_generate_exam_codes(request, subject_code):
    subject = get_lecturer_subject_or_404(
        request.user,
        subject_code=subject_code,
    )
    return redirect(f"{reverse('qna:lecturer_generate_codes_screen')}?subject_id={subject.id}")


@login_required
@require_POST
@lecturer_required_json
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
@lecturer_required_json
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
@lecturer_required_json
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

@login_required
@lecturer_required_json
def lecturer_export_questions_word(request):
    return JsonResponse(
        {
            "status": "FAIL",
            "message": "Chức năng xuất Word đã bị tắt theo yêu cầu mới.",
        },
        status=400,
    )

# =========================================================
# QUESTION BANK MANAGEMENT
# =========================================================
QUESTION_JOB_PREFIX = "qna_question_job_"
QUESTION_JOB_TIMEOUT = 60 * 30
AI_MAX_API_CALLS = 7
AI_BATCH_TARGET_TOTAL = 20
AI_BATCH_OVERSAMPLE_RATIO = 1.6
AI_MATERIAL_MAX_CHARS_PER_FILE = 8000
AI_MATERIAL_MAX_TOTAL_CHARS = 32000
AI_TEXT_CACHE_TIMEOUT = 60 * 60
QUESTION_JOB_LOCAL_FALLBACK = {}
QUESTION_JOB_LOCAL_FALLBACK_LOCK = Lock()
QUESTION_BANK_DUPLICATE_NAME_MESSAGE = "Tên ngân hàng đã tồn tại trong môn học này."
QUESTION_BANK_NAME_REQUIRED_MESSAGE = "Tên ngân hàng không được để trống."

def _get_ai_generation_profile(total_count: int) -> dict:
    total_count = int(total_count or 0)

    # Tạo rất ít câu: ưu tiên nhanh
    # Tạo nhiều: cần batch và context lớn hơn
    if total_count <= 60:
        return {
            "max_api_calls": 5,
            "batch_size": 20,
            "oversample_ratio": 1.6,
            "min_extra": 3,
            "max_chars_per_file": 6000,
            "max_total_chars": 24000,
            "no_progress_limit": 3,
        }

    # Tạo vừa: cân bằng tốc độ và độ đủ
    if total_count <= 30:
        return {
            "max_api_calls": 2,
            "batch_size": 15,
            "oversample_ratio": 1.25,
            "min_extra": 1,
            "max_chars_per_file": 4000,
            "max_total_chars": 14000,
            "no_progress_limit": 2,
        }

    # Tạo nhiều: cần batch và context lớn hơn
    if total_count <= 60:
        return {
            "max_api_calls": 4,
            "batch_size": 20,
            "oversample_ratio": 1.4,
            "min_extra": 2,
            "max_chars_per_file": 6000,
            "max_total_chars": 24000,
            "no_progress_limit": 2,
        }

    # Tạo rất nhiều, ví dụ 100 câu
    return {
        "max_api_calls": 8,
        "batch_size": AI_BATCH_TARGET_TOTAL,
        "oversample_ratio": 1.75,
        "min_extra": 3,
        "max_chars_per_file": AI_MATERIAL_MAX_CHARS_PER_FILE,
        "max_total_chars": AI_MATERIAL_MAX_TOTAL_CHARS,
        "no_progress_limit": 3,
    }

def _get_ai_refill_generation_profile(remaining_total: int, base_profile: dict) -> dict:
    remaining_total = int(remaining_total or 0)

    return {
        # Chỉ dùng để bù phần thiếu cuối, nên tối đa 1-2 request nhỏ.
        "max_api_calls": 2 if remaining_total > 3 else 1,
        "batch_size": max(remaining_total, 1),

        # Tạo dư mạnh hơn ở lượt bù vì thường chỉ thiếu 1-5 câu.
        "oversample_ratio": 2.5,
        "min_extra": 5,

        # Context ngắn hơn để không làm chậm nhiều.
        "max_chars_per_file": min(int(base_profile.get("max_chars_per_file", 6000)), 6000),
        "max_total_chars": min(int(base_profile.get("max_total_chars", 16000)), 16000),

        "no_progress_limit": 1,
    }

def _get_lecturer_subjects(request):
    return request.user.userprofile.subjects_taught.all().order_by("name")


def _get_selected_subject_for_lecturer(request, subject_id):
    subjects = _get_lecturer_subjects(request)
    if subject_id:
        return get_object_or_404(subjects, pk=subject_id)
    return None


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
    # Giữ lại để tương thích tên hàm cũ.
    return f"DRAFT_{workspace_id}_" if workspace_id else "DRAFT_"


DRAFT_EDIT_MARKER_RE = re.compile(r"\[DRAFT_EDIT:(\d+)\]")
DRAFT_WORKSPACE_MARKER_RE = re.compile(r"\[DRAFT_WORKSPACE:([^\]]+)\]")
DRAFT_INTERNAL_MARKER_RE = re.compile(r"\s*\[(?:DRAFT_EDIT:\d+|DRAFT_WORKSPACE:[^\]]+)\]\s*")


def _strip_draft_edit_text_marker(question_text: str) -> str:
    return DRAFT_INTERNAL_MARKER_RE.sub("\n", question_text or "").strip()


def _make_workspace_marker(workspace_id: str) -> str:
    return f"[DRAFT_WORKSPACE:{workspace_id}]" if workspace_id else ""


def _append_draft_workspace_marker(question_text: str, workspace_id: str) -> str:
    clean_text = _strip_draft_edit_text_marker(question_text)
    marker = _make_workspace_marker(workspace_id)
    if not marker:
        return clean_text
    return f"{clean_text}\n{marker}"


def _make_edit_draft_marker(source_question_id: int) -> str:
    return f"[DRAFT_EDIT:{int(source_question_id)}]"


def _append_draft_edit_text_marker(question_text: str, source_question_id: int, workspace_id: str = "") -> str:
    clean_text = _strip_draft_edit_text_marker(question_text)
    markers = [_make_edit_draft_marker(source_question_id)]
    workspace_marker = _make_workspace_marker(workspace_id)
    if workspace_marker:
        markers.append(workspace_marker)
    return f"{clean_text}\n" + "\n".join(markers)


def _parse_edit_draft_source_id_from_text(question_text: str):
    match = DRAFT_EDIT_MARKER_RE.search(question_text or "")
    if not match:
        return None

    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _filter_drafts_for_workspace(queryset, workspace_id: str):
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        return queryset
    return queryset.filter(question_text__contains=_make_workspace_marker(workspace_id))
def _question_exists_in_subject_excluding_ids(subject, question_text, exclude_ids=None):
    normalized = normalize_question_text(_strip_draft_edit_text_marker(question_text))
    if not normalized:
        return False

    queryset = Question.objects.filter(
        subject_id=subject,
        is_exam_clone=False,
    ).only("question_id", "question_text")

    clean_exclude_ids = {
        int(item)
        for item in (exclude_ids or [])
        if str(item).isdigit()
    }

    if clean_exclude_ids:
        queryset = queryset.exclude(pk__in=clean_exclude_ids)

    for item in queryset:
        if normalize_question_text(_strip_draft_edit_text_marker(item.question_text)) == normalized:
            return True

    return False
def _is_draft_question(question: Question) -> bool:
    return bool(getattr(question, "is_draft", False))
def _question_exists_in_subject(subject, question_text, exclude_question_id=None):
    exclude_ids = [exclude_question_id] if exclude_question_id else []
    return _question_exists_in_subject_excluding_ids(
        subject,
        question_text,
        exclude_ids=exclude_ids,
    )
def _validate_question_text_length(question_text: str) -> None:
    if question_text and len(question_text) > QUESTION_TEXT_MAX_LENGTH:
        raise ValueError(QUESTION_TEXT_TOO_LONG_MESSAGE)

def _first_form_error_message(form) -> str:
    for errors in form.errors.values():
        if errors:
            return str(errors[0])
    return "Dữ liệu không hợp lệ."
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

# =========================================================
# QUESTION BANK MANAGEMENT
# =========================================================


def _get_lecturer_question_banks(request):
    return QuestionBank.objects.filter(subject_id__in=_get_lecturer_subjects(request)).select_related("subject_id")


def _get_lecturer_question_banks_with_counts(request, subject=None):
    queryset = _get_lecturer_question_banks(request)

    if subject is not None:
        queryset = queryset.filter(subject_id=subject)

    return queryset.annotate(
        question_count_value=Count(
            "questions",
            filter=Q(questions__is_exam_clone=False)
            & Q(questions__is_draft=False),
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
    if getattr(question, "is_draft", False):
        source_text = "Bản nháp"
        source_type = "DRAFT"
    else:
        source_text = "Ngân hàng câu hỏi"
        source_type = "MANUAL"

    time_str = ""
    if question.created_at:
        local_time = timezone.localtime(question.created_at)
        time_str = local_time.strftime("%H:%M - %d/%m/%Y")

    return {
        "question_id": question.id,
        "content": _strip_draft_edit_text_marker(question.question_text),
        "difficulty": question.difficulty,
        "difficulty_label": question.get_difficulty_display(),
        "source": source_text,
        "source_type": source_type,
        "created_at": time_str,
        "updated_at": "",
        "is_draft": bool(getattr(question, "is_draft", False)),
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

def _extract_text_from_material_cached(material: LectureMaterial) -> str:
    uploaded_at = getattr(material, "uploaded_at", None)
    version = int(uploaded_at.timestamp()) if uploaded_at else 0
    cache_key = f"qna_material_text_{material.pk}_{version}"

    cached_text = cache.get(cache_key)
    if cached_text is not None:
        return cached_text

    text = _extract_text_from_material(material)
    cache.set(cache_key, text, timeout=AI_TEXT_CACHE_TIMEOUT)
    return text

def _truncate_text_for_llm(text: str, max_chars: int = 18000) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars]

def _slice_text_for_batch(text: str, max_chars: int, batch_index: int) -> str:
    text = (text or "").strip()
    if not text:
        return ""

    if len(text) <= max_chars:
        return text

    # Mỗi batch lấy một đoạn khác nhau của tài liệu.
    step = max_chars
    start = (batch_index * step) % len(text)
    end = start + max_chars

    if end <= len(text):
        return text[start:end]

    first_part = text[start:]
    second_part = text[: max_chars - len(first_part)]
    return f"{first_part}\n{second_part}"

def _build_material_context(
        materials,
        batch_index: int = 0,
        max_chars_per_file: int = AI_MATERIAL_MAX_CHARS_PER_FILE,
        max_total_chars: int = AI_MATERIAL_MAX_TOTAL_CHARS,
) -> str:
    blocks = []
    used_chars = 0

    for idx, material in enumerate(materials, start=1):
        if used_chars >= max_total_chars:
            break

        full_text = _extract_text_from_material_cached(material)

        remaining_limit = max_total_chars - used_chars
        per_file_limit = min(max_chars_per_file, remaining_limit)

        text = _slice_text_for_batch(
            full_text,
            max_chars=per_file_limit,
            batch_index=batch_index,
        )

        if text.strip():
            block = (
                f"TÀI LIỆU {idx}: {material.title}\n"
                f"NỘI DUNG TRÍCH ĐOẠN BATCH {batch_index + 1}:\n{text}"
            )
            blocks.append(block)
            used_chars += len(block)

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

def _build_batch_question_counts(
        remaining_counts: dict[str, int],
        batch_size: int = AI_BATCH_TARGET_TOTAL,
) -> dict[str, int]:
    remaining_total = sum(int(v or 0) for v in remaining_counts.values())

    if remaining_total <= 0:
        return {"EASY": 0, "MEDIUM": 0, "HARD": 0}

    batch_total = min(batch_size, remaining_total)

    batch_counts = {"EASY": 0, "MEDIUM": 0, "HARD": 0}

    # Chia theo tỷ lệ còn thiếu của từng mức độ.
    allocated = 0
    for difficulty, count in remaining_counts.items():
        count = int(count or 0)
        if count <= 0:
            continue

        value = max(1, round(batch_total * count / remaining_total))
        value = min(value, count)
        batch_counts[difficulty] = value
        allocated += value

    # Điều chỉnh nếu làm tròn bị lệch.
    while allocated > batch_total:
        for difficulty in ("HARD", "MEDIUM", "EASY"):
            if allocated <= batch_total:
                break
            if batch_counts[difficulty] > 0:
                batch_counts[difficulty] -= 1
                allocated -= 1

    while allocated < batch_total:
        for difficulty in ("EASY", "MEDIUM", "HARD"):
            if allocated >= batch_total:
                break
            if batch_counts[difficulty] < int(remaining_counts.get(difficulty, 0) or 0):
                batch_counts[difficulty] += 1
                allocated += 1

    return batch_counts

def _build_oversampled_question_counts(
        target_counts: dict[str, int],
        oversample_ratio: float = AI_BATCH_OVERSAMPLE_RATIO,
        min_extra: int = 2,
) -> dict[str, int]:
    request_counts = {}

    for difficulty, count in target_counts.items():
        count = int(count or 0)

        if count <= 0:
            request_counts[difficulty] = 0
            continue

        request_counts[difficulty] = max(
            count + min_extra,
            int(round(count * oversample_ratio)),
        )

    return request_counts

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
    if created_count > 0:
        message = f"Đã tạo {created_count} câu hỏi hợp lệ từ tài liệu."
    else:
        message = "Chưa tạo được câu hỏi hợp lệ từ tài liệu đã chọn."

    return message, ""

def _generate_questions_from_ai(
        subject: Subject,
        materials,
        total_count: int,
        level_config: dict,
        excluded_question_texts=None,
        batch_index: int = 0,
        generation_profile=None,
        usage_group_id: str = "",
        usage_feature: str = "QUESTION_GENERATION",
        usage_step_name: str = "",
        user=None,
):
    api_key = getattr(settings, "OPENAI_API_KEY", None)
    if not api_key:
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise Exception("Thiếu OPENAI_API_KEY trong settings.py hoặc biến môi trường")

    generation_profile = generation_profile or _get_ai_generation_profile(total_count)

    context_text = _build_material_context(
        materials,
        batch_index=batch_index,
        max_chars_per_file=generation_profile["max_chars_per_file"],
        max_total_chars=generation_profile["max_total_chars"],
    )
    if not context_text.strip():
        raise Exception("Không trích xuất được nội dung từ tài liệu tải lên.")

    target_counts = _normalize_requested_question_counts(total_count, level_config)
    request_counts = _build_oversampled_question_counts(
        target_counts,
        oversample_ratio=generation_profile["oversample_ratio"],
        min_extra=generation_profile["min_extra"],
    )
    request_total = sum(request_counts.values())

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

    excluded_items = list(dict.fromkeys(excluded_question_texts))[-12:]
    excluded_block = ""
    if excluded_items:
        excluded_block = "\n\nCác câu không được lặp lại:\n" + "\n".join(
            f"- {item}" for item in excluded_items
        )

    system_prompt = """
    Bạn là trợ lý tạo câu hỏi vấn đáp cho giảng viên đại học.

    Nhiệm vụ:
    - Chỉ được tạo câu hỏi dựa trên nội dung tài liệu được cung cấp.
    - Không bịa thêm kiến thức ngoài tài liệu.
    - Câu hỏi phải rõ ràng, đúng ngữ cảnh môn học.
    - Câu hỏi dạng vấn đáp, phù hợp để giảng viên hỏi sinh viên trực tiếp.
    - Không được tạo câu hỏi trùng ý nhau quá nhiều.
    - Phân loại đúng mức độ EASY / MEDIUM / HARD.

    Yêu cầu ngôn ngữ bắt buộc:
    - Tất cả câu hỏi trong trường "content" phải viết bằng tiếng Việt.
    - Nếu tài liệu nguồn là tiếng Anh, hãy hiểu nội dung và chuyển thành câu hỏi tiếng Việt.
    - Không được trả câu hỏi hoàn toàn bằng tiếng Anh.
    - Có thể giữ lại thuật ngữ chuyên ngành tiếng Anh nếu cần, ví dụ: Decision Tree, Naïve Bayes, Data Warehouse, nhưng câu hỏi chính vẫn phải là tiếng Việt.

    Mức độ:
    - EASY: hỏi khái niệm, định nghĩa, trình bày cơ bản.
    - MEDIUM: hỏi phân tích, so sánh, giải thích, liên hệ.
    - HARD: hỏi vận dụng, đánh giá, tình huống, lập luận sâu.

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
    """

    user_prompt = f"""
Môn học: {subject.name}
Mã môn: {subject.subject_code}
Ngôn ngữ đầu ra bắt buộc: tiếng Việt.
Nếu tài liệu là tiếng Anh, hãy chuyển ý sang câu hỏi tiếng Việt tự nhiên.

Hãy tạo đúng {request_total} câu hỏi vấn đáp mới cho batch hiện tại dựa trên nội dung tài liệu bên dưới.
Đây chỉ là một batch nhỏ trong quá trình tạo ngân hàng câu hỏi, không cần bao phủ toàn bộ tài liệu trong một lần.

Số lượng AI cần tạo:
- EASY: {request_counts['EASY']}
- MEDIUM: {request_counts['MEDIUM']}
- HARD: {request_counts['HARD']}

Sau khi AI trả về, backend sẽ lọc trùng và chỉ chọn đúng số câu cần cho batch này:
- EASY: {target_counts['EASY']}
- MEDIUM: {target_counts['MEDIUM']}
- HARD: {target_counts['HARD']}

Yêu cầu chất lượng:
- Câu hỏi phải bám sát tài liệu.
- Câu hỏi ngắn gọn, rõ ràng, có chiều sâu.
- Không lặp lại nội dung giữa các câu.
- Không được tạo câu hỏi trùng hoặc gần trùng với danh sách loại trừ.
- Có thể tạo dư số lượng, nhưng mọi câu hỏi đều phải đúng nội dung tài liệu.
{excluded_block}

TÀI LIỆU:
{context_text}
"""

    started_at = perf_counter()

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )

    latency_ms = int((perf_counter() - started_at) * 1000)

    if usage_group_id:
        record_ai_usage(
            group_id=usage_group_id,
            feature=usage_feature,
            step_name=usage_step_name or f"batch_{batch_index + 1}",
            model_name="gpt-4o-mini",
            usage=response.usage,
            user=user,
            subject=subject,
            latency_ms=latency_ms,
            metadata={
                "batch_index": batch_index,
                "target_total": total_count,
                "ai_request_total": request_total,
                "target_counts": target_counts,
                "request_counts": request_counts,
                "material_ids": [str(item.id) for item in materials],
                "max_chars_per_file": generation_profile.get("max_chars_per_file") if generation_profile else None,
                "max_total_chars": generation_profile.get("max_total_chars") if generation_profile else None,
            },
        )

    raw = response.choices[0].message.content or "{}"
    data = json.loads(raw)
    questions = data.get("questions", [])

    if not isinstance(questions, list):
        questions = []

    normalized_questions = _normalize_ai_generated_questions(
        questions,
        excluded_question_keys=excluded_question_keys,
    )

    collected_questions = []
    collected_question_keys = set()
    remaining_counts = dict(target_counts)

    for item in normalized_questions:
        content_key = normalize_question_text(item["content"])
        if not content_key or content_key in collected_question_keys:
            continue

        difficulty = item.get("difficulty")
        if difficulty not in remaining_counts:
            continue

        if remaining_counts[difficulty] <= 0:
            continue

        collected_questions.append(item)
        collected_question_keys.add(content_key)
        remaining_counts[difficulty] -= 1

    shortage, shortage_text = _build_question_shortage(remaining_counts)

    return {
        "questions": collected_questions,
        "remaining_counts": remaining_counts,
        "is_partial": bool(shortage),
        "shortage": shortage,
        "shortage_text": shortage_text,
    }
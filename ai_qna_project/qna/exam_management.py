from __future__ import annotations

import json
import random
import re
from decimal import Decimal
from typing import Dict, Sequence
from uuid import uuid4

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from .models import (
    DifficultyLevel,
    ExamCode,
    ExamCodeQuestion,
    ExamSet,
    ExamSetStatus,
    Question,
    QuestionBank,
    SemesterChoices,
    Subject,
)


def _ensure_lecturer(request):
    profile = getattr(request.user, "userprofile", None)
    if not profile or not profile.is_lecturer:
        raise PermissionDenied("Không có quyền truy cập.")


def _lecturer_subjects(request):
    return request.user.userprofile.subjects_taught.all().order_by("name")


def _validate_academic_year(value: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{4}$", (value or "").strip()))


def _clone_question_for_exam(source_question: Question) -> Question:
    return Question.objects.create(
        subject=source_question.subject,
        bank=source_question.bank,
        question_text=source_question.question_text,
        question_id_in_barem=f"EC_{uuid4().hex[:8]}",
        difficulty=source_question.difficulty,
        is_supplementary=False,
        is_exam_clone=True,
    )


def _delete_exam_clone(question: Question | None) -> None:
    if question and question.is_exam_clone:
        question.delete()


def _difficulty_meta():
    return {
        DifficultyLevel.EASY: {"label": "Dễ", "slug": "easy"},
        DifficultyLevel.MEDIUM: {"label": "Trung bình", "slug": "medium"},
        DifficultyLevel.HARD: {"label": "Khó", "slug": "hard"},
    }


def _score_for_difficulty(exam_set: ExamSet, difficulty: str) -> Decimal:
    if difficulty == DifficultyLevel.EASY:
        return exam_set.easy_score
    if difficulty == DifficultyLevel.MEDIUM:
        return exam_set.medium_score
    return exam_set.hard_score


def _required_counts(exam_set: ExamSet) -> Dict[str, int]:
    return {
        DifficultyLevel.EASY: exam_set.easy_pool_size,
        DifficultyLevel.MEDIUM: exam_set.medium_pool_size,
        DifficultyLevel.HARD: exam_set.hard_pool_size,
    }


def _ordered_items(code: ExamCode):
    meta = _difficulty_meta()
    items = []
    for item in code.exam_code_questions.select_related("question").order_by("display_order", "id"):
        question = item.question
        info = meta.get(item.difficulty, {"label": item.difficulty, "slug": item.difficulty.lower()})
        items.append(
            {
                "id": item.id,
                "question_id": question.id if question else None,
                "difficulty": item.difficulty,
                "label": info["label"],
                "slug": info["slug"],
                "display_order": item.display_order,
                "score": float(item.score or 0),
                "content": question.question_text if question else "",
            }
        )
    return items


def _serialize_exam_code(code: ExamCode) -> dict:
    items = _ordered_items(code)
    return {
        "id": code.id,
        "code_name": code.code_name,
        "code_number": code.code_number,
        "is_approved": code.is_approved,
        "is_linked": code.is_linked,
        "items": items,
        "preview": [
            {
                "index": index + 1,
                "content": item["content"],
                "label": item["label"],
            }
            for index, item in enumerate(items)
        ],
    }


def _build_question_pools(
    subject: Subject,
    bank_ids: Sequence[int],
    easy_count: int,
    medium_count: int,
    hard_count: int,
):
    base_qs = Question.objects.filter(
        subject=subject,
        bank_id__in=bank_ids,
        is_exam_clone=False,
    )

    pools = {
        DifficultyLevel.EASY: list(base_qs.filter(difficulty=DifficultyLevel.EASY)),
        DifficultyLevel.MEDIUM: list(base_qs.filter(difficulty=DifficultyLevel.MEDIUM)),
        DifficultyLevel.HARD: list(base_qs.filter(difficulty=DifficultyLevel.HARD)),
    }

    required = {
        DifficultyLevel.EASY: easy_count,
        DifficultyLevel.MEDIUM: medium_count,
        DifficultyLevel.HARD: hard_count,
    }

    labels = {
        DifficultyLevel.EASY: "dễ",
        DifficultyLevel.MEDIUM: "trung bình",
        DifficultyLevel.HARD: "khó",
    }

    for difficulty, needed in required.items():
        total = len(pools[difficulty])
        if total < needed:
            raise ValueError(
                f"Ngân hàng đang chọn chỉ có {total} câu {labels[difficulty]}, chưa đủ {needed} câu."
            )
        random.shuffle(pools[difficulty])

    return pools


def _calculate_max_versions(
    pools: dict,
    easy_count: int,
    medium_count: int,
    hard_count: int,
    allow_duplicates: bool,
) -> int:
    if allow_duplicates:
        return 999999

    max_easy = len(pools[DifficultyLevel.EASY]) // easy_count if easy_count else 0
    max_medium = len(pools[DifficultyLevel.MEDIUM]) // medium_count if medium_count else 0
    max_hard = len(pools[DifficultyLevel.HARD]) // hard_count if hard_count else 0
    return min(max_easy, max_medium, max_hard)


def _generate_version_blueprints(
    pools: dict,
    versions: int,
    easy_count: int,
    medium_count: int,
    hard_count: int,
    allow_duplicates: bool,
):
    result = []
    requirements = {
        DifficultyLevel.EASY: easy_count,
        DifficultyLevel.MEDIUM: medium_count,
        DifficultyLevel.HARD: hard_count,
    }

    if allow_duplicates:
        for _ in range(versions):
            selected = {}
            for difficulty, needed in requirements.items():
                pool = pools[difficulty]
                selected[difficulty] = random.sample(pool, needed)
            result.append(selected)
        return result

    max_versions = _calculate_max_versions(
        pools=pools,
        easy_count=easy_count,
        medium_count=medium_count,
        hard_count=hard_count,
        allow_duplicates=False,
    )
    if versions > max_versions:
        raise ValueError(
            f"Không thể tạo {versions} mã đề không trùng lặp câu hỏi. "
            f"Tối đa chỉ tạo được {max_versions} mã đề từ ngân hàng hiện tại."
        )

    shuffled_pools = {}
    for difficulty, items in pools.items():
        copied = items[:]
        random.shuffle(copied)
        shuffled_pools[difficulty] = copied

    for version_index in range(versions):
        selected = {}
        for difficulty, needed in requirements.items():
            start = version_index * needed
            end = start + needed
            selected[difficulty] = shuffled_pools[difficulty][start:end]
        result.append(selected)

    return result


def _build_exam_code_items(
    exam_code: ExamCode,
    selected_questions_by_difficulty: dict,
    shuffle_question_order: bool,
):
    items = []
    display_order = 1

    difficulty_sequence = [
        DifficultyLevel.EASY,
        DifficultyLevel.MEDIUM,
        DifficultyLevel.HARD,
    ]

    flat_rows = []
    for difficulty in difficulty_sequence:
        for source_question in selected_questions_by_difficulty[difficulty]:
            clone = _clone_question_for_exam(source_question)
            flat_rows.append(
                {
                    "question": clone,
                    "difficulty": difficulty,
                    "score": _score_for_difficulty(exam_code.exam_set, difficulty),
                    "display_order": display_order,
                }
            )
            display_order += 1

    if shuffle_question_order:
        random.shuffle(flat_rows)

    for index, row in enumerate(flat_rows, start=1):
        items.append(
            ExamCodeQuestion(
                exam_code=exam_code,
                question=row["question"],
                difficulty=row["difficulty"],
                display_order=index,
                score=row["score"],
            )
        )

    return items


def _replace_exam_code_items(code: ExamCode, new_blueprint: dict):
    old_items = list(code.exam_code_questions.select_related("question").all())

    code.exam_code_questions.all().delete()

    new_items = _build_exam_code_items(
        exam_code=code,
        selected_questions_by_difficulty=new_blueprint,
        shuffle_question_order=code.exam_set.shuffle_question_order,
    )
    ExamCodeQuestion.objects.bulk_create(new_items)

    for old_item in old_items:
        _delete_exam_clone(old_item.question)

    code.is_approved = False
    code.save(update_fields=["is_approved", "updated_at"])


def _collect_used_original_question_ids(exam_set: ExamSet, exclude_code_id: int | None = None):
    qs = ExamCodeQuestion.objects.filter(exam_code__exam_set=exam_set)

    if exclude_code_id is not None:
        qs = qs.exclude(exam_code_id=exclude_code_id)

    used = {
        DifficultyLevel.EASY: set(),
        DifficultyLevel.MEDIUM: set(),
        DifficultyLevel.HARD: set(),
    }

    bank_ids = list(exam_set.question_banks.values_list("id", flat=True))

    for item in qs.select_related("question"):
        question = item.question
        if not question:
            continue

        original = Question.objects.filter(
            subject=exam_set.subject,
            bank_id__in=bank_ids,
            is_exam_clone=False,
            difficulty=item.difficulty,
            question_text=question.question_text,
        ).first()

        if original:
            used[item.difficulty].add(original.id)

    return used


def _regenerate_single_code(code: ExamCode) -> None:
    exam_set = code.exam_set
    if exam_set is None:
        raise ValueError("Mã đề chưa thuộc bộ đề nào.")

    source_bank_ids = list(exam_set.question_banks.values_list("id", flat=True))
    if not source_bank_ids:
        raise ValueError("Bộ đề chưa có nguồn câu hỏi.")

    pools = _build_question_pools(
        subject=exam_set.subject,
        bank_ids=source_bank_ids,
        easy_count=exam_set.easy_pool_size,
        medium_count=exam_set.medium_pool_size,
        hard_count=exam_set.hard_pool_size,
    )

    requirements = _required_counts(exam_set)

    if exam_set.allow_duplicate_questions:
        blueprint = {
            difficulty: random.sample(pools[difficulty], requirements[difficulty])
            for difficulty in requirements
        }
        _replace_exam_code_items(code, blueprint)
        return

    used = _collect_used_original_question_ids(exam_set, exclude_code_id=code.id)

    labels = {
        DifficultyLevel.EASY: "dễ",
        DifficultyLevel.MEDIUM: "trung bình",
        DifficultyLevel.HARD: "khó",
    }

    blueprint = {}
    for difficulty, needed in requirements.items():
        available = [q for q in pools[difficulty] if q.id not in used[difficulty]]
        if len(available) < needed:
            raise ValueError(
                f"Không còn đủ câu {labels[difficulty]} mới để sinh lại mã đề này."
            )
        blueprint[difficulty] = random.sample(available, needed)

    _replace_exam_code_items(code, blueprint)


def _update_exam_code_content_from_payload(code: ExamCode, payload_items: list):
    if not isinstance(payload_items, list) or not payload_items:
        raise ValueError("Danh sách câu hỏi không hợp lệ.")

    existing_items = list(
        code.exam_code_questions.select_related("question").order_by("display_order", "id")
    )

    if len(existing_items) != len(payload_items):
        raise ValueError("Số lượng câu hỏi không khớp với mã đề.")

    for existing, incoming in zip(existing_items, payload_items):
        content = (incoming.get("content") or "").strip()
        if not content:
            raise ValueError("Vui lòng nhập đầy đủ nội dung câu hỏi.")

        question = existing.question
        if question is None:
            question = Question.objects.create(
                subject=code.subject,
                bank=None,
                question_text=content,
                question_id_in_barem=f"EC_{uuid4().hex[:8]}",
                difficulty=existing.difficulty,
                is_supplementary=False,
                is_exam_clone=True,
            )
            existing.question = question
            existing.save(update_fields=["question"])
        else:
            question.question_text = content
            question.difficulty = existing.difficulty
            question.is_exam_clone = True
            question.save(update_fields=["question_text", "difficulty", "is_exam_clone"])

    code.is_approved = False
    code.save(update_fields=["is_approved", "updated_at"])


def _prefetch_exam_sets(qs):
    return qs.select_related("subject").prefetch_related(
        "question_banks",
        Prefetch(
            "exam_codes",
            queryset=ExamCode.objects.prefetch_related(
                Prefetch(
                    "exam_code_questions",
                    queryset=ExamCodeQuestion.objects.select_related("question").order_by("display_order", "id"),
                ),
                "exam_session_groups",
            ),
        ),
    )


@login_required
@require_GET
def lecturer_exam_codes_screen(request):
    _ensure_lecturer(request)

    subjects = list(_lecturer_subjects(request))
    subject_id = (request.GET.get("subject_id") or "").strip()
    academic_year = (request.GET.get("academic_year") or "").strip()
    semester = (request.GET.get("semester") or "").strip()
    status_filter = (request.GET.get("status") or "").strip()
    linked_filter = (request.GET.get("linked") or "").strip()
    keyword = (request.GET.get("q") or "").strip()

    exam_sets = ExamSet.objects.filter(subject__in=subjects)
    if subject_id:
        exam_sets = exam_sets.filter(subject_id=subject_id)
    if academic_year:
        exam_sets = exam_sets.filter(academic_year=academic_year)
    if semester:
        exam_sets = exam_sets.filter(semester=semester)
    if status_filter:
        exam_sets = exam_sets.filter(status=status_filter)
    if linked_filter == "USED":
        exam_sets = exam_sets.filter(exam_codes__exam_session_groups__isnull=False).distinct()
    elif linked_filter == "UNUSED":
        exam_sets = exam_sets.exclude(exam_codes__exam_session_groups__isnull=False).distinct()
    if keyword:
        exam_sets = exam_sets.filter(
            Q(subject__name__icontains=keyword)
            | Q(subject__subject_code__icontains=keyword)
            | Q(name__icontains=keyword)
        )

    exam_sets = _prefetch_exam_sets(exam_sets)

    rows = []
    year_options = []
    seen_years = set()
    for exam_set in exam_sets:
        if exam_set.academic_year and exam_set.academic_year not in seen_years:
            year_options.append(exam_set.academic_year)
            seen_years.add(exam_set.academic_year)
        rows.append(
            {
                "id": exam_set.id,
                "subject_name": exam_set.subject.name,
                "subject_code": exam_set.subject.subject_code,
                "academic_year": exam_set.academic_year,
                "semester": exam_set.get_semester_display(),
                "version_count": exam_set.exam_codes.count(),
                "created_at": exam_set.created_at,
                "status": exam_set.status,
                "status_label": exam_set.status_label,
                "linked_label": exam_set.linked_status_label,
                "is_linked": exam_set.is_linked,
                "detail_url": f"/lecturer/exam-codes/{exam_set.id}/",
                "can_delete": not exam_set.is_linked and exam_set.status == ExamSetStatus.DRAFT,
            }
        )

    context = {
        "subjects": subjects,
        "rows": rows,
        "filter_values": {
            "subject_id": subject_id,
            "academic_year": academic_year,
            "semester": semester,
            "status": status_filter,
            "linked": linked_filter,
            "q": keyword,
        },
        "year_options": sorted(year_options, reverse=True),
        "semester_choices": SemesterChoices.choices,
        "status_choices": ExamSetStatus.choices,
    }
    return render(request, "qna/lecturer/lecturer_exam_codes_management.html", context)


@login_required
@require_GET
def lecturer_generate_codes_screen(request):
    _ensure_lecturer(request)

    subjects = list(_lecturer_subjects(request))
    selected_subject_id = (request.GET.get("subject_id") or "").strip()
    selected_subject = None
    if selected_subject_id:
        selected_subject = get_object_or_404(_lecturer_subjects(request), id=selected_subject_id)
    elif subjects:
        selected_subject = subjects[0]

    question_banks = []
    if selected_subject:
        question_banks = list(
            QuestionBank.objects.filter(subject=selected_subject)
            .annotate(real_question_count=Count("questions", filter=Q(questions__is_exam_clone=False)))
            .order_by("-created_at")
        )

    context = {
        "subjects": subjects,
        "selected_subject": selected_subject,
        "question_banks": question_banks,
        "semester_choices": SemesterChoices.choices,
        "default_year": request.GET.get("academic_year") or "2024-2025",
    }
    return render(request, "qna/lecturer/lecturer_generate_exam_codes.html", context)


@login_required
@require_GET
def lecturer_exam_set_detail_screen(request, exam_set_id: int):
    _ensure_lecturer(request)
    exam_set = get_object_or_404(
        _prefetch_exam_sets(ExamSet.objects.filter(subject__in=_lecturer_subjects(request))),
        id=exam_set_id,
    )
    exam_codes = [_serialize_exam_code(item) for item in exam_set.exam_codes.all()]
    context = {
        "exam_set": exam_set,
        "exam_codes_json": json.dumps(exam_codes, ensure_ascii=False),
        "publish_disabled": exam_set.approved_codes_count != exam_set.exam_codes.count(),
    }
    return render(request, "qna/lecturer/lecturer_exam_code_detail.html", context)


@login_required
@require_POST
def lecturer_create_exam_set(request):
    _ensure_lecturer(request)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    subject_id = payload.get("subject_id")
    academic_year = (payload.get("academic_year") or "").strip()
    semester = (payload.get("semester") or "").strip() or SemesterChoices.HK1
    number_of_versions = int(payload.get("number_of_versions") or 0)
    easy_count = int(payload.get("easy_count") or 0)
    medium_count = int(payload.get("medium_count") or 0)
    hard_count = int(payload.get("hard_count") or 0)
    easy_score = payload.get("easy_score") or 2.0
    medium_score = payload.get("medium_score") or 2.5
    hard_score = payload.get("hard_score") or 3.0
    shuffle_question_order = str(payload.get("shuffle_question_order", "true")).lower() in {"1", "true", "yes", "on"}
    allow_duplicate_questions = str(payload.get("allow_duplicate_questions", "false")).lower() in {"1", "true", "yes", "on"}
    source_bank_ids = payload.get("source_bank_ids") or []

    if isinstance(source_bank_ids, str):
        source_bank_ids = [item for item in source_bank_ids.split(",") if item]

    if not subject_id:
        return JsonResponse({"status": "FAIL", "message": "Vui lòng chọn môn học."}, status=400)
    if not source_bank_ids:
        return JsonResponse({"status": "FAIL", "message": "Vui lòng chọn Ngân hàng câu hỏi"}, status=400)
    if not _validate_academic_year(academic_year):
        return JsonResponse({"status": "FAIL", "message": "Năm học không hợp lệ ! Vui lòng nhập lại!"}, status=400)
    if number_of_versions <= 0:
        return JsonResponse({"status": "FAIL", "message": "Vui lòng nhập Số lượng mã đề"}, status=400)
    if easy_count <= 0 or medium_count <= 0 or hard_count <= 0:
        return JsonResponse({"status": "FAIL", "message": "Số lượng ma trận phải lớn hơn 0."}, status=400)

    subject = get_object_or_404(_lecturer_subjects(request), id=subject_id)
    bank_ids = [int(item) for item in source_bank_ids]
    banks = list(QuestionBank.objects.filter(subject=subject, id__in=bank_ids))
    if not banks:
        return JsonResponse({"status": "FAIL", "message": "Nguồn câu hỏi không hợp lệ."}, status=400)

    try:
        pools = _build_question_pools(subject, bank_ids, easy_count, medium_count, hard_count)
        blueprints = _generate_version_blueprints(
            pools=pools,
            versions=number_of_versions,
            easy_count=easy_count,
            medium_count=medium_count,
            hard_count=hard_count,
            allow_duplicates=allow_duplicate_questions,
        )
    except ValueError as exc:
        return JsonResponse({"status": "FAIL", "message": str(exc)}, status=400)

    with transaction.atomic():
        exam_set = ExamSet.objects.create(
            subject=subject,
            name=subject.name,
            academic_year=academic_year,
            semester=semester,
            number_of_versions=number_of_versions,
            easy_pool_size=easy_count,
            medium_pool_size=medium_count,
            hard_pool_size=hard_count,
            easy_score=easy_score,
            medium_score=medium_score,
            hard_score=hard_score,
            shuffle_question_order=shuffle_question_order,
            allow_duplicate_questions=allow_duplicate_questions,
            created_by=request.user,
            status=ExamSetStatus.DRAFT,
        )
        exam_set.question_banks.set(banks)

        created_codes = []
        for index in range(number_of_versions):
            code_number = 100 + index + 1
            created_codes.append(
                ExamCode(
                    subject=subject,
                    exam_set=exam_set,
                    code_name=str(code_number),
                    code_number=code_number,
                    source_material=", ".join(bank.name for bank in banks),
                    is_approved=False,
                )
            )

        created_codes = ExamCode.objects.bulk_create(created_codes)

        code_items = []
        for code, blueprint in zip(created_codes, blueprints):
            code_items.extend(
                _build_exam_code_items(
                    exam_code=code,
                    selected_questions_by_difficulty=blueprint,
                    shuffle_question_order=shuffle_question_order,
                )
            )

        ExamCodeQuestion.objects.bulk_create(code_items)

    return JsonResponse(
        {
            "status": "SUCCESS",
            "message": "Tạo đề thi thành công.",
            "detail_url": f"/lecturer/exam-codes/{exam_set.id}/",
            "exam_set_id": exam_set.id,
        }
    )


@login_required
@require_POST
def lecturer_update_exam_code_content(request, exam_code_id: int):
    _ensure_lecturer(request)
    code = get_object_or_404(
        ExamCode.objects.select_related("exam_set", "subject").prefetch_related(
            Prefetch(
                "exam_code_questions",
                queryset=ExamCodeQuestion.objects.select_related("question").order_by("display_order", "id"),
            )
        ),
        id=exam_code_id,
        subject__in=_lecturer_subjects(request),
    )

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    items = payload.get("items", [])

    try:
        with transaction.atomic():
            _update_exam_code_content_from_payload(code, items)
    except ValueError as exc:
        return JsonResponse({"status": "FAIL", "message": str(exc)}, status=400)

    code.refresh_from_db()
    return JsonResponse(
        {
            "status": "SUCCESS",
            "message": "Lưu nháp thành công.",
            "exam_code": _serialize_exam_code(code),
        }
    )


@login_required
@require_POST
def lecturer_approve_exam_code(request, exam_code_id: int):
    _ensure_lecturer(request)
    code = get_object_or_404(
        ExamCode.objects.select_related("exam_set", "subject").prefetch_related(
            Prefetch(
                "exam_code_questions",
                queryset=ExamCodeQuestion.objects.select_related("question").order_by("display_order", "id"),
            )
        ),
        id=exam_code_id,
        subject__in=_lecturer_subjects(request),
    )

    items = code.exam_code_questions.select_related("question").all()
    if not items.exists():
        return JsonResponse({"status": "FAIL", "message": "Mã đề chưa có câu hỏi."}, status=400)

    for item in items:
        if item.question is None or not (item.question.question_text or "").strip():
            return JsonResponse({"status": "FAIL", "message": "Vui lòng nhập nội dung."}, status=400)

    code.is_approved = True
    code.save(update_fields=["is_approved", "updated_at"])
    return JsonResponse(
        {
            "status": "SUCCESS",
            "message": "Duyệt mã đề thành công",
            "exam_code": _serialize_exam_code(code),
        }
    )


@login_required
@require_POST
def lecturer_regenerate_exam_code(request, exam_code_id: int):
    _ensure_lecturer(request)
    code = get_object_or_404(
        ExamCode.objects.select_related("exam_set", "subject").prefetch_related("exam_code_questions"),
        id=exam_code_id,
        subject__in=_lecturer_subjects(request),
    )
    if code.is_linked:
        return JsonResponse(
            {"status": "FAIL", "message": "Mã đề đang được sử dụng trong ca thi, không thể sinh lại."},
            status=400,
        )

    try:
        with transaction.atomic():
            _regenerate_single_code(code)
    except ValueError as exc:
        return JsonResponse({"status": "FAIL", "message": str(exc)}, status=400)

    code.refresh_from_db()
    return JsonResponse(
        {
            "status": "SUCCESS",
            "message": "Sinh lại mã đề thành công",
            "exam_code": _serialize_exam_code(code),
        }
    )


@login_required
@require_POST
def lecturer_delete_exam_code(request, exam_code_id: int):
    _ensure_lecturer(request)
    code = get_object_or_404(
        ExamCode.objects.select_related("exam_set", "subject").prefetch_related(
            Prefetch(
                "exam_code_questions",
                queryset=ExamCodeQuestion.objects.select_related("question"),
            )
        ),
        id=exam_code_id,
        subject__in=_lecturer_subjects(request),
    )
    if code.is_linked:
        return JsonResponse(
            {"status": "FAIL", "message": "Mã đề đang được sử dụng trong ca thi, không thể xóa."},
            status=400,
        )

    exam_set = code.exam_set
    old_questions = [item.question for item in code.exam_code_questions.all()]

    with transaction.atomic():
        code.delete()
        for question in old_questions:
            _delete_exam_clone(question)

        if exam_set:
            remaining = exam_set.exam_codes.count()
            exam_set.number_of_versions = remaining
            if remaining == 0:
                exam_set.delete()
                return JsonResponse(
                    {"status": "SUCCESS", "message": "Xóa mã đề thành công", "deleted_exam_set": True}
                )
            exam_set.save(update_fields=["number_of_versions", "updated_at"])

    return JsonResponse(
        {
            "status": "SUCCESS",
            "message": "Xóa mã đề thành công",
            "remaining": exam_set.exam_codes.count() if exam_set else 0,
        }
    )


@login_required
@require_POST
def lecturer_publish_exam_set(request, exam_set_id: int):
    _ensure_lecturer(request)
    exam_set = get_object_or_404(
        _prefetch_exam_sets(ExamSet.objects.filter(subject__in=_lecturer_subjects(request))),
        id=exam_set_id,
    )
    total_count = exam_set.exam_codes.count()
    approved_count = exam_set.approved_codes_count
    if total_count == 0:
        return JsonResponse({"status": "FAIL", "message": "Bộ đề chưa có mã đề nào để lưu."}, status=400)
    if approved_count != total_count:
        return JsonResponse(
            {"status": "FAIL", "message": f"Vui lòng duyệt {approved_count}/{total_count} mã đề !!!"},
            status=400,
        )

    exam_set.status = ExamSetStatus.APPROVED
    exam_set.save(update_fields=["status", "updated_at"])
    return JsonResponse(
        {"status": "SUCCESS", "message": "Lưu bộ đề thành công !", "redirect_url": "/lecturer/exam-codes/"}
    )


@login_required
@require_POST
def lecturer_save_exam_set_draft(request, exam_set_id: int):
    _ensure_lecturer(request)
    exam_set = get_object_or_404(
        ExamSet.objects.filter(subject__in=_lecturer_subjects(request)),
        id=exam_set_id,
    )
    exam_set.save(update_fields=["updated_at"])
    return JsonResponse({"status": "SUCCESS", "message": "Lưu nháp thành công."})


@login_required
@require_POST
def lecturer_delete_exam_set(request, exam_set_id: int):
    _ensure_lecturer(request)
    exam_set = get_object_or_404(
        _prefetch_exam_sets(ExamSet.objects.filter(subject__in=_lecturer_subjects(request))),
        id=exam_set_id,
    )
    if exam_set.status == ExamSetStatus.APPROVED or exam_set.is_linked:
        return JsonResponse(
            {"status": "FAIL", "message": "Bộ đề đã duyệt hoặc đang trong ca thi, không thể xóa"},
            status=400,
        )

    with transaction.atomic():
        for code in exam_set.exam_codes.all():
            old_questions = [item.question for item in code.exam_code_questions.select_related("question").all()]
            code.delete()
            for question in old_questions:
                _delete_exam_clone(question)
        exam_set.delete()

    return JsonResponse({"status": "SUCCESS", "message": "Xóa đề thi thành công !"})
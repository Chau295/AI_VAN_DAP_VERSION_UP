from __future__ import annotations

import json
import random
import re
from decimal import Decimal, InvalidOperation
from typing import Dict, Sequence
from uuid import uuid4

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.urls import reverse

from .academic_year import ACADEMIC_YEAR_ERROR_MESSAGE, is_valid_academic_year
from .exam_guards import subject_has_active_exam_group
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
    normalize_question_text,
)


# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

def _ensure_lecturer(request):
    profile = getattr(request.user, "userprofile", None)
    if not profile or not profile.is_lecturer:
        raise PermissionDenied("Không có quyền truy cập.")


def _lecturer_subjects(request):
    return request.user.userprofile.subjects_taught.all().order_by("name")


def _subject_has_ongoing_exam_group(subject: Subject) -> bool:
    return subject_has_active_exam_group(subject, now=timezone.now())


def _locked_subject_mutation_response():
    return JsonResponse(
        {"status": "FAIL", "message": "Không thể chỉnh sửa khi ca thi đang diễn ra."},
        status=403,
    )


def _validate_academic_year(value: str) -> bool:
    return is_valid_academic_year(value)


def _validate_exam_set_total_score(exam_set: ExamSet) -> None:
    total = exam_set.total_score
    if abs(float(total) - 10.0) > 0.001:
        raise ValueError("Tổng điểm bộ đề phải bằng 10.")


EXAM_SET_SUCCESS_MESSAGE = "Tạo bộ đề thành công."
EXAM_SET_TOTAL_OVER_MESSAGE = "Tổng điểm hiện tại đang vượt quá 10 điểm."
EXAM_SET_TOTAL_UNDER_MESSAGE = "Tổng điểm hiện tại chưa đủ 10 điểm."
EXAM_SET_NO_QUESTIONS_MESSAGE = "Mỗi mã đề phải có ít nhất 1 câu hỏi."
EXAM_SET_INVALID_QUESTION_SCORE_MESSAGE = "Mỗi câu hỏi phải có số điểm lớn hơn 0."
EXAM_SET_TARGET_TOTAL_SCORE = Decimal("10")


def _parse_int_field(raw_value, field_label: str) -> int:
    try:
        return int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_label} không hợp lệ.") from exc


def _parse_decimal_field(raw_value, field_label: str) -> Decimal:
    try:
        return Decimal(str(raw_value).strip())
    except (InvalidOperation, AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_label} không hợp lệ.") from exc


def _parse_exam_set_code_keyword(keyword: str) -> int | None:
    match = re.match(r"^BD-(\d+)$", (keyword or "").strip(), flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _validate_exam_set_creation_rules(
        *,
        number_of_versions: int,
        easy_count: int,
        medium_count: int,
        hard_count: int,
        easy_score: Decimal,
        medium_score: Decimal,
        hard_score: Decimal,
) -> Decimal:
    if number_of_versions <= 0:
        raise ValueError("Số lượng mã đề phải lớn hơn 0.")

    if any(count < 0 for count in (easy_count, medium_count, hard_count)):
        raise ValueError("Số lượng câu hỏi không hợp lệ.")

    total_questions = easy_count + medium_count + hard_count
    if total_questions <= 0:
        raise ValueError(EXAM_SET_NO_QUESTIONS_MESSAGE)

    score_pairs = (
        (easy_count, easy_score),
        (medium_count, medium_score),
        (hard_count, hard_score),
    )
    if any(count > 0 and score <= 0 for count, score in score_pairs):
        raise ValueError(EXAM_SET_INVALID_QUESTION_SCORE_MESSAGE)

    total_score = (
        Decimal(easy_count) * easy_score
        + Decimal(medium_count) * medium_score
        + Decimal(hard_count) * hard_score
    )

    # KHÔNG chặn khi tổng điểm khác 10
    return total_score


def _validate_exam_code_items(items: Sequence[ExamCodeQuestion]) -> None:
    if not items:
        raise ValueError(EXAM_SET_NO_QUESTIONS_MESSAGE)

    if any(Decimal(str(item.score or 0)) <= 0 for item in items):
        raise ValueError(EXAM_SET_INVALID_QUESTION_SCORE_MESSAGE)


def _question_exists_in_subject(subject: Subject, question_text: str, exclude_question_id: int | None = None) -> bool:
    normalized = normalize_question_text(question_text)
    queryset = Question.objects.filter(
        subject_id=subject,
        question_text_normalized=normalized,
        is_exam_clone=False,
    )
    if exclude_question_id:
        queryset = queryset.exclude(pk=exclude_question_id)
    return queryset.exists()


def _clone_question_for_exam(source_question: Question) -> Question:
    return Question.objects.create(
        subject_id=source_question.subject_id,
        question_bank_id=None,
        question_text=source_question.question_text,
        question_id_in_barem=f"EC_{uuid4().hex[:8]}",
        difficulty=source_question.difficulty,
        is_exam_clone=True,
    )


def _delete_exam_clone(question: Question | None) -> None:
    if question and question.is_exam_clone:
        question.delete()


def _mark_exam_set_draft(exam_set: ExamSet | None) -> None:
    if exam_set and exam_set.status != ExamSetStatus.DRAFT:
        exam_set.status = ExamSetStatus.DRAFT
        exam_set.save(update_fields=["status", "updated_at"])


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


def _next_exam_code_number(exam_set: ExamSet) -> int:
    latest_code = exam_set.exam_codes.order_by("-code_number", "-pk").first()
    return (latest_code.code_number if latest_code else 100) + 1


def _approval_error_for_code(code: ExamCode) -> str | None:
    items = list(code.exam_code_questions.select_related("question_id").all())
    if not items:
        return "Mã đề chưa có câu hỏi."

    for item in items:
        if item.question_id is None or not (item.question_id.question_text or "").strip():
            return "Vui lòng nhập đầy đủ nội dung câu hỏi trước khi duyệt."

    return None


def _exam_set_summary(exam_set: ExamSet) -> dict:
    prefetched_codes = getattr(exam_set, "_prefetched_objects_cache", {}).get("exam_codes")
    codes = list(prefetched_codes) if prefetched_codes is not None else list(exam_set.exam_codes.all())

    approved_count = 0
    used_count = 0
    for code in codes:
        if code.is_approved:
            approved_count += 1
        if code.session_rooms.exists():
            used_count += 1

    return {
        "total_codes_count": len(codes),
        "approved_codes_count": approved_count,
        "used_codes_count": used_count,
    }


def _ordered_items(code: ExamCode):
    meta = _difficulty_meta()
    items = []
    for item in code.exam_code_questions.select_related("question_id").order_by("display_order", "pk"):
        question = item.question_id
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
        "id": code.pk,
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
        subject_id=subject,
        question_bank_id__in=bank_ids,
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

    limits = []
    if easy_count:
        limits.append(len(pools[DifficultyLevel.EASY]) // easy_count)
    if medium_count:
        limits.append(len(pools[DifficultyLevel.MEDIUM]) // medium_count)
    if hard_count:
        limits.append(len(pools[DifficultyLevel.HARD]) // hard_count)
    return min(limits) if limits else 0


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
        for source_question in selected_questions_by_difficulty.get(difficulty, []):
            clone = _clone_question_for_exam(source_question)
            flat_rows.append(
                {
                    "question": clone,
                    "difficulty": difficulty,
                    "score": _score_for_difficulty(exam_code.exam_set_id, difficulty),
                    "display_order": display_order,
                }
            )
            display_order += 1

    for index, row in enumerate(flat_rows, start=1):
        items.append(
            ExamCodeQuestion(
                exam_code=exam_code,
                question_id=row["question"],
                difficulty=row["difficulty"],
                display_order=index,
                score=row["score"],
            )
        )

    return items


def _replace_exam_code_items(code: ExamCode, new_blueprint: dict):
    old_items = list(code.exam_code_questions.select_related("question_id").all())

    code.exam_code_questions.all().delete()

    new_items = _build_exam_code_items(
        exam_code=code,
        selected_questions_by_difficulty=new_blueprint,
    )
    ExamCodeQuestion.objects.bulk_create(new_items)

    for old_item in old_items:
        _delete_exam_clone(old_item.question_id)

    code.is_approved = False
    code.save(update_fields=["is_approved", "updated_at"])
    _mark_exam_set_draft(code.exam_set_id)


def _collect_used_original_question_ids(exam_set: ExamSet, exclude_code_id: int | None = None):
    qs = ExamCodeQuestion.objects.filter(exam_code__exam_set_id=exam_set)

    if exclude_code_id is not None:
        qs = qs.exclude(exam_code__pk=exclude_code_id)

    used = {
        DifficultyLevel.EASY: set(),
        DifficultyLevel.MEDIUM: set(),
        DifficultyLevel.HARD: set(),
    }

    bank_ids = list(exam_set.question_banks.values_list("pk", flat=True))

    for item in qs.select_related("question_id"):
        question = item.question_id
        if not question:
            continue

        original = Question.objects.filter(
            subject_id=exam_set.subject_id,
            question_bank_id__in=bank_ids,
            is_exam_clone=False,
            difficulty=item.difficulty,
            question_text_normalized=normalize_question_text(question.question_text),
        ).first()

        if original:
            used[item.difficulty].add(original.id)

    return used


def _regenerate_single_code(code: ExamCode) -> None:
    exam_set = code.exam_set_id
    if exam_set is None:
        raise ValueError("Mã đề chưa thuộc bộ đề nào.")

    source_bank_ids = list(exam_set.question_banks.values_list("pk", flat=True))
    if not source_bank_ids:
        raise ValueError("Bộ đề chưa có nguồn câu hỏi.")

    pools = _build_question_pools(
        subject=exam_set.subject_id,
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

    used = _collect_used_original_question_ids(exam_set, exclude_code_id=code.pk)

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
        code.exam_code_questions.select_related("question_id").order_by("display_order", "pk")
    )

    if len(existing_items) != len(payload_items):
        raise ValueError("Số lượng câu hỏi không khớp với mã đề.")

    existing_by_id = {str(item.id): item for item in existing_items}
    existing_by_order = {str(item.display_order): item for item in existing_items}
    seen_item_ids = set()
    seen_normalized_contents = set()

    for incoming in payload_items:
        existing = None
        incoming_id = str(incoming.get("id") or "").strip()
        if incoming_id:
            existing = existing_by_id.get(incoming_id)

        if existing is None:
            incoming_order = str(incoming.get("display_order") or "").strip()
            if incoming_order:
                existing = existing_by_order.get(incoming_order)

        if existing is None or existing.id in seen_item_ids:
            raise ValueError("Dữ liệu câu hỏi không hợp lệ.")

        seen_item_ids.add(existing.id)
        content = (incoming.get("content") or "").strip()
        if not content:
            raise ValueError("Vui lòng nhập đầy đủ nội dung câu hỏi.")

        normalized_content = normalize_question_text(content)
        if normalized_content in seen_normalized_contents:
            raise ValueError("Mã đề đang có câu hỏi bị trùng nội dung.")
        seen_normalized_contents.add(normalized_content)

        question = existing.question_id
        if question is None:
            question = Question.objects.create(
                subject_id=code.subject_id,
                question_text=content,
                question_id_in_barem=f"MANUAL_{uuid4().hex[:8]}",
                difficulty=existing.difficulty,
                is_exam_clone=True,
            )
            existing.question_id = question
            existing.save(update_fields=["question_id"])
        else:
            question.question_text = content
            question.save(update_fields=["question_text"])

    code.is_approved = False
    code.save(update_fields=["is_approved", "updated_at"])
    _mark_exam_set_draft(code.exam_set_id)


def _prefetch_exam_sets(qs):
    return qs.select_related("subject_id").prefetch_related(
        "question_banks",
        Prefetch(
            "exam_codes",
            queryset=ExamCode.objects.prefetch_related(
                Prefetch(
                    "exam_code_questions",
                    queryset=ExamCodeQuestion.objects.select_related("question_id").order_by("display_order", "pk"),
                ),
                "session_rooms",
            ),
        ),
    )


# ==============================================================================
# VIEW FUNCTIONS
# ==============================================================================
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger

LIST_PAGE_SIZE = 20

@login_required
@require_GET
def lecturer_exam_codes_screen(request):
    _ensure_lecturer(request)

    subjects = list(_lecturer_subjects(request))
    subject_id = (request.GET.get("subject_id") or "").strip()
    academic_year = (request.GET.get("academic_year") or "").strip()
    semester = (request.GET.get("semester") or "").strip()
    status_filter = (request.GET.get("status") or "").strip()
    linked_filter = (request.GET.get("linked") or "").strip().upper()
    keyword = (request.GET.get("q") or "").strip()

    exam_sets = ExamSet.objects.filter(subject_id__in=subjects)
    if subject_id:
        exam_sets = exam_sets.filter(subject_id=subject_id)
    if academic_year:
        exam_sets = exam_sets.filter(academic_year=academic_year)
    if semester:
        exam_sets = exam_sets.filter(semester=semester)
    if status_filter:
        exam_sets = exam_sets.filter(status=status_filter)
    if keyword:
        search_filter = Q(name__icontains=keyword) | Q(
            Q(name="") | Q(name__isnull=True),
            subject_id__name__icontains=keyword,
        )
        exam_set_code_id = _parse_exam_set_code_keyword(keyword)
        if exam_set_code_id is not None:
            search_filter |= Q(exam_set_id=exam_set_code_id)
        exam_sets = exam_sets.filter(search_filter)

    exam_sets = _prefetch_exam_sets(exam_sets)

    rows = []
    for es in exam_sets:
        summary = _exam_set_summary(es)
        used_codes_count = summary["used_codes_count"]

        if linked_filter == "USED" and used_codes_count <= 0:
            continue
        if linked_filter == "UNUSED" and used_codes_count > 0:
            continue

        rows.append(
            {
                "id": es.exam_set_id,
                "exam_set_code": es.exam_code or f"BD-{es.exam_set_id:04d}",
                "subject_name": es.subject_id.name,
                "subject_code": es.subject_id.subject_code,
                "academic_year": es.academic_year,
                "semester": es.get_semester_display(),
                "version_count": summary["total_codes_count"],
                "approved_codes_count": summary["approved_codes_count"],
                "used_codes_count": used_codes_count,
                "status": es.status,
                "is_linked": used_codes_count > 0,
                "can_delete": used_codes_count == 0,
                "created_at": es.created_at,
                "detail_url": reverse(
                    "qna:lecturer_exam_set_detail_screen",
                    kwargs={"exam_set_id": es.exam_set_id},
                ),
            }
        )

    page_number = request.GET.get("page", "1")
    paginator = Paginator(rows, LIST_PAGE_SIZE)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    year_options = sorted(list(set(ExamSet.objects.values_list("academic_year", flat=True))), reverse=True)
    context = {
        "subjects": subjects,
        "rows": list(page_obj.object_list),
        "year_options": year_options,
        "semester_choices": SemesterChoices.choices,
        "status_choices": ExamSetStatus.choices,
        "filter_values": request.GET,
        "page_obj": page_obj,
        "paginator": paginator,
    }
    return render(request, "qna/lecturer/lecturer_exam_codes_management.html", context)


@login_required
@require_GET
def lecturer_generate_codes_screen(request):
    _ensure_lecturer(request)
    subjects_qs = _lecturer_subjects(request)
    subjects = list(subjects_qs)
    selected_subject_id = request.GET.get("subject_id")

    selected_subject = None
    if selected_subject_id:
        selected_subject = get_object_or_404(subjects_qs, pk=selected_subject_id)
    elif subjects:
        selected_subject = subjects[0]

    question_banks = []
    if selected_subject:
        question_banks = QuestionBank.objects.filter(subject_id=selected_subject).annotate(
            real_question_count=Count("questions", filter=Q(questions__is_exam_clone=False))
        )

    context = {
        "subjects": subjects,
        "selected_subject": selected_subject,
        "question_banks": question_banks,
        "semester_choices": SemesterChoices.choices,
        "default_year": "2024-2025",
    }
    return render(request, "qna/lecturer/lecturer_generate_exam_codes.html", context)


@login_required
@require_GET
def lecturer_exam_set_detail_screen(request, exam_set_id):
    _ensure_lecturer(request)
    exam_set = get_object_or_404(
        ExamSet,
        exam_set_id=exam_set_id,
        subject_id__in=request.user.userprofile.subjects_taught.all()
    )

    exam_codes = exam_set.exam_codes.all().order_by('code_number', 'exam_code_id')
    codes_data = [_serialize_exam_code(code) for code in exam_codes]

    context = {
        "exam_set": exam_set,
        "exam_set_code": exam_set.exam_code or f"BD-{exam_set.exam_set_id:04d}",
        "total_codes_count": exam_codes.count(),
        "approved_codes_count": sum(1 for c in codes_data if c['is_approved']),
        "used_codes_count": sum(1 for c in codes_data if c['is_linked']),
        "exam_codes_json": json.dumps(codes_data),
        "exam_set_is_linked": exam_set.is_linked,
        "publish_disabled": exam_codes.filter(is_approved=False).exists() or exam_codes.count() == 0,
        "can_edit_exam_set": not exam_set.is_linked,
        "easy_score": exam_set.easy_score or 0,
        "medium_score": exam_set.medium_score or 0,
        "hard_score": exam_set.hard_score or 0,
    }
    return render(request, "qna/lecturer/lecturer_exam_code_detail.html", context)


@login_required
@require_POST
def lecturer_create_exam_set(request):
    _ensure_lecturer(request)
    payload = json.loads(request.body)

    subject = get_object_or_404(_lecturer_subjects(request), pk=payload.get("subject_id"))
    if _subject_has_ongoing_exam_group(subject):
        return _locked_subject_mutation_response()

    try:
        academic_year = "" if payload.get("academic_year") is None else str(payload.get("academic_year"))
        if not _validate_academic_year(academic_year):
            raise ValueError(ACADEMIC_YEAR_ERROR_MESSAGE)

        exam_code = (payload.get("exam_code") or "").strip()
        if not exam_code:
            raise ValueError("Vui lòng nhập mã bộ đề.")

        # Kiểm tra uniqueness (không phân biệt hoa thường)
        if ExamSet.objects.filter(exam_code__iexact=exam_code).exists():
            raise ValueError(f"Mã bộ đề '{exam_code}' đã tồn tại. Vui lòng chọn mã khác.")

        num_v = _parse_int_field(payload.get("number_of_versions"), "Số lượng mã đề")
        easy_c = _parse_int_field(payload.get("easy_count"), "Câu dễ")
        medium_c = _parse_int_field(payload.get("medium_count"), "Câu trung bình")
        hard_c = _parse_int_field(payload.get("hard_count"), "Câu khó")
        easy_s = _parse_decimal_field(payload.get("easy_score"), "Điểm câu dễ")
        medium_s = _parse_decimal_field(payload.get("medium_score"), "Điểm câu trung bình")
        hard_s = _parse_decimal_field(payload.get("hard_score"), "Điểm câu khó")

        total_score = _validate_exam_set_creation_rules(
            number_of_versions=num_v,
            easy_count=easy_c,
            medium_count=medium_c,
            hard_count=hard_c,
            easy_score=easy_s,
            medium_score=medium_s,
            hard_score=hard_s,
        )

        bank_ids = [int(i) for i in payload.get("source_bank_ids", [])]
        if len(bank_ids) > 10:
            return JsonResponse({"status": "FAIL", "message": "Chỉ được chọn tối đa 10 ngân hàng câu hỏi."}, status=400)
        pools = _build_question_pools(subject, bank_ids, easy_c, medium_c, hard_c)
        blueprints = _generate_version_blueprints(
            pools, num_v, easy_c, medium_c, hard_c,
            str(payload.get("allow_duplicate_questions")).lower() == "true"
        )
    except ValueError as e:
        return JsonResponse({"status": "FAIL", "message": str(e)}, status=400)

    with transaction.atomic():
        es = ExamSet.objects.create(
            subject_id=subject,
            name=subject.name,
            exam_code=exam_code,
            academic_year=academic_year,
            semester=payload.get("semester", SemesterChoices.HK1),
            number_of_versions=num_v,
            easy_pool_size=easy_c,
            medium_pool_size=medium_c,
            hard_pool_size=hard_c,
            easy_score=easy_s,
            medium_score=medium_s,
            hard_score=hard_s,
            created_by_user_id=request.user,
            status=ExamSetStatus.DRAFT
        )
        es.question_banks.set(QuestionBank.objects.filter(pk__in=bank_ids))

        for i in range(num_v):
            code_num = 101 + i
            code = ExamCode.objects.create(
                exam_set_id=es,
                subject_id=subject,
                code_name=str(code_num),
                code_number=code_num,
                is_approved=False
            )
            items = _build_exam_code_items(code, blueprints[i])
            _validate_exam_code_items(items)
            ExamCodeQuestion.objects.bulk_create(items)

    return JsonResponse({
        "status": "SUCCESS",
        "message": EXAM_SET_SUCCESS_MESSAGE,
        "exam_set_id": es.exam_set_id,
        "detail_url": reverse("qna:lecturer_exam_set_detail_screen", kwargs={"exam_set_id": es.exam_set_id}),
    })


@login_required
@require_POST
def lecturer_create_manual_exam_code(request, exam_set_id: int):
    _ensure_lecturer(request)
    exam_set = get_object_or_404(
        ExamSet,
        pk=exam_set_id,
        subject_id__in=_lecturer_subjects(request)
    )
    if _subject_has_ongoing_exam_group(exam_set.subject_id):
        return _locked_subject_mutation_response()

    if exam_set.is_linked:
        return JsonResponse(
            {"status": "FAIL", "message": "Bộ đề đã được sử dụng, không thể tạo thêm mã đề."},
            status=400
        )

    try:
        data = json.loads(request.body)
        payload_items = data.get("items", [])
        if not payload_items:
            return JsonResponse({"status": "FAIL", "message": "Nội dung mã đề trống."}, status=400)

        with transaction.atomic():
            last_code = exam_set.exam_codes.order_by("-code_number").first()
            next_num = (last_code.code_number + 1) if last_code else 101

            new_code = ExamCode.objects.create(
                exam_set_id=exam_set,
                subject_id=exam_set.subject_id,
                code_number=next_num,
                code_name=str(next_num),
                is_approved=False
            )

            db_questions = []
            for item in payload_items:
                content = (item.get("content") or "").strip()
                if not content:
                    raise ValueError("Vui lòng nhập đầy đủ nội dung câu hỏi.")

                if _question_exists_in_subject(exam_set.subject_id, content):
                    raise ValueError(f"Câu hỏi '{content[:30]}...' đã tồn tại trong ngân hàng.")

                difficulty = item.get("difficulty") or DifficultyLevel.MEDIUM

                q = Question.objects.create(
                    subject_id=exam_set.subject_id,
                    question_text=content,
                    question_id_in_barem=f"MANUAL_{uuid4().hex[:8]}",
                    difficulty=difficulty,
                    is_exam_clone=True
                )

                db_questions.append(
                    ExamCodeQuestion(
                        exam_code=new_code,
                        question_id=q,
                        difficulty=difficulty,
                        display_order=item.get("display_order", 1),
                        score=_score_for_difficulty(exam_set, difficulty)
                    )
                )

            ExamCodeQuestion.objects.bulk_create(db_questions)
            _mark_exam_set_draft(exam_set)

        return JsonResponse({
            "status": "SUCCESS",
            "message": f"Đã tạo mã đề {new_code.code_name} thành công.",
            "exam_code": _serialize_exam_code(new_code)
        })

    except ValueError as e:
        return JsonResponse({"status": "FAIL", "message": str(e)}, status=400)
    except Exception:
        return JsonResponse({"status": "FAIL", "message": "Có lỗi xảy ra khi tạo mã đề."}, status=500)


@login_required
@require_POST
def lecturer_update_exam_code_content(request, exam_code_id: int):
    _ensure_lecturer(request)
    code = get_object_or_404(ExamCode, pk=exam_code_id, subject_id__in=_lecturer_subjects(request))
    if _subject_has_ongoing_exam_group(code.subject_id):
        return _locked_subject_mutation_response()

    if code.session_rooms.exists():
        return JsonResponse({"status": "FAIL", "message": "Mã đề đã được sử dụng, không thể sửa."}, status=400)

    try:
        payload = json.loads(request.body)
        with transaction.atomic():
            _update_exam_code_content_from_payload(code, payload.get("items", []))
    except ValueError as e:
        return JsonResponse({"status": "FAIL", "message": str(e)}, status=400)

    return JsonResponse({
        "status": "SUCCESS",
        "message": "Cập nhật thành công.",
        "exam_code": _serialize_exam_code(code)
    })


@login_required
@require_POST
def lecturer_approve_exam_code(request, exam_code_id: int):
    _ensure_lecturer(request)
    code = get_object_or_404(ExamCode, pk=exam_code_id, subject_id__in=_lecturer_subjects(request))
    if _subject_has_ongoing_exam_group(code.subject_id):
        return _locked_subject_mutation_response()

    err = _approval_error_for_code(code)
    if err:
        return JsonResponse({"status": "FAIL", "message": err}, status=400)

    code.is_approved = True
    code.save(update_fields=["is_approved", "updated_at"])

    # Auto-publish logic
    exam_set_published = False
    exam_set = code.exam_set_id
    if not exam_set.exam_codes.filter(is_approved=False).exists():
        exam_set.status = ExamSetStatus.APPROVED
        exam_set.save(update_fields=["status", "updated_at"])
        exam_set_published = True

    return JsonResponse({
        "status": "SUCCESS",
        "message": "Đã duyệt mã đề.",
        "exam_code": _serialize_exam_code(code),
        "exam_set_published": exam_set_published
    })


@login_required
@require_POST
def lecturer_bulk_approve_exam_codes(request, exam_set_id: int):
    _ensure_lecturer(request)
    data = json.loads(request.body)
    code_ids = data.get("exam_code_ids", [])

    exam_set = get_object_or_404(ExamSet, pk=exam_set_id, subject_id__in=_lecturer_subjects(request))
    if _subject_has_ongoing_exam_group(exam_set.subject_id):
        return _locked_subject_mutation_response()

    codes = ExamCode.objects.filter(
        exam_set_id__pk=exam_set_id,
        pk__in=code_ids,
        is_approved=False
    )

    count = 0
    for code in codes:
        if not _approval_error_for_code(code):
            code.is_approved = True
            code.save(update_fields=["is_approved", "updated_at"])
            count += 1

    # Auto-publish logic
    exam_set_published = False
    if count > 0 and not exam_set.exam_codes.filter(is_approved=False).exists():
        exam_set.status = ExamSetStatus.APPROVED
        exam_set.save(update_fields=["status", "updated_at"])
        exam_set_published = True

    return JsonResponse({
        "status": "SUCCESS",
        "message": f"Đã duyệt {count} mã đề.",
        "approved_count": count,
        "exam_set_published": exam_set_published
    })


@login_required
@require_POST
def lecturer_regenerate_exam_code(request, exam_code_id: int):
    _ensure_lecturer(request)
    code = get_object_or_404(ExamCode, pk=exam_code_id, subject_id__in=_lecturer_subjects(request))
    if _subject_has_ongoing_exam_group(code.subject_id):
        return _locked_subject_mutation_response()

    if code.session_rooms.exists():
        return JsonResponse({"status": "FAIL", "message": "Mã đề đang sử dụng, không thể sinh lại."}, status=400)

    try:
        with transaction.atomic():
            _regenerate_single_code(code)
    except ValueError as e:
        return JsonResponse({"status": "FAIL", "message": str(e)}, status=400)

    return JsonResponse({
        "status": "SUCCESS",
        "message": "Sinh lại mã đề thành công.",
        "exam_code": _serialize_exam_code(code)
    })


@login_required
@require_POST
def lecturer_delete_exam_code(request, exam_code_id: int):
    _ensure_lecturer(request)
    code = get_object_or_404(ExamCode, pk=exam_code_id, subject_id__in=_lecturer_subjects(request))
    if code.is_linked:
        return JsonResponse(
            {
                "status": "FAIL",
                "message": "Mã đề đã được sử dụng trong ca thi nên không thể xóa.",
            },
            status=400,
        )

    if _subject_has_ongoing_exam_group(code.subject_id):
        return _locked_subject_mutation_response()

    exam_set = code.exam_set_id
    with transaction.atomic():
        for eq in code.exam_code_questions.all():
            _delete_exam_clone(eq.question_id)
        code.delete()
        _mark_exam_set_draft(exam_set)

    return JsonResponse({"status": "SUCCESS", "message": "Đã xóa mã đề thành công."})


@login_required
@require_POST
def lecturer_publish_exam_set(request, exam_set_id: int):
    _ensure_lecturer(request)
    exam_set = get_object_or_404(ExamSet, pk=exam_set_id, subject_id__in=_lecturer_subjects(request))
    if _subject_has_ongoing_exam_group(exam_set.subject_id):
        return _locked_subject_mutation_response()

    codes = exam_set.exam_codes.all()
    if not codes.exists():
        return JsonResponse({"status": "FAIL", "message": "Bộ đề không có mã đề nào."}, status=400)

    if codes.filter(is_approved=False).exists():
        return JsonResponse({"status": "FAIL", "message": "Vui lòng duyệt tất cả mã đề."}, status=400)

    try:
        _validate_exam_set_total_score(exam_set)
    except ValueError as exc:
        return JsonResponse({"status": "FAIL", "message": str(exc)}, status=400)

    exam_set.status = ExamSetStatus.APPROVED
    exam_set.save(update_fields=["status", "updated_at"])
    return JsonResponse(
        {"status": "SUCCESS", "message": "Lưu bộ đề thành công.", "redirect_url": "/lecturer/exam-codes/"})


@login_required
@require_POST
def lecturer_save_exam_set_draft(request, exam_set_id: int):
    _ensure_lecturer(request)
    exam_set = get_object_or_404(ExamSet, pk=exam_set_id, subject_id__in=_lecturer_subjects(request))
    if _subject_has_ongoing_exam_group(exam_set.subject_id):
        return _locked_subject_mutation_response()
    exam_set.save(update_fields=["updated_at"])
    return JsonResponse({"status": "SUCCESS", "message": "Đã lưu bản nháp."})


@login_required
@require_POST
def lecturer_delete_exam_set(request, exam_set_id: int):
    _ensure_lecturer(request)
    exam_set = get_object_or_404(
        ExamSet,
        pk=exam_set_id,
        subject_id__in=_lecturer_subjects(request),
    )
    if _subject_has_ongoing_exam_group(exam_set.subject_id):
        return _locked_subject_mutation_response()

    # CHỈ nhìn usage thực tế qua session_rooms
    has_used_codes = exam_set.exam_codes.filter(session_rooms__isnull=False).exists()

    if has_used_codes:
        return JsonResponse(
            {
                "status": "FAIL",
                "message": "Không thể xóa bộ đề này vì đã có mã đề được sử dụng.",
            },
            status=400,
        )

    with transaction.atomic():
        for code in exam_set.exam_codes.all():
            for eq in code.exam_code_questions.all():
                _delete_exam_clone(eq.question_id)
            code.delete()
        exam_set.delete()

    return JsonResponse(
        {
            "status": "SUCCESS",
            "message": "Xóa bộ đề thành công.",
        }
    )
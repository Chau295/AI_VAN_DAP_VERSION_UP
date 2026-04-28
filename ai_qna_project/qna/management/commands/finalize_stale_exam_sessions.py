# -*- coding: utf-8 -*-
from __future__ import annotations

from uuid import uuid4

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from qna.models import ExamResult, ExamSession
from qna.exam_result_helpers import (
    _compute_scores,
    _is_session_finalized,
    _student_has_started_exam,
)
from qna.exam_draft_audio import build_draft_audio_file, clear_draft_audio_files


ASR_TASK_CHANNEL = "asr-tasks"


def _exam_heartbeat_key(session_id: int) -> str:
    return f"exam_heartbeat:{session_id}"


def _exam_active_sessions_key() -> str:
    return "exam_active_sessions"


def _exam_active_question_key(session_id: int) -> str:
    return f"exam_active_question:{session_id}"


def _exam_recording_meta_key(session_id: int, question_id: int) -> str:
    return f"exam_recording_meta:{session_id}:{question_id}"


def _exam_draft_grading_queued_key(session_id: int, question_id: int) -> str:
    return f"exam_draft_grading_queued:{session_id}:{question_id}"


def _unmark_exam_session_active(session_id: int) -> None:
    try:
        active_ids = cache.get(_exam_active_sessions_key(), set())
        if not isinstance(active_ids, set):
            active_ids = set(active_ids or [])
        active_ids.discard(int(session_id))
        cache.set(_exam_active_sessions_key(), active_ids, timeout=24 * 60 * 60)
    except Exception:
        pass


def _finalize_started_exam_session_without_model_change(session: ExamSession | None) -> bool:
    if not session or not _student_has_started_exam(session):
        return False

    if not _is_session_finalized(session):
        update_fields = []

        if getattr(session, "session_status", None) != "COMPLETED":
            session.session_status = "COMPLETED"
            update_fields.append("session_status")

        if getattr(session, "completed_at", None) is None:
            session.completed_at = timezone.now()
            update_fields.append("completed_at")

        _, final_total = _compute_scores(session)
        final_total = round(float(final_total or 0), 2)

        if session.final_score != final_total:
            session.final_score = final_total
            update_fields.append("final_score")

        if update_fields:
            session.save(update_fields=update_fields)

    return True


def _get_active_question_id(session_id: int) -> int | None:
    question_id = cache.get(_exam_active_question_key(session_id))
    if not question_id:
        return None

    try:
        return int(question_id)
    except (TypeError, ValueError):
        return None


def _draft_result_exists(session: ExamSession, question_id: int) -> bool:
    return ExamResult.objects.filter(
        exam_session_id=session,
        question_id_id=question_id,
    ).exists()


def _enqueue_draft_audio_for_grading(
    *,
    session: ExamSession,
    question_id: int,
    audio_path: str,
    audio_mime_type: str,
) -> bool:
    """
    Đẩy audio draft vào lại ASR_TASK_CHANNEL để run_workers.py xử lý như WebSocket thật.
    Không tạo model mới, không migration.
    """
    channel_layer = get_channel_layer()
    if channel_layer is None:
        raise RuntimeError("Không lấy được channel_layer. Kiểm tra cấu hình CHANNEL_LAYERS.")

    with open(audio_path, "rb") as audio_file:
        audio_bytes = audio_file.read()

    if not audio_bytes:
        return False

    reply_channel = f"auto_finalize_{session.pk}_{question_id}_{uuid4().hex}"
    reply_group = f"exam_{session.pk}"

    common = {
        "session_id": str(session.pk),
        "question_id": str(question_id),
        "reply_channel": reply_channel,
        "reply_group": reply_group,
        "audio_mime_type": audio_mime_type or "audio/webm",
        "allow_after_expiry": True,
    }

    async_to_sync(channel_layer.send)(
        ASR_TASK_CHANNEL,
        {
            "type": "asr.stream.start",
            **common,
        },
    )

    async_to_sync(channel_layer.send)(
        ASR_TASK_CHANNEL,
        {
            "type": "asr.chunk",
            "audio_chunk": audio_bytes,
            **common,
        },
    )

    async_to_sync(channel_layer.send)(
        ASR_TASK_CHANNEL,
        {
            "type": "asr.stream.end",
            "mode": "main",
            **common,
        },
    )

    cache.set(
        _exam_draft_grading_queued_key(session.pk, question_id),
        timezone.now().timestamp(),
        timeout=10 * 60,
    )

    return True


def _try_enqueue_active_draft_answer(session: ExamSession) -> str:
    """
    Return:
    - "no_active_question"
    - "already_answered"
    - "no_draft_audio"
    - "already_queued"
    - "queued"
    """
    question_id = _get_active_question_id(session.pk)
    if not question_id:
        return "no_active_question"

    if _draft_result_exists(session, question_id):
        clear_draft_audio_files(session_id=session.pk, question_id=question_id)
        cache.delete(_exam_draft_grading_queued_key(session.pk, question_id))
        return "already_answered"

    queued_key = _exam_draft_grading_queued_key(session.pk, question_id)
    if cache.get(queued_key):
        return "already_queued"

    audio_path = build_draft_audio_file(
        session_id=session.pk,
        question_id=question_id,
    )

    if not audio_path:
        return "no_draft_audio"

    meta = cache.get(_exam_recording_meta_key(session.pk, question_id), {}) or {}
    audio_mime_type = meta.get("audio_mime_type") or "audio/webm"

    enqueued = _enqueue_draft_audio_for_grading(
        session=session,
        question_id=question_id,
        audio_path=audio_path,
        audio_mime_type=audio_mime_type,
    )

    return "queued" if enqueued else "no_draft_audio"


class Command(BaseCommand):
    help = "Tự động chấm draft audio và chốt các bài thi mất heartbeat."

    def add_arguments(self, parser):
        parser.add_argument(
            "--stale-seconds",
            type=int,
            default=45,
            help="Số giây không nhận heartbeat trước khi tự chốt bài.",
        )

    def handle(self, *args, **options):
        stale_seconds = int(options["stale_seconds"])
        now_ts = timezone.now().timestamp()

        active_ids = cache.get(_exam_active_sessions_key(), set())
        if not isinstance(active_ids, set):
            active_ids = set(active_ids or [])

        finalized_count = 0
        queued_draft_count = 0
        waiting_draft_count = 0
        missing_count = 0

        for session_id in list(active_ids):
            heartbeat_ts = cache.get(_exam_heartbeat_key(session_id))

            if heartbeat_ts is None:
                is_stale = True
            else:
                try:
                    is_stale = (now_ts - float(heartbeat_ts)) >= stale_seconds
                except (TypeError, ValueError):
                    is_stale = True

            if not is_stale:
                continue

            try:
                with transaction.atomic():
                    session = (
                        ExamSession.objects
                        .select_for_update()
                        .get(pk=session_id)
                    )

                    if _is_session_finalized(session):
                        _unmark_exam_session_active(session.pk)
                        cache.delete(_exam_heartbeat_key(session.pk))
                        cache.delete(_exam_active_question_key(session.pk))
                        continue

                    active_question_id = _get_active_question_id(session.pk)

                    if active_question_id:
                        draft_status = _try_enqueue_active_draft_answer(session)

                        if draft_status == "queued":
                            queued_draft_count += 1

                            # Quan trọng:
                            # Mới queue cho worker chấm, chưa finalize ngay.
                            # Lần command sau thấy ExamResult đã có thì mới finalize.
                            continue

                        if draft_status == "already_queued":
                            waiting_draft_count += 1

                            # Worker đang chấm hoặc đang chờ chấm.
                            # Không finalize vội để tránh mất câu đang ghi âm.
                            continue

                        if draft_status == "already_answered":
                            # Câu draft đã có kết quả, có thể finalize bên dưới.
                            pass

                        if draft_status == "no_draft_audio":
                            # Có active_question nhưng không có audio draft.
                            # Trường hợp này có thể finalize bài với các câu đã có.
                            pass

                    session.refresh_from_db()

                    finalized = _finalize_started_exam_session_without_model_change(session)
                    if finalized:
                        finalized_count += 1

                    _unmark_exam_session_active(session.pk)
                    cache.delete(_exam_heartbeat_key(session.pk))
                    cache.delete(_exam_active_question_key(session.pk))

            except ExamSession.DoesNotExist:
                missing_count += 1
                _unmark_exam_session_active(session_id)
                cache.delete(_exam_heartbeat_key(session_id))
                cache.delete(_exam_active_question_key(session_id))

        self.stdout.write(
            self.style.SUCCESS(
                f"Auto finalized {finalized_count} stale sessions. "
                f"Draft queued: {queued_draft_count}. "
                f"Waiting draft: {waiting_draft_count}. "
                f"Missing sessions: {missing_count}."
            )
        )
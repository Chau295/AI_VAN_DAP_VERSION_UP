# qna/management/commands/run_workers.py
import logging
import json
import os
import re
import asyncio
import subprocess
from time import perf_counter

from qna.services.ai_usage_service import record_ai_usage
from datetime import timedelta
from unicodedata import normalize
from uuid import uuid4
import wave
from array import array
from typing import Optional, Dict, Any

import fitz
import openai
from docx import Document

from django.core.management.base import BaseCommand
from django.conf import settings
from channels.layers import get_channel_layer
from channels.db import database_sync_to_async
from asgiref.sync import sync_to_async
from qna.exam_runtime import (
    EXAM_GRADING_STATUS_ERROR,
    EXAM_GRADING_STATUS_PROGRESS,
    EXAM_GRADING_STATUS_RESULT,
    is_exam_group_open,
    is_exam_group_submission_window_open,
    store_exam_grading_state,
)
from qna.models import (
    Question,
    ExamSession,
    ExamResult,
    Subject,
    LectureMaterial,
    resolve_uploaded_audio_extension,
)
from django.core.cache import cache
from django.utils import timezone
from django.db import IntegrityError, transaction

@database_sync_to_async
def _load_main_question_context(session_id, question_id, allow_after_expiry=False):
    session = (
        ExamSession.objects.select_related("exam_session_group_id")
        .prefetch_related("questions")
        .get(pk=session_id)
    )
    ordered_question_ids = [item.pk for item in session.questions.all()]
    if int(question_id) not in ordered_question_ids:
        raise ValueError("Question does not belong to this session.")

    question = Question.objects.only("question_text").get(pk=question_id)
    already_answered = ExamResult.objects.filter(
        exam_session_id_id=session_id,
        question_id_id=question_id,
    ).exists()
    exam_is_open = is_exam_group_open(session.exam_session_group_id)
    submission_window_open = is_exam_group_submission_window_open(
        session.exam_session_group_id,
        allow_post_end_grace=allow_after_expiry,
    )
    session_is_active = (
        session.session_status == "STARTED"
        and session.started_at is not None
        and session.completed_at is None
        and submission_window_open
    )
    return {
        "question_text": question.question_text,
        "ordered_question_ids": ordered_question_ids,
        "exam_is_open": exam_is_open,
        "submission_window_open": submission_window_open,
        "session_is_active": session_is_active,
        "already_answered": already_answered,
    }


@database_sync_to_async
def _create_main_exam_result(session_id, question_id, transcript, score, feedback):
    try:
        with transaction.atomic():
            exam_result = ExamResult.objects.create(
                exam_session_id_id=session_id,
                question_id_id=question_id,
                transcript=transcript,
                score=score,
                feedback=feedback,
            )
        return {"id": exam_result.id, "score": exam_result.score, "created": True}
    except IntegrityError:
        exam_result = ExamResult.objects.get(
            exam_session_id_id=session_id,
            question_id_id=question_id,
        )
        return {"id": exam_result.id, "score": exam_result.score, "created": False}

@database_sync_to_async
def _record_worker_ai_usage(
    *,
    group_id: str,
    feature: str,
    step_name: str,
    model_name: str,
    usage=None,
    session_id=None,
    question_id=None,
    latency_ms: int = 0,
    audio_duration_seconds: float = 0,
    success: bool = True,
    error_message: str = "",
    metadata: dict | None = None,
):
    session = None
    question = None

    if session_id:
        session = (
            ExamSession.objects
            .select_related("subject_id", "user_id")
            .filter(pk=session_id)
            .first()
        )

    if question_id:
        question = Question.objects.filter(pk=question_id).first()

    return record_ai_usage(
        group_id=group_id,
        feature=feature,
        step_name=step_name,
        model_name=model_name,
        usage=usage,
        user=session.user_id if session else None,
        subject=session.subject_id if session else None,
        exam_session=session,
        question=question,
        latency_ms=latency_ms,
        audio_duration_seconds=audio_duration_seconds,
        success=success,
        error_message=error_message,
        metadata=metadata or {},
    )

logger = logging.getLogger(__name__)

# ====== HẰNG SỐ / HELPERS ======
ASR_TASK_CHANNEL = "asr-tasks"
CHUNK_LOG_INTERVAL = 25
EBML_MAGIC = b"\x1A\x45\xDF\xA3"


def has_ebml_header(first_bytes: bytes) -> bool:
    return first_bytes.startswith(EBML_MAGIC)


def wav_duration_and_rms(path: str):
    try:
        with wave.open(path, 'rb') as w:
            fr, n, sw, ch = w.getframerate(), w.getnframes(), w.getsampwidth(), w.getnchannels()
            if fr <= 0 or n <= 0: return 0.0, 0.0
            duration = n / float(fr)
            raw = w.readframes(n)
        if sw != 2 or ch != 1: return duration, 0.0
        samples = array('h', raw)
        if not samples: return duration, 0.0
        acc = sum(float(s) * float(s) for s in samples)
        rms = (acc / len(samples)) ** 0.5
        return duration, rms
    except Exception as e:
        logger.error(f"Không thể phân tích WAV {path}: {e}")
        return 0.0, 0.0


def preprocess_text_vietnamese(text: str) -> str:
    text = text.lower()
    text = normalize('NFC', text)
    text = re.sub(r'[^\w\s]', '', text)
    return re.sub(r'\s+', ' ', text).strip()


async def rephrase_text_with_chatgpt(
    text,
    question_text,
    client,
    *,
    usage_group_id: str = "",
    session_id=None,
    question_id=None,
):
    if not text or client is None:
        return text

    # Prompt cải tiến với ràng buộc chặt chẽ hơn
    system_prompt = """Bạn là một trợ lý sửa lỗi văn bản tiếng Việt.

Nguyên tắc quan trọng:
1. CHỈ sửa lỗi chính tả, lỗi đánh máy, lỗi nhận dạng giọng nói
2. KHÔNG ĐƯỢC thêm từ mới nào
3. KHÔNG ĐƯỢC xóa từ nào, trừ từ lặp thừa
4. KHÔNG ĐƯỢC thay đổi cấu trúc câu
5. KHÔNG ĐƯỢC thay đổi ý nghĩa của câu
6. KHÔNG ĐƯỢC diễn giải lại nội dung

Ví dụ:
- "chủng năng" → "chức năng" (đúng)
- "tôi là sinh viên" → "tôi là một sinh viên" (SAI - đã thêm từ)
- "tôi nói nói về" → "tôi nói về" (đúng - xóa lặp)
- "data" → "dữ liệu" (SAI - đã thay đổi từ)

Chỉ trả về văn bản đã sửa, KHÔNG thêm giải thích."""

    user_prompt = f"""Câu trả lời sau được nhận dạng từ giọng nói, cần sửa lỗi:

--- Câu hỏi ---
{question_text}

--- Câu trả lời gốc ---
{text}

--- Yêu cầu ---
Chỉ sửa lỗi chính tả/đánh máy. Giữ nguyên mọi thứ khác.
"""

    try:
        started_at = perf_counter()

        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
        )

        latency_ms = int((perf_counter() - started_at) * 1000)

        if usage_group_id:
            await _record_worker_ai_usage(
                group_id=usage_group_id,
                feature="ANSWER_REPHRASE",
                step_name=f"rephrase_question_{question_id}",
                model_name="gpt-4o-mini",
                usage=resp.usage,
                session_id=session_id,
                question_id=question_id,
                latency_ms=latency_ms,
                metadata={
                    "input_length": len(text or ""),
                    "question_length": len(question_text or ""),
                },
            )
        rephrased = resp.choices[0].message.content.strip()

        # Validation: Kiểm tra độ tương đồng
        if text and rephrased and text != rephrased:
            # Tính độ tương đồng đơn giản dựa trên độ dài
            len_original = len(text.split())
            len_rephrased = len(rephrased.split())
            ratio = len_rephrased / len_original if len_original > 0 else 1

            # Nếu độ dài thay đổi quá nhiều (> 30%), dùng bản gốc
            if ratio < 0.7 or ratio > 1.3:
                logger.warning(
                    "Rephrasing thay đổi độ dài quá nhiều: %s -> %s (ratio=%.2f). Sử dụng bản gốc.",
                    len_original,
                    len_rephrased,
                    ratio,
                )
                return text

        return rephrased
    except Exception as e:
        logger.error("Lỗi rephrase OpenAI: %s", e, exc_info=True)
        return text


async def score_student_answer_with_openai(
    student_answer_raw,
    question_text,
    openai_client,
    model_name="gpt-4o-mini",
    *,
    usage_group_id: str = "",
    session_id=None,
    question_id=None,
):
    if openai_client is None: return 0.0, "OpenAI client chưa được khởi tạo."

    max_score = 10.0

    system_prompt = (
        "Bạn là một chuyên gia chấm điểm câu trả lời vấn đáp về khoa học dữ liệu.\n\n"
        "Nguyên tắc chấm điểm:\n"
        f"1. Chấm điểm trực tiếp theo chất lượng nội dung. Điểm tối đa: {max_score:.2f}\n"
        "2. Đánh giá độ chính xác, độ đầy đủ, mức độ giải thích và sự liên quan với câu hỏi.\n"
        "3. ẢO GIÁC DETECTION: Nếu câu trả lời có vẻ được AI tạo ra (ví dụ: quá hoàn hảo, "
        "cấu trúc không tự nhiên, từ vựng không phù hợp với sinh viên), giảm 30-50% điểm.\n"
        "4. Câu trả lời quá ngắn hoặc không liên quan: 0 điểm.\n"
        "5. Câu trả lời chỉ đọc lại câu hỏi: 0 điểm.\n"
        "6. Phải có nội dung thực sự từ sinh viên mới có điểm.\n\n"
        "Đầu ra bắt buộc: JSON format:\n"
        '{"diem_so": float, "phan_hoi": "string"}\n'
        "KHÔNG thêm văn bản nào khác ngoài JSON."
    )

    user_prompt = (
        f"--- Câu hỏi ---\n{question_text}\n\n"
        f"--- Câu trả lời sinh viên (đã transcribe từ giọng nói) ---\n{student_answer_raw}\n\n"
        f"--- Chấm điểm ---\n"
        f"1. Đánh giá: Câu trả lời có được sinh viên thực sự nói không?\n"
        f"2. Chấm điểm dựa trên độ đúng, độ đầy đủ và chiều sâu giải thích.\n"
        f"3. Nếu có ảo giác AI, ghi rõ trong phản hồi và giảm điểm.\n\n"
        f"Trả về JSON duy nhất, KHÔNG thêm chữ nào khác."
    )

    try:
        started_at = perf_counter()

        resp = await asyncio.to_thread(
            openai_client.chat.completions.create,
            model=model_name,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
        )

        latency_ms = int((perf_counter() - started_at) * 1000)

        if usage_group_id:
            await _record_worker_ai_usage(
                group_id=usage_group_id,
                feature="EXAM_GRADING",
                step_name=f"grade_question_{question_id}",
                model_name=model_name,
                usage=resp.usage,
                session_id=session_id,
                question_id=question_id,
                latency_ms=latency_ms,
                metadata={
                    "answer_length": len(student_answer_raw or ""),
                    "question_length": len(question_text or ""),
                },
            )
        data = json.loads(resp.choices[0].message.content)
        score = min(max(float(data.get("diem_so", 0.0)), 0.0), max_score)
        feedback = data.get("phan_hoi", "Không có phản hồi.")
        return score, feedback
    except Exception as e:
        logger.error("Lỗi khi chấm điểm OpenAI: %s", e, exc_info=True)
        return 0.0, f"Lỗi hệ thống chấm điểm AI: {e}"


def convert_audio_to_wav(source_path: str) -> Optional[str]:
    wav_path = os.path.splitext(source_path)[0] + ".wav"
    try:
        ffmpeg_binary = getattr(settings, "FFMPEG_BINARY", "ffmpeg")
        command = [ffmpeg_binary, "-hide_banner", "-loglevel", "warning", "-nostdin", "-fflags", "+genpts",
                   "-i", source_path, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", wav_path]
        subprocess.run(command, check=True, capture_output=True, text=True)
        logger.info(f"Đã chuyển đổi thành công {source_path} sang {wav_path}")
        return wav_path
    except subprocess.CalledProcessError as e:
        logger.error(f"Lỗi khi chạy ffmpeg: {e.stderr}")
        return None
    except FileNotFoundError:
        logger.error(f"Lỗi: không tìm thấy ffmpeg tại '{ffmpeg_binary}'.")
        return None


def extract_material_text(material) -> str:
    path = material.file_path or ""
    lower = path.lower()

    try:
        if lower.endswith(".txt"):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()

        if lower.endswith(".docx"):
            doc = Document(path)
            return "\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())

        if lower.endswith(".pdf"):
            pdf_document = fitz.open(path)
            texts = []
            for page_num in range(pdf_document.page_count):
                page = pdf_document.load_page(page_num)
                page_text = page.get_text("text") or ""
                if page_text.strip():
                    texts.append(page_text.strip())
            return "\n".join(texts)

    except Exception as e:
        logger.warning("Không thể đọc file %s: %s", path, e)

    return ""


# ====== WORKER CLASS ======
class Command(BaseCommand):
    help = 'Chạy worker lắng nghe các tác vụ AI từ channel layer'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.channel_layer = get_channel_layer()
        self.audio_chunks = {}
        self.audio_stream_meta = {}

        self.stdout.write("Đang cấu hình OpenAI client...")
        try:
            self.openai_client = openai.OpenAI()
            self.openai_client.models.list()
            self.stdout.write("✅ OpenAI (ChatGPT & Whisper) đã sẵn sàng.")
        except Exception as e:
            self.openai_client = None
            self.stderr.write(f"Lỗi cấu hình OpenAI: {e} - đặt OPENAI_API_KEY trước.")


    async def _persist_grading_state(
        self,
        session_id,
        question_id,
        *,
        status,
        progress=None,
        result=None,
        error_message="",
    ):
        if not session_id or not question_id:
            return None
        return await sync_to_async(store_exam_grading_state)(
            session_id,
            question_id,
            status=status,
            progress=progress,
            result=result,
            error_message=error_message,
        )

    async def _emit_exam_event(self, *, reply_channel=None, reply_group=None, event_type, message):
        event = {"type": event_type, "message": message}
        try:
            if reply_group:
                await self.channel_layer.group_send(reply_group, event)
                return True
            if reply_channel:
                await self.channel_layer.send(reply_channel, event)
                return True
        except Exception:
            logger.exception(
                "[EVENT] Không thể gửi %s: reply_group=%s reply_channel=%s",
                event_type,
                reply_group,
                reply_channel,
            )
        return False

    async def _send_exam_progress(
        self,
        reply_channel,
        reply_group=None,
        *,
        stage,
        session_id=None,
        question_id=None,
        detail="",
        transcript=None,
        rephrased=None,
        score=None,
        result_id=None,
    ):
        if not reply_channel:
            return

        payload = {
            "stage": stage,
            "session_id": session_id,
            "question_id": question_id,
            "detail": detail or "",
        }
        if transcript is not None:
            payload["transcript"] = transcript
        if rephrased is not None:
            payload["rephrased"] = rephrased
        if score is not None:
            payload["score"] = score
        if result_id is not None:
            payload["result_id"] = result_id

        await self._persist_grading_state(
            session_id,
            question_id,
            status=EXAM_GRADING_STATUS_PROGRESS,
            progress=payload,
        )

        logger.info(
            "[PROGRESS] session_id=%s question_id=%s stage=%s detail=%s",
            session_id,
            question_id,
            stage,
            detail,
        )

        await self._emit_exam_event(
            reply_channel=reply_channel,
            reply_group=reply_group,
            event_type="exam.progress",
            message=payload,
        )

    async def _send_exam_error(
        self,
        reply_channel,
        reply_group=None,
        *,
        session_id=None,
        question_id=None,
        message="",
    ):
        error_message = str(message or "Có lỗi xảy ra trong quá trình chấm điểm.")
        await self._persist_grading_state(
            session_id,
            question_id,
            status=EXAM_GRADING_STATUS_ERROR,
            error_message=error_message,
        )
        await self._emit_exam_event(
            reply_channel=reply_channel,
            reply_group=reply_group,
            event_type="exam.error",
            message=error_message,
        )

    async def _send_exam_result(
        self,
        reply_channel,
        reply_group=None,
        *,
        session_id=None,
        question_id=None,
        payload=None,
    ):
        message = payload or {}
        await self._persist_grading_state(
            session_id,
            question_id,
            status=EXAM_GRADING_STATUS_RESULT,
            result=message,
        )
        await self._emit_exam_event(
            reply_channel=reply_channel,
            reply_group=reply_group,
            event_type="exam.result",
            message=message,
        )

    async def process_audio_and_transcribe(
            self,
            reply_channel,
            reply_group,
            chunks,
            whisper_prompt: Optional[str] = None,
            session_id=None,
            question_id=None,
            audio_mime_type: Optional[str] = None,
            usage_group_id: str = "",
    ):
        logger.info(
            "[ASR] Bắt đầu xử lý audio: session_id=%s question_id=%s reply_channel=%s chunks_count=%s",
            session_id,
            question_id,
            reply_channel,
            len(chunks),
        )
        if not chunks:
            logger.warning(
                "[ASR] Bỏ qua vì không có chunk audio: session_id=%s question_id=%s reply_channel=%s",
                session_id,
                question_id,
                reply_channel,
            )
            await self._send_exam_error(
                reply_channel,
                reply_group,
                session_id=session_id,
                question_id=question_id,
                message="Không nhận được dữ liệu âm thanh để chấm điểm.",
            )
            return None
        await self._send_exam_progress(
            reply_channel,
            reply_group,
            stage="received_audio",
            session_id=session_id,
            question_id=question_id,
            detail="Đã nhận âm thanh từ trình duyệt.",
        )
        source_extension = resolve_uploaded_audio_extension("audio", audio_mime_type or "")
        first = chunks[0]
        if source_extension == ".webm" and (len(first) < 4 or not has_ebml_header(first[:4])):
            logger.error(
                "[ASR] Lỗi bước validate_ebml_header: session_id=%s question_id=%s reply_channel=%s chunks_count=%s",
                session_id,
                question_id,
                reply_channel,
                len(chunks),
            )
            await self._send_exam_error(
                reply_channel,
                reply_group,
                session_id=session_id,
                question_id=question_id,
                message='Dữ liệu audio không hợp lệ (thiếu header).',
            )
            return None
        unique_id = re.sub(r'[^a-zA-Z0-9]', '_', reply_channel)
        source_path = os.path.join(settings.BASE_DIR, f'temp_audio_{unique_id}_{uuid4().hex[:8]}{source_extension}')
        logger.info(
            "[ASR] Đang ghi file audio tạm: session_id=%s question_id=%s reply_channel=%s path=%s audio_mime_type=%s",
            session_id,
            question_id,
            reply_channel,
            source_path,
            audio_mime_type,
        )
        try:
            with open(source_path, 'wb') as f:
                for c in chunks: f.write(c)
        except Exception as e:
            logger.error(
                "[ASR] Lỗi bước write_temp_audio: session_id=%s question_id=%s reply_channel=%s path=%s error=%s",
                session_id,
                question_id,
                reply_channel,
                source_path,
                e,
                exc_info=True,
            )
            await self._send_exam_error(
                reply_channel,
                reply_group,
                session_id=session_id,
                question_id=question_id,
                message='Lỗi hệ thống khi ghi file âm thanh.',
            )
            return None
        logger.info(
            "[ASR] Đang convert audio sang WAV: session_id=%s question_id=%s reply_channel=%s source_path=%s",
            session_id,
            question_id,
            reply_channel,
            source_path,
        )
        await self._send_exam_progress(
            reply_channel,
            reply_group,
            stage="converting_audio",
            session_id=session_id,
            question_id=question_id,
            detail="Đang chuyển audio sang WAV.",
        )
        wav_path = await asyncio.to_thread(convert_audio_to_wav, source_path)
        if os.path.exists(source_path): os.remove(source_path)
        if not wav_path:
            logger.error(
                "[ASR] Lỗi bước convert_audio_to_wav: session_id=%s question_id=%s reply_channel=%s source_path=%s",
                session_id,
                question_id,
                reply_channel,
                source_path,
            )
            await self._send_exam_error(
                reply_channel,
                reply_group,
                session_id=session_id,
                question_id=question_id,
                message='Lỗi xử lý file âm thanh.',
            )
            return None
        logger.info(
            "[ASR] Đã convert xong WAV: session_id=%s question_id=%s reply_channel=%s wav_path=%s",
            session_id,
            question_id,
            reply_channel,
            wav_path,
        )
        duration, rms = wav_duration_and_rms(wav_path)
        logger.info(
            "[ASR] WAV metrics: session_id=%s question_id=%s duration=%.2fs rms=%.1f",
            session_id,
            question_id,
            duration,
            rms,
        )
        await self._send_exam_progress(
            reply_channel,
            reply_group,
            stage="wav_ready",
            session_id=session_id,
            question_id=question_id,
            detail=f"Đã tạo WAV thành công ({duration:.2f}s, RMS={rms:.1f}).",
        )

        # Kiểm tra RMS để phát hiện silence, tức là không có âm thanh thực.
        # Ngưỡng RMS < 50.0 thường là silence hoặc nhiễu nền.
        RMS_THRESHOLD = 50.0
        if rms < RMS_THRESHOLD:
            logger.info(
                "[ASR] Phát hiện silence: session_id=%s question_id=%s rms=%.1f threshold=%.1f",
                session_id,
                question_id,
                rms,
                RMS_THRESHOLD,
            )
            await self._send_exam_error(
                reply_channel,
                reply_group,
                session_id=session_id,
                question_id=question_id,
                message='Không có âm thanh được phát hiện.',
            )
            return None

        # Removed duration check - users can now submit audio of any length
        try:
            # Sử dụng prompt context để giúp Whisper hiểu bối cảnh tốt hơn.
            context_prompt = whisper_prompt or ""
            if context_prompt:
                context_prompt = f"Bối cảnh: {context_prompt}. "

            logger.info(
                "[ASR] Đang gọi Whisper: session_id=%s question_id=%s reply_channel=%s prompt_present=%s",
                session_id,
                question_id,
                reply_channel,
                bool(whisper_prompt),
            )
            await self._send_exam_progress(
                reply_channel,
                reply_group,
                stage="transcribing",
                session_id=session_id,
                question_id=question_id,
                detail="Đang gọi Whisper để nhận diện giọng nói.",
            )
            started_at = perf_counter()

            with open(wav_path, "rb") as audio_file:
                transcription = await asyncio.to_thread(
                    self.openai_client.audio.transcriptions.create,
                    model="whisper-1",
                    file=audio_file,
                    language="vi",
                    temperature=0,
                    prompt=context_prompt,
                )

            latency_ms = int((perf_counter() - started_at) * 1000)

            if usage_group_id:
                await _record_worker_ai_usage(
                    group_id=usage_group_id,
                    feature="SPEECH_TO_TEXT",
                    step_name=f"transcribe_question_{question_id}",
                    model_name="whisper-1",
                    usage=getattr(transcription, "usage", None),
                    session_id=session_id,
                    question_id=question_id,
                    latency_ms=latency_ms,
                    audio_duration_seconds=duration,
                    metadata={
                        "audio_mime_type": audio_mime_type,
                        "rms": rms,
                        "duration_seconds": duration,
                    },
                )

            raw = transcription.text.strip()
            logger.info(
                "[ASR] Whisper trả transcript: session_id=%s question_id=%s transcript=%r",
                session_id,
                question_id,
                raw,
            )
            # Validation thêm
            if raw:
                await self._send_exam_progress(
                    reply_channel,
                    reply_group,
                    stage="transcript_ready",
                    session_id=session_id,
                    question_id=question_id,
                    detail="Đã nhận transcript từ Whisper.",
                    transcript=raw,
                )
                # Kiểm tra nếu transcript quá ngắn.
                words = raw.split()
                if len(words) < 3 and len(chunks) > 0:
                    logger.warning(
                        "[ASR] Transcript quá ngắn: session_id=%s question_id=%s words=%s dù có dữ liệu audio.",
                        session_id,
                        question_id,
                        len(words),
                    )

            # Nếu Whisper trả về chuỗi rỗng, coi như không có âm thanh.
            if not raw:
                logger.info(
                    "[ASR] Whisper trả chuỗi rỗng: session_id=%s question_id=%s",
                    session_id,
                    question_id,
                )
                await self._send_exam_error(
                    reply_channel,
                    reply_group,
                    session_id=session_id,
                    question_id=question_id,
                    message='Không có âm thanh được phát hiện.',
                )
                return None

            return raw
        except Exception as e:
            logger.error(
                "[ASR] Lỗi bước whisper_transcribe: session_id=%s question_id=%s reply_channel=%s error=%s",
                session_id,
                question_id,
                reply_channel,
                e,
                exc_info=True,
            )
            await self._send_exam_error(
                reply_channel,
                reply_group,
                session_id=session_id,
                question_id=question_id,
                message='Không thể nhận diện giọng nói từ file âm thanh.',
            )
            return None
        finally:
            if os.path.exists(wav_path): os.remove(wav_path)

    async def process_main_question(self, message):
        reply_channel = message['reply_channel']
        reply_group = message.get('reply_group')
        question_id = message.get('question_id')
        session_id = message.get('session_id')
        usage_group_id = f"exam_attempt_{session_id}"
        chunks = message.get('__chunks', [])
        logger.info(
            "[MAIN] Bắt đầu process_main_question: session_id=%s question_id=%s reply_channel=%s chunks_count=%s",
            session_id,
            question_id,
            reply_channel,
            len(chunks),
        )
        if not all([session_id, question_id, chunks]):
            logger.error(
                "[MAIN] Thiếu dữ liệu đầu vào: session_id=%s question_id=%s reply_channel=%s chunks_count=%s",
                session_id,
                question_id,
                reply_channel,
                len(chunks),
            )
            await self._send_exam_error(
                reply_channel,
                reply_group,
                session_id=session_id,
                question_id=question_id,
                message='Thiếu dữ liệu audio hoặc thông tin câu hỏi để chấm điểm.',
            )
            return
        current_step = "load_question_context"
        try:
            question_context = await _load_main_question_context(
                session_id,
                question_id,
                allow_after_expiry=bool(message.get('allow_after_expiry')),
            )
            logger.info(
                "[MAIN] Đã load context: session_id=%s question_id=%s exam_is_open=%s submission_window_open=%s session_is_active=%s already_answered=%s",
                session_id,
                question_id,
                question_context["exam_is_open"],
                question_context["submission_window_open"],
                question_context["session_is_active"],
                question_context["already_answered"],
            )

            current_step = "parse_question_id"
            question_id_int = int(question_id)

            if not question_context["exam_is_open"]:
                logger.warning(
                    "[MAIN] exam_is_open=False: session_id=%s question_id=%s",
                    session_id,
                    question_id_int,
                )

            if not question_context["session_is_active"]:
                logger.warning(
                    "[MAIN] Phiên thi không active: session_id=%s question_id=%s exam_is_open=%s submission_window_open=%s",
                    session_id,
                    question_id_int,
                    question_context["exam_is_open"],
                    question_context["submission_window_open"],
                )
                await self._send_exam_error(
                    reply_channel,
                    reply_group,
                    session_id=session_id,
                    question_id=question_id_int,
                    message='Phiên thi không còn hợp lệ để nộp câu trả lời.',
                )
                return

            if not question_context["submission_window_open"]:
                logger.warning(
                    "[MAIN] exam_is_open=False hoặc submission window đã đóng: session_id=%s question_id=%s",
                    session_id,
                    question_id_int,
                )
                await self._send_exam_error(
                    reply_channel,
                    reply_group,
                    session_id=session_id,
                    question_id=question_id_int,
                    message='Thời gian thi đã kết thúc. Không thể gửi câu trả lời.',
                )
                return

            if question_context["already_answered"]:
                logger.warning(
                    "[MAIN] Câu hỏi đã nộp trước đó: session_id=%s question_id=%s",
                    session_id,
                    question_id_int,
                )
                await self._send_exam_error(
                    reply_channel,
                    reply_group,
                    session_id=session_id,
                    question_id=question_id_int,
                    message='Câu hỏi này đã được nộp trước đó.',
                )
                return

            ordered_question_ids = question_context["ordered_question_ids"]
            question_order = next((index + 1 for index, item_id in enumerate(ordered_question_ids) if item_id == question_id_int), None)
            question_label = f"Cau {question_order}" if question_order else f"Q{question_id}"
            logger.info(
                "[MAIN] Bắt đầu bước process_audio_and_transcribe: session_id=%s question_id=%s chunks_count=%s",
                session_id,
                question_id_int,
                len(chunks),
            )
            current_step = "process_audio_and_transcribe"
            raw_transcript = await self.process_audio_and_transcribe(
                reply_channel,
                reply_group,
                chunks,
                whisper_prompt=question_context["question_text"],
                session_id=session_id,
                question_id=question_id_int,
                audio_mime_type=message.get('audio_mime_type'),
                usage_group_id=usage_group_id,
            )
            logger.info(
                "[MAIN] Kết thúc bước process_audio_and_transcribe: session_id=%s question_id=%s transcript_status=%s",
                session_id,
                question_id_int,
                "empty_or_error" if raw_transcript == "" else ("none" if raw_transcript is None else "ok"),
            )
            if raw_transcript is None or raw_transcript == "":
                logger.warning(
                    "[MAIN] Dừng xử lý sau bước process_audio_and_transcribe: session_id=%s question_id=%s",
                    session_id,
                    question_id_int,
                )
                return
            logger.info(
                "[MAIN] Bắt đầu bước rephrase_text_with_chatgpt: session_id=%s question_id=%s",
                session_id,
                question_id_int,
            )
            current_step = "rephrase_text_with_chatgpt"
            await self._send_exam_progress(
                reply_channel,
                reply_group,
                stage="rephrasing",
                session_id=session_id,
                question_id=question_id_int,
                detail="Đang sửa lỗi và chuẩn hóa câu trả lời.",
                transcript=raw_transcript,
            )
            rephrased = await rephrase_text_with_chatgpt(
                raw_transcript,
                question_context["question_text"],
                self.openai_client,
                usage_group_id=usage_group_id,
                session_id=session_id,
                question_id=question_id_int,
            )
            if rephrased == raw_transcript:
                logger.info(
                    "[MAIN] Kết thúc bước rephrase_text_with_chatgpt: session_id=%s question_id=%s action=skip_or_no_change transcript=%r",
                    session_id,
                    question_id_int,
                    rephrased,
                )
            else:
                logger.info(
                    "[MAIN] Kết thúc bước rephrase_text_with_chatgpt: session_id=%s question_id=%s action=rephrased transcript=%r",
                    session_id,
                    question_id_int,
                    rephrased,
                )
            await self._send_exam_progress(
                reply_channel,
                reply_group,
                stage="rephrase_ready",
                session_id=session_id,
                question_id=question_id_int,
                detail="Đã chuẩn hóa câu trả lời.",
                transcript=raw_transcript,
                rephrased=rephrased,
            )
            logger.info(
                "[MAIN] Bắt đầu bước score_student_answer_with_openai: session_id=%s question_id=%s",
                session_id,
                question_id_int,
            )
            current_step = "score_student_answer_with_openai"
            await self._send_exam_progress(
                reply_channel,
                reply_group,
                stage="scoring",
                session_id=session_id,
                question_id=question_id_int,
                detail="Đang chấm điểm câu trả lời.",
                transcript=raw_transcript,
                rephrased=rephrased,
            )
            final_score, feedback = await score_student_answer_with_openai(
                rephrased,
                question_context["question_text"],
                self.openai_client,
                usage_group_id=usage_group_id,
                session_id=session_id,
                question_id=question_id_int,
            )
            final_score = float(min(max(final_score, 0.0), 10.0))
            logger.info(
                "[MAIN] Kết thúc bước score_student_answer_with_openai: session_id=%s question_id=%s label=%s final_score=%.2f",
                session_id,
                question_id_int,
                question_label,
                final_score,
            )
            await self._send_exam_progress(
                reply_channel,
                reply_group,
                stage="score_ready",
                session_id=session_id,
                question_id=question_id_int,
                detail=f"Điểm hiện tại: {final_score:.2f}/10",
                transcript=raw_transcript,
                rephrased=rephrased,
                score=final_score,
            )
            logger.info(
                "[MAIN] Bắt đầu bước _create_main_exam_result: session_id=%s question_id=%s final_score=%.2f",
                session_id,
                question_id_int,
                final_score,
            )
            current_step = "_create_main_exam_result"
            exam_result = await _create_main_exam_result(
                session_id=session_id,
                question_id=question_id_int,
                transcript=rephrased,
                score=final_score,
                feedback=feedback,
            )
            logger.info(
                "[MAIN] Kết thúc bước _create_main_exam_result: session_id=%s question_id=%s result_id=%s created=%s",
                session_id,
                question_id_int,
                exam_result["id"],
                exam_result["created"],
            )
            if not exam_result["created"]:
                logger.warning(
                    "[MAIN] ExamResult đã tồn tại, không tạo mới: session_id=%s question_id=%s result_id=%s",
                    session_id,
                    question_id_int,
                    exam_result["id"],
                )
                await self._send_exam_error(
                    reply_channel,
                    reply_group,
                    session_id=session_id,
                    question_id=question_id_int,
                    message='Câu hỏi này đã được nộp trước đó.',
                )
                return
            await self._send_exam_progress(
                reply_channel,
                reply_group,
                stage="result_saved",
                session_id=session_id,
                question_id=question_id_int,
                detail=f"Đã lưu ExamResult #{exam_result['id']}.",
                transcript=raw_transcript,
                rephrased=rephrased,
                score=float(exam_result['score']),
                result_id=exam_result['id'],
            )
            result_payload = {
                'type': 'main_question_complete',
                'data': {
                    'result_id': exam_result['id'],
                    'score': exam_result['score'],
                    'question_id': question_id_int,
                    'transcript': rephrased,
                    'feedback': feedback,
                },
            }
            logger.info(
                "[MAIN] Bắt đầu bước channel_layer.send exam.result: session_id=%s question_id=%s result_id=%s reply_channel=%s",
                session_id,
                question_id_int,
                exam_result['id'],
                reply_channel,
            )
            current_step = "channel_layer.send exam.result"
            await self._send_exam_result(
                reply_channel,
                reply_group,
                session_id=session_id,
                question_id=question_id_int,
                payload=result_payload,
            )
            logger.info(
                "[MAIN] Đã gửi exam.result: session_id=%s question_id=%s result_id=%s reply_channel=%s",
                session_id,
                question_id_int,
                exam_result['id'],
                reply_channel,
            )
        except Exception as e:
            logger.error(
                "[MAIN] Lỗi tại bước %s: session_id=%s question_id=%s reply_channel=%s error=%s",
                current_step,
                session_id,
                question_id,
                reply_channel,
                e,
                exc_info=True,
            )
            await self._send_exam_error(
                reply_channel,
                reply_group,
                session_id=session_id,
                question_id=question_id,
                message=f'Lỗi worker: {str(e)}',
            )

    async def process_generate_questions(self, message):
        job_id = message["job_id"]
        subject_id = message["subject_id"]
        document_ids = message["document_ids"]
        total_count = message["total_count"]
        level_config = message["level_config"]

        cache_key = f"qna_question_job_{job_id}"

        try:
            cache.set(cache_key, {
                "status": "PROCESSING",
                "progress": 20,
                "questions": [],
                "summary": {},
                "error_message": "",
            }, timeout=1800)

            subject = await sync_to_async(Subject.objects.get)(id=subject_id)
            materials = await sync_to_async(list)(
                LectureMaterial.objects.filter(subject=subject, id__in=document_ids)
            )

            doc_text_parts = []
            for m in materials:
                text = await sync_to_async(extract_material_text)(m)
                doc_text_parts.append(f"--- Nguồn: {m.title} ---\n{text[:5000]}")

            doc_text = "\n\n".join(doc_text_parts)

            easy = int(level_config.get("easy", 0))
            medium = int(level_config.get("medium", 0))
            hard = int(level_config.get("hard", 0))

            system_prompt = (
                "Bạn là một chuyên gia học thuật ra đề thi vấn đáp đại học.\n"
                "Nhiệm vụ: Dựa vào nội dung tài liệu được cung cấp, tạo câu hỏi đúng với số lượng và mức độ được yêu cầu.\n"
                "Đầu ra bắt buộc: JSON object theo format "
                '{"questions": [{"content": "...", "difficulty": "EASY|MEDIUM|HARD", "source": "tên tài liệu"}]}'
            )

            user_prompt = f"""
                Tạo tổng cộng {total_count} câu hỏi:
                - EASY: {easy}
                - MEDIUM: {medium}
                - HARD: {hard}

                Dựa trên tài liệu sau:
                {doc_text}
                """

            if self.openai_client is None:
                raise RuntimeError("OpenAI client chưa được khởi tạo.")

            resp = await asyncio.to_thread(
                self.openai_client.chat.completions.create,
                model="gpt-4o-mini",
                response_format={"type": "json_object"},
                temperature=0.7,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )

            data = json.loads(resp.choices[0].message.content)
            questions_data = data.get("questions", [])

            formatted_questions = []
            for q in questions_data:
                diff = str(q.get("difficulty", "MEDIUM")).upper()
                if diff not in ["EASY", "MEDIUM", "HARD"]:
                    diff = "MEDIUM"

                content = (q.get("content") or "").strip()

                # ==== Bắt buộc lưu vào database với tiền tố DRAFT_ ====
                new_q = await sync_to_async(Question.objects.create)(
                    subject=subject,
                    question_text=content,
                    difficulty=diff,
                    question_id_in_barem=f"DRAFT_AI_{uuid4().hex[:8]}"  # Lưu dưới dạng nháp
                )

                formatted_questions.append({
                    "id": new_q.id,
                    "content": content,
                    "difficulty": diff,
                    "source": q.get("source", subject.name),
                    "created_at": timezone.now().strftime("%d/%m/%Y"),
                })

            summary = {
                "all": len(formatted_questions),
                "easy": len([q for q in formatted_questions if q["difficulty"] == "EASY"]),
                "medium": len([q for q in formatted_questions if q["difficulty"] == "MEDIUM"]),
                "hard": len([q for q in formatted_questions if q["difficulty"] == "HARD"]),
            }

            cache.set(cache_key, {
                "status": "COMPLETE",
                "progress": 100,
                "questions": formatted_questions,
                "summary": summary,
                "error_message": "",
            }, timeout=1800)

            logger.info(f"Đã sinh xong {len(formatted_questions)} câu hỏi cho job_id={job_id}")

        except Exception as exc:
            logger.error(f"Lỗi khi sinh câu hỏi (job {job_id}): {exc}", exc_info=True)
            cache.set(cache_key, {
                "status": "FAIL",
                "progress": 100,
                "questions": [],
                "summary": {},
                "error_message": str(exc),
            }, timeout=1800)

    async def run(self):
        logger.info("Worker đang lắng nghe trên kênh '%s'.", ASR_TASK_CHANNEL)
        while True:
            message = await self.channel_layer.receive(ASR_TASK_CHANNEL)
            task_type = message.get('type')
            reply_channel = message.get('reply_channel')
            reply_group = message.get('reply_group')
            session_id = message.get('session_id')
            question_id = message.get('question_id')

            logger.info(
                "[RUN] Đã nhận message: task_type=%s reply_channel=%s reply_group=%s session_id=%s question_id=%s",
                task_type,
                reply_channel,
                reply_group,
                session_id,
                question_id,
            )

            if task_type == 'ai.generate_questions':
                logger.info("[RUN] Nhận lệnh sinh câu hỏi AI: job_id=%s", message.get('job_id'))
                asyncio.create_task(self.process_generate_questions(message))
                continue

            if not reply_channel and not reply_group:
                logger.warning(
                    "[RUN] Bỏ qua message không có target reply: task_type=%s session_id=%s question_id=%s",
                    task_type,
                    session_id,
                    question_id,
                )
                continue

            if task_type == 'asr.stream.start':
                logger.info(
                    "[RUN] START stream: reply_channel=%s session_id=%s question_id=%s",
                    reply_channel,
                    session_id,
                    question_id,
                )
                self.audio_chunks[reply_channel] = []
                self.audio_stream_meta[reply_channel] = {
                    'session_id': session_id,
                    'question_id': question_id,
                    'audio_mime_type': message.get('audio_mime_type') or '',
                    'allow_after_expiry': bool(message.get('allow_after_expiry')),
                    'reply_group': reply_group,
                    'stream_start_received': True,
                }
            elif task_type == 'asr.chunk':
                chunk = message.get('audio_chunk', b'')
                if reply_channel in self.audio_chunks:
                    self.audio_chunks[reply_channel].append(chunk)
                    stream_meta = self.audio_stream_meta.setdefault(reply_channel, {})
                    if session_id is not None:
                        stream_meta['session_id'] = session_id
                    if question_id is not None:
                        stream_meta['question_id'] = question_id
                    if message.get('audio_mime_type'):
                        stream_meta['audio_mime_type'] = message.get('audio_mime_type')
                    if message.get('reply_group'):
                        stream_meta['reply_group'] = message.get('reply_group')
                    chunk_count = len(self.audio_chunks[reply_channel])
                    if chunk_count == 1 or chunk_count % CHUNK_LOG_INTERVAL == 0:
                        logger.info(
                            "[RUN] Nhận asr.chunk: reply_channel=%s session_id=%s question_id=%s chunk_index=%s chunk_bytes=%s stream_start_received=%s",
                            reply_channel,
                            stream_meta.get('session_id'),
                            stream_meta.get('question_id'),
                            chunk_count,
                            len(chunk),
                            stream_meta.get('stream_start_received', True),
                        )
                else:
                    logger.warning(
                        "[RUN] Nhận asr.chunk trước START stream: reply_channel=%s session_id=%s question_id=%s chunk_bytes=%s",
                        reply_channel,
                        session_id,
                        question_id,
                        len(chunk),
                    )
            elif task_type == 'asr.stream.end':
                mode = message.get('mode') or 'main'
                stream_started = reply_channel in self.audio_chunks
                chunks = self.audio_chunks.pop(reply_channel, [])
                stream_meta = self.audio_stream_meta.pop(reply_channel, {})
                session_id = session_id or stream_meta.get('session_id')
                question_id = question_id or stream_meta.get('question_id')
                audio_mime_type = message.get('audio_mime_type') or stream_meta.get('audio_mime_type') or ''
                allow_after_expiry = bool(message.get('allow_after_expiry') or stream_meta.get('allow_after_expiry'))
                reply_group = reply_group or stream_meta.get('reply_group')
                logger.info(
                    "[RUN] END stream: reply_channel=%s session_id=%s question_id=%s mode=%s stream_start_received=%s chunk_count=%s stream_end_received=True audio_mime_type=%s",
                    reply_channel,
                    session_id,
                    question_id,
                    mode,
                    stream_started,
                    len(chunks),
                    audio_mime_type,
                )
                if not chunks:
                    logger.warning(
                        "[RUN] Không có chunk audio để xử lý: reply_channel=%s session_id=%s question_id=%s mode=%s stream_start_received=%s",
                        reply_channel,
                        session_id,
                        question_id,
                        mode,
                        stream_started,
                    )
                    await self._send_exam_error(
                        reply_channel,
                        reply_group,
                        session_id=session_id,
                        question_id=question_id,
                        message='Không nhận được dữ liệu âm thanh để chấm điểm.',
                    )
                    continue

                message['session_id'] = session_id
                message['question_id'] = question_id
                message['audio_mime_type'] = audio_mime_type
                message['allow_after_expiry'] = allow_after_expiry
                message['reply_group'] = reply_group
                message['__chunks'] = chunks
                asyncio.create_task(self.process_main_question(message))
            else:
                logger.warning("[RUN] Bỏ qua message không hỗ trợ: %s", task_type)

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        self.stdout.write(self.style.SUCCESS('Starting AI Worker...'))
        try:
            asyncio.run(self.run())
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('Worker stopped by user.'))


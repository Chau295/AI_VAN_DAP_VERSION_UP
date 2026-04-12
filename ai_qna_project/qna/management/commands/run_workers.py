# qna/management/commands/run_workers.py
import logging
import json
import os
import re
import asyncio
import subprocess
from datetime import timedelta
from unicodedata import normalize
from uuid import uuid4
import wave
from array import array
from typing import Optional, Dict, Any

try:
    import torch
except ImportError:  # pragma: no cover - optional dependency
    torch = None
import fitz
import openai
from docx import Document

from django.core.management.base import BaseCommand
from django.conf import settings
from channels.layers import get_channel_layer
from channels.db import database_sync_to_async
from asgiref.sync import sync_to_async
from qna.models import (
    Question,
    ExamSession,
    ExamResult,
    Subject,
    LectureMaterial,
)
from django.core.cache import cache
from django.utils import timezone
from django.db import IntegrityError, transaction


def _is_exam_group_open(exam_group):
    """Check if exam group is currently open for taking exam"""
    if not exam_group or exam_group.status == "CANCELLED":
        return False
    now = timezone.now()
    start_at = getattr(exam_group, "start_at", None)
    end_at = getattr(exam_group, "end_at", None)
    if not start_at or not end_at:
        return False
    return start_at <= now <= end_at


@database_sync_to_async
def _load_main_question_context(session_id, question_id):
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
    exam_is_open = _is_exam_group_open(session.exam_session_group_id)
    session_is_active = (
        session.session_status == "STARTED"
        and session.started_at is not None
        and session.completed_at is None
        and session.session_status != "COMPLETED"
        and exam_is_open
    )
    return {
        "question_text": question.question_text,
        "ordered_question_ids": ordered_question_ids,
        "exam_is_open": exam_is_open,
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


logger = logging.getLogger(__name__)

# ====== HẰNG SỐ / HELPERS ======
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


async def rephrase_text_with_chatgpt(text, question_text, client):
    if not text or client is None:
        return text

    system_prompt = """Bạn là một trợ lý sửa lỗi văn bản tiếng Việt.

NGUYÊN TẮC QUAN TRỌNG:
1. CHỈ sửa lỗi chính tả, lỗi đánh máy, lỗi nhận dạng giọng nói
2. KHÔNG ĐƯỢC thêm từ mới nào
3. KHÔNG ĐƯỢC xóa từ nào (trừ từ lặp thừa)
4. KHÔNG ĐƯỢC thay đổi cấu trúc câu
5. KHÔNG ĐƯỢC thay đổi ý nghĩa của câu
6. KHÔNG ĐƯỢC diễn giải lại nội dung

VÍ DỤ:
- "chủng năng" → "chuẩn năng" (đúng)
- "tôi là sinh viên" → "tôi là một sinh viên" (SAI - đã thêm từ)
- "tôi nói nói về" → "tôi nói về" (đúng - xóa lặp)
- "data" → "dữ liệu" (SAI - đã thay đổi từ)

Chỉ trả về văn bản đã sửa, KHÔNG thêm giải thích."""

    user_prompt = f"""Câu trả lời sau đây được nhận dạng từ giọng nói, cần sửa lỗi:

--- CÂU HỎI ---
{question_text}

--- CÂU TRẢ LỜI GỐC ---
{text}

--- YÊU CẦU ---
Chỉ sửa lỗi chính tả/đánh máy. Giữ nguyên mọi thứ khác.
"""

    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
        )
        rephrased = resp.choices[0].message.content.strip()

        if text and rephrased and text != rephrased:
            len_original = len(text.split())
            len_rephrased = len(rephrased.split())
            ratio = len_rephrased / len_original if len_original > 0 else 1

            if ratio < 0.7 or ratio > 1.3:
                logger.warning(
                    f"Rephrasing thay đổi độ dài quá nhiều: "
                    f"{len_original} → {len_rephrased} (ratio={ratio:.2f}). "
                    f"Sử dụng bản gốc."
                )
                return text

        return rephrased
    except Exception as e:
        logger.error(f"Lỗi rephrase OpenAI: {e}")
        return text


async def score_student_answer_with_openai(student_answer_raw, question_text, openai_client, model_name="gpt-4o-mini"):
    if openai_client is None: return 0.0, "OpenAI client chưa được khởi tạo."

    max_score = 10.0

    system_prompt = (
        "Bạn là một chuyên gia chấm điểm câu trả lời vấn đáp về khoa học dữ liệu.\n\n"
        "NGUYÊN TẮC CHẤM ĐIỂM:\n"
        f"1. Chấm điểm trực tiếp theo chất lượng nội dung. Điểm tối đa: {max_score:.2f}\n"
        "2. Đánh giá độ chính xác, độ đầy đủ, mức độ giải thích và sự liên quan với câu hỏi.\n"
        "3. ẢO GIÁC DETECTION: Nếu câu trả lời có vẻ được AI tạo ra (ví dụ: quá hoàn hảo, "
        "cấu trúc không tự nhiên, từ vựng không phù hợp với sinh viên), giảm 30-50% điểm.\n"
        "4. Câu trả lời quá ngắn hoặc không liên quan: 0 điểm.\n"
        "5. Câu trả lời chỉ đọc lại câu hỏi: 0 điểm.\n"
        "6. Phải có nội dung thực sự từ sinh viên mới có điểm.\n\n"
        "ĐẦU RA BẮT BUỘC: JSON format:\n"
        '{"diem_so": float, "phan_hoi": "string"}\n'
        "KHÔNG thêm văn bản nào khác ngoài JSON."
    )

    user_prompt = (
        f"--- CÂU HỎI ---\n{question_text}\n\n"
        f"--- CÂU TRẢ LỜI SINH VIÊN (đã transcribe từ giọng nói) ---\n{student_answer_raw}\n\n"
        f"--- CHẤM ĐIỂM ---\n"
        f"1. Đánh giá: Câu trả lời có được sinh viên thực sự nói không?\n"
        f"2. Chấm điểm dựa trên độ đúng, độ đầy đủ và chiều sâu giải thích.\n"
        f"3. Nếu có ảo giác AI, ghi rõ trong phần hồi và giảm điểm.\n\n"
        f"Trả về JSON duy nhất, KHÔNG thêm chữ nào khác."
    )

    try:
        resp = await asyncio.to_thread(
            openai_client.chat.completions.create,
            model=model_name,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        score = min(max(float(data.get("diem_so", 0.0)), 0.0), max_score)
        feedback = data.get("phan_hoi", "Không có phản hồi.")
        return score, feedback
    except Exception as e:
        logger.error(f"Lỗi khi chấm điểm OpenAI: {e}")
        return 0.0, f"Lỗi hệ thống chấm điểm AI: {e}"


def convert_webm_to_wav(webm_path: str) -> Optional[str]:
    wav_path = webm_path.replace(".webm", ".wav")
    try:
        ffmpeg_binary = getattr(settings, "FFMPEG_BINARY", "ffmpeg")
        command = [ffmpeg_binary, "-hide_banner", "-loglevel", "warning", "-nostdin", "-fflags", "+genpts",
                   "-i", webm_path, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", wav_path]
        subprocess.run(command, check=True, capture_output=True, text=True)
        logger.info(f"Đã chuyển đổi thành công {webm_path} sang {wav_path}")
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
        logger.warning(f"Không thể đọc file {path}: {e}")

    return ""


# ====== WORKER CLASS ======
class Command(BaseCommand):
    help = 'Chạy worker lắng nghe các tác vụ AI từ channel layer'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.channel_layer = get_channel_layer()
        self.audio_chunks = {}
        self.device = "cuda" if (torch is not None and torch.cuda.is_available()) else "cpu"
        self.stdout.write(f"Sử dụng thiết bị: {self.device}")
        self.stdout.write("Đang cấu hình OpenAI client...")
        try:
            self.openai_client = openai.OpenAI()
            self.openai_client.models.list()
            self.stdout.write("✅ OpenAI (ChatGPT & Whisper) đã sẵn sàng.")
        except Exception as e:
            self.openai_client = None
            self.stderr.write(f"Lỗi cấu hình OpenAI: {e} - đặt OPENAI_API_KEY trước.")

    async def process_audio_and_transcribe(self, reply_channel, chunks, whisper_prompt: Optional[str] = None):
        if not chunks:
            logger.warning("Không có chunk âm thanh nào để xử lý.")
            return None
        first = chunks[0]
        if len(first) < 4 or not has_ebml_header(first[:4]):
            logger.error("Các chunk không chứa EBML header. Dữ liệu audio không hợp lệ.")
            await self.channel_layer.send(reply_channel, {'type': 'exam.error',
                                                          'message': 'Dữ liệu audio không hợp lệ (thiếu header).'})
            return None
        unique_id = re.sub(r'[^a-zA-Z0-9]', '_', reply_channel)
        webm_path = os.path.join(settings.BASE_DIR, f'temp_audio_{unique_id}_{uuid4().hex[:8]}.webm')
        try:
            with open(webm_path, 'wb') as f:
                for c in chunks: f.write(c)
        except Exception as e:
            logger.error(f"Lỗi ghi file tạm: {e}")
            await self.channel_layer.send(reply_channel,
                                          {'type': 'exam.error', 'message': 'Lỗi hệ thống khi ghi file âm thanh.'})
            return None
        wav_path = await asyncio.to_thread(convert_webm_to_wav, webm_path)
        if os.path.exists(webm_path): os.remove(webm_path)
        if not wav_path:
            await self.channel_layer.send(reply_channel, {'type': 'exam.error', 'message': 'Lỗi xử lý file âm thanh.'})
            return None
        duration, rms = wav_duration_and_rms(wav_path)
        logger.info(f"WAV duration ~ {duration:.2f}s; RMS ~ {rms:.1f}")

        RMS_THRESHOLD = 50.0
        if rms < RMS_THRESHOLD:
            logger.info(f"Phát hiện silence (RMS={rms:.1f} < {RMS_THRESHOLD}). Trả về 'Không có âm thanh'.")
            await self.channel_layer.send(reply_channel,
                                          {'type': 'exam.error', 'message': 'Không có âm thanh được phát hiện.'})
            return None

        try:
            context_prompt = whisper_prompt or ""
            if context_prompt:
                context_prompt = f"Bối cảnh: {context_prompt}. "

            with open(wav_path, "rb") as audio_file:
                transcription = await asyncio.to_thread(
                    self.openai_client.audio.transcriptions.create,
                    model="whisper-1",
                    file=audio_file,
                    language="vi",
                    temperature=0,
                    prompt=context_prompt,
                )

            raw = transcription.text.strip()
            logger.info(f"Transcript nhận được: '{raw}'")

            if raw:
                words = raw.split()
                if len(words) < 3 and len(chunks) > 0:
                    logger.warning(
                        f"Transcript quá ngắn ({len(words)} từ) dù có dữ liệu audio. "
                        f"Có thể lỗi transcribe."
                    )

            if not raw:
                logger.info("Whisper trả về chuỗi rỗng. Trả về 'Không có âm thanh'.")
                await self.channel_layer.send(reply_channel,
                                              {'type': 'exam.error', 'message': 'Không có âm thanh được phát hiện.'})
                return None

            return raw
        except Exception as e:
            logger.error(f"Lỗi Whisper: {e}")
            return ""
        finally:
            if os.path.exists(wav_path): os.remove(wav_path)

    async def process_main_question(self, message):
        reply_channel = message['reply_channel']
        question_id = message.get('question_id')
        session_id = message.get('session_id')
        chunks = message.get('__chunks', [])
        if not all([session_id, question_id, chunks]):
            logger.error(
                f"Tác vụ 'main' thiếu dữ liệu. session_id={session_id}, question_id={question_id}, chunks={len(chunks)}")
            return
        try:
            question_context = await _load_main_question_context(session_id, question_id)
            question_id_int = int(question_id)

            if not question_context["session_is_active"]:
                await self.channel_layer.send(reply_channel, {
                    'type': 'exam.error',
                    'message': 'Phiên thi không còn hợp lệ để nộp câu trả lời.'
                })
                return

            if not question_context["exam_is_open"]:
                logger.warning(f"Exam time has ended for session {session_id}. Rejecting audio processing.")
                await self.channel_layer.send(reply_channel, {
                    'type': 'exam.error',
                    'message': 'Thời gian thi đã kết thúc. Không thể gửi câu trả lời.'
                })
                return

            if question_context["already_answered"]:
                await self.channel_layer.send(reply_channel, {
                    'type': 'exam.error',
                    'message': 'Câu hỏi này đã được nộp trước đó.'
                })
                return

            ordered_question_ids = question_context["ordered_question_ids"]
            question_order = next((index + 1 for index, item_id in enumerate(ordered_question_ids) if item_id == question_id_int), None)
            question_label = f"Câu {question_order}" if question_order else f"Q{question_id}"
            raw_transcript = await self.process_audio_and_transcribe(reply_channel, chunks,
                                                                     whisper_prompt=question_context["question_text"])
            if raw_transcript is None or raw_transcript == "":
                return
            rephrased = await rephrase_text_with_chatgpt(raw_transcript, question_context["question_text"], self.openai_client)
            final_score, feedback = await score_student_answer_with_openai(rephrased, question_context["question_text"], self.openai_client)
            final_score = float(min(max(final_score, 0.0), 10.0))
            logger.info(f"Chấm điểm {question_label} -> Final={final_score:.2f}")
            exam_result = await _create_main_exam_result(
                session_id=session_id,
                question_id=question_id_int,
                transcript=rephrased,
                score=final_score,
                feedback=feedback,
            )
            if not exam_result["created"]:
                await self.channel_layer.send(reply_channel, {
                    'type': 'exam.error',
                    'message': 'Câu hỏi này đã được nộp trước đó.'
                })
                return
            await self.channel_layer.send(reply_channel,
                                          {'type': 'exam.result', 'message': {'type': 'main_question_complete',
                                                                              'data': {'result_id': exam_result['id'],
                                                                                       'score': exam_result['score'],
                                                                                       'question_id': question_id_int,
                                                                                       'transcript': rephrased,
                                                                                       'feedback': feedback}
                                                                              }})
        except Exception as e:
            logger.error(f"Lỗi không mong muốn trong process_main_question: {e}", exc_info=True)
            await self.channel_layer.send(reply_channel, {'type': 'exam.error', 'message': f'Lỗi worker: {str(e)}'})

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
                "ĐẦU RA BẮT BUỘC: JSON object theo format "
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

                new_q = await sync_to_async(Question.objects.create)(
                    subject=subject,
                    question_text=content,
                    difficulty=diff,
                    question_id_in_barem=f"DRAFT_AI_{uuid4().hex[:8]}"
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
        logger.info("Worker đang lắng nghe trên kênh 'asr-tasks'.")
        while True:
            message = await self.channel_layer.receive('asr-tasks')
            task_type = message.get('type')

            if task_type == 'ai.generate_questions':
                logger.info(f"Nhận lệnh sinh câu hỏi AI, job_id={message.get('job_id')}")
                asyncio.create_task(self.process_generate_questions(message))
                continue

            reply_channel = message.get('reply_channel')
            if not reply_channel:
                continue

            if task_type == 'asr.stream.start':
                logger.info(f"Bắt đầu stream cho kênh {reply_channel}")
                self.audio_chunks[reply_channel] = []
            elif task_type == 'asr.chunk':
                if reply_channel in self.audio_chunks:
                    self.audio_chunks[reply_channel].append(message.get('audio_chunk', b''))
            elif task_type == 'asr.stream.end':
                mode = message.get('mode') or 'main'
                logger.info(f"Kết thúc stream, nhận lệnh xử lý '{mode}' cho kênh {reply_channel}")

                chunks = self.audio_chunks.pop(reply_channel, [])
                if not chunks:
                    logger.warning(f"Không có chunk audio nào để xử lý cho kênh {reply_channel}. Bỏ qua.")
                    continue

                message['__chunks'] = chunks
                asyncio.create_task(self.process_main_question(message))
            else:
                logger.warning(f"Bỏ qua message không hỗ trợ: {task_type}")

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        self.stdout.write(self.style.SUCCESS('Starting AI Worker...'))
        try:
            asyncio.run(self.run())
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('Worker stopped by user.'))
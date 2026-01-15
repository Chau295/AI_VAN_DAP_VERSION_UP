# qna/management/commands/run_workers.py
import logging
import json
import os
import re
import asyncio
import subprocess
from unicodedata import normalize
from uuid import uuid4
import wave
from array import array
from typing import Optional, Dict, Any

import torch
from transformers import AutoTokenizer, AutoModel
from torch.nn.functional import cosine_similarity
import openai

from django.core.management.base import BaseCommand
from django.conf import settings
from channels.layers import get_channel_layer
from asgiref.sync import sync_to_async
from django.core.exceptions import ObjectDoesNotExist
from qna.models import Question, ExamSession, ExamResult, SupplementaryResult

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


def get_sentence_embedding(text, tokenizer, model, device):
    inputs = tokenizer(text, return_tensors='pt', padding=True, truncation=True, max_length=256)
    inputs = {k: v.to(device) for k, v in inputs.items()}
    with torch.no_grad():
        outputs = model(**inputs)
    return outputs.last_hidden_state.mean(dim=1)


async def rephrase_text_with_chatgpt(text, question_text, client):
    if not text or client is None: 
        return text
    
    # Prompt cải tiến với ràng buộc chặt chẽ hơn
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

    user_prompt = f"""Câu trả lời sau được nhận dạng từ giọng nói, cần sửa lỗi:

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
            temperature=0.0,  # Giảm randomness
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
                    f"Rephrasing thay đổi độ dài quá nhiều: "
                    f"{len_original} → {len_rephrased} (ratio={ratio:.2f}). "
                    f"Sử dụng bản gốc."
                )
                return text
        
        return rephrased
    except Exception as e:
        logger.error(f"Lỗi rephrase OpenAI: {e}")
        return text


## THAY ĐỔI: Tăng ngưỡng tương đồng để việc so khớp chặt chẽ hơn
def score_student_answer_traditional(student_answer_raw, question_barem, tokenizer, model, device,
                                     similarity_threshold=0.65):
    if tokenizer is None or model is None: return 0.0
    student_answer_pre = preprocess_text_vietnamese(student_answer_raw)
    if not student_answer_pre: return 0.0

    student_emb = get_sentence_embedding(student_answer_pre, tokenizer, model, device)

    # --- BỘ LỌC MỚI: KIỂM TRA LẶP LẠI CÂU HỎI ---
    question_text = question_barem.get('question', '')
    if question_text:
        question_pre = preprocess_text_vietnamese(question_text)
        question_emb = get_sentence_embedding(question_pre, tokenizer, model, device)
        # So sánh câu trả lời với chính câu hỏi
        self_similarity = cosine_similarity(student_emb, question_emb, dim=1).item()
        # Nếu giống > 90% thì coi như là đọc lại câu hỏi
        if self_similarity > 0.9:
            logger.warning(
                f"Phát hiện câu trả lời giống câu hỏi (độ tương đồng: {self_similarity:.2f}). Chấm 0 điểm cho PhoBERT.")
            return 0.0
    # --- KẾT THÚC BỘ LỌC MỚI ---

    total_score = 0.0
    for kp in question_barem.get("key_points", []):
        kp_pre = preprocess_text_vietnamese(kp.get("text", ""))
        kp_emb = get_sentence_embedding(kp_pre, tokenizer, model, device)
        sim = cosine_similarity(student_emb, kp_emb, dim=1).item()
        if sim >= similarity_threshold:
            total_score += float(kp.get("weight", 0.0))
    return min(total_score, float(question_barem.get("max_score", 10.0)))

async def score_student_answer_with_openai(student_answer_raw, question_barem, openai_client, model_name="gpt-4o-mini"):
    if openai_client is None: return 0.0, "OpenAI client chưa được khởi tạo."

    max_score = float(question_barem.get("max_score", 10.0))
    
    system_prompt = (
        "Bạn là một chuyên gia chấm điểm câu trả lời vấn đáp về khoa học dữ liệu.\n\n"
        "NGUYÊN TẮC CHẤM ĐIỂM:\n"
        f"1. Chấm điểm nghiêm ngặt theo barem. Điểm tối đa: {max_score:.2f}\n"
        "2. ẢO GIÁC DETECTION: Nếu câu trả lời có vẻ được AI tạo ra (ví dụ: quá hoàn hảo, "
        "cấu trúc không tự nhiên, từ vựng không phù hợp với sinh viên), giảm 30-50% điểm.\n"
        "3. Câu trả lời quá ngắn hoặc không liên quan: 0 điểm.\n"
        "4. Câu trả lời chỉ đọc lại câu hỏi: 0 điểm.\n"
        "5. Phải có nội dung thực sự từ sinh viên mới có điểm.\n\n"
        "ĐẦU RA BẮT BUỘC: JSON format:\n"
        '{"diem_so": float, "phan_hoi": "string"}\n'
        "KHÔNG thêm văn bản nào khác ngoài JSON."
    )
    
    barem_lines = [
        f"- {kp.get('id', 'KP')}: (Trọng số {kp.get('weight', 0)}). {kp.get('text', '')}" 
        for kp in question_barem.get("key_points", [])
    ]
    barem_block = "\n".join(barem_lines) if barem_lines else "(Không có key_points)"
    
    user_prompt = (
        f"--- CÂU HỎI ---\n{question_barem.get('question', '')}\n\n"
        f"--- BAREM CHẤM ĐIỂM ---\n{barem_block}\n\n"
        f"--- CÂU TRẢ LỜI SINH VIÊN (đã transcribe từ giọng nói) ---\n{student_answer_raw}\n\n"
        f"--- CHẤM ĐIỂM ---\n"
        f"1. Đánh giá: Câu trả lời có được sinh viên thực sự nói không?\n"
        f"2. Chấm điểm theo barem (nếu câu trả lời hợp lệ).\n"
        f"3. Nếu có ảo giác AI, ghi rõ trong phản hồi và giảm điểm.\n\n"
        f"Trả về JSON duy nhất, KHÔNG thêm chữ nào khác."
    )
    
    try:
        resp = await asyncio.to_thread(
            openai_client.chat.completions.create,
            model=model_name,
            response_format={"type": "json_object"},
            temperature=0,
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
        command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-fflags", "+genpts",
                   "-i", webm_path, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", wav_path]
        subprocess.run(command, check=True, capture_output=True, text=True)
        logger.info(f"Đã chuyển đổi thành công {webm_path} sang {wav_path}")
        return wav_path
    except subprocess.CalledProcessError as e:
        logger.error(f"Lỗi khi chạy ffmpeg: {e.stderr}")
        return None
    except FileNotFoundError:
        logger.error("Lỗi: không tìm thấy 'ffmpeg' trong PATH.")
        return None


# ====== WORKER CLASS ======
class Command(BaseCommand):
    help = 'Chạy worker lắng nghe các tác vụ AI từ channel layer'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.channel_layer = get_channel_layer()
        self.audio_chunks = {}
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.stdout.write(f"Sử dụng thiết bị: {self.device}")
        self.stdout.write("Đang tải PhoBERT model...")
        try:
            self.phobert_tokenizer = AutoTokenizer.from_pretrained("vinai/phobert-base")
            self.phobert_model = AutoModel.from_pretrained("vinai/phobert-base").to(self.device)
            self.phobert_model.eval()
            self.stdout.write("✅ PhoBERT đã sẵn sàng.")
        except Exception as e:
            self.phobert_model = None;
            self.phobert_tokenizer = None
            self.stderr.write(self.style.ERROR(f"Lỗi khi tải PhoBERT: {e} - sẽ bỏ qua chấm điểm barem."))
        self.stdout.write("Đang cấu hình OpenAI client...")
        try:
            self.openai_client = openai.OpenAI()
            self.openai_client.models.list()
            self.stdout.write("✅ OpenAI (ChatGPT & Whisper) đã sẵn sàng.")
        except Exception as e:
            self.openai_client = None
            self.stderr.write(self.style.ERROR(f"Lỗi cấu hình OpenAI: {e} - đặt OPENAI_API_KEY trước."))

    @sync_to_async
    def get_question_and_barem(self, question_id):
        try:
            question_obj = Question.objects.select_related('subject').get(pk=question_id)
            quiz_file_path = os.path.join(settings.BASE_DIR, 'quiz_data', question_obj.subject.quiz_data_file)
            with open(quiz_file_path, 'r', encoding='utf-8') as f:
                all_barems = json.load(f)
            for barem in all_barems:
                if barem['id'] == question_obj.question_id_in_barem:
                    barem['question'] = barem.get('question') or question_obj.question_text
                    return barem
        except Exception as e:
            logger.error(f"Không thể tải barem cho câu hỏi {question_id}: {e}")
        return None

    @sync_to_async
    def get_last_main_question_id_for_session(self, session_id: str) -> Optional[int]:
        """
        Tries to find the last main question answered in a given session.
        This is a fallback for when main_question_id is not sent with the supplementary question task.
        """
        try:
            last_result = ExamResult.objects.filter(session_id=session_id).latest('pk')
            return last_result.question_id
        except ObjectDoesNotExist:
            logger.warning(f"No previous ExamResult found for session_id={session_id} to infer main_question_id.")
            return None

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
        
        # Kiểm tra RMS để phát hiện silence (không có âm thanh thực)
        # Ngưỡng RMS < 50.0 thường là silence hoặc nhiễu nền
        RMS_THRESHOLD = 50.0
        if rms < RMS_THRESHOLD:
            logger.info(f"Phát hiện silence (RMS={rms:.1f} < {RMS_THRESHOLD}). Trả về 'Không có âm thanh'.")
            await self.channel_layer.send(reply_channel, 
                {'type': 'exam.error', 'message': 'Không có âm thanh được phát hiện.'})
            return None
        
        # Removed duration check - users can now submit audio of any length
        try:
            # Sử dụng prompt context để giúp Whisper hiểu bối cảnh tốt hơn
            context_prompt = whisper_prompt or ""
            if context_prompt:
                context_prompt = f"Bối cảnh: {context_prompt}. "
            
            transcription = await asyncio.to_thread(
                self.openai_client.audio.transcriptions.create,
                model="whisper-1",
                file=audio_file,
                language="vi",
                temperature=0,  # Giảm randomness
                prompt=context_prompt,  # Context giúp hiểu từ vựng chuyên môn
            )
            
            raw = transcription.text.strip()
            logger.info(f"Transcript nhận được: '{raw}'")
            
            # Validation thêm
            if raw:
                # Kiểm tra nếu transcript quá ngắn
                words = raw.split()
                if len(words) < 3 and len(chunks) > 0:
                    logger.warning(
                        f"Transcript quá ngắn ({len(words)} từ) dù có dữ liệu audio. "
                        f"Có thể lỗi transcribe."
                    )
            
            # Nếu Whisper trả về chuỗi rỗng, coi như không có âm thanh
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
            question_barem = await self.get_question_and_barem(question_id)
            if not question_barem:
                await self.channel_layer.send(reply_channel,
                                              {'type': 'exam.error', 'message': 'Lỗi hệ thống: Không tìm thấy barem.'})
                return
            raw_transcript = await self.process_audio_and_transcribe(reply_channel, chunks,
                                                                     whisper_prompt=question_barem.get('question', ''))
            if raw_transcript is None or raw_transcript == "": return
            rephrased = await rephrase_text_with_chatgpt(raw_transcript, question_barem['question'], self.openai_client)
            traditional_task = asyncio.to_thread(score_student_answer_traditional, rephrased, question_barem,
                                                 self.phobert_tokenizer, self.phobert_model, self.device)
            openai_task = score_student_answer_with_openai(rephrased, question_barem, self.openai_client)
            traditional_score, (openai_score, feedback) = await asyncio.gather(traditional_task, openai_task)
            WEIGHT_OPENAI = 0.7
            WEIGHT_TRADITIONAL = 0.3
            final_score = (openai_score * WEIGHT_OPENAI) + (traditional_score * WEIGHT_TRADITIONAL)
            final_score = float(min(max(final_score, 0.0), 10.0))
            logger.info(
                f"Chấm điểm CÂU HỎI CHÍNH (Q{question_id}): PhoBERT={traditional_score:.2f}, OpenAI={openai_score:.2f} -> Final={final_score:.2f}")
            session = await sync_to_async(ExamSession.objects.get)(pk=session_id)
            question = await sync_to_async(Question.objects.get)(pk=question_id)
            exam_result = await sync_to_async(ExamResult.objects.create)(
                session=session, question=question, transcript=rephrased, score=final_score, feedback=feedback
            )
            await self.channel_layer.send(reply_channel,
                                          {'type': 'exam.result', 'message': {'type': 'main_question_complete',
                                                                              'data': {'result_id': exam_result.id,
                                                                                       'score': exam_result.score,
                                                                                       'question_id': question_id,
                                                                                       'transcript': rephrased,
                                                                                       'feedback': feedback}
                                                                              }})
        except Exception as e:
            logger.error(f"Lỗi không mong muốn trong process_main_question: {e}", exc_info=True)
            await self.channel_layer.send(reply_channel, {'type': 'exam.error', 'message': f'Lỗi worker: {str(e)}'})

    async def process_supplementary_question(self, message):
        reply_channel = message['reply_channel']
        session_id = message.get('session_id')
        question_text = message.get('question_text')
        ## THAY ĐỔI: Lấy main_question_id để có thể truy xuất đúng barem.
        main_question_id = message.get('main_question_id')
        max_score = message.get('max_score', 2.0)
        chunks = message.get('__chunks', [])

        # Fallback: If main_question_id is not provided, try to infer it from the session.
        if not main_question_id and session_id:
            logger.info(f"main_question_id not in message for session {session_id}. Inferring from last main question.")
            main_question_id = await self.get_last_main_question_id_for_session(session_id)

        if not all([session_id, question_text, main_question_id, chunks]):
            logger.error(f"Tác vụ 'supplementary' thiếu dữ liệu (cần có main_question_id).")
            await self.channel_layer.send(reply_channel, {'type': 'exam.error',
                                                          'message': 'Lỗi: Yêu cầu xử lý câu hỏi phụ không hợp lệ.'})
            return
        try:
            question_barem = await self.get_question_and_barem(main_question_id)
            if not question_barem:
                await self.channel_layer.send(reply_channel, {'type': 'exam.error',
                                                              'message': f'Lỗi: Không tìm thấy barem cho câu hỏi chính ID {main_question_id}.'})
                return

            raw_transcript = await self.process_audio_and_transcribe(reply_channel, chunks,
                                                                     whisper_prompt=f"Bài thi vấn đáp. Câu hỏi phụ: {question_text}")
            if raw_transcript is None or raw_transcript == "": return
            rephrased = await rephrase_text_with_chatgpt(raw_transcript, question_text, self.openai_client)

            ## THAY ĐỔI: Áp dụng chấm điểm HYBRID giống câu hỏi chính.
            temp_barem_for_supp = question_barem.copy()
            temp_barem_for_supp['question'] = question_text
            temp_barem_for_supp['max_score'] = 10.0  # Chấm trên thang 10 rồi scale sau

            traditional_task = asyncio.to_thread(score_student_answer_traditional, rephrased, temp_barem_for_supp,
                                                 self.phobert_tokenizer, self.phobert_model, self.device)
            openai_task = score_student_answer_with_openai(rephrased, temp_barem_for_supp, self.openai_client)
            traditional_score, (openai_score, feedback) = await asyncio.gather(traditional_task, openai_task)

            WEIGHT_OPENAI = 0.7
            WEIGHT_TRADITIONAL = 0.3
            score_on_10 = (openai_score * WEIGHT_OPENAI) + (traditional_score * WEIGHT_TRADITIONAL)

            final_score = (score_on_10 / 10.0) * float(max_score)
            final_score = float(min(max(final_score, 0.0), float(max_score)))

            ## THAY ĐỔI: Thêm log chi tiết cho điểm câu hỏi phụ theo yêu cầu.
            logger.info(
                f"Chấm điểm CÂU HỎI PHỤ (cho Q{main_question_id}): PhoBERT={traditional_score:.2f}, OpenAI={openai_score:.2f} -> Final={final_score:.2f}/{max_score}")

            session = await sync_to_async(ExamSession.objects.get)(pk=session_id)
            supp = await sync_to_async(SupplementaryResult.objects.create)(
                session=session, question_text=question_text, transcript=rephrased,
                score=final_score, max_score=max_score, feedback=feedback
            )
            await self.channel_layer.send(reply_channel,
                                          {'type': 'exam.result', 'message': {'type': 'supp_question_complete',
                                                                              'data': {'result_id': supp.id,
                                                                                       'score': supp.score,
                                                                                       'max_score': supp.max_score,
                                                                                       'question_text': question_text,
                                                                                       'transcript': rephrased,
                                                                                       'feedback': feedback}
                                                                              }})
        except Exception as e:
            logger.error(f"Lỗi không mong muốn trong process_supplementary_question: {e}", exc_info=True)
            await self.channel_layer.send(reply_channel, {'type': 'exam.error', 'message': f'Lỗi worker: {str(e)}'})

    async def run(self):
        logger.info("Worker đang lắng nghe trên kênh 'asr-tasks'...")
        while True:
            message = await self.channel_layer.receive('asr-tasks')
            task_type = message.get('type')
            reply_channel = message.get('reply_channel')
            if not reply_channel: continue

            if task_type == 'asr.stream.start':
                logger.info(f"Bắt đầu stream cho kênh {reply_channel}")
                self.audio_chunks[reply_channel] = []
            elif task_type == 'asr.chunk':
                if reply_channel in self.audio_chunks:
                    self.audio_chunks[reply_channel].append(message.get('audio_chunk', b''))
            elif task_type == 'asr.stream.end':
                mode = message.get('mode') or ('main' if message.get('question_id') else 'supplementary')
                logger.info(f"Kết thúc stream, nhận lệnh xử lý '{mode}' cho kênh {reply_channel}")

                chunks = self.audio_chunks.pop(reply_channel, [])
                if not chunks:
                    logger.warning(f"Không có chunk audio nào để xử lý cho kênh {reply_channel}. Bỏ qua.")
                    continue
                message['__chunks'] = chunks
                if mode in ('main', 'primary'):
                    asyncio.create_task(self.process_main_question(message))
                else:
                    asyncio.create_task(self.process_supplementary_question(message))
            else:
                logger.warning(f"Bỏ qua message không hỗ trợ: {task_type}")

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        self.stdout.write(self.style.SUCCESS('Starting AI Worker...'))
        try:
            asyncio.run(self.run())
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('Worker stopped by user.'))

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
try:
    from transformers import AutoTokenizer, AutoModel
except ImportError:  # pragma: no cover - optional dependency
    AutoTokenizer = None
    AutoModel = None

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
        and not session.is_completed
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

# ====== Háº°NG Sá» / HELPERS ======
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
        logger.error(f"KhÃ´ng thá»ƒ phÃ¢n tÃ­ch WAV {path}: {e}")
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

    # Prompt cáº£i tiáº¿n vá»›i rÃ ng buá»™c cháº·t cháº½ hÆ¡n
    system_prompt = """Báº¡n lÃ  má»™t trá»£ lÃ½ sá»­a lá»—i vÄƒn báº£n tiáº¿ng Viá»‡t.

NGUYÃŠN Táº®C QUAN TRá»ŒNG:
1. CHá»ˆ sá»­a lá»—i chÃ­nh táº£, lá»—i Ä‘Ã¡nh mÃ¡y, lá»—i nháº­n dáº¡ng giá»ng nÃ³i
2. KHÃ”NG ÄÆ¯á»¢C thÃªm tá»« má»›i nÃ o
3. KHÃ”NG ÄÆ¯á»¢C xÃ³a tá»« nÃ o (trá»« tá»« láº·p thá»«a)
4. KHÃ”NG ÄÆ¯á»¢C thay Ä‘á»•i cáº¥u trÃºc cÃ¢u
5. KHÃ”NG ÄÆ¯á»¢C thay Ä‘á»•i Ã½ nghÄ©a cá»§a cÃ¢u
6. KHÃ”NG ÄÆ¯á»¢C diá»…n giáº£i láº¡i ná»™i dung

VÃ Dá»¤:
- "chá»§ng nÄƒng" â†’ "chuáº©n nÄƒng" (Ä‘Ãºng)
- "tÃ´i lÃ  sinh viÃªn" â†’ "tÃ´i lÃ  má»™t sinh viÃªn" (SAI - Ä‘Ã£ thÃªm tá»«)
- "tÃ´i nÃ³i nÃ³i vá»" â†’ "tÃ´i nÃ³i vá»" (Ä‘Ãºng - xÃ³a láº·p)
- "data" â†’ "dá»¯ liá»‡u" (SAI - Ä‘Ã£ thay Ä‘á»•i tá»«)

Chá»‰ tráº£ vá» vÄƒn báº£n Ä‘Ã£ sá»­a, KHÃ”NG thÃªm giáº£i thÃ­ch."""

    user_prompt = f"""CÃ¢u tráº£ lá»i sau Ä‘Æ°á»£c nháº­n dáº¡ng tá»« giá»ng nÃ³i, cáº§n sá»­a lá»—i:

--- CÃ‚U Há»ŽI ---
{question_text}

--- CÃ‚U TRáº¢ Lá»œI Gá»C ---
{text}

--- YÃŠU Cáº¦U ---
Chá»‰ sá»­a lá»—i chÃ­nh táº£/Ä‘Ã¡nh mÃ¡y. Giá»¯ nguyÃªn má»i thá»© khÃ¡c.
"""

    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,  # Giáº£m randomness
        )
        rephrased = resp.choices[0].message.content.strip()

        # Validation: Kiá»ƒm tra Ä‘á»™ tÆ°Æ¡ng Ä‘á»“ng
        if text and rephrased and text != rephrased:
            # TÃ­nh Ä‘á»™ tÆ°Æ¡ng Ä‘á»“ng Ä‘Æ¡n giáº£n dá»±a trÃªn Ä‘á»™ dÃ i
            len_original = len(text.split())
            len_rephrased = len(rephrased.split())
            ratio = len_rephrased / len_original if len_original > 0 else 1

            # Náº¿u Ä‘á»™ dÃ i thay Ä‘á»•i quÃ¡ nhiá»u (> 30%), dÃ¹ng báº£n gá»‘c
            if ratio < 0.7 or ratio > 1.3:
                logger.warning(
                    f"Rephrasing thay Ä‘á»•i Ä‘á»™ dÃ i quÃ¡ nhiá»u: "
                    f"{len_original} â†’ {len_rephrased} (ratio={ratio:.2f}). "
                    f"Sá»­ dá»¥ng báº£n gá»‘c."
                )
                return text

        return rephrased
    except Exception as e:
        logger.error(f"Lá»—i rephrase OpenAI: {e}")
        return text


async def score_student_answer_with_openai(student_answer_raw, question_text, openai_client, model_name="gpt-4o-mini"):
    if openai_client is None: return 0.0, "OpenAI client chÆ°a Ä‘Æ°á»£c khá»Ÿi táº¡o."

    max_score = 10.0

    system_prompt = (
        "Báº¡n lÃ  má»™t chuyÃªn gia cháº¥m Ä‘iá»ƒm cÃ¢u tráº£ lá»i váº¥n Ä‘Ã¡p vá» khoa há»c dá»¯ liá»‡u.\n\n"
        "NGUYÃŠN Táº®C CHáº¤M ÄIá»‚M:\n"
        f"1. Cháº¥m Ä‘iá»ƒm trá»±c tiáº¿p theo cháº¥t lÆ°á»£ng ná»™i dung. Äiá»ƒm tá»‘i Ä‘a: {max_score:.2f}\n"
        "2. ÄÃ¡nh giÃ¡ Ä‘á»™ chÃ­nh xÃ¡c, Ä‘á»™ Ä‘áº§y Ä‘á»§, má»©c Ä‘á»™ giáº£i thÃ­ch vÃ  sá»± liÃªn quan vá»›i cÃ¢u há»i.\n"
        "3. áº¢O GIÃC DETECTION: Náº¿u cÃ¢u tráº£ lá»i cÃ³ váº» Ä‘Æ°á»£c AI táº¡o ra (vÃ­ dá»¥: quÃ¡ hoÃ n háº£o, "
        "cáº¥u trÃºc khÃ´ng tá»± nhiÃªn, tá»« vá»±ng khÃ´ng phÃ¹ há»£p vá»›i sinh viÃªn), giáº£m 30-50% Ä‘iá»ƒm.\n"
        "4. CÃ¢u tráº£ lá»i quÃ¡ ngáº¯n hoáº·c khÃ´ng liÃªn quan: 0 Ä‘iá»ƒm.\n"
        "5. CÃ¢u tráº£ lá»i chá»‰ Ä‘á»c láº¡i cÃ¢u há»i: 0 Ä‘iá»ƒm.\n"
        "6. Pháº£i cÃ³ ná»™i dung thá»±c sá»± tá»« sinh viÃªn má»›i cÃ³ Ä‘iá»ƒm.\n\n"
        "Äáº¦U RA Báº®T BUá»˜C: JSON format:\n"
        '{"diem_so": float, "phan_hoi": "string"}\n'
        "KHÃ”NG thÃªm vÄƒn báº£n nÃ o khÃ¡c ngoÃ i JSON."
    )

    user_prompt = (
        f"--- CÃ‚U Há»ŽI ---\n{question_text}\n\n"
        f"--- CÃ‚U TRáº¢ Lá»œI SINH VIÃŠN (Ä‘Ã£ transcribe tá»« giá»ng nÃ³i) ---\n{student_answer_raw}\n\n"
        f"--- CHáº¤M ÄIá»‚M ---\n"
        f"1. ÄÃ¡nh giÃ¡: CÃ¢u tráº£ lá»i cÃ³ Ä‘Æ°á»£c sinh viÃªn thá»±c sá»± nÃ³i khÃ´ng?\n"
        f"2. Cháº¥m Ä‘iá»ƒm dá»±a trÃªn Ä‘á»™ Ä‘Ãºng, Ä‘á»™ Ä‘áº§y Ä‘á»§ vÃ  chiá»u sÃ¢u giáº£i thÃ­ch.\n"
        f"3. Náº¿u cÃ³ áº£o giÃ¡c AI, ghi rÃµ trong pháº£n há»“i vÃ  giáº£m Ä‘iá»ƒm.\n\n"
        f"Tráº£ vá» JSON duy nháº¥t, KHÃ”NG thÃªm chá»¯ nÃ o khÃ¡c."
    )

    try:
        resp = await asyncio.to_thread(
            openai_client.chat.completions.create,
            model=model_name,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        score = min(max(float(data.get("diem_so", 0.0)), 0.0), max_score)
        feedback = data.get("phan_hoi", "KhÃ´ng cÃ³ pháº£n há»“i.")
        return score, feedback
    except Exception as e:
        logger.error(f"Lá»—i khi cháº¥m Ä‘iá»ƒm OpenAI: {e}")
        return 0.0, f"Lá»—i há»‡ thá»‘ng cháº¥m Ä‘iá»ƒm AI: {e}"


def convert_webm_to_wav(webm_path: str) -> Optional[str]:
    wav_path = webm_path.replace(".webm", ".wav")
    try:
        ffmpeg_binary = getattr(settings, "FFMPEG_BINARY", "ffmpeg")
        command = [ffmpeg_binary, "-hide_banner", "-loglevel", "warning", "-nostdin", "-fflags", "+genpts",
                   "-i", webm_path, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", wav_path]
        subprocess.run(command, check=True, capture_output=True, text=True)
        logger.info(f"ÄÃ£ chuyá»ƒn Ä‘á»•i thÃ nh cÃ´ng {webm_path} sang {wav_path}")
        return wav_path
    except subprocess.CalledProcessError as e:
        logger.error(f"Lá»—i khi cháº¡y ffmpeg: {e.stderr}")
        return None
    except FileNotFoundError:
        logger.error(f"Lá»—i: khÃ´ng tÃ¬m tháº¥y ffmpeg táº¡i '{ffmpeg_binary}'.")
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
        logger.warning(f"KhÃ´ng thá»ƒ Ä‘á»c file {path}: {e}")

    return ""


# ====== WORKER CLASS ======
class Command(BaseCommand):
    help = 'Cháº¡y worker láº¯ng nghe cÃ¡c tÃ¡c vá»¥ AI tá»« channel layer'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.channel_layer = get_channel_layer()
        self.audio_chunks = {}
        self.device = "cuda" if (torch is not None and torch.cuda.is_available()) else "cpu"
        self.stdout.write(f"Sá»­ dá»¥ng thiáº¿t bá»‹: {self.device}")
        self.stdout.write("Dang tai PhoBERT model...")
        if torch is None or AutoTokenizer is None or AutoModel is None:
            self.phobert_model = None
            self.phobert_tokenizer = None
            self.stderr.write("PhoBERT dependencies chua san sang - se bo qua mo hinh cuc bo.")
        else:
            try:
                self.phobert_tokenizer = AutoTokenizer.from_pretrained("vinai/phobert-base")
                self.phobert_model = AutoModel.from_pretrained("vinai/phobert-base").to(self.device)
                self.phobert_model.eval()
                self.stdout.write("PhoBERT da san sang.")
            except Exception as e:
                self.phobert_model = None
                self.phobert_tokenizer = None
                self.stderr.write(f"Loi khi tai PhoBERT: {e} - se bo qua mo hinh cuc bo.")
        self.stdout.write("Äang cáº¥u hÃ¬nh OpenAI client...")
        try:
            self.openai_client = openai.OpenAI()
            self.openai_client.models.list()
            self.stdout.write("âœ… OpenAI (ChatGPT & Whisper) Ä‘Ã£ sáºµn sÃ ng.")
        except Exception as e:
            self.openai_client = None
            self.stderr.write(f"Lá»—i cáº¥u hÃ¬nh OpenAI: {e} - Ä‘áº·t OPENAI_API_KEY trÆ°á»›c.")

    async def process_audio_and_transcribe(self, reply_channel, chunks, whisper_prompt: Optional[str] = None):
        if not chunks:
            logger.warning("KhÃ´ng cÃ³ chunk Ã¢m thanh nÃ o Ä‘á»ƒ xá»­ lÃ½.")
            return None
        first = chunks[0]
        if len(first) < 4 or not has_ebml_header(first[:4]):
            logger.error("CÃ¡c chunk khÃ´ng chá»©a EBML header. Dá»¯ liá»‡u audio khÃ´ng há»£p lá»‡.")
            await self.channel_layer.send(reply_channel, {'type': 'exam.error',
                                                          'message': 'Dá»¯ liá»‡u audio khÃ´ng há»£p lá»‡ (thiáº¿u header).'})
            return None
        unique_id = re.sub(r'[^a-zA-Z0-9]', '_', reply_channel)
        webm_path = os.path.join(settings.BASE_DIR, f'temp_audio_{unique_id}_{uuid4().hex[:8]}.webm')
        try:
            with open(webm_path, 'wb') as f:
                for c in chunks: f.write(c)
        except Exception as e:
            logger.error(f"Lá»—i ghi file táº¡m: {e}")
            await self.channel_layer.send(reply_channel,
                                          {'type': 'exam.error', 'message': 'Lá»—i há»‡ thá»‘ng khi ghi file Ã¢m thanh.'})
            return None
        wav_path = await asyncio.to_thread(convert_webm_to_wav, webm_path)
        if os.path.exists(webm_path): os.remove(webm_path)
        if not wav_path:
            await self.channel_layer.send(reply_channel, {'type': 'exam.error', 'message': 'Lá»—i xá»­ lÃ½ file Ã¢m thanh.'})
            return None
        duration, rms = wav_duration_and_rms(wav_path)
        logger.info(f"WAV duration ~ {duration:.2f}s; RMS ~ {rms:.1f}")

        # Kiá»ƒm tra RMS Ä‘á»ƒ phÃ¡t hiá»‡n silence (khÃ´ng cÃ³ Ã¢m thanh thá»±c)
        # NgÆ°á»¡ng RMS < 50.0 thÆ°á»ng lÃ  silence hoáº·c nhiá»…u ná»n
        RMS_THRESHOLD = 50.0
        if rms < RMS_THRESHOLD:
            logger.info(f"PhÃ¡t hiá»‡n silence (RMS={rms:.1f} < {RMS_THRESHOLD}). Tráº£ vá» 'KhÃ´ng cÃ³ Ã¢m thanh'.")
            await self.channel_layer.send(reply_channel,
                                          {'type': 'exam.error', 'message': 'KhÃ´ng cÃ³ Ã¢m thanh Ä‘Æ°á»£c phÃ¡t hiá»‡n.'})
            return None

        # Removed duration check - users can now submit audio of any length
        try:
            # Sá»­ dá»¥ng prompt context Ä‘á»ƒ giÃºp Whisper hiá»ƒu bá»‘i cáº£nh tá»‘t hÆ¡n
            context_prompt = whisper_prompt or ""
            if context_prompt:
                context_prompt = f"Bá»‘i cáº£nh: {context_prompt}. "

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
            logger.info(f"Transcript nháº­n Ä‘Æ°á»£c: '{raw}'")

            # Validation thÃªm
            if raw:
                # Kiá»ƒm tra náº¿u transcript quÃ¡ ngáº¯n
                words = raw.split()
                if len(words) < 3 and len(chunks) > 0:
                    logger.warning(
                        f"Transcript quÃ¡ ngáº¯n ({len(words)} tá»«) dÃ¹ cÃ³ dá»¯ liá»‡u audio. "
                        f"CÃ³ thá»ƒ lá»—i transcribe."
                    )

            # Náº¿u Whisper tráº£ vá» chuá»—i rá»—ng, coi nhÆ° khÃ´ng cÃ³ Ã¢m thanh
            if not raw:
                logger.info("Whisper tráº£ vá» chuá»—i rá»—ng. Tráº£ vá» 'KhÃ´ng cÃ³ Ã¢m thanh'.")
                await self.channel_layer.send(reply_channel,
                                              {'type': 'exam.error', 'message': 'KhÃ´ng cÃ³ Ã¢m thanh Ä‘Æ°á»£c phÃ¡t hiá»‡n.'})
                return None

            return raw
        except Exception as e:
            logger.error(f"Lá»—i Whisper: {e}")
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
                f"Tac vu 'main' thieu du lieu. session_id={session_id}, question_id={question_id}, chunks={len(chunks)}")
            return
        try:
            question_context = await _load_main_question_context(session_id, question_id)
            question_id_int = int(question_id)

            if not question_context["session_is_active"]:
                await self.channel_layer.send(reply_channel, {
                    'type': 'exam.error',
                    'message': 'Phien thi khong con hop le de nop cau tra loi.'
                })
                return

            if not question_context["exam_is_open"]:
                logger.warning(f"Exam time has ended for session {session_id}. Rejecting audio processing.")
                await self.channel_layer.send(reply_channel, {
                    'type': 'exam.error',
                    'message': 'Thoi gian thi da ket thuc. Khong the gui cau tra loi.'
                })
                return

            if question_context["already_answered"]:
                await self.channel_layer.send(reply_channel, {
                    'type': 'exam.error',
                    'message': 'Cau hoi nay da duoc nop truoc do.'
                })
                return

            ordered_question_ids = question_context["ordered_question_ids"]
            question_order = next((index + 1 for index, item_id in enumerate(ordered_question_ids) if item_id == question_id_int), None)
            question_label = f"Cau {question_order}" if question_order else f"Q{question_id}"
            raw_transcript = await self.process_audio_and_transcribe(reply_channel, chunks,
                                                                     whisper_prompt=question_context["question_text"])
            if raw_transcript is None or raw_transcript == "":
                return
            rephrased = await rephrase_text_with_chatgpt(raw_transcript, question_context["question_text"], self.openai_client)
            final_score, feedback = await score_student_answer_with_openai(rephrased, question_context["question_text"], self.openai_client)
            final_score = float(min(max(final_score, 0.0), 10.0))
            logger.info(f"Cham diem {question_label} -> Final={final_score:.2f}")
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
                    'message': 'Cau hoi nay da duoc nop truoc do.'
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
            logger.error(f"Loi khong mong muon trong process_main_question: {e}", exc_info=True)
            await self.channel_layer.send(reply_channel, {'type': 'exam.error', 'message': f'Loi worker: {str(e)}'})

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
                doc_text_parts.append(f"--- Nguá»“n: {m.title} ---\n{text[:5000]}")

            doc_text = "\n\n".join(doc_text_parts)

            easy = int(level_config.get("easy", 0))
            medium = int(level_config.get("medium", 0))
            hard = int(level_config.get("hard", 0))

            system_prompt = (
                "Báº¡n lÃ  má»™t chuyÃªn gia há»c thuáº­t ra Ä‘á» thi váº¥n Ä‘Ã¡p Ä‘áº¡i há»c.\n"
                "Nhiá»‡m vá»¥: Dá»±a vÃ o ná»™i dung tÃ i liá»‡u Ä‘Æ°á»£c cung cáº¥p, táº¡o cÃ¢u há»i Ä‘Ãºng vá»›i sá»‘ lÆ°á»£ng vÃ  má»©c Ä‘á»™ Ä‘Æ°á»£c yÃªu cáº§u.\n"
                "Äáº¦U RA Báº®T BUá»˜C: JSON object theo format "
                '{"questions": [{"content": "...", "difficulty": "EASY|MEDIUM|HARD", "source": "tÃªn tÃ i liá»‡u"}]}'
            )

            user_prompt = f"""
                Táº¡o tá»•ng cá»™ng {total_count} cÃ¢u há»i:
                - EASY: {easy}
                - MEDIUM: {medium}
                - HARD: {hard}

                Dá»±a trÃªn tÃ i liá»‡u sau:
                {doc_text}
                """

            if self.openai_client is None:
                raise RuntimeError("OpenAI client chÆ°a Ä‘Æ°á»£c khá»Ÿi táº¡o.")

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

                # ==== Báº®T BUá»˜C LÆ¯U VÃ€O DATABASE Vá»šI TIá»€N Tá» DRAFT_ ====
                new_q = await sync_to_async(Question.objects.create)(
                    subject=subject,
                    question_text=content,
                    difficulty=diff,
                    question_id_in_barem=f"DRAFT_AI_{uuid4().hex[:8]}"  # LÆ°u dÆ°á»›i dáº¡ng nhÃ¡p
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

            logger.info(f"ÄÃ£ sinh xong {len(formatted_questions)} cÃ¢u há»i cho job_id={job_id}")

        except Exception as exc:
            logger.error(f"Lá»—i khi sinh cÃ¢u há»i (job {job_id}): {exc}", exc_info=True)
            cache.set(cache_key, {
                "status": "FAIL",
                "progress": 100,
                "questions": [],
                "summary": {},
                "error_message": str(exc),
            }, timeout=1800)

    async def run(self):
        logger.info("Worker Ä‘ang láº¯ng nghe trÃªn kÃªnh 'asr-tasks'.")
        while True:
            message = await self.channel_layer.receive('asr-tasks')
            task_type = message.get('type')

            if task_type == 'ai.generate_questions':
                logger.info(f"Nháº­n lá»‡nh sinh cÃ¢u há»i AI, job_id={message.get('job_id')}")
                asyncio.create_task(self.process_generate_questions(message))
                continue

            reply_channel = message.get('reply_channel')
            if not reply_channel:
                continue

            if task_type == 'asr.stream.start':
                logger.info(f"Báº¯t Ä‘áº§u stream cho kÃªnh {reply_channel}")
                self.audio_chunks[reply_channel] = []
            elif task_type == 'asr.chunk':
                if reply_channel in self.audio_chunks:
                    self.audio_chunks[reply_channel].append(message.get('audio_chunk', b''))
            elif task_type == 'asr.stream.end':
                mode = message.get('mode') or 'main'
                logger.info(f"Káº¿t thÃºc stream, nháº­n lá»‡nh xá»­ lÃ½ '{mode}' cho kÃªnh {reply_channel}")

                chunks = self.audio_chunks.pop(reply_channel, [])
                if not chunks:
                    logger.warning(f"KhÃ´ng cÃ³ chunk audio nÃ o Ä‘á»ƒ xá»­ lÃ½ cho kÃªnh {reply_channel}. Bá» qua.")
                    continue

                message['__chunks'] = chunks
                asyncio.create_task(self.process_main_question(message))
            else:
                logger.warning(f"Bá» qua message khÃ´ng há»— trá»£: {task_type}")

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        self.stdout.write(self.style.SUCCESS('Starting AI Worker...'))
        try:
            asyncio.run(self.run())
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('Worker stopped by user.'))


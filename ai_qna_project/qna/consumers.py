import base64
import json
import logging

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from .exam_runtime import (
    EXAM_GRADING_STATUS_PENDING,
    is_exam_group_open,
    should_mark_lecturer_cheating_flag,
    store_exam_grading_state,
)
from .models import ExamSession, ViolationImage

logger = logging.getLogger(__name__)

ASR_TASK_CHANNEL = "asr-tasks"
CHUNK_LOG_INTERVAL = 25


def _is_truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

def _session_is_valid_for_exam(session):
    if not session:
        return False
    if session.session_status != "STARTED":
        return False
    if session.started_at is None:
        return False
    if session.completed_at is not None:
        return False
    if getattr(session, "verification_status", "ALLOW") not in {"ALLOW", "WARNING_ALLOW"}:
        return False
    if not session.questions.exists():
        return False

    exam_group = getattr(session, "exam_session_group_id", None) or getattr(session, "exam_session_group", None)
    return is_exam_group_open(exam_group)


class ExamConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return

        self.session_id = self.scope["url_route"]["kwargs"].get("session_id")
        if not self.session_id:
            await self.close(code=4404)
            return

        self.session = await self.get_session_by_id(self.session_id)
        if not self.session:
            await self.close(code=4404)
            return

        session_owner = getattr(self.session, "user_id", None)
        session_owner_pk = getattr(session_owner, "pk", session_owner)
        if session_owner_pk != getattr(user, "pk", None):
            await self.close(code=4403)
            return

        is_valid = await sync_to_async(_session_is_valid_for_exam)(self.session)
        if not is_valid:
            await self.close(code=4408)
            return

        self.audio_stream_started = False
        self.audio_chunk_count = 0
        self.current_audio_question_id = None

        self.room_group_name = f"exam_{self.session_id}"
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    @sync_to_async
    def _store_grading_state(self, session_id, question_id, **kwargs):
        return store_exam_grading_state(session_id, question_id, **kwargs)

    @sync_to_async
    def get_session_by_id(self, session_id):
        try:
            return (
                ExamSession.objects
                .select_related("exam_session_group_id", "user_id")
                .prefetch_related("questions")
                .get(pk=session_id)
            )
        except ExamSession.DoesNotExist:
            return None

    async def receive(self, text_data=None, bytes_data=None):
        if text_data:
            data = json.loads(text_data)

            message_type = data.get("type")
            action = data.get("action")

            normalized_action = None
            if action:
                normalized_action = action
            elif message_type == "asr.stream.start":
                normalized_action = "start_stream"
            elif message_type == "asr.stream.end":
                normalized_action = "end_stream"
            elif message_type == "proctor_violation":
                normalized_action = "violation"

            if normalized_action == "start_stream":
                question_id = data.get("question_id")
                audio_mime_type = data.get("audio_mime_type") or ""
                allow_after_expiry = _is_truthy(data.get("allow_after_expiry"))
                self.audio_stream_started = True
                self.audio_chunk_count = 0
                self.current_audio_question_id = question_id
                if question_id:
                    await self._store_grading_state(
                        self.session_id,
                        question_id,
                        status=EXAM_GRADING_STATUS_PENDING,
                        progress={
                            "stage": "queued",
                            "session_id": int(self.session_id),
                            "question_id": int(question_id),
                            "detail": "Đã gửi âm thanh tới hàng đợi chấm điểm.",
                        },
                    )
                logger.info(
                    "[WS->Redis] Gửi asr.stream.start: target=%s reply_channel=%s reply_group=%s session_id=%s question_id=%s audio_mime_type=%s allow_after_expiry=%s",
                    ASR_TASK_CHANNEL,
                    self.channel_name,
                    self.room_group_name,
                    self.session_id,
                    question_id,
                    audio_mime_type,
                    allow_after_expiry,
                )
                await self.channel_layer.send(
                    ASR_TASK_CHANNEL,
                    {
                        "type": "asr.stream.start",
                        "session_id": self.session_id,
                        "question_id": question_id,
                        "audio_mime_type": audio_mime_type,
                        "allow_after_expiry": allow_after_expiry,
                        "reply_channel": self.channel_name,
                        "reply_group": self.room_group_name,
                    },
                )
                return

            if normalized_action == "end_stream":
                question_id = data.get("question_id") or self.current_audio_question_id
                mode = data.get("mode", "main")
                audio_mime_type = data.get("audio_mime_type") or ""
                allow_after_expiry = _is_truthy(data.get("allow_after_expiry"))
                logger.info(
                    "[WS->Redis] Gửi asr.stream.end: target=%s reply_channel=%s reply_group=%s session_id=%s question_id=%s mode=%s chunk_count=%s stream_started=%s audio_mime_type=%s allow_after_expiry=%s",
                    ASR_TASK_CHANNEL,
                    self.channel_name,
                    self.room_group_name,
                    self.session_id,
                    question_id,
                    mode,
                    self.audio_chunk_count,
                    self.audio_stream_started,
                    audio_mime_type,
                    allow_after_expiry,
                )
                await self.channel_layer.send(
                    ASR_TASK_CHANNEL,
                    {
                        "type": "asr.stream.end",
                        "session_id": self.session_id,
                        "question_id": question_id,
                        "mode": mode,
                        "audio_mime_type": audio_mime_type,
                        "allow_after_expiry": allow_after_expiry,
                        "reply_channel": self.channel_name,
                        "reply_group": self.room_group_name,
                    },
                )
                self.audio_stream_started = False
                self.audio_chunk_count = 0
                self.current_audio_question_id = None
                return

            if normalized_action == "violation":
                violation_type = data.get("violation_type")
                image_data_url = data.get("image") or data.get("image_base64")
                if violation_type and image_data_url:
                    await self.save_violation_image(violation_type, image_data_url)
                    await self.mark_session_cheating(violation_type)
                return

            logger.warning("Unsupported websocket message: %s", data)
            return

        if bytes_data:
            self.audio_chunk_count += 1
            chunk_index = self.audio_chunk_count
            if not self.audio_stream_started:
                logger.warning(
                    "[WS->Redis] Nhận audio chunk trước start_stream: reply_channel=%s session_id=%s question_id=%s chunk_bytes=%s",
                    self.channel_name,
                    self.session_id,
                    self.current_audio_question_id,
                    len(bytes_data),
                )
            if chunk_index == 1 or chunk_index % CHUNK_LOG_INTERVAL == 0:
                logger.info(
                    "[WS->Redis] Gửi asr.chunk: target=%s reply_channel=%s session_id=%s question_id=%s chunk_index=%s chunk_bytes=%s stream_started=%s",
                    ASR_TASK_CHANNEL,
                    self.channel_name,
                    self.session_id,
                    self.current_audio_question_id,
                    chunk_index,
                    len(bytes_data),
                    self.audio_stream_started,
                )
            await self.channel_layer.send(
                ASR_TASK_CHANNEL,
                {
                    "type": "asr.chunk",
                    "audio_chunk": bytes_data,
                    "session_id": self.session_id,
                    "question_id": self.current_audio_question_id,
                    "reply_channel": self.channel_name,
                    "reply_group": getattr(self, "room_group_name", ""),
                },
            )

    @sync_to_async
    def save_violation_image(self, violation_type, image_data_url):
        try:
            header, imgstr = image_data_url.split(";base64,", 1)
            mime = header.split(":", 1)[1]
            image_data = base64.b64decode(imgstr)

            ViolationImage.objects.create(
                exam_session_id=self.session,
                image_mime=mime,
                violation_type=violation_type,
                image_blob=image_data,
            )
        except Exception:
            logger.exception("Error saving violation image for session %s", self.session_id)

    @sync_to_async
    def mark_session_cheating(self, violation_type):
        if not should_mark_lecturer_cheating_flag(violation_type) or self.session.cheating_flag:
            return
        self.session.cheating_flag = True
        self.session.save(update_fields=["cheating_flag"])

    async def exam_result(self, event):
        logger.info(
            "[Redis->WS] Gửi exam.result tới browser: session_id=%s question_id=%s result_type=%s",
            self.session_id,
            event.get("message", {}).get("data", {}).get("question_id"),
            event.get("message", {}).get("type"),
        )
        await self.send(text_data=json.dumps({"type": "exam.result", "message": event["message"]}))

    async def exam_error(self, event):
        logger.warning(
            "[Redis->WS] Gửi exam.error tới browser: session_id=%s message=%s",
            self.session_id,
            event.get("message"),
        )
        await self.send(text_data=json.dumps({"type": "exam.error", "message": event["message"]}))

    async def exam_progress(self, event):
        payload = event.get("message", {})
        logger.info(
            "[Redis->WS] Gửi exam.progress tới browser: session_id=%s question_id=%s stage=%s detail=%s",
            self.session_id,
            payload.get("question_id"),
            payload.get("stage"),
            payload.get("detail"),
        )
        await self.send(text_data=json.dumps({"type": "exam.progress", "message": payload}))

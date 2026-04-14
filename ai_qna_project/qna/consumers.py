import base64
import json
import logging

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

from .models import ExamSession, ViolationImage

logger = logging.getLogger(__name__)


def _is_exam_group_open(exam_group):
    if not exam_group or exam_group.status == "CANCELLED":
        return False
    now = timezone.now()
    start_at = getattr(exam_group, "start_at", None)
    end_at = getattr(exam_group, "end_at", None)
    if not start_at or not end_at:
        return False
    return start_at <= now <= end_at


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
    return _is_exam_group_open(exam_group)


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

        self.room_group_name = f"exam_{self.session_id}"
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

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
                await self.channel_layer.send(
                    "asr_worker",
                    {
                        "type": "asr.stream.start",
                        "session_id": self.session_id,
                        "question_id": data.get("question_id"),
                        "reply_channel": self.channel_name,
                    },
                )
                return

            if normalized_action == "end_stream":
                await self.channel_layer.send(
                    "asr_worker",
                    {
                        "type": "asr.stream.end",
                        "session_id": self.session_id,
                        "question_id": data.get("question_id"),
                        "mode": data.get("mode", "main"),
                        "reply_channel": self.channel_name,
                    },
                )
                return

            if normalized_action == "violation":
                violation_type = data.get("violation_type")
                image_data_url = data.get("image") or data.get("image_base64")
                if violation_type and image_data_url:
                    await self.save_violation_image(violation_type, image_data_url)
                    await self.mark_session_cheating()
                return

            logger.warning("Unsupported websocket message: %s", data)
            return

        if bytes_data:
            await self.channel_layer.send(
                "asr_worker",
                {
                    "type": "asr.chunk",
                    "audio_chunk": bytes_data,
                    "reply_channel": self.channel_name,
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
    def mark_session_cheating(self):
        self.session.cheating_flag = True
        self.session.save(update_fields=["cheating_flag"])

    async def exam_result(self, event):
        await self.send(text_data=json.dumps({"type": "exam.result", "message": event["message"]}))

    async def exam_error(self, event):
        await self.send(text_data=json.dumps({"type": "exam.error", "message": event["message"]}))
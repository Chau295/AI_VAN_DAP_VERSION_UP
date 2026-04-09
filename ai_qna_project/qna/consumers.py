import json
import base64

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth import get_user_model

from .models import ExamSession, ViolationImage

User = get_user_model()


class ExamConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        path_session_id = self.scope.get("url_route", {}).get("kwargs", {}).get("session_id")

        if path_session_id:
            self.session_id = str(path_session_id)
        else:
            query_string = self.scope["query_string"].decode()
            params = dict(q.split("=") for q in query_string.split("&") if "=" in q)
            self.session_id = params.get("session_id")

        if not self.session_id:
            await self.close(code=4001)
            return

        try:
            self.session = await sync_to_async(
                ExamSession.objects.select_related("user_id").get
            )(pk=self.session_id)
            self.user = self.session.user
            self.room_group_name = f"exam_{self.session_id}"
            self.reply_channel = self.channel_name

            await self.channel_layer.group_add(self.room_group_name, self.channel_name)
            await self.accept()
            print(f"User {self.user.username} connected for session {self.session_id}.")
        except ExamSession.DoesNotExist:
            await self.close(code=4004)

    async def disconnect(self, code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
            print(f"User disconnected from session {self.session_id}.")

    async def receive(self, text_data=None, bytes_data=None):
        # Forward audio chunks to ASR tasks
        if bytes_data:
            await self.channel_layer.send(
                "asr-tasks",
                {
                    "type": "asr.chunk",
                    "reply_channel": self.reply_channel,
                    "audio_chunk": bytes_data,
                },
            )
            return

        if text_data:
            try:
                data = json.loads(text_data)
                task_type = data.get("type")

                # Handle ASR Stream
                if task_type in ["asr.stream.start", "asr.stream.end"]:
                    message = {
                        "reply_channel": self.reply_channel,
                        **data,
                    }
                    await self.channel_layer.send("asr-tasks", message)

                # YÊU CẦU 3: Nhận sự kiện cảnh báo gian lận từ Client qua WebSocket
                elif task_type == "proctor_violation":
                    violation_type = data.get("violation_type", "TWO_FACES")
                    image_base64 = data.get("image_base64")
                    if image_base64:
                        await self.save_violation_image(violation_type, image_base64)
                        await self.channel_layer.group_send(
                            self.room_group_name,
                            {
                                "type": "exam.result",
                                "message": {
                                    "type": "proctor_violation_recorded",
                                    "data": {
                                        "violation_type": violation_type,
                                        "detected_count": data.get("detected_count", 0),
                                    },
                                },
                            },
                        )

            except json.JSONDecodeError:
                print(f"Received invalid JSON from client: {text_data}")

    @sync_to_async
    def save_violation_image(self, violation_type, image_base64):
        """Hàm lưu trữ ảnh gian lận đồng bộ vào CSDL"""
        try:
            # Tách header của chuỗi base64 nếu có
            if ";base64," in image_base64:
                format_part, imgstr = image_base64.split(";base64,")
                mime = format_part.split(":")[1]
            else:
                imgstr = image_base64
                mime = "image/jpeg"

            image_data = base64.b64decode(imgstr)

            # Tạo record hình ảnh gian lận
            ViolationImage.objects.create(
                exam_session_id=self.session,
                image_blob=image_data,
                image_mime=mime,
                violation_type=violation_type
            )

            # Cập nhật cờ gian lận vào phiên thi
            self.session.cheating_flag = True
            self.session.save(update_fields=['cheating_flag'])
            print(f"Recorded violation {violation_type} for session {self.session_id}")

        except Exception as e:
            print(f"Error saving violation image: {e}")

    async def exam_result(self, event):
        message = event["message"]
        await self.send(
            text_data=json.dumps(
                {
                    "type": "exam.result",
                    "message": message,
                }
            )
        )

    async def exam_error(self, event):
        message = event["message"]
        await self.send(
            text_data=json.dumps(
                {
                    "type": "exam.error",
                    "message": message,
                }
            )
        )

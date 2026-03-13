# qna/consumers.py
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from .models import ExamSession

User = get_user_model()


class ExamConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # Ưu tiên lấy session_id từ URL path (kwargs)
        path_session_id = self.scope.get('url_route', {}).get('kwargs', {}).get('session_id')

        if path_session_id:
            self.session_id = str(path_session_id)
        else:
            # Nếu không có trong path, quay lại lấy từ query string
            query_string = self.scope['query_string'].decode()
            params = dict(q.split('=') for q in query_string.split('&') if '=' in q)
            self.session_id = params.get('session_id')

        if not self.session_id:
            await self.close(code=4001)
            return

        try:
            self.session = await sync_to_async(ExamSession.objects.select_related('user').get)(pk=self.session_id)
            self.user = self.session.user

            self.room_group_name = f'exam_{self.session_id}'
            self.reply_channel = self.channel_name  # Dùng làm khóa cho worker

            await self.channel_layer.group_add(
                self.room_group_name,
                self.channel_name
            )
            await self.accept()
            print(f"User {self.user.username} connected for session {self.session_id}.")

        except ExamSession.DoesNotExist:
            await self.close(code=4004)

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )
            print(f"User disconnected from session {self.session_id}.")

    async def receive(self, text_data=None, bytes_data=None):
        # Xử lý dữ liệu nhị phân (audio blob) trước
        if bytes_data:
            # Chuyển tiếp audio blob đến worker dưới dạng 'asr.chunk'
            await self.channel_layer.send('asr-tasks', {
                'type': 'asr.chunk',
                'reply_channel': self.reply_channel,
                'audio_chunk': bytes_data,
            })
            return

        # Xử lý dữ liệu văn bản (lệnh JSON)
        if text_data:
            try:
                data = json.loads(text_data)
                task_type = data.get('type')

                # Chỉ chuyển tiếp các lệnh điều khiển stream
                if task_type in ['asr.stream.start', 'asr.stream.end']:
                    message = {
                        'reply_channel': self.reply_channel,
                        **data  # Gửi toàn bộ nội dung message
                    }
                    await self.channel_layer.send('asr-tasks', message)
            except json.JSONDecodeError:
                print(f"Received invalid JSON from client: {text_data}")

    async def exam_result(self, event):
        message = event['message']
        await self.send(text_data=json.dumps({
            'type': 'exam.result',
            'message': message
        }))

    async def exam_error(self, event):
        message = event['message']
        await self.send(text_data=json.dumps({
            'type': 'exam.error',
            'message': message
        }))
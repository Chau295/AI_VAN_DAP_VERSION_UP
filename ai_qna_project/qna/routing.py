# qna/routing.py
from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    # Route cũ, hỗ trợ ?session_id=<id>
    re_path(r"ws/exam/$", consumers.ExamConsumer.as_asgi()),

    # Route MỚI, hỗ trợ /ws/exam/<session_id>/
    re_path(r"ws/exam/(?P<session_id>\w+)/$", consumers.ExamConsumer.as_asgi()),
]
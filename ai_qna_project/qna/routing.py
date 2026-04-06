from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r"ws/exam/$", consumers.ExamConsumer.as_asgi()),
    re_path(r"ws/exam/(?P<session_id>\w+)/$", consumers.ExamConsumer.as_asgi()),
]
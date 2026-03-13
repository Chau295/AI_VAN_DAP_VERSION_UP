import os
from django.core.asgi import get_asgi_application

# BƯỚC 1: Thiết lập môi trường và gọi get_asgi_application() ĐẦU TIÊN.
# Dòng này sẽ tải tất cả các cài đặt của Django.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_qna_project.settings')
django_asgi_app = get_asgi_application()

# BƯỚC 2: BÂY GIỜ mới import các thành phần của Channels.
# Việc import bây giờ đã an toàn vì Django đã được cấu hình.
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
import qna.routing  # Import file routing của app qna

application = ProtocolTypeRouter({
    # Các kết nối HTTP vẫn dùng ứng dụng Django mặc định đã được tải ở trên
    "http": django_asgi_app,

    # Các kết nối WebSocket sẽ được xử lý bởi routing của Channels
    "websocket": AuthMiddlewareStack(
        URLRouter(
            qna.routing.websocket_urlpatterns
        )
    ),
})
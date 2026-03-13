# ai_qna_project/urls.py
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.staticfiles.urls import staticfiles_urlpatterns


urlpatterns = [
    path("admin/", admin.site.urls),

    # Bộ URL mặc định của Django auth (login, logout, password reset, ...)
    path("accounts/", include("django.contrib.auth.urls")),

    # URL của ứng dụng QnA
    path("", include("qna.urls")),
]

# Phục vụ media khi DEBUG = True
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# Phục vụ static khi DEBUG = True (hữu ích khi chạy bằng Daphne)
urlpatterns += staticfiles_urlpatterns()

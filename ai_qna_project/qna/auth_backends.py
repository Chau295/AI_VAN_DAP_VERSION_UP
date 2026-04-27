from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

from .models import UserProfile
from .lecturer_student_accounts import normalize_student_code


class StudentCodeOrUsernameBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        user_model = get_user_model()
        username = username or kwargs.get(user_model.USERNAME_FIELD)

        if not username or password is None:
            return None

        raw_username = str(username).strip()
        normalized_code = normalize_student_code(raw_username)

        # Ưu tiên kiểm tra username cho admin/giảng viên/sinh viên
        user = (
            user_model._default_manager.filter(username__iexact=raw_username).first()
            or user_model._default_manager.filter(username__iexact=normalized_code).first()
        )

        # Nếu không thấy username, tìm theo mã sinh viên trong UserProfile
        if user is None and normalized_code:
            profile = (
                UserProfile.objects.select_related("user_id")
                .filter(student_id__iexact=normalized_code)
                .first()
            )
            user = profile.user_id if profile else None

        if user and user.check_password(password) and self.user_can_authenticate(user):
            return user

        return None
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

from .models import UserProfile
from .student_accounts import normalize_student_code


class StudentCodeOrUsernameBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        user_model = get_user_model()
        username = username or kwargs.get(user_model.USERNAME_FIELD)
        if not username or password is None:
            return None

        normalized_code = normalize_student_code(username)

        user = (
            user_model._default_manager.filter(username__iexact=username).first()
            or user_model._default_manager.filter(username__iexact=normalized_code).first()
        )

        if user is None and normalized_code:
            profile = (
                # Đã sửa "user" thành "user_id"
                UserProfile.objects.select_related("user_id")
                .filter(student_id__iexact=normalized_code)
                .first()
            )
            # Đã sửa profile.user thành profile.user_id
            user = profile.user_id if profile else None

        if user and user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
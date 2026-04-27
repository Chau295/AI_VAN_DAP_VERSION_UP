from functools import wraps

from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect

from .models import Subject, ExamSession


def get_user_profile(user):
    return getattr(user, "userprofile", None)


def is_lecturer(user) -> bool:
    profile = get_user_profile(user)
    return bool(user and user.is_authenticated and profile and profile.is_lecturer)


def is_student(user) -> bool:
    profile = get_user_profile(user)
    return bool(user and user.is_authenticated and profile and not profile.is_lecturer)


def lecturer_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not is_lecturer(request.user):
            return redirect("qna:dashboard")
        return view_func(request, *args, **kwargs)
    return _wrapped


def student_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not is_student(request.user):
            return redirect("qna:lecturer_dashboard")
        return view_func(request, *args, **kwargs)
    return _wrapped


def lecturer_required_json(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not is_lecturer(request.user):
            return JsonResponse(
                {"success": False, "error": "Không có quyền truy cập."},
                status=403,
            )
        return view_func(request, *args, **kwargs)
    return _wrapped


def student_required_json(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not is_student(request.user):
            return JsonResponse(
                {"success": False, "error": "Không có quyền truy cập."},
                status=403,
            )
        return view_func(request, *args, **kwargs)
    return _wrapped


def get_lecturer_subject_or_404(user, *, subject_code=None, subject_id=None):
    """
    Chỉ trả về môn học nếu giảng viên hiện tại được phân công dạy môn đó.
    Dùng thay cho get_object_or_404(Subject, ...).
    """
    profile = get_user_profile(user)
    if not profile or not profile.is_lecturer:
        raise Subject.DoesNotExist

    qs = profile.subjects_taught.all()

    if subject_code is not None:
        return get_object_or_404(qs, subject_code=subject_code)

    if subject_id is not None:
        return get_object_or_404(qs, pk=subject_id)

    return get_object_or_404(qs)


def lecturer_can_access_subject(user, subject) -> bool:
    profile = get_user_profile(user)
    if not profile or not profile.is_lecturer or not subject:
        return False
    return profile.subjects_taught.filter(pk=subject.pk).exists()


def student_can_access_subject(user, subject) -> bool:
    profile = get_user_profile(user)
    if not profile or profile.is_lecturer or not subject:
        return False
    return profile.subjects_enrolled.filter(pk=subject.pk).exists()


def user_can_access_exam_session(user, session: ExamSession) -> bool:
    """
    Giảng viên: được xem nếu dạy đúng môn của session.
    Sinh viên: được xem nếu session thuộc chính user đó.
    """
    profile = get_user_profile(user)
    if not profile or not session:
        return False

    if profile.is_lecturer:
        subject_pk = getattr(session, "subject_id_id", None)
        return profile.subjects_taught.filter(pk=subject_pk).exists()

    user_pk = getattr(session, "user_id_id", None)
    return user_pk == user.pk


def forbid_if_no_exam_session_access(user, session):
    if not user_can_access_exam_session(user, session):
        return HttpResponseForbidden("Không có quyền truy cập bài thi này.")
    return None
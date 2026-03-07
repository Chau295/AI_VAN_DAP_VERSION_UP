from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

from .models import (
    Subject,
    Question,
    ExamSession,
    ExamResult,
    SupplementaryResult,
    UserProfile,
    ExamCode,
    LectureMaterial,
    ExamRoom,
    ExamSessionGroup,
    ExamSessionRoom,
)


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = "Hồ sơ sinh viên"
    fk_name = "user"


class UserAdmin(BaseUserAdmin):
    inlines = (UserProfileInline,)


admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ("name", "subject_code", "exam_password")
    search_fields = ("name", "subject_code")


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = (
        "question_text",
        "subject",
        "question_id_in_barem",
        "difficulty",
        "is_supplementary",
    )
    list_filter = ("subject", "difficulty", "is_supplementary")
    search_fields = ("question_text", "subject__name", "question_id_in_barem")


@admin.register(ExamCode)
class ExamCodeAdmin(admin.ModelAdmin):
    list_display = (
        "code_name",
        "subject",
        "question_easy",
        "question_medium",
        "question_hard",
        "is_approved",
        "created_at",
    )
    list_filter = ("subject", "is_approved")
    search_fields = ("code_name", "subject__name", "source_material")


@admin.register(LectureMaterial)
class LectureMaterialAdmin(admin.ModelAdmin):
    list_display = ("title", "subject", "file_type", "uploaded_at")
    list_filter = ("subject", "file_type")
    search_fields = ("title", "subject__name", "file_path")


@admin.register(ExamRoom)
class ExamRoomAdmin(admin.ModelAdmin):
    list_display = ("room_name", "room_code", "capacity")
    search_fields = ("room_name", "room_code")


class ExamSessionRoomInline(admin.TabularInline):
    model = ExamSessionRoom
    extra = 0


@admin.register(ExamSessionGroup)
class ExamSessionGroupAdmin(admin.ModelAdmin):
    list_display = (
        "group_name",
        "subject",
        "exam_date",
        "duration_minutes",
        "status",
        "created_by",
        "created_at",
    )
    list_filter = ("subject", "status")
    search_fields = ("group_name", "subject__name")
    filter_horizontal = ("exam_codes",)
    inlines = [ExamSessionRoomInline]


@admin.register(ExamSessionRoom)
class ExamSessionRoomAdmin(admin.ModelAdmin):
    list_display = ("exam_group", "room")
    search_fields = ("exam_group__group_name", "room__room_name")
    filter_horizontal = ("students",)


@admin.register(ExamSession)
class ExamSessionAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "subject",
        "exam_group",
        "created_at",
        "is_completed",
        "final_score",
        "verification_status",
    )
    list_filter = ("subject", "is_completed", "user", "verification_status")
    search_fields = ("user__username", "subject__name")
    date_hierarchy = "created_at"
    filter_horizontal = ("questions",)


@admin.register(ExamResult)
class ExamResultAdmin(admin.ModelAdmin):
    list_display = ("session", "question", "score", "answered_at")
    list_filter = ("session__subject",)
    search_fields = ("session__user__username", "question__question_text")


@admin.register(SupplementaryResult)
class SupplementaryResultAdmin(admin.ModelAdmin):
    list_display = ("session", "question_text", "score", "max_score", "created_at")
    list_filter = ("session__subject",)
    search_fields = ("session__user__username", "question_text")
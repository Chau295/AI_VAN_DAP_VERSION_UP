from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

from .models import (
    Subject,
    UserProfile,
    QuestionBank,
    Question,
    ExamSet,
    ExamCode,
    ExamCodeQuestion,
    LectureMaterial,
    ExamSessionGroup,
    ExamSessionRoom,
    ExamSession,
    ExamResult,
    ViolationImage,
    StudentRosterUpload,
    StudentRosterStudent,
)

class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = "Hồ sơ người dùng"
    fk_name = "user_id"


class UserAdmin(BaseUserAdmin):
    inlines = (UserProfileInline,)


admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ("subject_id", "name", "subject_code", "exam_password")
    search_fields = ("name", "subject_code")
    ordering = ("name",)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user_profile_id",
        "user_id",
        "full_name",
        "student_id",
        "class_name",
        "is_lecturer",
    )
    search_fields = (
        "user_id__username",
        "full_name",
        "student_id",
    )
    list_filter = ("is_lecturer",)


@admin.register(QuestionBank)
class QuestionBankAdmin(admin.ModelAdmin):
    list_display = ("question_bank_id", "name", "subject_id", "created_at")
    list_filter = ("subject_id",)
    search_fields = ("name",)


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = (
        "question_id",
        "subject_id",
        "question_bank_id",
        "difficulty",
        "created_at",
    )
    list_filter = ("subject_id", "difficulty", "question_bank_id")
    search_fields = ("question_text",)


class ExamCodeQuestionInline(admin.TabularInline):
    model = ExamCodeQuestion
    extra = 1


@admin.register(ExamSet)
class ExamSetAdmin(admin.ModelAdmin):
    list_display = ("exam_set_id", "name", "subject_id", "status", "created_by_user_id", "created_at")
    list_filter = ("subject_id", "status")
    search_fields = ("name",)


@admin.register(ExamCode)
class ExamCodeAdmin(admin.ModelAdmin):
    list_display = ("exam_code_id", "code_name", "exam_set_id", "created_at")
    list_filter = ("exam_set_id__subject_id", "exam_set_id")
    search_fields = ("code_name",)
    inlines = [ExamCodeQuestionInline]


@admin.register(LectureMaterial)
class LectureMaterialAdmin(admin.ModelAdmin):
    list_display = ("lecture_material_id", "title", "subject_id", "uploaded_at")
    list_filter = ("subject_id",)
    search_fields = ("title",)


class ExamSessionRoomInline(admin.TabularInline):
    model = ExamSessionRoom
    extra = 1


@admin.register(ExamSessionGroup)
class ExamSessionGroupAdmin(admin.ModelAdmin):
    list_display = ("exam_session_group_id", "group_name", "subject_id", "exam_date", "status")
    list_filter = ("status", "subject_id")
    search_fields = ("group_name",)
    inlines = [ExamSessionRoomInline]


@admin.register(ExamSession)
class ExamSessionAdmin(admin.ModelAdmin):
    list_display = ("exam_session_id", "user_id", "exam_session_group_id", "session_status", "started_at")
    list_filter = ("session_status", "verification_status")
    search_fields = ("user_id__username", "user_id__userprofile__full_name")


@admin.register(ExamResult)
class ExamResultAdmin(admin.ModelAdmin):
    list_display = ("exam_result_id", "exam_session_id", "question_id", "score", "answered_at")
    list_filter = ("exam_session_id__exam_session_group_id",)
    search_fields = ("exam_session_id__user_id__username",)


@admin.register(ViolationImage)
class ViolationImageAdmin(admin.ModelAdmin):
    list_display = ("violation_image_id", "exam_session_id", "violation_type", "timestamp")
    list_filter = ("violation_type", "timestamp")


class StudentRosterStudentInline(admin.TabularInline):
    model = StudentRosterStudent
    extra = 0
    fields = (
        "row_number",
        "student_code",
        "full_name",
        "class_name",
        "email",
        "linked_user_id",
        "account_created",
        "account_status",
    )
    readonly_fields = ("created_at",)


@admin.register(StudentRosterUpload)
class StudentRosterUploadAdmin(admin.ModelAdmin):
    list_display = (
        "student_roster_upload_id",
        "title",
        "subject_id",
        "academic_year",
        "semester",
        "total_students",
        "status",
        "created_by_user_id",
        "created_at",
        "account_created_at",
    )
    search_fields = (
        "title",
        "original_file_name",
        "subject_id__name",
        "subject_id__subject_code",
    )
    list_filter = ("subject_id", "academic_year", "semester", "status")
    inlines = [StudentRosterStudentInline]
    ordering = ("-created_at",)


@admin.register(StudentRosterStudent)
class StudentRosterStudentAdmin(admin.ModelAdmin):
    list_display = (
        "student_roster_student_id",
        "student_roster_upload_id",
        "row_number",
        "student_code",
        "full_name",
        "class_name",
        "linked_user_id",
        "account_created",
        "account_status",
        "created_at",
    )
    search_fields = (
        "student_code",
        "full_name",
        "class_name",
    )
    list_filter = ("account_status", "account_created")
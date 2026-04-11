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
    ExamRoom,
    ExamSessionGroup,
    ExamSessionRoom,
    ExamSession,
    ExamResult,
    ViolationImage,  # ÄÃ£ thÃªm model nÃ y Ä‘á»ƒ quáº£n lÃ½ áº£nh gian láº­n
    StudentRosterUpload,
    StudentRosterStudent,
)


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = "Há»“ sÆ¡ ngÆ°á»i dÃ¹ng"
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
        "class_name",
    )
    list_filter = ("is_lecturer",)
    filter_horizontal = ("subjects_taught",)


@admin.register(QuestionBank)
class QuestionBankAdmin(admin.ModelAdmin):
    list_display = ("question_bank_id", "name", "subject_id", "created_at")
    search_fields = ("name", "subject_id__name", "subject_id__subject_code")
    list_filter = ("subject_id",)
    ordering = ("-created_at",)


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = (
        "question_id",
        "short_text",
        "subject_id",
        "question_bank_id",
        "question_id_in_barem",
        "difficulty",
        "is_exam_clone",
        "is_draft",
        "created_at",
    )
    list_filter = (
        "subject_id",
        "question_bank_id",
        "difficulty",
        "is_exam_clone",
        "is_draft",
    )
    search_fields = (
        "question_text",
        "question_text_normalized",
        "subject_id__name",
        "subject_id__subject_code",
        "question_id_in_barem",
    )
    ordering = ("question_id_in_barem", "question_id")


class ExamCodeInline(admin.TabularInline):
    model = ExamCode
    extra = 0
    fields = ("exam_code_id", "code_name", "code_number", "is_approved", "created_at")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ExamSet)
class ExamSetAdmin(admin.ModelAdmin):
    list_display = (
        "exam_set_id",
        "display_name",
        "subject_id",
        "academic_year",
        "semester",
        "number_of_versions",
        "status",
        "created_by_user_id",
        "created_at",
        "updated_at",
    )
    search_fields = (
        "name",
        "subject_id__name",
        "subject_id__subject_code",
        "academic_year",
    )
    list_filter = ("subject_id", "semester", "status")
    filter_horizontal = ("question_banks",)
    inlines = [ExamCodeInline]


@admin.register(ExamCode)
class ExamCodeAdmin(admin.ModelAdmin):
    list_display = (
        "exam_code_id",
        "code_name",
        "code_number",
        "subject_id",
        "exam_set_id",
        "is_approved",
        "created_at",
        "updated_at",
    )
    search_fields = (
        "code_name",
        "subject_id__name",
        "subject_id__subject_code",
        "exam_set_id__name",
    )
    list_filter = ("subject_id", "is_approved")
    ordering = ("code_number", "created_at")


@admin.register(ExamCodeQuestion)
class ExamCodeQuestionAdmin(admin.ModelAdmin):
    list_display = (
        "exam_code_question_id",
        "exam_code_id",
        "question_id",
        "difficulty",
        "display_order",
        "score",
    )
    search_fields = (
        "exam_code_id__code_name",
        "question_id__question_text",
        "question_id__question_id_in_barem",
    )
    list_filter = ("difficulty", "exam_code_id__subject_id")
    ordering = ("exam_code_id", "display_order", "exam_code_question_id")


@admin.register(LectureMaterial)
class LectureMaterialAdmin(admin.ModelAdmin):
    list_display = (
        "lecture_material_id",
        "title",
        "subject_id",
        "question_bank_id",
        "file_type",
        "uploaded_at",
        "workspace_id",
    )
    list_filter = ("subject_id", "file_type")
    search_fields = (
        "title",
        "subject_id__name",
        "subject_id__subject_code",
        "file_path",
        "workspace_id",
    )
    ordering = ("-uploaded_at",)


@admin.register(ExamRoom)
class ExamRoomAdmin(admin.ModelAdmin):
    list_display = ("exam_room_id", "room_name", "room_code", "capacity")
    search_fields = ("room_name", "room_code")
    ordering = ("room_code",)


class ExamSessionRoomInline(admin.TabularInline):
    model = ExamSessionRoom
    extra = 0
    fields = (
        "exam_session_room_id",
        "exam_room_id",
        "room_name",
        "expected_students",
        "room_password",
        "exam_set_id",
        "display_order",
    )


@admin.register(ExamSessionGroup)
class ExamSessionGroupAdmin(admin.ModelAdmin):
    list_display = (
        "exam_session_group_id",
        "group_name",
        "subject_id",
        "academic_year",
        "semester",
        "exam_date",
        "duration_minutes",
        "status",
        "created_by_user_id",
        "created_at",
    )
    list_filter = ("subject_id", "semester", "status")
    search_fields = (
        "group_name",
        "subject_id__name",
        "subject_id__subject_code",
        "academic_year",
    )
    filter_horizontal = ("exam_codes",)
    inlines = [ExamSessionRoomInline]


@admin.register(ExamSessionRoom)
class ExamSessionRoomAdmin(admin.ModelAdmin):
    list_display = (
        "exam_session_room_id",
        "exam_session_group_id",
        "exam_room_id",
        "room_name",
        "expected_students",
        "exam_set_id",
        "display_order",
    )
    search_fields = (
        "exam_session_group_id__group_name",
        "exam_room_id__room_name",
        "room_name",
    )
    filter_horizontal = ("exam_codes", "students")
    ordering = ("exam_session_group_id", "display_order", "exam_session_room_id")


@admin.register(ExamSession)
class ExamSessionAdmin(admin.ModelAdmin):
    list_display = (
        "exam_session_id",
        "user_id",
        "subject_id",
        "exam_session_group_id",
        "created_at",
        "is_completed",
        "final_score",
        "verification_status",
        "cheating_flag",
    )
    list_filter = ("subject_id", "session_status", "verification_status", "cheating_flag")
    search_fields = ("user_id__username", "subject_id__name", "subject_id__subject_code")
    date_hierarchy = "created_at"
    filter_horizontal = ("questions",)
    ordering = ("-created_at",)


@admin.register(ExamResult)
class ExamResultAdmin(admin.ModelAdmin):
    list_display = ("exam_result_id", "exam_session_id", "question_id", "score", "answered_at")
    list_filter = ("exam_session_id__subject_id",)
    search_fields = ("exam_session_id__user_id__username", "question_id__question_text")
    ordering = ("-answered_at",)


# ÄÄƒng kÃ½ hiá»ƒn thá»‹ áº£nh gian láº­n trong admin
@admin.register(ViolationImage)
class ViolationImageAdmin(admin.ModelAdmin):
    list_display = ("violation_image_id", "exam_session_id", "violation_type", "timestamp")
    list_filter = ("violation_type",)
    search_fields = ("exam_session_id__user_id__username",)
    ordering = ("-timestamp",)


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
        "email",
        "linked_user_id__username",
    )
    list_filter = ("account_created", "account_status", "student_roster_upload_id__subject_id")
    ordering = ("student_roster_upload_id", "row_number", "student_roster_student_id")

from django.conf import settings
from django.conf.urls.static import static
from django.urls import path
from django.views.generic import RedirectView

from . import views

app_name = "qna"

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="qna:dashboard", permanent=False), name="root"),

    path("student/dashboard/", views.dashboard_view, name="dashboard"),
    path("student/exam_page/<str:subject_code>/", views.exam_view, name="exam_page"),
    path("student/exam_page/<str:subject_code>/password/", views.exam_password_view, name="exam_password"),
    path("student/exam_page/<str:subject_code>/password/verify/", views.verify_exam_password, name="verify_exam_password"),
    path("student/exam_page/<str:subject_code>/verify/", views.pre_exam_verification_view, name="pre_exam_verification"),

    path("profile/", views.profile_view, name="profile"),
    path("profile/update-image/", views.update_profile_image, name="update_profile_image"),

    path("history/", views.history_view, name="history"),
    path("history/<int:session_id>/", views.history_detail_view, name="history_detail"),

    # APIs
    path("api/save_exam_result/", views.save_exam_result, name="save_exam_result"),
    path("api/save_supplementary_result/", views.save_supplementary_result, name="save_supplementary_result"),
    path("api/get_supplementary/<int:session_id>/", views.get_supplementary_for_session, name="get_supplementary_for_session"),
    path("api/finalize_session/<int:session_id>/", views.finalize_session_view, name="finalize_session"),

    # Face Verification APIs
    path("api/verify-face/", views.verify_student_face, name="verify_face"),
    path("api/verification-images/<int:session_id>/", views.get_verification_images, name="verification_images"),

    path("post-login-redirect/", views.post_login_redirect, name="post_login_redirect"),

    # ===========================
    # NEW LECTURER INTERFACE
    # ===========================

    # 1. Dashboard
    path("lecturer/dashboard/", views.lecturer_dashboard, name="lecturer_dashboard"),

    # 2. Môn học
    path("lecturer/subjects/", views.lecturer_subject_list, name="lecturer_subject_list"),
    path("lecturer/subjects/<str:subject_code>/", views.lecturer_subject_workspace, name="lecturer_subject_workspace"),
    path("lecturer/subjects/<str:subject_code>/dashboard/", views.lecturer_subject_dashboard, name="lecturer_subject_dashboard"),

    path("lecturer/subjects/<str:subject_code>/questions/", views.lecturer_question_management, name="lecturer_question_management"),
    path("lecturer/subjects/<str:subject_code>/generate-codes/", views.lecturer_generate_exam_codes, name="lecturer_generate_exam_codes"),
    path("lecturer/subjects/<str:subject_code>/create-session/", views.lecturer_create_exam_session, name="lecturer_create_exam_session"),
    path("lecturer/subjects/<str:subject_code>/student-review/", views.lecturer_student_review, name="lecturer_student_review"),
    path("lecturer/subjects/<str:subject_code>/export-results/", views.lecturer_export_exam_results, name="lecturer_export_exam_results"),

    # 3. Quản lý câu hỏi
    path("lecturer/questions/", views.lecturer_questions_screen, name="lecturer_questions_screen"),

    # Route phụ cho màn mới nhưng dùng chung logic
    path("lecturer/question-management/", views.question_bank_list_screen, name="question_bank_list_screen"),
    path("lecturer/question-management/detail/", views.question_bank_detail_screen, name="question_bank_detail_screen"),

    # API quản lý câu hỏi cũ
    path("lecturer/api/question/create/", views.lecturer_create_question, name="lecturer_create_question"),
    path("lecturer/api/question/<int:question_id>/update/", views.lecturer_update_question, name="lecturer_update_question"),
    path("lecturer/api/question/<int:question_id>/delete/", views.lecturer_delete_question, name="lecturer_delete_question"),
    path("lecturer/api/question/import/", views.lecturer_import_questions, name="lecturer_import_questions"),

    # API quản lý câu hỏi mới
    path("api/lecturer/subjects/", views.api_get_lecturer_subjects, name="api_get_lecturer_subjects"),
    path("api/lecturer/question-banks/", views.api_get_question_banks, name="api_get_question_banks"),
    path("api/lecturer/question-banks/create/", views.api_create_question_bank, name="api_create_question_bank"),
    path("api/lecturer/question-banks/<int:bank_id>/save/", views.api_save_question_bank_questions, name="api_save_question_bank_questions"),
    path("api/lecturer/question-banks/<int:bank_id>/delete/", views.api_delete_question_bank, name="api_delete_question_bank"),

    path("api/lecturer/materials/presign/", views.api_material_presign, name="api_material_presign"),
    path("api/lecturer/materials/upload-complete/", views.api_material_upload_complete, name="api_material_upload_complete"),
    path("api/lecturer/materials/", views.api_get_materials, name="api_get_materials"),

    path("api/lecturer/questions/", views.api_get_questions, name="api_get_questions"),
    path("api/lecturer/questions/manual/", views.api_create_manual_question, name="api_create_manual_question"),
    path("api/lecturer/questions/<int:question_id>/update/", views.api_update_question_bank_question, name="api_update_question_v2"),
    path("api/lecturer/questions/<int:question_id>/delete/", views.api_delete_question_bank_question, name="api_delete_question_v2"),
    path("api/lecturer/questions/bulk-level/", views.api_bulk_update_question_level, name="api_bulk_update_question_level"),
    path("api/lecturer/questions/bulk-delete/", views.api_bulk_delete_questions, name="api_bulk_delete_questions"),

    path("api/lecturer/generate-questions/", views.api_generate_questions, name="api_generate_questions"),
    path("api/lecturer/generate-questions/status/", views.api_generate_questions_status, name="api_generate_questions_status"),

    path("lecturer/api/material/<int:material_id>/delete/", views.lecturer_delete_material, name="lecturer_delete_material"),

    # 3.5 Quản lý Mã đề thi
    path("lecturer/exam-codes/", views.lecturer_exam_codes_screen, name="lecturer_exam_codes_screen"),

    # 4. Sinh mã đề bằng AI
    path("lecturer/generate-codes/", views.lecturer_generate_codes_screen, name="lecturer_generate_codes_screen"),
    path("lecturer/api/material/upload/", views.lecturer_upload_material_screen, name="lecturer_upload_material_screen"),
    path("lecturer/api/generate-codes-ai/", views.lecturer_generate_codes_with_ai, name="lecturer_generate_codes_with_ai"),
    path("lecturer/api/exam-code/<int:exam_code_id>/approve/", views.lecturer_approve_exam_code, name="lecturer_approve_exam_code"),
    path("lecturer/api/exam-code/<int:exam_code_id>/edit-question/", views.lecturer_edit_exam_code_question, name="lecturer_edit_exam_code_question"),
    path("lecturer/api/exam-code/<int:exam_code_id>/update/", views.lecturer_update_exam_code_question, name="lecturer_update_exam_code_question"),
    path("lecturer/api/exam-code/<int:exam_code_id>/delete/", views.lecturer_delete_exam_code, name="lecturer_delete_exam_code"),

    # 5. Tạo ca thi
    path("lecturer/create-session/", views.lecturer_create_session_screen, name="lecturer_create_session_screen"),
    path("lecturer/api/exam-group/create/", views.lecturer_create_exam_group_screen, name="lecturer_create_exam_group_screen"),
    path("lecturer/api/exam-group/<int:exam_group_id>/update/", views.lecturer_update_exam_group, name="lecturer_update_exam_group"),
    path("lecturer/api/session-room/<int:session_room_id>/import-students/", views.lecturer_import_students_to_room, name="lecturer_import_students_to_room"),
    path("lecturer/api/exam-group/<int:exam_group_id>/random-assign/", views.lecturer_random_assign_students, name="lecturer_random_assign_students"),

    # 6. Danh sách ca thi
    path("lecturer/exam-sessions/", views.lecturer_exam_sessions_list, name="lecturer_exam_sessions_list"),

    # 7. Xem bài làm sinh viên
    path("lecturer/student-review/", views.lecturer_student_review_screen, name="lecturer_student_review_screen"),
    path("lecturer/session/<int:session_id>/detail/", views.lecturer_session_detail, name="lecturer_session_detail"),

    # 8. Xuất báo cáo
    path("lecturer/export-reports/", views.lecturer_export_reports_screen, name="lecturer_export_reports_screen"),
    path("lecturer/api/export-results/", views.lecturer_export_exam_results_screen, name="lecturer_export_exam_results_screen"),

    # 9. Quản lý Phòng
    path("lecturer/manage-rooms/", views.lecturer_manage_rooms, name="lecturer_manage_rooms"),
    path("lecturer/api/room/create/", views.lecturer_create_room, name="lecturer_create_room"),
    path("lecturer/api/room/<int:room_id>/delete/", views.lecturer_delete_room, name="lecturer_delete_room"),

    #10. Profile giảng viên
    # === BẠN PHẢI CHẮC CHẮN ĐÃ THÊM DÒNG NÀY ===
    path("lecturer/profile/", views.lecturer_profile_view, name="lecturer_profile"),

    #11. Export word
    path("api/lecturer/export-questions-word/",views.lecturer_export_questions_word,name="lecturer_export_questions_word"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
import json
import os
import re
import shutil
import tempfile
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import views
from .models import (
    DifficultyLevel,
    ExamCode,
    ExamCodeQuestion,
    ExamResult,
    ExamSession,
    ExamSessionGroup,
    ExamSessionRoom,
    ExamSet,
    ExamSetStatus,
    LectureMaterial,
    Question,
    QuestionBank,
    StudentRosterAccountStatus,
    StudentRosterStudent,
    StudentRosterUpload,
    Subject,
    UserProfile,
    resolve_audio_mime_type,
)

TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "student-roster-tests",
    }
}

TEST_CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}


class LecturerStudentRosterUploadTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(dir=os.path.dirname(__file__))
        self.override = override_settings(
            MEDIA_ROOT=self.media_root,
            CACHES=TEST_CACHES,
            CHANNEL_LAYERS=TEST_CHANNEL_LAYERS,
        )
        self.override.enable()

        self.user_model = get_user_model()
        self.lecturer = self.user_model.objects.create_user(
            username="lecturer01",
            password="Password123!",
        )
        self.profile = UserProfile.objects.create(
            user=self.lecturer,
            is_lecturer=True,
            full_name="Giang Vien",
        )
        self.subject = Subject.objects.create(
            name="Kho va khai pha du lieu",
            subject_code="DS401",
        )
        self.profile.subjects_taught.add(self.subject)
        self.client.force_login(self.lecturer)

    def _upload_roster(self, rows, file_name="Danh_sach_L01.csv"):
        upload = SimpleUploadedFile(
            file_name,
            (
                    "student_code,full_name,gender,date_of_birth,class_name,email\n"
                    + "\n".join(rows)
                    + "\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("qna:lecturer_student_list_upload", args=[self.subject.subject_code]),
            {
                "academic_year": "2024-2025",
                "semester": "HK1",
                "files": upload,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 200)
        return StudentRosterUpload.objects.get(subject_id=self.subject)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_ajax_upload_creates_roster_and_students(self):
        upload = SimpleUploadedFile(
            "Danh_sach_L01.csv",
            (
                "student_code,full_name,gender,date_of_birth,class_name,email\n"
                "SV001,Nguyen Van A,Nam,12/05/2003,CNT01,sv001@example.com\n"
                "SV002,Tran Thi B,Nu,24/08/2003,CNT01,sv002@example.com\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("qna:lecturer_student_list_upload", args=[self.subject.subject_code]),
            {
                "academic_year": "2024-2025",
                "semester": "HK1",
                "files": upload,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertTrue(payload["success"])
        self.assertEqual(payload["uploaded_count"], 1)
        self.assertEqual(len(payload["results"]), 1)

        roster = StudentRosterUpload.objects.get(subject_id=self.subject)
        self.assertEqual(roster.academic_year, "2024-2025")
        self.assertEqual(roster.semester, "HK1")
        self.assertEqual(roster.total_students, 2)
        self.assertEqual(StudentRosterStudent.objects.filter(student_roster_upload_id=roster).count(), 2)
        self.assertEqual(payload["results"][0]["total_students"], 2)

    def test_ajax_upload_rejects_invalid_academic_year_with_letters(self):
        upload = SimpleUploadedFile(
            "Danh_sach_L01.csv",
            (
                "student_code,full_name,gender,date_of_birth,class_name,email\n"
                "SV001,Nguyen Van A,Nam,12/05/2003,CNT01,sv001@example.com\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("qna:lecturer_student_list_upload", args=[self.subject.subject_code]),
            {
                "academic_year": "2025-20a6",
                "semester": "HK1",
                "files": upload,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["message"], "Năm học phải đúng định dạng xxxx-xxxx và chỉ được chứa chữ số.")
        self.assertEqual(StudentRosterUpload.objects.count(), 0)

    def test_ajax_upload_rejects_invalid_extension(self):
        upload = SimpleUploadedFile(
            "danh_sach_sinh_vien.pdf",
            b"fake-pdf-content",
            content_type="application/pdf",
        )

        response = self.client.post(
            reverse("qna:lecturer_student_list_upload", args=[self.subject.subject_code]),
            {
                "academic_year": "2024-2025",
                "semester": "HK1",
                "files": upload,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()

        self.assertFalse(payload["success"])
        self.assertEqual(payload["uploaded_count"], 0)
        self.assertEqual(StudentRosterUpload.objects.count(), 0)
        self.assertEqual(len(payload["errors"]), 1)
        self.assertIn("excel", payload["errors"][0]["message"].lower())

    def test_batch_create_accounts_creates_loginable_student_accounts(self):
        roster = self._upload_roster([
            "SV001,Nguyen Van A,Nam,12/05/2003,CNT01,sv001@example.com",
        ])

        response = self.client.post(
            reverse("qna:lecturer_student_list_create_accounts", args=[roster.id]),
            {
                "default_password": "SafePass123!",
                "confirm_password": "SafePass123!",
            },
        )

        self.assertEqual(response.status_code, 302)

        roster.refresh_from_db()
        student_row = roster.students.get(student_code="SV001")
        student_user = self.user_model.objects.get(username="SV001")

        self.assertEqual(roster.status, "CREATED")
        self.assertIsNotNone(roster.account_created_at)
        self.assertTrue(student_row.account_created)
        self.assertEqual(student_row.linked_user, student_user)

        self.client.logout()
        self.assertTrue(self.client.login(username="SV001", password="SafePass123!"))

    def test_uploaded_roster_marks_existing_accounts_separately_from_pending_students(self):
        existing_user = self.user_model.objects.create_user(
            username="legacy.sv004",
            password="LegacyPass123!",
        )
        UserProfile.objects.create(
            user=existing_user,
            is_lecturer=False,
            full_name="Sinh Vien Da Co Tai Khoan",
            student_id="SV004",
        )

        roster = self._upload_roster([
            "SV004,Sinh Vien Da Co Tai Khoan,Nam,12/05/2003,CNT01,sv004@example.com",
            "SV005,Sinh Vien Chua Co,Nu,24/08/2003,CNT01,sv005@example.com",
        ])

        existing_row = roster.students.get(student_code="SV004")
        pending_row = roster.students.get(student_code="SV005")

        self.assertTrue(existing_row.account_created)
        self.assertEqual(existing_row.account_status, StudentRosterAccountStatus.EXISTING)
        self.assertFalse(pending_row.account_created)
        self.assertEqual(pending_row.account_status, StudentRosterAccountStatus.PENDING)

        management_response = self.client.get(
            reverse("qna:lecturer_student_list_management", args=[self.subject.subject_code]),
            {
                "q": "Danh_sach_L01",
                "academic_year": "2024-2025",
                "semester": "HK1",
                "status": "PENDING",
            },
        )
        self.assertEqual(management_response.status_code, 200)

        detail_response = self.client.get(
            reverse("qna:lecturer_student_list_detail", args=[roster.id]),
            {"q": "SV004"},
        )
        self.assertEqual(detail_response.status_code, 200)

    def test_existing_student_account_is_reused_without_resetting_password(self):
        existing_user = self.user_model.objects.create_user(
            username="legacy.student",
            password="LegacyPass123!",
        )
        UserProfile.objects.create(
            user=existing_user,
            is_lecturer=False,
            full_name="Sinh Vien Cu",
            student_id="SV002",
        )

        roster = self._upload_roster([
            "SV002,Sinh Vien Cu,Nam,12/05/2003,CNT01,sv002@example.com",
            "SV003,Sinh Vien Moi,Nu,24/08/2003,CNT01,sv003@example.com",
        ])

        confirm_response = self.client.post(
            reverse("qna:lecturer_student_list_create_accounts", args=[roster.id]),
            {
                "default_password": "BatchPass123!",
                "confirm_password": "BatchPass123!",
            },
        )

        self.assertEqual(confirm_response.status_code, 302)
        self.assertRedirects(
            confirm_response,
            reverse("qna:lecturer_student_list_detail", args=[roster.id]),
            fetch_redirect_response=False,
        )
        self.assertIn("student_account_modal", self.client.session)
        self.assertTrue(self.client.session["student_account_modal"]["need_skip_confirm"])
        self.assertEqual(len(self.client.session["student_account_modal"]["existing_students"]), 1)

        response = self.client.post(
            reverse("qna:lecturer_student_list_create_accounts", args=[roster.id]),
            {
                "default_password": "BatchPass123!",
                "confirm_password": "BatchPass123!",
                "skip_existing": "1",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.user_model.objects.filter(username="legacy.student").count(), 1)
        self.assertEqual(self.user_model.objects.filter(username="SV002").count(), 0)
        self.assertEqual(self.user_model.objects.filter(username="SV003").count(), 1)

        roster.refresh_from_db()
        reused_row = roster.students.get(student_code="SV002")
        created_row = roster.students.get(student_code="SV003")
        existing_user.refresh_from_db()

        self.assertEqual(roster.status, "CREATED")
        self.assertEqual(reused_row.linked_user, existing_user)
        self.assertTrue(reused_row.account_created)
        self.assertTrue(created_row.account_created)
        self.assertEqual(reused_row.account_status, StudentRosterAccountStatus.EXISTING)
        self.assertEqual(created_row.account_status, StudentRosterAccountStatus.CREATED)
        self.assertTrue(existing_user.check_password("LegacyPass123!"))

        self.client.logout()
        self.assertTrue(self.client.login(username="legacy.student", password="LegacyPass123!"))
        self.client.logout()
        self.assertFalse(self.client.login(username="legacy.student", password="BatchPass123!"))
        self.client.logout()
        self.assertTrue(self.client.login(username="SV003", password="BatchPass123!"))

    def test_detail_screen_uses_new_status_labels_and_hides_account_summary(self):
        existing_user = self.user_model.objects.create_user(
            username="legacy.student.detail",
            password="LegacyPass123!",
        )
        UserProfile.objects.create(
            user=existing_user,
            is_lecturer=False,
            full_name="Sinh Vien Da Co Tai Khoan",
            student_id="SV004",
        )

        created_user = self.user_model.objects.create_user(
            username="SV005",
            password="BatchPass123!",
        )
        UserProfile.objects.create(
            user=created_user,
            is_lecturer=False,
            full_name="Sinh Vien Moi Tao",
            student_id="SV005",
        )

        roster = self._upload_roster([
            "SV004,Sinh Vien Da Co Tai Khoan,Nam,12/05/2003,CNT01,sv004@example.com",
            "SV005,Sinh Vien Moi Tao,Nu,24/08/2003,CNT01,sv005@example.com",
            "SV006,Sinh Vien Chua Co,Nu,24/08/2003,CNT01,sv006@example.com",
        ])

        created_row = roster.students.get(student_code="SV005")
        created_row.linked_user = created_user
        created_row.account_created = True
        created_row.account_status = StudentRosterAccountStatus.CREATED
        created_row.save(update_fields=["linked_user_id", "account_created", "account_status"])

        response = self.client.get(reverse("qna:lecturer_student_list_detail", args=[roster.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<th>Trạng thái</th>", html=True)
        self.assertNotContains(response, "Trạng thái tài khoản")
        self.assertContains(response, "Đã có tài khoản")
        self.assertContains(response, "Mới tạo tài khoản")
        self.assertContains(response, "Chưa có tài khoản")
        self.assertNotContains(response, "sinh viên đã có tài khoản")
        self.assertNotContains(response, "có sẵn •")
        self.assertNotContains(response, "Hoàn tất lúc")
        self.assertNotContains(response, "Thời điểm hoàn tất")

    def test_management_screen_has_clear_filter_action(self):
        self._upload_roster([
            "SV001,Nguyen Van A,Nam,12/05/2003,CNT01,sv001@example.com",
        ])

        response = self.client.get(
            reverse("qna:lecturer_student_list_management", args=[self.subject.subject_code]),
            {
                "q": "SV001",
                "academic_year": "2024-2025",
                "semester": "HK1",
                "status": "PENDING",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Xóa bộ lọc")
        self.assertContains(
            response,
            reverse("qna:lecturer_student_list_management", args=[self.subject.subject_code]),
        )

    def test_upload_warning_uses_subject_name_instead_of_missing_subject_name_field(self):
        self._upload_roster([
            "SV010,Sinh Vien Trung,Nam,12/05/2003,CNT01,sv010@example.com",
        ])

        upload = SimpleUploadedFile(
            "Danh_sach_L02.csv",
            (
                "student_code,full_name,gender,date_of_birth,class_name,email\n"
                "SV010,Sinh Vien Trung,Nam,12/05/2003,CNT01,sv010@example.com\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("qna:lecturer_student_list_upload", args=[self.subject.subject_code]),
            {
                "academic_year": "2024-2025",
                "semester": "HK1",
                "files": upload,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        warnings = response.json()["results"][0]["warnings"]
        self.assertTrue(any(self.subject.name in warning["message"] for warning in warnings))

    def disabled_ajax_upload_reports_skipped_rows_legacy_fixture(self):
        upload = SimpleUploadedFile(
            "Danh_sach_L02.csv",
            (
                "Mã số sinh viên,Họ và tên,Giới tính,Ngày sinh,Lớp,Email\n"
                "SV010,Nguyen Van Ten,Nam,12/05/2003,CNT01,sv010@example.com\n"
                "SV011,,Nữ,24/08/2003,CNT01,sv011@example.com\n"
                "SV010,Nguyen Van Trung,Nam,24/08/2003,CNT01,sv010-dup@example.com\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("qna:lecturer_student_list_upload", args=[self.subject.subject_code]),
            {
                "academic_year": "2024-2025",
                "semester": "HK1",
                "files": upload,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["uploaded_count"], 1)
        self.assertEqual(payload["results"][0]["total_students"], 1)
        self.assertEqual(len(payload["results"][0]["warnings"]), 2)
        self.assertIn("Dòng 3", payload["results"][0]["warnings"][0]["message"])
        self.assertIn("Dòng 4", payload["results"][0]["warnings"][1]["message"])

    def test_ajax_upload_reports_skipped_rows(self):
        upload = SimpleUploadedFile(
            "Danh_sach_L02.csv",
            (
                "student_code,full_name,gender,date_of_birth,class_name,email\n"
                "SV010,Nguyen Van Ten,Nam,12/05/2003,CNT01,sv010@example.com\n"
                "SV011,,Nu,24/08/2003,CNT01,sv011@example.com\n"
                "SV010,Nguyen Van Trung,Nam,24/08/2003,CNT01,sv010-dup@example.com\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("qna:lecturer_student_list_upload", args=[self.subject.subject_code]),
            {
                "academic_year": "2024-2025",
                "semester": "HK1",
                "files": upload,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["uploaded_count"], 1)
        self.assertEqual(payload["results"][0]["total_students"], 1)
        self.assertEqual(len(payload["results"][0]["warnings"]), 2)
        self.assertEqual(payload["results"][0]["warnings"][0]["row_number"], 3)
        self.assertEqual(payload["results"][0]["warnings"][1]["row_number"], 4)

    def test_invalid_batch_password_keeps_create_modal_open_with_inline_error(self):
        roster = self._upload_roster([
            "SV001,Nguyen Van A,Nam,12/05/2003,CNT01,sv001@example.com",
        ])

        response = self.client.post(
            reverse("qna:lecturer_student_list_create_accounts", args=[roster.id]),
            {
                "default_password": "BatchPass123!",
                "confirm_password": "Mismatch123!",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(
            response,
            reverse("qna:lecturer_student_list_detail", args=[roster.id]),
            fetch_redirect_response=False,
        )

        session = self.client.session
        self.assertIn("student_account_modal", session)
        self.assertTrue(session["student_account_modal"]["open"])
        self.assertTrue(session["student_account_modal"]["error"])
        error_message = session["student_account_modal"]["error"]

        detail_response = self.client.get(
            reverse("qna:lecturer_student_list_detail", args=[roster.id])
        )
        self.assertContains(detail_response, 'id="passwordValidationMessage"')
        self.assertContains(detail_response, "show")
        self.assertContains(detail_response, error_message)

        roster.refresh_from_db()
        self.assertEqual(roster.status, "PENDING")
        self.assertFalse(roster.students.get(student_code="SV001").account_created)


class LecturerRedirectTests(TestCase):
    def setUp(self):
        self.user_model = get_user_model()
        self.lecturer = self.user_model.objects.create_user(
            username="lecturer_redirect",
            password="Password123!",
        )
        UserProfile.objects.create(
            user=self.lecturer,
            is_lecturer=True,
            full_name="Giang Vien Redirect",
        )

        self.student = self.user_model.objects.create_user(
            username="student_redirect",
            password="Password123!",
        )
        UserProfile.objects.create(
            user=self.student,
            is_lecturer=False,
            full_name="Sinh Vien Redirect",
        )

    def test_root_redirects_lecturer_to_lecturer_dashboard(self):
        self.client.force_login(self.lecturer)
        response = self.client.get(reverse("qna:root"))
        self.assertRedirects(response, reverse("qna:lecturer_dashboard"))

    def test_student_dashboard_redirects_lecturer_to_lecturer_dashboard(self):
        self.client.force_login(self.lecturer)
        response = self.client.get(reverse("qna:dashboard"))
        self.assertRedirects(response, reverse("qna:lecturer_dashboard"))

    def test_lecturer_subject_list_redirects_to_lecturer_dashboard(self):
        self.client.force_login(self.lecturer)
        response = self.client.get(reverse("qna:lecturer_subject_list"))
        self.assertRedirects(response, reverse("qna:lecturer_dashboard"))

    def test_root_redirects_student_to_student_dashboard(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("qna:root"))
        self.assertRedirects(response, reverse("qna:dashboard"))


class LecturerDashboardTests(TestCase):
    def setUp(self):
        self.user_model = get_user_model()
        self.lecturer = self.user_model.objects.create_user(
            username="lecturer_dashboard_counts",
            password="Password123!",
        )
        self.profile = UserProfile.objects.create(
            user=self.lecturer,
            is_lecturer=True,
            full_name="Giang Vien Dashboard",
        )
        self.subject_a = Subject.objects.create(name="Kho va Khai pha du lieu", subject_code="DS001")
        self.subject_b = Subject.objects.create(name="Phan tich du lieu bang python", subject_code="DS002")
        self.profile.subjects_taught.add(self.subject_a, self.subject_b)
        self.client.force_login(self.lecturer)

        for index in range(2):
            ExamSessionGroup.objects.create(
                subject=self.subject_a,
                group_name=f"Ca thi {index + 1}",
                academic_year="2025-2026",
                semester="HK1",
                exam_date=timezone.now() + timedelta(days=index + 1),
                duration_minutes=60,
                status="SCHEDULED",
                created_by=self.lecturer,
            )

        roster_1 = StudentRosterUpload.objects.create(
            subject=self.subject_a,
            academic_year="2025-2026",
            semester="HK1",
            title="Lop A",
            original_file_name="lop_a.xlsx",
            total_students=2,
            created_by=self.lecturer,
        )
        roster_2 = StudentRosterUpload.objects.create(
            subject=self.subject_a,
            academic_year="2025-2026",
            semester="HK2",
            title="Lop B",
            original_file_name="lop_b.xlsx",
            total_students=2,
            created_by=self.lecturer,
        )
        StudentRosterStudent.objects.create(
            roster=roster_1,
            row_number=1,
            student_code="SV001",
            full_name="Sinh Vien 1",
        )
        StudentRosterStudent.objects.create(
            roster=roster_1,
            row_number=2,
            student_code="SV002",
            full_name="Sinh Vien 2",
        )
        StudentRosterStudent.objects.create(
            roster=roster_2,
            row_number=1,
            student_code="SV002",
            full_name="Sinh Vien 2",
        )
        StudentRosterStudent.objects.create(
            roster=roster_2,
            row_number=2,
            student_code="SV003",
            full_name="Sinh Vien 3",
        )

    def test_lecturer_dashboard_subject_cards_use_database_counts(self):
        response = self.client.get(reverse("qna:lecturer_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.subject_a.name)
        self.assertContains(response, self.subject_b.name)

        subjects = {subject.subject_code: subject for subject in response.context["subjects"]}
        self.assertEqual(subjects["DS001"].created_exam_count, 2)
        self.assertEqual(subjects["DS001"].participating_student_count, 3)
        self.assertEqual(subjects["DS002"].created_exam_count, 0)
        self.assertEqual(subjects["DS002"].participating_student_count, 0)

        content = response.content.decode("utf-8")
        self.assertIn("<strong>2</strong>", content)
        self.assertIn("<strong>3</strong>", content)
        self.assertGreaterEqual(content.count("<strong>0</strong>"), 2)
        self.assertNotIn("<strong>10</strong>", content)
        self.assertNotIn("<strong>50</strong>", content)


class LecturerQuestionManagementTests(TestCase):
    def setUp(self):
        self.user_model = get_user_model()
        self.lecturer = self.user_model.objects.create_user(
            username="lecturer_questions",
            password="Password123!",
        )
        self.profile = UserProfile.objects.create(
            user=self.lecturer,
            is_lecturer=True,
            full_name="Giang Vien Question",
        )
        self.subject = Subject.objects.create(
            name="Tri tue nhan tao",
            subject_code="AI401",
        )
        self.profile.subjects_taught.add(self.subject)
        self.client.force_login(self.lecturer)

        self.bank = QuestionBank.objects.create(
            subject=self.subject,
            name="Ngan hang goc",
        )

    def _post_json(self, route_name, payload, args=None):
        return self.client.post(
            reverse(route_name, args=args or []),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _create_active_exam_group(self):
        return ExamSessionGroup.objects.create(
            subject=self.subject,
            group_name="Ca thi đang diễn ra",
            academic_year="2025-2026",
            semester="HK1",
            exam_date=timezone.now() - timedelta(minutes=5),
            duration_minutes=30,
            status="SCHEDULED",
            created_by=self.lecturer,
            configuration_data={"rooms": [], "rosters": []},
)

    def test_question_management_screen_exposes_has_active_exam_flag_for_selected_subject(self):
        self._create_active_exam_group()

        response = self.client.get(
            reverse("qna:lecturer_questions_screen"),
            {"subject_id": self.subject.id, "mode": "detail"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-has-active-exam="true"')

    def test_question_management_screen_no_auto_select_subject_when_not_provided(self):
        response = self.client.get(reverse("qna:lecturer_questions_screen"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="">Chọn môn học</option>')
        self.assertNotContains(response, "Ngan hang goc")

    def test_question_management_screen_shows_empty_state_when_no_subject_selected(self):
        response = self.client.get(reverse("qna:lecturer_questions_screen"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="">Chọn môn học</option>')
        self.assertContains(response, "Vui lòng chọn môn học để quản lý câu hỏi.")
        self.assertNotContains(response, "Ngan hang goc")

    def test_question_management_screen_displays_only_selected_subject_data(self):
        other_subject = Subject.objects.create(
            name="Machine Learning",
            subject_code="ML401",
        )
        self.profile.subjects_taught.add(other_subject)
        other_bank = QuestionBank.objects.create(
            subject=other_subject,
            name="ML Bank",
        )

        response = self.client.get(
            reverse("qna:lecturer_questions_screen"),
            {"subject_id": str(self.subject.id)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'<option value="{self.subject.id}" selected>')
        self.assertContains(response, "Ngan hang goc")
        self.assertNotContains(response, "ML Bank")

    def test_question_management_screen_with_mode_detail_without_subject_shows_empty_state(self):
        response = self.client.get(
            reverse("qna:lecturer_questions_screen"),
            {"mode": "detail", "view": "bank", "bank_id": str(self.bank.id)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="">Chọn môn học</option>')
        self.assertContains(response, "Vui lòng chọn môn học để quản lý câu hỏi.")
        self.assertNotContains(response, "Ngan hang goc")

    def test_create_question_bank_trims_name_and_rejects_duplicate_in_same_subject(self):
        create_response = self._post_json(
            "qna:api_create_question_bank",
            {
                "subject_id": str(self.subject.id),
                "name": "  Ngan hang 1  ",
            },
        )

        self.assertEqual(create_response.status_code, 200)
        create_payload = create_response.json()
        self.assertEqual(create_payload["status"], "SUCCESS")
        self.assertEqual(create_payload["bank_name"], "Ngan hang 1")
        self.assertTrue(QuestionBank.objects.filter(subject_id=self.subject, name="Ngan hang 1").exists())

        duplicate_response = self._post_json(
            "qna:api_create_question_bank",
            {
                "subject_id": str(self.subject.id),
                "name": "Ngan hang 1",
            },
        )

        self.assertEqual(duplicate_response.status_code, 400)
        duplicate_payload = duplicate_response.json()
        self.assertEqual(duplicate_payload["status"], "FAIL")
        self.assertEqual(duplicate_payload["message"], "Tên ngân hàng đã tồn tại trong môn học này.")
        self.assertEqual(QuestionBank.objects.filter(subject_id=self.subject, name="Ngan hang 1").count(), 1)

    def test_create_question_bank_allows_same_name_in_different_subjects(self):
        other_subject = Subject.objects.create(
            name="Hoc may",
            subject_code="ML401",
        )
        self.profile.subjects_taught.add(other_subject)

        first_response = self._post_json(
            "qna:api_create_question_bank",
            {
                "subject_id": str(self.subject.id),
                "name": "Ngan hang 1",
            },
        )
        second_response = self._post_json(
            "qna:api_create_question_bank",
            {
                "subject_id": str(other_subject.id),
                "name": "Ngan hang 1",
            },
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(first_response.json()["status"], "SUCCESS")
        self.assertEqual(second_response.json()["status"], "SUCCESS")
        self.assertTrue(QuestionBank.objects.filter(subject_id=self.subject, name="Ngan hang 1").exists())
        self.assertTrue(QuestionBank.objects.filter(subject_id=other_subject, name="Ngan hang 1").exists())

    def test_create_question_bank_rejects_blank_name_after_trim(self):
        response = self._post_json(
            "qna:api_create_question_bank",
            {
                "subject_id": str(self.subject.id),
                "name": "   ",
            },
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["message"], "Tên ngân hàng không được để trống.")

    def test_update_question_bank_rejects_duplicate_name_in_same_subject(self):
        first_bank = QuestionBank.objects.create(subject=self.subject, name="Ngan hang 1")
        second_bank = QuestionBank.objects.create(subject=self.subject, name="Ngan hang 2")

        response = self._post_json(
            "qna:api_update_question_bank",
            {"name": "  Ngan hang 1  "},
            args=[second_bank.id],
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["message"], "Tên ngân hàng đã tồn tại trong môn học này.")

        first_bank.refresh_from_db()
        second_bank.refresh_from_db()
        self.assertEqual(first_bank.name, "Ngan hang 1")
        self.assertEqual(second_bank.name, "Ngan hang 2")

    def test_question_bank_model_constraint_is_scoped_per_subject(self):
        QuestionBank.objects.create(subject=self.subject, name="Constraint bank")

        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                QuestionBank.objects.create(subject=self.subject, name="Constraint bank")

        other_subject = Subject.objects.create(
            name="Thi giac may tinh",
            subject_code="CV401",
        )
        QuestionBank.objects.create(subject=other_subject, name="Constraint bank")
        self.assertEqual(QuestionBank.objects.filter(name="Constraint bank").count(), 2)

    def test_question_management_screen_keeps_cookie_helper_javascript_valid(self):
        response = self.client.get(
            reverse("qna:lecturer_questions_screen"),
            {"subject_id": self.subject.id, "mode": "detail"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "if (document.cookie && document.cookie !== '') {")
        self.assertNotContains(response, "<d></d>ocument.cookie")

    def test_legacy_question_management_route_redirects_to_new_screen(self):
        response = self.client.get(reverse("qna:lecturer_question_management", args=[self.subject.subject_code]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('qna:lecturer_questions_screen')}?subject_id={self.subject.id}&mode=detail",
        )

    def test_legacy_generate_exam_codes_route_redirects_to_new_screen(self):
        response = self.client.get(reverse("qna:lecturer_generate_exam_codes", args=[self.subject.subject_code]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('qna:lecturer_generate_codes_screen')}?subject_id={self.subject.id}",
        )

    def test_legacy_upload_material_api_returns_gone(self):
        response = self.client.post(reverse("qna:lecturer_upload_material_screen"))

        self.assertEqual(response.status_code, 410)
        self.assertFalse(response.json()["success"])

    def test_create_manual_question_and_list_by_bank(self):
        create_response = self._post_json(
            "qna:api_create_manual_question",
            {
                "subject_id": str(self.subject.id),
                "real_bank_id": str(self.bank.id),
                "view_type": "bank",
                "difficulty": DifficultyLevel.MEDIUM,
                "content": "Mo ta overfitting trong machine learning.",
            },
        )

        self.assertEqual(create_response.status_code, 200)
        create_payload = create_response.json()
        self.assertEqual(create_payload["status"], "SUCCESS")

        question = Question.objects.get(pk=create_payload["question"]["question_id"])
        self.assertEqual(question.bank, self.bank)
        self.assertEqual(question.subject, self.subject)
        self.assertEqual(question.difficulty, DifficultyLevel.MEDIUM)

        list_response = self.client.get(
            reverse("qna:api_get_questions"),
            {
                "bank_id": str(self.subject.id),
                "real_bank_id": str(self.bank.id),
                "view_type": "bank",
                "difficulty": "ALL",
            },
        )

        self.assertEqual(list_response.status_code, 200)
        list_payload = list_response.json()
        self.assertEqual(list_payload["status"], "SUCCESS")
        self.assertEqual(list_payload["summary"]["all"], 1)
        self.assertEqual(list_payload["summary"]["medium"], 1)
        self.assertEqual(len(list_payload["questions"]), 1)
        self.assertEqual(
            list_payload["questions"][0]["content"],
            "Mo ta overfitting trong machine learning.",
        )

    def test_question_bank_apis_ignore_exam_clone_questions(self):
        Question.objects.create(
            subject=self.subject,
            bank=self.bank,
            question_text="Cau goc",
            question_id_in_barem="Q_AI401_010",
            difficulty=DifficultyLevel.EASY,
            is_exam_clone=False,
        )
        Question.objects.create(
            subject=self.subject,
            bank=self.bank,
            question_text="Câu clone không được hiển thị",
            question_id_in_barem="EC_legacy_clone",
            difficulty=DifficultyLevel.HARD,
            is_exam_clone=True,
        )

        bank_response = self.client.get(
            reverse("qna:api_get_question_banks"),
            {"subject_id": str(self.subject.id)},
        )
        self.assertEqual(bank_response.status_code, 200)
        self.assertEqual(bank_response.json()["question_banks"][0]["question_count"], 1)

        list_response = self.client.get(
            reverse("qna:api_get_questions"),
            {
                "bank_id": str(self.subject.id),
                "real_bank_id": str(self.bank.id),
                "view_type": "bank",
                "difficulty": "ALL",
            },
        )
        self.assertEqual(list_response.status_code, 200)
        payload = list_response.json()
        self.assertEqual(payload["summary"]["all"], 1)
        self.assertEqual(len(payload["questions"]), 1)
        self.assertEqual(payload["questions"][0]["content"], "Cau goc")

    def test_create_manual_question_rejects_bank_outside_lecturer_scope(self):
        other_subject = Subject.objects.create(
            name="Môn khác",
            subject_code="OT401",
        )
        other_bank = QuestionBank.objects.create(
            subject=other_subject,
            name="Bank ngoài phạm vi",
        )

        response = self._post_json(
            "qna:api_create_manual_question",
            {
                "subject_id": str(self.subject.id),
                "real_bank_id": str(other_bank.id),
                "view_type": "bank",
                "difficulty": DifficultyLevel.MEDIUM,
                "content": "Câu hỏi không hợp lệ.",
            },
        )

        self.assertEqual(response.status_code, 404)

    def test_bulk_update_and_bulk_delete_questions(self):
        first = Question.objects.create(
            subject=self.subject,
            bank=self.bank,
            question_text="Cau 1",
            question_id_in_barem="Q_AI401_001",
            difficulty=DifficultyLevel.EASY,
        )
        second = Question.objects.create(
            subject=self.subject,
            bank=self.bank,
            question_text="Cau 2",
            question_id_in_barem="Q_AI401_002",
            difficulty=DifficultyLevel.MEDIUM,
        )

        update_response = self._post_json(
            "qna:api_bulk_update_question_level",
            {
                "bank_id": str(self.subject.id),
                "question_ids": [first.id, second.id],
                "difficulty": DifficultyLevel.HARD,
            },
        )

        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["affected"], 2)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.difficulty, DifficultyLevel.HARD)
        self.assertEqual(second.difficulty, DifficultyLevel.HARD)

        delete_response = self._post_json(
            "qna:api_bulk_delete_questions",
            {
                "bank_id": str(self.subject.id),
                "question_ids": [first.id],
            },
        )

        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_response.json()["affected"], 1)
        self.assertFalse(Question.objects.filter(pk=first.id).exists())
        self.assertTrue(Question.objects.filter(pk=second.id).exists())

    def test_update_question_bank_question_is_blocked_while_exam_is_ongoing(self):
        question = Question.objects.create(
            subject=self.subject,
            bank=self.bank,
            question_text="Câu hỏi đang được khóa",
            question_id_in_barem="Q_AI401_LOCKED",
            difficulty=DifficultyLevel.EASY,
        )
        self._create_active_exam_group()

        response = self._post_json(
            "qna:api_update_question_v2",
            {
                "content": "Noi dung moi khi dang thi",
                "difficulty": DifficultyLevel.HARD,
            },
            args=[question.id],
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["status"], "FAIL")
        self.assertTrue(response.json()["message"])

    def test_save_generated_questions_into_new_question_bank(self):
        workspace_id = "ws_regression"
        draft_one = Question.objects.create(
            subject=self.subject,
            question_text="Ban nhap 1",
            question_id_in_barem=f"DRAFT_{workspace_id}_001",
            difficulty=DifficultyLevel.EASY,
        )
        draft_two = Question.objects.create(
            subject=self.subject,
            question_text="Ban nhap 2",
            question_id_in_barem=f"DRAFT_{workspace_id}_002",
            difficulty=DifficultyLevel.MEDIUM,
        )
        draft_three = Question.objects.create(
            subject=self.subject,
            question_text="Ban nhap 3",
            question_id_in_barem=f"DRAFT_{workspace_id}_003",
            difficulty=DifficultyLevel.HARD,
        )

        create_bank_response = self._post_json(
            "qna:api_create_question_bank",
            {
                "subject_id": str(self.subject.id),
                "name": "Ngan hang moi",
            },
        )
        new_bank_id = create_bank_response.json()["bank_id"]

        save_response = self._post_json(
            "qna:api_save_question_bank_questions",
            {
                "question_ids": [draft_one.id, draft_two.id],
                "workspace_id": workspace_id,
            },
            args=[new_bank_id],
        )

        self.assertEqual(save_response.status_code, 200)
        draft_one.refresh_from_db()
        draft_two.refresh_from_db()

        self.assertEqual(draft_one.bank.id, int(new_bank_id))
        self.assertEqual(draft_two.bank.id, int(new_bank_id))
        self.assertTrue(draft_one.question_id_in_barem.startswith("SAVED_"))
        self.assertTrue(draft_two.question_id_in_barem.startswith("SAVED_"))
        self.assertFalse(Question.objects.filter(pk=draft_three.id).exists())

    def test_save_generated_questions_returns_updated_bank_count(self):
        Question.objects.create(
            subject=self.subject,
            bank=self.bank,
            question_text="Cau san co",
            question_id_in_barem="MAN_EXISTING_001",
            difficulty=DifficultyLevel.EASY,
        )
        workspace_id = "ws_existing_bank"
        draft_one = Question.objects.create(
            subject=self.subject,
            question_text="Ban nhap them 1",
            question_id_in_barem=f"DRAFT_{workspace_id}_001",
            difficulty=DifficultyLevel.EASY,
        )
        draft_two = Question.objects.create(
            subject=self.subject,
            question_text="Ban nhap them 2",
            question_id_in_barem=f"DRAFT_{workspace_id}_002",
            difficulty=DifficultyLevel.MEDIUM,
        )

        save_response = self._post_json(
            "qna:api_save_question_bank_questions",
            {
                "question_ids": [draft_one.id, draft_two.id],
                "workspace_id": workspace_id,
            },
            args=[self.bank.id],
        )

        self.assertEqual(save_response.status_code, 200)
        payload = save_response.json()
        self.assertEqual(payload["saved_count"], 2)
        self.assertEqual(payload["question_count"], 3)

        bank_response = self.client.get(
            reverse("qna:api_get_question_banks"),
            {"subject_id": str(self.subject.id)},
        )
        self.assertEqual(bank_response.status_code, 200)
        self.assertEqual(bank_response.json()["question_banks"][0]["question_count"], 3)

    def test_import_questions_accepts_flexible_columns_and_reports_invalid_rows(self):
        upload = SimpleUploadedFile(
            "questions.csv",
            (
                "content,difficulty\n"
                "Mo ta bai toan,De\n"
                "Phan tich du lieu,Trung binh\n"
                "Dong loi,Khong hop le\n"
                ",Hard\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("qna:lecturer_import_questions"),
            {
                "subject_id": str(self.subject.id),
                "file": upload,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["imported_count"], 2)
        self.assertEqual(payload["invalid_count"], 2)
        self.assertTrue(Question.objects.filter(subject_id=self.subject, question_text="Mo ta bai toan").exists())
        self.assertTrue(Question.objects.filter(subject_id=self.subject, question_text="Phan tich du lieu").exists())


class LecturerExamManagementTests(TestCase):
    def setUp(self):
        self.user_model = get_user_model()
        self.lecturer = self.user_model.objects.create_user(
            username="lecturer_exams",
            password="Password123!",
        )
        self.profile = UserProfile.objects.create(
            user=self.lecturer,
            is_lecturer=True,
            full_name="Giang Vien Exam",
        )
        self.subject = Subject.objects.create(
            name="Khai pha du lieu",
            subject_code="DS402",
        )
        self.profile.subjects_taught.add(self.subject)
        self.client.force_login(self.lecturer)

        self.bank = QuestionBank.objects.create(
            subject=self.subject,
            name="Ngan hang sinh de",
        )

        for index in range(2):
            Question.objects.create(
                subject=self.subject,
                bank=self.bank,
                question_text=f"Easy {index}",
                question_id_in_barem=f"E_{index}",
                difficulty=DifficultyLevel.EASY,
            )
            Question.objects.create(
                subject=self.subject,
                bank=self.bank,
                question_text=f"Medium {index}",
                question_id_in_barem=f"M_{index}",
                difficulty=DifficultyLevel.MEDIUM,
            )
            Question.objects.create(
                subject=self.subject,
                bank=self.bank,
                question_text=f"Hard {index}",
                question_id_in_barem=f"H_{index}",
                difficulty=DifficultyLevel.HARD,
            )

    def _post_json(self, route_name, payload, args=None):
        return self.client.post(
            reverse(route_name, args=args or []),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _build_exam_set_payload(self, number_of_versions=2, **overrides):
        payload = {
            "subject_id": str(self.subject.id),
            "academic_year": "2025-2026",
            "semester": "HK1",
            "number_of_versions": number_of_versions,
            "easy_count": 1,
            "medium_count": 1,
            "hard_count": 1,
            "easy_score": 2,
            "medium_score": 3,
            "hard_score": 5,
            "shuffle_question_order": False,
            "allow_duplicate_questions": False,
            "source_bank_ids": [self.bank.id],
        }
        payload.update(overrides)
        return payload

    def _create_exam_set_response(self, number_of_versions=2, **overrides):
        return self._post_json(
            "qna:lecturer_create_exam_set",
            self._build_exam_set_payload(number_of_versions=number_of_versions, **overrides),
        )

    def _create_exam_set(self, number_of_versions=2, **overrides):
        response = self._create_exam_set_response(number_of_versions=number_of_versions, **overrides)
        self.assertEqual(response.status_code, 200)
        return ExamSet.objects.get(pk=response.json()["exam_set_id"])

    def _link_exam_code(self, exam_code, group_name="Ca thi da dung"):
        exam_group = ExamSessionGroup.objects.create(
            subject=self.subject,
            group_name=group_name,
            exam_date=timezone.now(),
            duration_minutes=60,
            status="SCHEDULED",
            created_by=self.lecturer,
        )
        exam_group.exam_codes.add(exam_code)

        session_room = ExamSessionRoom.objects.create(
            exam_session_group_id=exam_group,
            room_name="Phong da gan ma de",
            expected_students=1,
            exam_set_id=exam_code.exam_set_id,
            display_order=1,
        )
        session_room.exam_codes.add(exam_code)
        return exam_group

    def _create_active_exam_group(self, *exam_codes):
        exam_group = ExamSessionGroup.objects.create(
            subject=self.subject,
            group_name="Ca thi đang diễn ra",
            academic_year="2025-2026",
            semester="HK1",
            exam_date=timezone.now() - timedelta(minutes=5),
            duration_minutes=30,
            status="SCHEDULED",
            created_by=self.lecturer,
        )
        if exam_codes:
            exam_group.exam_codes.add(*exam_codes)
        return exam_group

    def test_create_exam_set_generates_exam_codes_and_clone_questions(self):
        exam_set = self._create_exam_set(number_of_versions=2)

        self.assertEqual(exam_set.status, ExamSetStatus.DRAFT)
        self.assertEqual(exam_set.exam_codes.count(), 2)
        self.assertEqual(exam_set.question_banks.count(), 1)

        for code in exam_set.exam_codes.all():
            items = list(code.exam_code_questions.select_related("question_id").all())
            self.assertEqual(len(items), 3)
            self.assertTrue(all(item.question.is_exam_clone for item in items))
            self.assertTrue(all(item.question.bank is None for item in items))

    def test_create_exam_set_returns_success_message_when_total_score_equals_ten(self):
        response = self._create_exam_set_response()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "SUCCESS")
        self.assertEqual(payload["message"], "Tạo bộ đề thành công.")
        self.assertEqual(ExamSet.objects.count(), 1)

    def test_create_exam_set_accepts_valid_academic_year_format(self):
        response = self._create_exam_set_response(academic_year="2025-2026")

        self.assertEqual(response.status_code, 200)
        exam_set = ExamSet.objects.get(pk=response.json()["exam_set_id"])
        self.assertEqual(exam_set.academic_year, "2025-2026")

    def test_create_exam_set_rejects_academic_year_with_letters(self):
        response = self._create_exam_set_response(academic_year="2025-20a6")

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["message"], "Năm học phải đúng định dạng xxxx-xxxx và chỉ được chứa chữ số.")
        self.assertEqual(ExamSet.objects.count(), 0)

    def test_create_exam_set_rejects_academic_year_with_wrong_separator(self):
        response = self._create_exam_set_response(academic_year="2025/2026")

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["message"], "Năm học phải đúng định dạng xxxx-xxxx và chỉ được chứa chữ số.")
        self.assertEqual(ExamSet.objects.count(), 0)

    def test_create_exam_set_rejects_total_score_greater_than_ten(self):
        response = self._create_exam_set_response(hard_score=6)

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["message"], "Tổng điểm hiện tại đang vượt quá 10 điểm.")
        self.assertEqual(ExamSet.objects.count(), 0)
        self.assertEqual(ExamCode.objects.count(), 0)
        self.assertEqual(ExamCodeQuestion.objects.count(), 0)

    def test_create_exam_set_rejects_total_score_less_than_ten(self):
        response = self._create_exam_set_response(hard_score=4)

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["message"], "Tổng điểm hiện tại chưa đủ 10 điểm.")
        self.assertEqual(ExamSet.objects.count(), 0)
        self.assertEqual(ExamCode.objects.count(), 0)
        self.assertEqual(ExamCodeQuestion.objects.count(), 0)

    def test_create_exam_set_rejects_zero_total_questions(self):
        response = self._create_exam_set_response(easy_count=0, medium_count=0, hard_count=0)

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["message"], "Mỗi mã đề phải có ít nhất 1 câu hỏi.")
        self.assertEqual(ExamSet.objects.count(), 0)
        self.assertEqual(ExamCode.objects.count(), 0)
        self.assertEqual(ExamCodeQuestion.objects.count(), 0)

    def test_create_exam_set_rejects_positive_count_with_zero_score(self):
        response = self._create_exam_set_response(easy_score=0)

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["message"], "Mỗi câu hỏi phải có số điểm lớn hơn 0.")
        self.assertEqual(ExamSet.objects.count(), 0)
        self.assertEqual(ExamCode.objects.count(), 0)
        self.assertEqual(ExamCodeQuestion.objects.count(), 0)

    def test_create_exam_set_creates_only_non_empty_exam_codes_with_positive_scores(self):
        exam_set = self._create_exam_set(number_of_versions=2)

        for code in exam_set.exam_codes.all():
            items = list(code.exam_code_questions.all())
            self.assertGreater(len(items), 0)
            self.assertTrue(all(float(item.score or 0) > 0 for item in items))

    def test_create_exam_set_ignores_shuffle_question_order_payload(self):
        response = self._create_exam_set_response(number_of_versions=1)

        self.assertEqual(response.status_code, 200)
        exam_set = ExamSet.objects.get(pk=response.json()["exam_set_id"])
        exam_code = exam_set.exam_codes.get()
        ordered_difficulties = list(
            exam_code.exam_code_questions.order_by("display_order").values_list("difficulty", flat=True)
        )

        self.assertTrue(exam_set.shuffle_question_order)
        self.assertEqual(
            ordered_difficulties,
            [DifficultyLevel.EASY, DifficultyLevel.MEDIUM, DifficultyLevel.HARD],
        )

    def test_generate_exam_screen_allows_manual_count_input(self):
        response = self.client.get(
            reverse("qna:lecturer_generate_codes_screen"),
            {"subject_id": self.subject.id},
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertContains(response, 'class="matrix-toolbar"')
        self.assertContains(response, 'class="field matrix-count-field"')
        self.assertContains(response, 'class="field matrix-score-field"')
        self.assertContains(response, 'id="easyCount" type="number" min="0" step="1"')
        self.assertContains(response, 'id="mediumCount" type="number" min="0" step="1"')
        self.assertContains(response, 'id="hardCount" type="number" min="0" step="1"')
        self.assertContains(response, "Mức độ câu hỏi")
        self.assertIsNone(re.search("[\\u00C3\\u00C2\\u00C4\\u00C5]", content))
        self.assertNotContains(response, 'class="stepper-input"')
        self.assertNotContains(response, 'type="hidden" id="easyCount"')

    def test_generate_exam_screen_starts_with_zero_matrix_values(self):
        response = self.client.get(
            reverse("qna:lecturer_generate_codes_screen"),
            {"subject_id": self.subject.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="easyCount" type="number" min="0" step="1" value="0"')
        self.assertContains(response, 'id="mediumCount" type="number" min="0" step="1" value="0"')
        self.assertContains(response, 'id="hardCount" type="number" min="0" step="1" value="0"')
        self.assertContains(response, 'id="easyScore" type="number" min="0" step="0.5" value="0"')
        self.assertContains(response, 'id="mediumScore" type="number" min="0" step="0.5" value="0"')
        self.assertContains(response, 'id="hardScore" type="number" min="0" step="0.5" value="0"')
        self.assertContains(response, 'id="totalQuestions">0</strong>')
        self.assertContains(response, 'id="totalScore">0.00</strong>')

    def test_generate_exam_screen_includes_summary_validation_messages(self):
        response = self.client.get(
            reverse("qna:lecturer_generate_codes_screen"),
            {"subject_id": self.subject.id},
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertContains(response, 'id="summaryMessage"')
        self.assertContains(response, "Tạo bộ đề thành công.")
        self.assertContains(response, "Tổng điểm hiện tại đang vượt quá 10 điểm.")
        self.assertContains(response, "Tổng điểm hiện tại chưa đủ 10 điểm.")
        self.assertContains(response, "Mỗi mã đề phải có ít nhất 1 câu hỏi.")
        self.assertContains(response, "Mỗi câu hỏi phải có số điểm lớn hơn 0.")
        self.assertIn("getConfigValidation", content)
        self.assertIn("btn.disabled = !validation.valid;", content)

    def test_legacy_create_session_route_redirects_to_session_wizard(self):
        response = self.client.get(
            reverse("qna:lecturer_create_exam_session", args=[self.subject.subject_code])
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('qna:lecturer_create_session_screen')}?subject_id={self.subject.id}",
        )

    def test_legacy_student_review_route_redirects_to_new_screen(self):
        response = self.client.get(
            reverse("qna:lecturer_student_review", args=[self.subject.subject_code])
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('qna:lecturer_student_review_screen')}?subject_id={self.subject.id}",
        )

    def test_exam_set_list_shows_set_code_and_clickable_row_without_edit_icon(self):
        exam_set = self._create_exam_set(number_of_versions=1)

        response = self.client.get(reverse("qna:lecturer_exam_codes_screen"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertEqual(response.context["rows"][0]["exam_set_code"], f"BD-{exam_set.id:04d}")
        self.assertEqual(
            response.context["rows"][0]["detail_url"],
            reverse("qna:lecturer_exam_set_detail_screen", args=[exam_set.id]),
        )
        self.assertContains(response, 'class="qm2-clickable-row"')
        self.assertContains(response, f'data-detail-url="/lecturer/exam-codes/{exam_set.id}/"')
        self.assertNotContains(response, "fa-pen")

    def test_exam_set_list_searches_by_exam_set_code(self):
        target_exam_set = self._create_exam_set(number_of_versions=1)
        other_exam_set = self._create_exam_set(number_of_versions=1)

        response = self.client.get(
            reverse("qna:lecturer_exam_codes_screen"),
            {"q": f"BD-{target_exam_set.id:04d}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertEqual(response.context["rows"][0]["id"], target_exam_set.id)
        self.assertContains(response, f"BD-{target_exam_set.id:04d}")
        self.assertNotContains(response, f"BD-{other_exam_set.id:04d}")

    def test_exam_set_list_searches_by_exam_set_name(self):
        target_exam_set = self._create_exam_set(number_of_versions=1)
        other_exam_set = self._create_exam_set(number_of_versions=1)
        target_exam_set.name = "Bộ đề Hồi quy tuyến tính"
        target_exam_set.save(update_fields=["name", "updated_at"])
        other_exam_set.name = "Bộ đề Cây quyết định"
        other_exam_set.save(update_fields=["name", "updated_at"])

        response = self.client.get(
            reverse("qna:lecturer_exam_codes_screen"),
            {"q": "Hồi quy"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertEqual(response.context["rows"][0]["id"], target_exam_set.id)
        self.assertContains(response, f"BD-{target_exam_set.id:04d}")
        self.assertNotContains(response, f"BD-{other_exam_set.id:04d}")

    def test_exam_set_list_search_returns_no_results_when_keyword_does_not_match(self):
        self._create_exam_set(number_of_versions=1)

        response = self.client.get(
            reverse("qna:lecturer_exam_codes_screen"),
            {"q": "khong-co-ket-qua"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["rows"], [])
        self.assertContains(response, "Chưa có bộ đề nào phù hợp với bộ lọc hiện tại.")

    def test_exam_set_list_search_works_together_with_linked_filter(self):
        used_exam_set = self._create_exam_set(number_of_versions=1)
        unused_exam_set = self._create_exam_set(number_of_versions=1)
        used_exam_set.name = "Bộ đề Phân cụm KMeans"
        used_exam_set.save(update_fields=["name", "updated_at"])
        unused_exam_set.name = "Bộ đề Phân cụm DBSCAN"
        unused_exam_set.save(update_fields=["name", "updated_at"])
        self._link_exam_code(used_exam_set.exam_codes.first())

        response = self.client.get(
            reverse("qna:lecturer_exam_codes_screen"),
            {"q": "Phân cụm", "linked": "USED"},
)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertContains(response, 'class="qm2-pagination"')
        self.assertContains(response, "1-1")
        self.assertContains(response, f"BD-{first_exam_set.id:04d}")
        self.assertNotContains(response, f"BD-{second_exam_set.id:04d}")

    def test_exam_set_list_pagination_page_number(self):
        for i in range(25):
            self._create_exam_set(number_of_versions=1)

        response = self.client.get(
            reverse("qna:lecturer_exam_codes_screen"),
            {"page": "2"},
        )
        self.assertEqual(response.status_code, 200)
        page_obj = response.context["page_obj"]
        self.assertEqual(page_obj.number, 2)
        self.assertTrue(len(page_obj.object_list) <= 20)

    def test_exam_set_list_pagination_preserves_filter(self):
        for i in range(25):
            es = self._create_exam_set(number_of_versions=1)
            es.name = "Bộ đề PCA"
            es.save(update_fields=["name", "updated_at"])

        response = self.client.get(
            reverse("qna:lecturer_exam_codes_screen"),
            {"q": "PCA", "page": "2"},
        )
        self.assertEqual(response.status_code, 200)
        page_obj = response.context["page_obj"]
        self.assertIn("q", response.context["filter_values"])

    def test_exam_set_detail_hides_regenerate_action_and_shows_set_code(self):
        exam_set = self._create_exam_set(number_of_versions=1)

        response = self.client.get(
            reverse("qna:lecturer_exam_set_detail_screen", args=[exam_set.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"BD-{exam_set.id:04d}")
        self.assertNotContains(response, "fa-rotate-right")
        self.assertNotContains(response, "Sinh lại")

    def test_exam_set_detail_uses_bulk_toolbar_and_keeps_manual_create_action(self):
        exam_set = self._create_exam_set(number_of_versions=1)

        response = self.client.get(
            reverse("qna:lecturer_exam_set_detail_screen", args=[exam_set.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="btnManualCreate"')
        self.assertContains(response, 'id="bulkBar"')
        self.assertContains(response, 'id="drawerTitle"')
        self.assertContains(response, 'clearCodeSelection()')
        self.assertContains(response, 'fa-circle-check')
        self.assertNotContains(response, 'id="btnBatchApprove"')
        self.assertNotContains(response, 'class="summary-bar"')
        self.assertNotContains(response, "fa-eye")

    def test_unused_unapproved_exam_code_can_be_deleted(self):
        exam_set = self._create_exam_set(number_of_versions=1)
        exam_code = exam_set.exam_codes.get()

        response = self.client.post(
            reverse("qna:lecturer_delete_exam_code", args=[exam_code.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "SUCCESS")
        self.assertFalse(ExamCode.objects.filter(pk=exam_code.id).exists())

    def test_unused_approved_exam_code_can_be_deleted(self):
        exam_set = self._create_exam_set(number_of_versions=1)
        exam_code = exam_set.exam_codes.get()
        exam_code.is_approved = True
        exam_code.save(update_fields=["is_approved", "updated_at"])

        response = self.client.post(
            reverse("qna:lecturer_delete_exam_code", args=[exam_code.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "SUCCESS")
        self.assertFalse(ExamCode.objects.filter(pk=exam_code.id).exists())

    def test_exam_set_detail_delete_ui_is_locked_by_is_linked_not_is_approved(self):
        exam_set = self._create_exam_set(number_of_versions=2)
        approved_unused_code, used_code = list(exam_set.exam_codes.order_by("code_number", "exam_code_id"))
        approved_unused_code.is_approved = True
        approved_unused_code.save(update_fields=["is_approved", "updated_at"])
        self._link_exam_code(used_code)

        response = self.client.get(
            reverse("qna:lecturer_exam_set_detail_screen", args=[exam_set.id])
        )

        self.assertEqual(response.status_code, 200)
        codes_data = json.loads(response.context["exam_codes_json"])
        approved_unused_payload = next(item for item in codes_data if item["id"] == approved_unused_code.id)
        used_payload = next(item for item in codes_data if item["id"] == used_code.id)
        self.assertTrue(approved_unused_payload["is_approved"])
        self.assertFalse(approved_unused_payload["is_linked"])
        self.assertTrue(used_payload["is_linked"])
        self.assertContains(response, "selectAll.disabled = !selectableIds.length;")
        self.assertContains(response, "${code.is_linked ? 'disabled' : ''}")
        self.assertNotContains(response, "examSetIsLinked || code.is_linked ? 'disabled' : ''")

    def test_exam_set_list_filters_by_linked_usage(self):
        used_exam_set = self._create_exam_set(number_of_versions=1)
        unused_exam_set = self._create_exam_set(number_of_versions=1)

        used_code = used_exam_set.exam_codes.first()
        self._link_exam_code(used_code)

        used_response = self.client.get(reverse("qna:lecturer_exam_codes_screen"), {"linked": "USED"})
        self.assertContains(used_response, f"BD-{used_exam_set.id:04d}")
        self.assertNotContains(used_response, f"BD-{unused_exam_set.id:04d}")

        unused_response = self.client.get(reverse("qna:lecturer_exam_codes_screen"), {"linked": "UNUSED"})
        self.assertContains(unused_response, f"BD-{unused_exam_set.id:04d}")
        self.assertNotContains(unused_response, f"BD-{used_exam_set.id:04d}")

    def test_exam_set_list_shows_approved_and_used_code_counts(self):
        exam_set = self._create_exam_set(number_of_versions=2)
        approved_code, _ = list(exam_set.exam_codes.order_by("code_number", "exam_code_id"))
        approved_code.is_approved = True
        approved_code.save(update_fields=["is_approved", "updated_at"])
        self._link_exam_code(approved_code)

        response = self.client.get(reverse("qna:lecturer_exam_codes_screen"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"BD-{exam_set.id:04d}")
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertEqual(response.context["rows"][0]["approved_codes_count"], 1)
        self.assertEqual(response.context["rows"][0]["used_codes_count"], 1)

    def test_create_manual_exam_code_creates_blank_questions_and_updates_counts(self):
        exam_set = self._create_exam_set(number_of_versions=1)
        manual_items = [
            {
                "display_order": 1,
                "difficulty": DifficultyLevel.EASY,
                "content": "Cau thu cong de",
            },
            {
                "display_order": 2,
                "difficulty": DifficultyLevel.MEDIUM,
                "content": "Cau thu cong trung binh",
            },
            {
                "display_order": 3,
                "difficulty": DifficultyLevel.HARD,
                "content": "Cau thu cong kho",
            },
        ]

        response = self._post_json(
            "qna:lecturer_create_manual_exam_code",
            {"items": manual_items},
            args=[exam_set.id],
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        exam_set.refresh_from_db()

        self.assertEqual(payload["status"], "SUCCESS")
        self.assertEqual(exam_set.exam_codes.count(), 2)
        self.assertEqual(exam_set.number_of_versions, 1)

        new_code = exam_set.exam_codes.order_by("-code_number", "-exam_code_id").first()
        items = list(new_code.exam_code_questions.select_related("question_id").order_by("display_order"))

        self.assertEqual(payload["exam_code"]["id"], new_code.id)
        self.assertEqual(payload["exam_code"]["code_number"], new_code.code_number)
        self.assertFalse(new_code.is_approved)
        self.assertEqual(len(items), 3)
        self.assertEqual(
            [item.question.question_text for item in items],
            [item["content"] for item in manual_items],
        )
        self.assertEqual(
            [item.difficulty for item in items],
            [DifficultyLevel.EASY, DifficultyLevel.MEDIUM, DifficultyLevel.HARD],
        )
        self.assertTrue(all(item.question.is_exam_clone for item in items))

    def test_bulk_approve_exam_codes_marks_only_valid_codes(self):
        exam_set = self._create_exam_set(number_of_versions=2)
        valid_code, invalid_code = list(exam_set.exam_codes.order_by("code_number", "exam_code_id"))
        invalid_item = invalid_code.exam_code_questions.select_related("question_id").order_by("display_order").first()
        invalid_item.question.question_text = "   "
        invalid_item.question.save(update_fields=["question_text"])

        response = self._post_json(
            "qna:lecturer_bulk_approve_exam_codes",
            {"exam_code_ids": [valid_code.id, invalid_code.id]},
            args=[exam_set.id],
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        valid_code.refresh_from_db()
        invalid_code.refresh_from_db()

        self.assertEqual(payload["status"], "SUCCESS")
        self.assertEqual(payload["approved_count"], 1)
        self.assertTrue(valid_code.is_approved)
        self.assertFalse(invalid_code.is_approved)

    def test_publish_requires_all_codes_to_be_approved_first(self):
        exam_set = self._create_exam_set(number_of_versions=1)

        publish_response = self.client.post(
            reverse("qna:lecturer_publish_exam_set", args=[exam_set.id])
        )
        self.assertEqual(publish_response.status_code, 400)
        self.assertEqual(publish_response.json()["status"], "FAIL")

        exam_code = exam_set.exam_codes.get()
        approve_response = self.client.post(
            reverse("qna:lecturer_approve_exam_code", args=[exam_code.id])
        )
        self.assertEqual(approve_response.status_code, 200)

        publish_response = self.client.post(
            reverse("qna:lecturer_publish_exam_set", args=[exam_set.id])
        )
        self.assertEqual(publish_response.status_code, 200)

        exam_set.refresh_from_db()
        self.assertEqual(exam_set.status, ExamSetStatus.APPROVED)

    def test_updating_approved_exam_code_downgrades_exam_set_to_draft(self):
        exam_set = self._create_exam_set(number_of_versions=1)
        exam_code = exam_set.exam_codes.get()

        approve_response = self.client.post(
            reverse("qna:lecturer_approve_exam_code", args=[exam_code.id])
        )
        self.assertEqual(approve_response.status_code, 200)

        publish_response = self.client.post(
            reverse("qna:lecturer_publish_exam_set", args=[exam_set.id])
        )
        self.assertEqual(publish_response.status_code, 200)

        exam_code.refresh_from_db()
        items = list(exam_code.exam_code_questions.select_related("question_id").all())
        update_response = self._post_json(
            "qna:lecturer_update_exam_code_content",
            {
                "items": [
                    {
                        "id": item.id,
                        "display_order": item.display_order,
                        "difficulty": item.difficulty,
                        "content": f"{item.question.question_text} da sua",
                    }
                    for item in items
                ]
            },
            args=[exam_code.id],
        )

        self.assertEqual(update_response.status_code, 200)
        exam_set.refresh_from_db()
        exam_code.refresh_from_db()
        self.assertEqual(exam_set.status, ExamSetStatus.DRAFT)
        self.assertFalse(exam_code.is_approved)

    def test_used_exam_code_cannot_be_edited(self):
        exam_set = self._create_exam_set(number_of_versions=1)
        exam_code = exam_set.exam_codes.get()
        self._link_exam_code(exam_code)
        items = list(exam_code.exam_code_questions.select_related("question_id").all())

        response = self._post_json(
            "qna:lecturer_update_exam_code_content",
            {
                "items": [
                    {
                        "id": item.id,
                        "display_order": item.display_order,
                        "difficulty": item.difficulty,
                        "content": f"{item.question.question_text} da sua",
                    }
                    for item in items
                ]
            },
            args=[exam_code.id],
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["status"], "FAIL")

    def test_update_exam_code_content_is_blocked_while_exam_is_ongoing(self):
        exam_set = self._create_exam_set(number_of_versions=1)
        exam_code = exam_set.exam_codes.get()
        items = list(exam_code.exam_code_questions.select_related("question_id").all())
        self._create_active_exam_group(exam_code)

        response = self._post_json(
            "qna:lecturer_update_exam_code_content",
            {
                "items": [
                    {
                        "id": item.id,
                        "display_order": item.display_order,
                        "difficulty": item.difficulty,
                        "content": f"{item.question.question_text} dang khoa",
                    }
                    for item in items
                ]
            },
            args=[exam_code.id],
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["status"], "FAIL")
        self.assertTrue(response.json()["message"])

    def test_publish_exam_set_is_blocked_while_exam_is_ongoing(self):
        exam_set = self._create_exam_set(number_of_versions=1)
        exam_code = exam_set.exam_codes.get()
        exam_code.is_approved = True
        exam_code.save(update_fields=["is_approved", "updated_at"])
        self._create_active_exam_group(exam_code)

        response = self.client.post(
            reverse("qna:lecturer_publish_exam_set", args=[exam_set.id])
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["status"], "FAIL")
        self.assertTrue(response.json()["message"])


class LecturerExamSessionManagementTests(TestCase):
    def setUp(self):
        self.user_model = get_user_model()
        self.lecturer = self.user_model.objects.create_user(
            username="lecturer_sessions",
            password="Password123!",
        )
        self.profile = UserProfile.objects.create(
            user=self.lecturer,
            is_lecturer=True,
            full_name="Giang Vien Session",
        )
        self.subject = Subject.objects.create(
            name="To chuc ca thi",
            subject_code="DS403",
        )
        self.profile.subjects_taught.add(self.subject)
        self.client.force_login(self.lecturer)

        self.bank = QuestionBank.objects.create(
            subject=self.subject,
            name="Ngan hang ca thi",
        )

        for index in range(3):
            Question.objects.create(
                subject=self.subject,
                bank=self.bank,
                question_text=f"Easy session {index}",
                question_id_in_barem=f"SE_{index}",
                difficulty=DifficultyLevel.EASY,
            )
            Question.objects.create(
                subject=self.subject,
                bank=self.bank,
                question_text=f"Medium session {index}",
                question_id_in_barem=f"SM_{index}",
                difficulty=DifficultyLevel.MEDIUM,
            )
            Question.objects.create(
                subject=self.subject,
                bank=self.bank,
                question_text=f"Hard session {index}",
                question_id_in_barem=f"SH_{index}",
                difficulty=DifficultyLevel.HARD,
            )

        self.student_user_1 = self.user_model.objects.create_user(
            username="SV9001",
            password="Password123!",
        )
        UserProfile.objects.create(
            user=self.student_user_1,
            is_lecturer=False,
            full_name="Sinh Vien 1",
            student_id="SV9001",
        )
        self.student_user_2 = self.user_model.objects.create_user(
            username="SV9002",
            password="Password123!",
        )
        UserProfile.objects.create(
            user=self.student_user_2,
            is_lecturer=False,
            full_name="Sinh Vien 2",
            student_id="SV9002",
        )

        self.roster = StudentRosterUpload.objects.create(
            subject=self.subject,
            academic_year="2025-2026",
            semester="HK1",
            title="Danh_sach_L01",
            original_file_name="Danh_sach_L01.xlsx",
            total_students=2,
            created_by=self.lecturer,
        )
        self.roster_student_1 = StudentRosterStudent.objects.create(
            roster=self.roster,
            row_number=1,
            student_code="SV9001",
            full_name="Sinh Vien 1",
            linked_user=self.student_user_1,
            account_created=True,
            account_status=StudentRosterAccountStatus.EXISTING,
        )
        self.roster_student_2 = StudentRosterStudent.objects.create(
            roster=self.roster,
            row_number=2,
            student_code="SV9002",
            full_name="Sinh Vien 2",
            linked_user=self.student_user_2,
            account_created=True,
            account_status=StudentRosterAccountStatus.EXISTING,
        )

        self.exam_set = self._create_approved_exam_set()
        self.exam_codes = list(self.exam_set.exam_codes.order_by("code_number", "exam_code_id"))


@override_settings(CACHES=TEST_CACHES)
class StudentExamAccessTests(TestCase):
    def setUp(self):
        self.user_model = get_user_model()
        self.lecturer = self.user_model.objects.create_user(username="lecturer_exam_access", password="testpass")
        self.lecturer_profile = UserProfile.objects.create(user=self.lecturer, is_lecturer=True)
        self.student = self.user_model.objects.create_user(username="SVEXAM01", password="testpass")
        UserProfile.objects.create(user=self.student, is_lecturer=False, full_name="Sinh Vien Exam",
                                   student_id="SVEXAM01")
        self.subject = Subject.objects.create(name="Mon thi truy cap", subject_code="DS500")
        self.lecturer_profile.subjects_taught.add(self.subject)

        self.exam_group = ExamSessionGroup.objects.create(
            subject=self.subject,
            group_name="Ca thi truy cap",
            academic_year="2025-2026",
            semester="HK1",
            exam_date=timezone.now() + timedelta(minutes=15),
            duration_minutes=60,
            status="SCHEDULED",
            created_by=self.lecturer,
            configuration_data={"rooms": [], "rosters": []},
        )

        self.room = self.exam_group.session_rooms.create(
            room_name="Phong 101",
            expected_students=1,
            room_password="ABCDE",
            display_order=1,
        )
        self.room.students.add(self.student)

        self.bank = QuestionBank.objects.create(
            subject=self.subject,
            name="Ngan hang ca thi",
        )

        for index in range(3):
            Question.objects.create(
                subject=self.subject,
                bank=self.bank,
                question_text=f"Easy session {index}",
                question_id_in_barem=f"SE_{index}",
                difficulty=DifficultyLevel.EASY,
            )
            Question.objects.create(
                subject=self.subject,
                bank=self.bank,
                question_text=f"Medium session {index}",
                question_id_in_barem=f"SM_{index}",
                difficulty=DifficultyLevel.MEDIUM,
            )
            Question.objects.create(
                subject=self.subject,
                bank=self.bank,
                question_text=f"Hard session {index}",
                question_id_in_barem=f"SH_{index}",
                difficulty=DifficultyLevel.HARD,
            )

        self.student_user_1 = self.user_model.objects.create_user(
            username="SV9001",
            password="Password123!",
        )
        UserProfile.objects.create(
            user=self.student_user_1,
            is_lecturer=False,
            full_name="Sinh Vien 1",
            student_id="SV9001",
        )
        self.student_user_2 = self.user_model.objects.create_user(
            username="SV9002",
            password="Password123!",
        )
        UserProfile.objects.create(
            user=self.student_user_2,
            is_lecturer=False,
            full_name="Sinh Vien 2",
            student_id="SV9002",
        )

        self.roster = StudentRosterUpload.objects.create(
            subject=self.subject,
            academic_year="2025-2026",
            semester="HK1",
            title="Danh_sach_L01",
            original_file_name="Danh_sach_L01.xlsx",
            total_students=2,
            created_by=self.lecturer,
        )
        self.roster_student_1 = StudentRosterStudent.objects.create(
            roster=self.roster,
            row_number=1,
            student_code="SV9001",
            full_name="Sinh Vien 1",
            linked_user=self.student_user_1,
            account_created=True,
            account_status=StudentRosterAccountStatus.EXISTING,
        )
        self.roster_student_2 = StudentRosterStudent.objects.create(
            roster=self.roster,
            row_number=2,
            student_code="SV9002",
            full_name="Sinh Vien 2",
            linked_user=self.student_user_2,
            account_created=True,
            account_status=StudentRosterAccountStatus.EXISTING,
        )

        self.client.force_login(self.lecturer)
        self.exam_set = self._create_approved_exam_set()
        self.exam_codes = list(self.exam_set.exam_codes.order_by("code_number", "exam_code_id"))

    def _open_exam_window(self):
        self.exam_group.exam_date = timezone.now() - timedelta(minutes=5)
        self.exam_group.duration_minutes = 30
        self.exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])

    def _verify_room_password(self, password="ABCDE"):
        return self.client.post(
            reverse("qna:verify_exam_password", args=[self.subject.subject_code]),
            {"password": password},
        )

    def _verify_face(self):
            return self.client.post(
                reverse("qna:verify_face"),
                {
                    "face_image": "data:image/jpeg;base64,ZmFrZQ==",
                    "subject_code": self.subject.subject_code,
                },
            )

    def _complete_pre_exam_flow(self):
        password_response = self._verify_room_password()
        self.assertEqual(password_response.status_code, 302)
        face_response = self._verify_face()
        self.assertEqual(face_response.status_code, 200)
        return face_response

    def _open_verified_exam_page(self):
        self._open_exam_window()
        self.client.force_login(self.student)
        verify_response = self._complete_pre_exam_flow()
        self.assertEqual(verify_response.status_code, 200)
        response = self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]))
        self.assertEqual(response.status_code, 200)
        return response

    def test_student_dashboard_disables_exam_button_when_exam_not_open(self):
                self.client.force_login(self.student)
                response = self.client.get(reverse("qna:dashboard"))
                self.assertEqual(response.status_code, 200)
                subject_card = next(item for item in response.context["subject_cards"] if item["subject"] == self.subject)
                self.assertEqual(subject_card["entry_button_label"], "Chưa thể vào thi")
                self.assertEqual(subject_card["status_label"], "Chưa đến giờ thi")

    def test_exam_password_rejects_access_outside_exam_window(self):
                self.client.force_login(self.student)
                response = self.client.get(reverse("qna:exam_password", args=[self.subject.subject_code]), )
                self.assertEqual(response.status_code, 302)

    def test_verify_exam_password_accepts_matching_room_password_during_open_window(self):
                self._open_exam_window()
                self.client.force_login(self.student)

                response = self._verify_room_password()

                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("qna:pre_exam_verification", args=[self.subject.subject_code]), response.url)
                self.assertEqual(ExamSession.objects.filter(subject_id=self.subject).count(), 0)
                password_state = self.client.session.get(views._student_exam_password_session_key(self.exam_group.pk))
                self.assertEqual(password_state["room_id"], self.room.pk)

    def test_exam_page_rejects_when_password_not_verified(self):
                self._open_exam_window()
                self.client.force_login(self.student)

                response = self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]), )

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, reverse("qna:exam_password", args=[self.subject.subject_code]))
                self.assertFalse(
                    ExamSession.objects.filter(subject_id=self.subject, exam_session_group_id=self.exam_group).exists())

    def test_exam_page_rejects_when_face_verification_missing(self):
                self._open_exam_window()
                self.client.force_login(self.student)
                self._verify_room_password()

                response = self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]), )

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, reverse("qna:pre_exam_verification", args=[self.subject.subject_code]))
                self.assertFalse(
                    ExamSession.objects.filter(subject_id=self.subject, exam_session_group_id=self.exam_group).exists())

    def test_exam_page_rejects_when_entry_token_missing(self):
                self._open_exam_window()
                self.client.force_login(self.student)
                self._complete_pre_exam_flow()

                session_store = self.client.session
                session_store.pop(views._student_exam_entry_session_key(self.exam_group.pk), None)
                session_store.save()

                response = self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]), )

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, reverse("qna:pre_exam_verification", args=[self.subject.subject_code]))
                self.assertFalse(
                    ExamSession.objects.filter(subject_id=self.subject, exam_session_group_id=self.exam_group).exists())

    def test_student_dashboard_enables_exam_button_during_open_window_without_room_password(self):
                self.exam_group.exam_date = timezone.now() - timedelta(minutes=5)
                self.exam_group.duration_minutes = 30
                self.exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])
                self.room.room_password = ""
                self.room.save(update_fields=["room_password"])
                self.client.force_login(self.student)

                response = self.client.get(reverse("qna:dashboard"))

                self.assertEqual(response.status_code, 200)
                subject_card = next(item for item in response.context["subject_cards"] if item["subject"] == self.subject)
                self.assertEqual(subject_card["status_label"], "Đang trong ca thi")
                self.assertEqual(subject_card["entry_button_label"], "Vào thi")

    def test_exam_password_redirects_directly_to_verification_when_room_has_no_password(self):
                self.exam_group.exam_date = timezone.now() - timedelta(minutes=5)
                self.exam_group.duration_minutes = 30
                self.exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])
                self.room.room_password = ""
                self.room.save(update_fields=["room_password"])
                self.client.force_login(self.student)

                response = self.client.get(reverse("qna:exam_password", args=[self.subject.subject_code]), )

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, reverse("qna:pre_exam_verification", args=[self.subject.subject_code]))

    def test_exam_page_rejects_direct_url_after_exam_window_closes(self):
                self._open_exam_window()
                Question.objects.create(
                    subject=self.subject,
                    question_text="Câu hỏi đồng bộ thời gian",
                    question_id_in_barem="Q_LOCK_TIME",
                    difficulty=DifficultyLevel.EASY,
                )
                self.client.force_login(self.student)

                verify_response = self._complete_pre_exam_flow()
                self.assertEqual(verify_response.status_code, 200)

                self.exam_group.exam_date = timezone.now() - timedelta(hours=2)
                self.exam_group.duration_minutes = 30
                self.exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])

                response = self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]), )

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, reverse("qna:dashboard"))

    def test_exam_page_renders_server_synced_deadline_constants(self):
        self._open_exam_window()
        Question.objects.create(
            subject=self.subject,
            question_text="Cau hoi hien thi deadline",
            question_id_in_barem="Q_SERVER_TIME",
            difficulty=DifficultyLevel.EASY,
        )
        self.client.force_login(self.student)

        verify_response = self._complete_pre_exam_flow()
        self.assertEqual(verify_response.status_code, 200)

        response = self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]), )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "const SERVER_NOW_MS =")
        self.assertContains(response, "const EXAM_END_AT_MS =")
        self.assertEqual(
            ExamSession.objects.filter(subject_id=self.subject, exam_session_group_id=self.exam_group).count(),
            1)

    def test_exam_page_js_visibility_change_only_warns(self):
        response = self._open_verified_exam_page()
        html = response.content.decode("utf-8")

        visibility_block = re.search(
            r"document\.addEventListener\('visibilitychange', \(\) => \{(?P<body>.*?)\n\s*\}\);",
            html,
            re.S,
        )

        self.assertIsNotNone(visibility_block)
        body = visibility_block.group("body")
        self.assertIn("alert('Không được chuyển tab khi thi!');", body)
        self.assertNotIn("forceExamTermination", body)
        self.assertNotIn("finalizeAndShowResult", body)

    def test_exam_page_renders_realtime_progress_panel_and_pagehide_finalize_hook(self):
        response = self._open_verified_exam_page()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="grading-stage-text"')
        self.assertNotContains(response, 'id="grading-transcript-text"')
        self.assertNotContains(response, 'id="grading-rephrased-text"')
        self.assertContains(response, "message.type === 'exam.progress'")
        self.assertContains(response, "/api/exam-progress/")
        self.assertNotContains(response, "navigator.sendBeacon")
        self.assertNotContains(response, "window.addEventListener('pagehide'")

    def test_exam_page_js_timeout_flushes_audio_before_finalize(self):
        response = self._open_verified_exam_page()
        html = response.content.decode("utf-8")

        onstop_block = re.search(
            r"state\.mediaRecorder\.onstop = async \(\) => \{(?P<body>.*?)\n\s*\};",
            html,
            re.S,
        )

        self.assertIsNotNone(onstop_block)
        self.assertIn("const shouldAllowLateAnswerFlush = () => state.pendingExamFinalize && state.pendingFinalizeIsTimeout;", html)
        self.assertIn("allow_after_expiry: allowLateAnswerFlush", html)
        self.assertIn("audio_mime_type: audioMimeType", html)
        self.assertIn("await submitAudio(audioBlob);", onstop_block.group("body"))
        self.assertNotIn("finalizeAndShowResult", onstop_block.group("body"))
        self.assertNotIn("skipAudioSubmit", html)

    def test_exam_page_js_face_violations_do_not_force_finalize(self):
        response = self._open_verified_exam_page()
        html = response.content.decode("utf-8")

        camera_block = re.search(
            r"function handleCameraInterrupted\(\) \{(?P<body>.*?)\n\s*\}\n\n\s*function sendProctoringViolation",
            html,
            re.S,
        )
        no_face_block = re.search(
            r"if \(detections\.length === 0\) \{(?P<body>.*?)\n\s*\} else \{",
            html,
            re.S,
        )
        multi_face_block = re.search(
            r"if \(detections\.length > PROCTORING_CONFIG\.MULTIPLE_FACES_LIMIT\) \{(?P<body>.*?)\n\s*\} else \{",
            html,
            re.S,
        )

        self.assertIsNotNone(camera_block)
        self.assertIsNotNone(no_face_block)
        self.assertIsNotNone(multi_face_block)
        self.assertNotIn("forceExamTermination", camera_block.group("body"))
        self.assertNotIn("forceExamTermination", no_face_block.group("body"))
        self.assertNotIn("forceExamTermination", multi_face_block.group("body"))
        self.assertIn("showProctoringWarning", camera_block.group("body"))
        self.assertIn("showProctoringWarning", no_face_block.group("body"))
        self.assertIn("showProctoringWarning", multi_face_block.group("body"))

    def test_student_dashboard_shows_finished_exam_as_outside_exam_window(self):
                self.exam_group.exam_date = timezone.now() - timedelta(hours=2)
                self.exam_group.duration_minutes = 30
                self.exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])
                self.client.force_login(self.student)

                response = self.client.get(reverse("qna:dashboard"))

                self.assertEqual(response.status_code, 200)
                subject_card = next(item for item in response.context["subject_cards"] if item["subject"] == self.subject)
                self.assertEqual(subject_card["status_label"], "Ngoài thời gian thi")
                self.assertEqual(subject_card["entry_button_label"], "Chưa thể vào thi")

    def test_student_dashboard_renders_realtime_live_note_hook(self):
                self.client.force_login(self.student)

                response = self.client.get(reverse("qna:dashboard"))

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "data-live-note")
                self.assertContains(response, "data-entry-button")

    def test_student_dashboard_disables_entry_after_exam_started(self):
                self._open_exam_window()
                ExamSession.objects.create(
                    user=self.student,
                    subject=self.subject,
                    exam_session_group_id=self.exam_group,
                    started_at=timezone.now(),
                    session_status="STARTED",
                    verification_status="ALLOW",
                )
                self.client.force_login(self.student)

                response = self.client.get(reverse("qna:dashboard"))

                subject_card = next(item for item in response.context["subject_cards"] if item["subject"] == self.subject)
                self.assertEqual(subject_card["entry_lock_state"], "STARTED")
                self.assertEqual(subject_card["entry_button_label"], "Tiếp tục thi")
                self.assertEqual(subject_card["status_label"], "Đang thực hiện ca thi")

    def test_verify_face_does_not_create_exam_session_before_exam_entry(self):
                self._open_exam_window()
                self.client.force_login(self.student)
                self._verify_room_password()

                response = self._verify_face()

                self.assertEqual(response.status_code, 200)
                self.assertEqual(ExamSession.objects.filter(subject_id=self.subject).count(), 0)
                entry_state = self.client.session.get(views._student_exam_entry_session_key(self.exam_group.pk))
                self.assertEqual(entry_state["room_id"], self.room.pk)
                self.assertTrue(entry_state["entry_token"])

    def test_save_exam_result_is_rejected_after_exam_window_closes(self):
                question = Question.objects.create(
                    subject=self.subject,
                    question_text="Cau hoi nop sau gio",
                    question_id_in_barem="Q_AFTER_WINDOW",
                    difficulty=DifficultyLevel.EASY,
                )
                session = ExamSession.objects.create(
                    user=self.student,
                    subject=self.subject,
                    exam_session_group_id=self.exam_group,
                    started_at=timezone.now(),
                    session_status="STARTED",
                    verification_status="ALLOW",
                )
                session.questions.add(question)
                ExamResult.objects.create(
                    exam_session_id=session,
                    question_id=question,
                    transcript="Ban ghi server",
                    score=8.0,
                    feedback="Nhan xet server",
                )
                self.client.force_login(self.student)

                self.exam_group.exam_date = timezone.now() - timedelta(hours=2)
                self.exam_group.duration_minutes = 30
                self.exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])

                response = self.client.post(
                    reverse("qna:save_exam_result"),
                    {
                        "session_id": session.id,
                        "question_id": question.id,
                        "audio_file": SimpleUploadedFile(
                            "answer.webm",
                            b"fake-webm-data",
                            content_type="audio/webm",
                        ),
                    },
                )

                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["status"], "error")
                self.assertIn("gian thi", response.json()["message"].lower())

    def test_student_can_only_enter_exam_page_once(self):
                self._open_exam_window()
                Question.objects.create(
                    subject=self.subject,
                    question_text="Cau hoi thi 1",
                    question_id_in_barem="Q1",
                    difficulty=DifficultyLevel.EASY,
                )
                self.client.force_login(self.student)

                verify_response = self._complete_pre_exam_flow()
                self.assertEqual(verify_response.status_code, 200)

                first_entry = self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]), )
                self.assertEqual(first_entry.status_code, 200)

                second_entry = self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]), )
                session = ExamSession.objects.get(subject_id=self.subject, exam_session_group_id=self.exam_group)
                session.refresh_from_db()

                self.assertEqual(second_entry.status_code, 200)
                self.assertEqual(session.session_status, "STARTED")
                self.assertIsNone(session.completed_at)
                self.assertEqual(
                    ExamSession.objects.filter(subject_id=self.subject, exam_session_group_id=self.exam_group).count(),
                    1)

    def test_verify_face_rejects_reentry_after_student_started_exam(self):
                self._open_exam_window()
                Question.objects.create(
                    subject=self.subject,
                    question_text="Cau hoi thi 2",
                    question_id_in_barem="Q2",
                    difficulty=DifficultyLevel.EASY,
                )
                self.client.force_login(self.student)

                self._complete_pre_exam_flow()
                self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]), )

                self._verify_room_password()
                reentry_response = self._verify_face()
                session = ExamSession.objects.get(subject_id=self.subject, exam_session_group_id=self.exam_group)
                session.refresh_from_db()

                self.assertEqual(reentry_response.status_code, 200)
                self.assertEqual(reentry_response.json()["status"], "success")
                self.assertTrue(reentry_response.json()["resume"])
                self.assertEqual(
                    reentry_response.json()["redirect_url"],
                    reverse("qna:exam_page", args=[self.subject.subject_code]),
                )
                self.assertEqual(session.session_status, "STARTED")
                self.assertIsNone(session.completed_at)

    def test_exam_page_resume_includes_existing_results_for_answered_questions(self):
                response = self._open_verified_exam_page()
                self.assertEqual(response.status_code, 200)

                session = ExamSession.objects.get(subject_id=self.subject, exam_session_group_id=self.exam_group)
                question = Question.objects.create(
                    subject=self.subject,
                    question_text="Cau hoi da tra loi",
                    question_id_in_barem="Q_RESUME_1",
                    difficulty=DifficultyLevel.EASY,
                )
                session.questions.set([question])
                ExamResult.objects.create(
                    exam_session_id=session,
                    question_id=question,
                    transcript="Ban ghi da co",
                    score=8.0,
                    feedback="Nhan xet da co",
                )

                resumed_response = self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]))

                self.assertEqual(resumed_response.status_code, 200)
                self.assertEqual(len(resumed_response.context["existing_results"]), 1)
                self.assertEqual(resumed_response.context["existing_results"][0]["question_id"], question.id)
                self.assertEqual(resumed_response.context["existing_results"][0]["transcript"], "Ban ghi da co")

    def _post_json(self, route_name, payload, args=None):
                return self.client.post(
                    reverse(route_name, args=args or []),
                    data=json.dumps(payload),
                    content_type="application/json",
                )

    def _create_approved_exam_set(self):
                response = self._post_json(
                    "qna:lecturer_create_exam_set",
                    {
                        "subject_id": str(self.subject.id),
                        "academic_year": "2025-2026",
                        "semester": "HK1",
                        "number_of_versions": 2,
                        "easy_count": 1,
                        "medium_count": 1,
                        "hard_count": 1,
                        "easy_score": 2,
                        "medium_score": 3,
                        "hard_score": 5,
                        "allow_duplicate_questions": False,
                        "source_bank_ids": [self.bank.id],
                    },
                )
                self.assertEqual(response.status_code, 200)
                exam_set = ExamSet.objects.get(pk=response.json()["exam_set_id"])

                for exam_code in exam_set.exam_codes.all():
                    approve_response = self.client.post(
                        reverse("qna:lecturer_approve_exam_code", args=[exam_code.id])
                    )
                    self.assertEqual(approve_response.status_code, 200)

                publish_response = self.client.post(
                    reverse("qna:lecturer_publish_exam_set", args=[exam_set.id])
                )
                self.assertEqual(publish_response.status_code, 200)
                exam_set.refresh_from_db()
                return exam_set

    def _build_session_payload(self, submit_action="CONFIRM"):
                future_start = timezone.localtime(timezone.now() + timedelta(days=2)).replace(
                    hour=8,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                future_end = future_start + timedelta(minutes=90)
                return {
                    "subject_id": self.subject.id,
                    "group_name": "Thi giua ky DS403",
                    "academic_year": "2025-2026",
                    "semester": "HK1",
                    "exam_date": future_start.strftime("%Y-%m-%d"),
                    "start_time": future_start.strftime("%H:%M"),
                    "end_time": future_end.strftime("%H:%M"),
                    "description": "Ca thi được tạo từ wizard mới",
                    "exam_password": "EXAM123",
                    "roster_ids": [self.roster.id],
                    "submit_action": submit_action,
                    "rooms": [
                        {
                            "client_id": "room-1",
                            "room_name": "Phong A101",
                            "student_count": 1,
                            "password": "A1011",
                            "exam_set_id": self.exam_set.id,
                            "exam_code_ids": [self.exam_codes[0].id],
                            "assignments": [
                                {
                                    "roster_student_id": self.roster_student_1.id,
                                    "student_code": self.roster_student_1.student_code,
                                    "full_name": self.roster_student_1.full_name,
                                    "linked_user_id": self.student_user_1.id,
                                    "exam_code_id": self.exam_codes[0].id,
                                    "display_order": 1,
                                }
                            ],
                            "display_order": 1,
                        },
                        {
                            "client_id": "room-2",
                            "room_name": "Phong A102",
                            "student_count": 1,
                            "password": "A1022",
                            "exam_set_id": self.exam_set.id,
                            "exam_code_ids": [self.exam_codes[1].id],
                            "assignments": [
                                {
                                    "roster_student_id": self.roster_student_2.id,
                                    "student_code": self.roster_student_2.student_code,
                                    "full_name": self.roster_student_2.full_name,
                                    "linked_user_id": self.student_user_2.id,
                                    "exam_code_id": self.exam_codes[1].id,
                                    "display_order": 1,
                                }
                            ],
                            "display_order": 2,
                        },
                    ],
                }

    def _create_exam_group(self, submit_action="CONFIRM", **overrides):
                payload = self._build_session_payload(submit_action=submit_action)
                payload.update(overrides)
                response = self._post_json(
                    "qna:lecturer_create_exam_group_screen",
                    payload,
                )
                self.assertEqual(response.status_code, 200)
                return ExamSessionGroup.objects.get(pk=response.json()["exam_group_id"])

    def test_create_session_screen_renders_new_wizard(self):
                response = self.client.get(
                    reverse("qna:lecturer_create_session_screen"),
                    {"subject_id": self.subject.id},
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'data-step-panel="1"')
                self.assertContains(response, 'data-step-panel="2"')
                self.assertContains(response, 'data-step-panel="3"')
                self.assertContains(response, "wizardSubmitButton")
                self.assertNotContains(response, "Tao de thi moi")

    def test_create_session_screen_uses_clean_room_fields(self):
                response = self.client.get(
                    reverse("qna:lecturer_create_session_screen"),
                    {"subject_id": self.subject.id},
                )

                self.assertEqual(response.status_code, 200)
                content = response.content.decode("utf-8")
                self.assertIn('class="es-input es-room-count-input"', content)
                self.assertIn('placeholder="Mật khẩu tự random"', content)
                self.assertNotIn('placeholder="Nhập 5 ký tự"', content)
                self.assertNotIn('data-room-step="increase"', content)
                self.assertNotIn('${assignedCount}/${room.student_count || 0} SV', content)
                self.assertNotIn('Giảng viên tự nhập đúng 5 ký tự cho phòng thi.', content)
                self.assertNotIn(': "Chưa gắn bộ đề";', content)
                self.assertNotIn(': "Chưa gắn";', content)
                self.assertIsNone(re.search("[\\u00C3\\u00C2\\u00C4\\u00C5]", content))

    def test_create_exam_group_screen_persists_session_configuration(self):
                response = self._post_json(
                    "qna:lecturer_create_exam_group_screen",
                    self._build_session_payload(),
                )

                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertTrue(payload["success"])

                exam_group = ExamSessionGroup.objects.get(pk=payload["exam_group_id"])
                self.assertEqual(exam_group.subject, self.subject)
                self.assertEqual(exam_group.academic_year, "2025-2026")
                self.assertEqual(exam_group.semester, "HK1")
                self.assertEqual(exam_group.description, "Ca thi được tạo từ wizard mới")
                self.assertEqual(exam_group.status, "SCHEDULED")
                self.assertEqual(exam_group.exam_codes.count(), 2)
                self.assertEqual(exam_group.session_rooms.count(), 2)
                self.assertEqual(exam_group.session_rooms.first().exam_set, self.exam_set)

    def test_create_exam_group_screen_keeps_same_day_duration_behavior(self):
                response = self._post_json(
                    "qna:lecturer_create_exam_group_screen",
                    self._build_session_payload(),
                )

                self.assertEqual(response.status_code, 200)
                exam_group = ExamSessionGroup.objects.get(pk=response.json()["exam_group_id"])
                self.assertEqual(exam_group.duration_minutes, 90)
                self.assertEqual(
                    timezone.localtime(exam_group.start_at).strftime("%Y-%m-%d %H:%M"),
                    f"{timezone.localtime(exam_group.start_at).strftime('%Y-%m-%d')} 08:00",
                )
                self.assertEqual(
                    timezone.localtime(exam_group.end_at).strftime("%Y-%m-%d %H:%M"),
                    f"{timezone.localtime(exam_group.start_at).strftime('%Y-%m-%d')} 09:30",
                )

    def test_create_exam_group_screen_allows_overnight_session_and_computes_duration(self):
                payload = self._build_session_payload()
                overnight_start = timezone.localtime(timezone.now() + timedelta(days=3)).replace(
                    hour=23,
                    minute=30,
                    second=0,
                    microsecond=0,
                )
                payload["exam_date"] = overnight_start.strftime("%Y-%m-%d")
                payload["start_time"] = "23:30"
                payload["end_time"] = "01:00"

                response = self._post_json(
                    "qna:lecturer_create_exam_group_screen",
                    payload,
                )

                self.assertEqual(response.status_code, 200)
                payload_json = response.json()
                self.assertTrue(payload_json["success"])
                self.assertNotEqual(
                    payload_json.get("message"),
                    "Thời gian kết thúc phải sau thời gian bắt đầu.",
                )

                exam_group = ExamSessionGroup.objects.get(pk=payload_json["exam_group_id"])
                self.assertEqual(exam_group.duration_minutes, 90)
                self.assertEqual(timezone.localtime(exam_group.start_at).strftime("%Y-%m-%d %H:%M"), overnight_start.strftime("%Y-%m-%d 23:30"))
                self.assertEqual(
                    timezone.localtime(exam_group.end_at).strftime("%Y-%m-%d %H:%M"),
                    (overnight_start + timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M"),
                )

    def test_create_exam_group_screen_rejects_invalid_datetime_payload(self):
                payload = self._build_session_payload()
                payload["start_time"] = "khong-hop-le"

                response = self._post_json(
                    "qna:lecturer_create_exam_group_screen",
                    payload,
                )

                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["success"])
                self.assertEqual(response.json()["status"], "FAIL")
                self.assertEqual(response.json()["message"], "Định dạng ngày giờ không hợp lệ.")

    def test_create_exam_group_screen_rejects_invalid_academic_year_format(self):
                payload = self._build_session_payload()
                payload["academic_year"] = "2025/2026"

                response = self._post_json(
                    "qna:lecturer_create_exam_group_screen",
                    payload,
                )

                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["success"])
                self.assertEqual(response.json()["status"], "FAIL")
                self.assertEqual(
                    response.json()["message"],
                    "Năm học phải đúng định dạng xxxx-xxxx và chỉ được chứa chữ số.",
                )

    def test_random_assign_students_updates_configuration_data_source_of_truth(self):
                exam_group = self._create_exam_group()

                response = self.client.post(
                    reverse("qna:lecturer_random_assign_students", args=[exam_group.id])
                )

                self.assertEqual(response.status_code, 200)
                exam_group.refresh_from_db()

                room_configs = exam_group.configuration_data["rooms"]
                self.assertEqual(len(room_configs), 2)
                mirrored_user_ids = {
                    room.display_order: set(room.students.values_list("id", flat=True))
                    for room in exam_group.session_rooms.order_by("display_order")
                }
                for room_cfg in room_configs:
                    room_order = room_cfg["display_order"]
                    assignment_user_ids = {
                        assignment["linked_user_id"]
                        for assignment in room_cfg.get("assignments") or []
                    }
                    self.assertEqual(assignment_user_ids, mirrored_user_ids[room_order])

                room_cfg, assignment = views._get_room_assignment_for_user(exam_group, self.student_user_1)
                self.assertIsNotNone(room_cfg)
                self.assertIsNotNone(assignment)
                self.assertTrue(assignment["exam_code_id"])

    def test_import_students_to_room_updates_configuration_data_source_of_truth(self):
                exam_group = self._create_exam_group()
                room = exam_group.session_rooms.order_by("display_order").first()

                config = exam_group.configuration_data
                config["rooms"][0]["assignments"] = []
                exam_group.configuration_data = config
                exam_group.save(update_fields=["configuration_data", "updated_at"])
                room.students.clear()

                upload = SimpleUploadedFile(
                    "room_import.csv",
                    "username\nSV9001\n".encode("utf-8-sig"),
                    content_type="text/csv",
                )

                response = self.client.post(
                    reverse("qna:lecturer_import_students_to_room", args=[room.id]),
                    {"csv_file": upload},
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["imported_count"], 1)

                exam_group.refresh_from_db()
                room.refresh_from_db()
                self.assertEqual(
                    exam_group.configuration_data["rooms"][0]["assignments"][0]["student_code"],
                    "SV9001",
                )
                self.assertTrue(room.students.filter(pk=self.student_user_1.id).exists())
                self.assertEqual(exam_group.configuration_data["rosters"][0]["id"], self.roster.id)
                self.assertEqual(len(exam_group.configuration_data["rooms"]), 2)
                self.assertEqual(
                    exam_group.configuration_data["rooms"][0]["assignments"][0]["student_code"],
                    "SV9001",
                )

    def test_exam_sessions_list_filters_by_status_and_keyword(self):
                confirmed = self._post_json(
                    "qna:lecturer_create_exam_group_screen",
                    self._build_session_payload(),
                )
                self.assertEqual(confirmed.status_code, 200)

                draft_payload = self._build_session_payload(submit_action="DRAFT")
                draft_payload["group_name"] = "Nhap tam DS403"
                draft_payload["roster_ids"] = []
                draft_payload["rooms"] = []
                draft_response = self._post_json(
                    "qna:lecturer_create_exam_group_screen",
                    draft_payload,
                )
                self.assertEqual(draft_response.status_code, 200)

                response = self.client.get(
                    reverse("qna:lecturer_exam_sessions_list"),
                    {
                        "subject_id": self.subject.id,
                        "status": "DRAFT",
                        "q": "Nhap tam",
                    },
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Nhap tam DS403")
                self.assertNotContains(response, "Thi giua ky DS403")
                self.assertContains(response, 'name="status"')

    def test_exam_session_detail_screen_renders_new_detail_layout(self):
                exam_group = self._create_exam_group()

                response = self.client.get(
                    reverse("qna:lecturer_exam_session_detail_screen", args=[exam_group.id])
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Chi tiết ca thi")
                self.assertContains(response, exam_group.group_name)
                self.assertContains(
                    response,
                    reverse("qna:lecturer_exam_session_common_list_screen", args=[exam_group.id]),
                )
                self.assertContains(response, "Phong A101")
                self.assertContains(response, "A1011")

    def test_exam_session_common_list_screen_renders_assigned_students(self):
                exam_group = self._create_exam_group()

                response = self.client.get(
                    reverse("qna:lecturer_exam_session_common_list_screen", args=[exam_group.id])
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Xem danh sách chung")
                self.assertContains(response, "SV9001")
                self.assertContains(response, "SV9002")
                self.assertContains(response, "Phong A101")
                self.assertContains(response, "Phong A102")

    def test_delete_exam_group_removes_upcoming_session(self):
                exam_group = self._create_exam_group()

                response = self.client.post(
                    reverse("qna:lecturer_delete_exam_group", args=[exam_group.id]),
                    follow=True,
                )

                self.assertEqual(response.status_code, 200)
                self.assertFalse(ExamSessionGroup.objects.filter(pk=exam_group.id).exists())
                self.assertContains(response, "Xóa ca thi thành công")

    def test_locked_exam_session_detail_hides_edit_actions(self):
                exam_group = self._create_exam_group()
                exam_group.exam_date = timezone.now() - timedelta(hours=2)
                exam_group.status = "SCHEDULED"
                exam_group.save(update_fields=["exam_date", "status", "updated_at"])

                response = self.client.get(
                    reverse("qna:lecturer_exam_session_detail_screen", args=[exam_group.id])
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Chi tiết ca thi")
                self.assertNotContains(response, "Xóa ca thi")
                self.assertNotContains(response, "Chỉnh sửa")

    def test_edit_mode_falls_back_to_view_when_session_is_locked(self):
                exam_group = ExamSessionGroup.objects.create(
                    subject=self.subject,
                    group_name="Ca thi da khoa",
                    academic_year="2025-2026",
                    semester="HK1",
                    exam_date=timezone.now() - timedelta(hours=2),
                    duration_minutes=60,
                    status="SCHEDULED",
                    created_by=self.lecturer,
                )

                response = self.client.get(
                    reverse("qna:lecturer_create_session_screen"),
                    {
                        "subject_id": self.subject.id,
                        "session_id": exam_group.id,
                        "mode": "edit",
                    },
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "wizardViewBackButton")
                self.assertNotContains(
                    response,
                    'id="saveDraftButton" class="es-btn es-btn-secondary"',
                )

    def test_draft_session_is_auto_locked_when_exam_window_is_ongoing(self):
                exam_group = self._create_exam_group(submit_action="DRAFT")
                exam_group.exam_date = timezone.now() - timedelta(minutes=10)
                exam_group.duration_minutes = 60
                exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])

                exam_group.refresh_from_db()
                self.assertEqual(exam_group.computed_status, "ONGOING")
                self.assertFalse(exam_group.can_modify)

                response = self.client.get(
                    reverse("qna:lecturer_create_session_screen"),
                    {
                        "subject_id": self.subject.id,
                        "session_id": exam_group.id,
                        "mode": "edit",
                    },
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "wizardViewBackButton")
                self.assertContains(response, "Đang diễn ra")
                self.assertNotContains(
                    response,
                    'id="saveDraftButton" class="es-btn es-btn-secondary"',
                )

    def test_create_exam_group_screen_allows_extra_room_capacity(self):
                payload = self._build_session_payload()
                payload["rooms"][0]["student_count"] = 2
                payload["rooms"][1]["student_count"] = 3

                response = self._post_json(
                    "qna:lecturer_create_exam_group_screen",
                    payload,
                )

                self.assertEqual(response.status_code, 200)
                exam_group = ExamSessionGroup.objects.get(pk=response.json()["exam_group_id"])
                room_counts = list(
                    exam_group.session_rooms.order_by("display_order").values_list("expected_students", flat=True)
                )

                self.assertEqual(room_counts, [2, 3])

    def test_student_review_requires_subject_selection_before_showing_results(self):
                exam_group = ExamSessionGroup.objects.create(
                    subject=self.subject,
                    group_name="Ca thi xem bai lam",
                    academic_year="2025-2026",
                    semester="HK1",
                    exam_date=timezone.now() - timedelta(hours=2),
                    duration_minutes=60,
                    status="SCHEDULED",
                    created_by=self.lecturer,
                )
                ExamSession.objects.create(
                    user=self.student_user_1,
                    subject=self.subject,
                    exam_session_group_id=exam_group,
                    session_status="COMPLETED",
                    completed_at=timezone.now() - timedelta(hours=1),
                )

                response = self.client.get(reverse("qna:lecturer_student_review_screen"))

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, '<option value="">Chọn môn học</option>', html=True)
                self.assertContains(response, "Vui lòng chọn môn học để xem kết quả.")
                self.assertNotContains(response, "Danh sách Bài làm")
                self.assertNotContains(response, "Sinh Vien 1")
                self.assertIsNone(response.context["selected_subject"])
                self.assertEqual(list(response.context["sessions"]), [])

    def test_student_review_only_shows_results_for_selected_subject(self):
                other_subject = Subject.objects.create(
                    name="Mon khac",
                    subject_code="DS404",
                )
                self.profile.subjects_taught.add(other_subject)

                current_group = ExamSessionGroup.objects.create(
                    subject=self.subject,
                    group_name="Ca thi mon hien tai",
                    academic_year="2025-2026",
                    semester="HK1",
                    exam_date=timezone.now() - timedelta(hours=2),
                    duration_minutes=60,
                    status="SCHEDULED",
                    created_by=self.lecturer,
                )
                other_group = ExamSessionGroup.objects.create(
                    subject=other_subject,
                    group_name="Ca thi mon khac",
                    academic_year="2025-2026",
                    semester="HK1",
                    exam_date=timezone.now() - timedelta(hours=2),
                    duration_minutes=60,
                    status="SCHEDULED",
                    created_by=self.lecturer,
                )

                ExamSession.objects.create(
                    user=self.student_user_1,
                    subject=self.subject,
                    exam_session_group_id=current_group,
                    session_status="COMPLETED",
                    completed_at=timezone.now() - timedelta(hours=1),
                )
                ExamSession.objects.create(
                    user=self.student_user_2,
                    subject=other_subject,
                    exam_session_group_id=other_group,
                    session_status="COMPLETED",
                    completed_at=timezone.now() - timedelta(hours=1),
                )

                response = self.client.get(
                    reverse("qna:lecturer_student_review_screen"),
                    {"subject_id": self.subject.id},
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Sinh Vien 1")
                self.assertNotContains(response, "Sinh Vien 2")
                self.assertEqual(response.context["selected_subject"], self.subject)

    def test_student_review_shows_only_one_red_triangle_for_flagged_session(self):
                exam_group = self._create_exam_group()
                exam_group.exam_date = timezone.now() - timedelta(hours=2)
                exam_group.duration_minutes = 60
                exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])

                flagged_session = ExamSession.objects.create(
                    user=self.student_user_1,
                    subject=self.subject,
                    exam_session_group_id=exam_group,
                    cheating_flag=True,
                    session_status="COMPLETED",
                )
                flagged_session.violation_images.create(
                    image_blob=b"img1",
                    image_mime="image/jpeg",
                    violation_type="TWO_FACES",
                )
                ExamSession.objects.create(
                    user=self.student_user_2,
                    subject=self.subject,
                    exam_session_group_id=exam_group,
                    cheating_flag=False,
                    session_status="COMPLETED",
                )

                response = self.client.get(
                    reverse("qna:lecturer_student_review_screen"),
                    {"subject_id": self.subject.id, "exam_group_id": exam_group.id},
                )

                self.assertEqual(response.status_code, 200)
                html = response.content.decode("utf-8")
                self.assertEqual(html.count("fa-exclamation-triangle"), 1)
                self.assertNotIn("detail-link-disabled", html)

    def test_student_review_hides_no_face_only_warning_from_lecturer(self):
                exam_group = self._create_exam_group()
                session = ExamSession.objects.create(
                    user=self.student_user_1,
                    subject=self.subject,
                    exam_session_group_id=exam_group,
                    cheating_flag=True,
                )
                session.violation_images.create(
                    image_blob=b"img1",
                    image_mime="image/jpeg",
                    violation_type="NO_FACE",
                )

                response = self.client.get(
                    reverse("qna:lecturer_student_review_screen"),
                    {"subject_id": self.subject.id},
                )

                self.assertEqual(response.status_code, 200)
                html = response.content.decode("utf-8")
                self.assertEqual(html.count("fa-exclamation-triangle"), 0)

    def test_lecturer_can_open_incomplete_session_detail_after_exam_ends(self):
                exam_group = self._create_exam_group()
                exam_group.exam_date = timezone.now() - timedelta(hours=2)
                exam_group.duration_minutes = 60
                exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])
                session = ExamSession.objects.create(
                    user=self.student_user_1,
                    subject=self.subject,
                    exam_session_group_id=exam_group,
                )

                response = self.client.get(
                    reverse("qna:lecturer_session_detail", args=[session.id])
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Vắng thi")

    def test_lecturer_cannot_open_ongoing_session_detail(self):
                exam_group = self._create_exam_group()
                exam_group.exam_date = timezone.now() - timedelta(minutes=10)
                exam_group.duration_minutes = 60
                exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])
                session = ExamSession.objects.create(
                    user=self.student_user_1,
                    subject=self.subject,
                    exam_session_group_id=exam_group,
                )

                response = self.client.get(
                    reverse("qna:lecturer_session_detail", args=[session.id])
                )

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, reverse("qna:lecturer_student_review_screen"))

    def test_student_review_does_not_link_ongoing_session_detail(self):
                exam_group = self._create_exam_group()
                exam_group.exam_date = timezone.now() - timedelta(minutes=10)
                exam_group.duration_minutes = 60
                exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])
                session = ExamSession.objects.create(
                    user=self.student_user_1,
                    subject=self.subject,
                    exam_session_group_id=exam_group,
                    cheating_flag=True,
                )

                response = self.client.get(
                    reverse("qna:lecturer_student_review_screen"),
                    {"subject_id": self.subject.id, "exam_group_id": exam_group.id},
                )

                self.assertEqual(response.status_code, 200)
                html = response.content.decode("utf-8")
                self.assertNotIn(f'data-detail-url="{reverse("qna:lecturer_session_detail", args=[session.id])}"', html)

    def test_student_review_marks_assigned_student_without_session_as_absent(self):
                exam_group = self._create_exam_group()
                exam_group.exam_date = timezone.now() - timedelta(hours=2)
                exam_group.duration_minutes = 60
                exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])

                ExamSession.objects.create(
                    user=self.student_user_1,
                    subject=self.subject,
                    exam_session_group_id=exam_group,
                    session_status="COMPLETED",
                )

                response = self.client.get(
                    reverse("qna:lecturer_student_review_screen"),
                    {"subject_id": self.subject.id, "exam_group_id": exam_group.id},
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "SV9002")
                html = response.content.decode("utf-8")
                self.assertIn('status-pill status-absent', html)
                self.assertIn("Vắng thi", html)

    def test_student_review_marks_started_session_without_answers_as_completed_after_exam_ends(self):
                exam_group = self._create_exam_group()
                exam_group.exam_date = timezone.now() - timedelta(hours=2)
                exam_group.duration_minutes = 60
                exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])

                session = ExamSession.objects.create(
                    user=self.student_user_1,
                    subject=self.subject,
                    exam_session_group_id=exam_group,
                    started_at=timezone.now() - timedelta(hours=1),
                    session_status="PENDING",
                )

                response = self.client.get(
                    reverse("qna:lecturer_student_review_screen"),
                    {"subject_id": self.subject.id, "exam_group_id": exam_group.id},
                )

                self.assertEqual(response.status_code, 200)
                review_row = next(
                    row for row in response.context["sessions"]
                    if getattr(row, "id", None) == session.id
                )
                self.assertEqual(review_row.review_status, "COMPLETED")
                self.assertEqual(review_row.review_status_label, "Đã hoàn thành")
                self.assertTrue(review_row.can_view_detail)
                self.assertTrue(review_row.can_display_score)

    def test_lecturer_session_detail_shows_completed_for_started_session_after_exam_ends(self):
                exam_group = self._create_exam_group()
                exam_group.exam_date = timezone.now() - timedelta(hours=2)
                exam_group.duration_minutes = 60
                exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])

                session = ExamSession.objects.create(
                    user=self.student_user_1,
                    subject=self.subject,
                    exam_session_group_id=exam_group,
                    started_at=timezone.now() - timedelta(hours=1),
                    session_status="STARTED",
                )

                response = self.client.get(
                    reverse("qna:lecturer_session_detail", args=[session.id])
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Đã hoàn thành")
                self.assertNotContains(response, "Đã kết thúc")

    def test_history_view_marks_started_session_as_completed_after_exam_ends(self):
                self.exam_group.exam_date = timezone.now() - timedelta(hours=2)
                self.exam_group.duration_minutes = 60
                self.exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])
                session = ExamSession.objects.create(
                    user=self.student,
                    subject=self.subject,
                    exam_session_group_id=self.exam_group,
                    started_at=timezone.now() - timedelta(hours=1),
                    session_status="STARTED",
                    verification_status="ALLOW",
                )
                self.client.force_login(self.student)

                response = self.client.get(reverse("qna:history"))

                self.assertEqual(response.status_code, 200)
                history_session = next(
                    item for item in response.context["sessions"]
                    if item.id == session.id
                )
                self.assertEqual(history_session.history_status, "COMPLETED")
                self.assertEqual(history_session.history_status_label, "Đã hoàn thành")
                self.assertContains(response, "Đã hoàn thành")

    def test_history_view_does_not_fall_back_to_absent_when_started_flag_exists(self):
                self.exam_group.exam_date = timezone.now() - timedelta(hours=2)
                self.exam_group.duration_minutes = 60
                self.exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])
                session = ExamSession.objects.create(
                    user=self.student,
                    subject=self.subject,
                    exam_session_group_id=self.exam_group,
                    session_status="STARTED",
                    verification_status="ALLOW",
                )
                self.client.force_login(self.student)

                response = self.client.get(reverse("qna:history"))

                self.assertEqual(response.status_code, 200)
                history_session = next(
                    item for item in response.context["sessions"]
                    if item.id == session.id
                )
                self.assertEqual(history_session.history_status, "COMPLETED")
                self.assertContains(response, "Đã hoàn thành")
                self.assertNotContains(response, "Vắng thi")

    def test_history_view_shows_in_progress_badge_for_active_started_session(self):
                self.exam_group.exam_date = timezone.now() - timedelta(minutes=5)
                self.exam_group.duration_minutes = 60
                self.exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])
                session = ExamSession.objects.create(
                    user=self.student,
                    subject=self.subject,
                    exam_session_group_id=self.exam_group,
                    started_at=timezone.now() - timedelta(minutes=1),
                    session_status="STARTED",
                    verification_status="ALLOW",
                )
                self.client.force_login(self.student)

                response = self.client.get(reverse("qna:history"))

                self.assertEqual(response.status_code, 200)
                history_session = next(
                    item for item in response.context["sessions"]
                    if item.id == session.id
                )
                self.assertEqual(history_session.history_status, "IN_PROGRESS")
                self.assertContains(response, "Đang thực hiện")

    def test_history_detail_redirects_back_to_exam_while_session_is_in_progress(self):
                self.exam_group.exam_date = timezone.now() - timedelta(minutes=5)
                self.exam_group.duration_minutes = 60
                self.exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])

                self.client.force_login(self.student)
                self._verify_room_password()
                self._verify_face()

                session = ExamSession.objects.create(
                    user=self.student,
                    subject=self.subject,
                    exam_session_group_id=self.exam_group,
                    started_at=timezone.now() - timedelta(minutes=1),
                    session_status="STARTED",
                    verification_status="ALLOW",
                )

                response = self.client.get(reverse("qna:history_detail", args=[session.id]))

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, reverse("qna:exam_page", args=[self.subject.subject_code]))

    def test_session_detail_renders_all_violation_items_for_expand_button(self):
                exam_group = self._create_exam_group()
                exam_group.exam_date = timezone.now() - timedelta(hours=2)
                exam_group.duration_minutes = 60
                exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])
                session = ExamSession.objects.create(
                    user=self.student_user_1,
                    subject=self.subject,
                    exam_session_group_id=exam_group,
                    session_status="COMPLETED",
                    cheating_flag=True,
                )
                session.violation_images.create(
                    image_blob=b"img1",
                    image_mime="image/jpeg",
                    violation_type="TWO_FACES",
                )
                session.violation_images.create(
                    image_blob=b"img2",
                    image_mime="image/jpeg",
                    violation_type="NO_FACE",
                )
                session.violation_images.create(
                    image_blob=b"img3",
                    image_mime="image/jpeg",
                    violation_type="CAMERA_OFF",
                )

                response = self.client.get(
                    reverse("qna:lecturer_session_detail", args=[session.id])
                )

                self.assertEqual(response.status_code, 200)
                html = response.content.decode("utf-8")
                self.assertNotIn("Không phát hiện khuôn mặt trong khung hình", html)
                self.assertEqual(html.count('class="fraud-item'), 2)
                self.assertIn("data-fraud-toggle", html)

    def test_create_exam_group_screen_rejects_room_password_not_exactly_five_characters(self):
                payload = self._build_session_payload()
                payload["rooms"][0]["password"] = "A101"

                response = self._post_json(
                    "qna:lecturer_create_exam_group_screen",
                    payload,
                )

                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["success"])
                self.assertEqual(response.json()["status"], "FAIL")
                self.assertIn("5", response.json()["message"])

    def test_create_exam_group_screen_auto_generates_room_password_when_blank(self):
                payload = self._build_session_payload()
                payload["rooms"][0]["password"] = ""

                response = self._post_json(
                    "qna:lecturer_create_exam_group_screen",
                    payload,
                )

                self.assertEqual(response.status_code, 200)
                exam_group = ExamSessionGroup.objects.get(pk=response.json()["exam_group_id"])
                first_room = exam_group.session_rooms.order_by("display_order").first()
                self.assertTrue(first_room.room_password)
                self.assertEqual(len(first_room.room_password), 5)

    def test_update_exam_group_rejects_start_time_in_past(self):
                exam_group = self._create_exam_group()
                payload = self._build_session_payload()
                local_now = timezone.localtime()
                if local_now.hour >= 2:
                    past_start = local_now.replace(hour=local_now.hour - 1, minute=0, second=0, microsecond=0)
                else:
                    past_start = (local_now - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
                past_end = past_start + timedelta(minutes=90)

                payload["exam_date"] = past_start.strftime("%Y-%m-%d")
                payload["start_time"] = past_start.strftime("%H:%M")
                payload["end_time"] = past_end.strftime("%H:%M")

                response = self._post_json(
                    "qna:lecturer_update_exam_group",
                    payload,
                    args=[exam_group.id],
                )

                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["success"])
                self.assertEqual(response.json()["status"], "FAIL")
                self.assertTrue(response.json()["message"])

    def test_update_locked_exam_group_is_rejected(self):
                exam_group = ExamSessionGroup.objects.create(
                    subject=self.subject,
                    group_name="Ca thi da dien ra",
                    academic_year="2025-2026",
                    semester="HK1",
                    exam_date=timezone.now() - timedelta(hours=2),
                    duration_minutes=60,
                    status="SCHEDULED",
                    created_by=self.lecturer,
                )
                exam_group.exam_codes.add(*self.exam_codes)

                response = self._post_json(
                    "qna:lecturer_update_exam_group",
                    self._build_session_payload(),
                    args=[exam_group.id],
                )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["status"], "FAIL")

@override_settings(CACHES=TEST_CACHES)
class SessionDetailScoreSyncTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(dir=os.path.dirname(__file__))
        self.override = override_settings(MEDIA_ROOT=self.media_root, CACHES=TEST_CACHES)
        self.override.enable()

        self.user_model = get_user_model()
        self.lecturer = self.user_model.objects.create_user(
            username="lecturer_sync_scores",
            password="Password123!",
        )
        self.student = self.user_model.objects.create_user(
            username="SVSYNC01",
            password="Password123!",
        )
        self.lecturer_profile = UserProfile.objects.create(
            user=self.lecturer,
            is_lecturer=True,
            full_name="Giang Vien Sync",
        )
        UserProfile.objects.create(
            user=self.student,
            is_lecturer=False,
            full_name="Sinh Vien Sync",
            student_id="SVSYNC01",
            class_name="CNTT01",
        )

        self.subject = Subject.objects.create(
            name="Dong bo diem lich su thi",
            subject_code="SYNC401",
        )
        self.lecturer_profile.subjects_taught.add(self.subject)
        self.session_seed = 0

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def _create_completed_session(self, question_specs):
        self.session_seed += 1

        count_map = {
            DifficultyLevel.EASY: 0,
            DifficultyLevel.MEDIUM: 0,
            DifficultyLevel.HARD: 0,
        }
        score_map = {
            DifficultyLevel.EASY: 0.0,
            DifficultyLevel.MEDIUM: 0.0,
            DifficultyLevel.HARD: 0.0,
        }

        for spec in question_specs:
            difficulty = spec["difficulty"]
            max_score = float(spec["max_score"])
            existing_score = score_map[difficulty]
            if count_map[difficulty] and abs(existing_score - max_score) > 0.001:
                raise AssertionError("Mỗi mức độ chỉ nên dùng một max score trong fixture test này.")
            count_map[difficulty] += 1
            score_map[difficulty] = max_score

        exam_set = ExamSet.objects.create(
            subject=self.subject,
            name=f"Bo de dong bo {self.session_seed}",
            academic_year="2025-2026",
            semester="HK1",
            number_of_versions=1,
            easy_pool_size=count_map[DifficultyLevel.EASY],
            medium_pool_size=count_map[DifficultyLevel.MEDIUM],
            hard_pool_size=count_map[DifficultyLevel.HARD],
            easy_score=score_map[DifficultyLevel.EASY],
            medium_score=score_map[DifficultyLevel.MEDIUM],
            hard_score=score_map[DifficultyLevel.HARD],
            created_by=self.lecturer,
        )
        exam_code = ExamCode.objects.create(
            subject=self.subject,
            exam_set=exam_set,
            code_name=f"SYNC-{self.session_seed}",
            code_number=self.session_seed,
        )

        exam_group = ExamSessionGroup.objects.create(
            subject=self.subject,
            group_name=f"Ca dong bo {self.session_seed}",
            academic_year="2025-2026",
            semester="HK1",
            exam_date=timezone.now() - timedelta(hours=2),
            duration_minutes=30,
            status="SCHEDULED",
            created_by=self.lecturer,
            configuration_data={
                "rooms": [
                    {
                        "room_name": f"Phong dong bo {self.session_seed}",
                        "exam_set_id": exam_set.id,
                        "exam_code_ids": [exam_code.id],
                        "assignments": [
                            {
                                "linked_user_id": self.student.id,
                                "student_code": self.student.userprofile.student_id,
                                "full_name": self.student.userprofile.full_name,
                                "exam_code_id": exam_code.id,
                                "display_order": 1,
                            }
                        ],
                        "display_order": 1,
                    }
                ],
                "rosters": [],
            },
        )
        exam_group.exam_codes.add(exam_code)

        session_room = exam_group.session_rooms.create(
            room_name=f"Phong dong bo {self.session_seed}",
            expected_students=1,
            room_password="ABCDE",
            exam_set=exam_set,
            display_order=1,
        )
        session_room.students.add(self.student)
        session_room.exam_codes.add(exam_code)

        persisted_final_score = round(
            sum(float(spec.get("raw_score") or 0) for spec in question_specs) / max(len(question_specs), 1),
            2,
        )
        session = ExamSession.objects.create(
            user=self.student,
            subject=self.subject,
            exam_session_group_id=exam_group,
            started_at=timezone.now() - timedelta(hours=1, minutes=20),
            completed_at=timezone.now() - timedelta(hours=1),
            session_status="COMPLETED",
            final_score=persisted_final_score,
            verification_status="ALLOW",
        )

        questions = []
        for index, spec in enumerate(question_specs, start=1):
            question = Question.objects.create(
                subject=self.subject,
                question_text=f"Cau dong bo {self.session_seed}-{index}",
                question_id_in_barem=f"SYNC_{self.session_seed}_{index}",
                difficulty=spec["difficulty"],
                is_exam_clone=True,
            )
            questions.append(question)
            ExamCodeQuestion.objects.create(
                exam_code=exam_code,
                question=question,
                difficulty=spec["difficulty"],
                display_order=index,
                score=spec["max_score"],
            )

            if spec.get("raw_score") is None:
                continue

            result_kwargs = {
                "exam_session_id": session,
                "question_id": question,
                "transcript": spec.get("transcript") or f"Transcript {self.session_seed}-{index}",
                "score": spec["raw_score"],
                "feedback": spec.get("feedback") or f"Nhan xet {self.session_seed}-{index}",
            }
            if spec.get("audio_name"):
                audio_name = spec["audio_name"]
                result_kwargs["audio_file"] = SimpleUploadedFile(
                    audio_name,
                    b"fake-audio-data",
                    content_type=spec.get("audio_content_type") or resolve_audio_mime_type(audio_name) or "audio/mpeg",
                )
            ExamResult.objects.create(**result_kwargs)

        session.questions.set(questions)
        return session

    def _question_snapshot(self, response):
        snapshot = []
        for item in response.context["question_details"]:
            snapshot.append(
                {
                    "order": item.order,
                    "question": item.question.question_text,
                    "status": item.status_label,
                    "display_score": round(float(item.display_score_value), 2),
                    "display_suffix": item.display_score_suffix,
                    "question_max": item.question_max_score,
                    "transcript": item.result.transcript if item.result else None,
                    "feedback": item.result.feedback if item.result else None,
                }
            )
        return snapshot

    def test_student_history_detail_hides_audio_player_and_filename(self):
        session = self._create_completed_session(
            [
                {
                    "difficulty": DifficultyLevel.EASY,
                    "max_score": 2.0,
                    "raw_score": 8.5,
                    "transcript": "Ban ghi cau 1",
                    "feedback": "Nhan xet cau 1",
                    "audio_name": "sync_audio_q1.mp3",
                },
                {
                    "difficulty": DifficultyLevel.MEDIUM,
                    "max_score": 3.0,
                    "raw_score": 6.0,
                    "transcript": "Ban ghi cau 2",
                    "feedback": "Nhan xet cau 2",
                },
                {
                    "difficulty": DifficultyLevel.HARD,
                    "max_score": 5.0,
                    "raw_score": 9.0,
                    "transcript": "Ban ghi cau 3",
                    "feedback": "Nhan xet cau 3",
                },
            ]
        )

        self.client.force_login(self.student)
        response = self.client.get(reverse("qna:history_detail", args=[session.id]))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertNotIn("<audio", html)
        self.assertNotIn("sync_audio_q1.mp3", html)
        self.assertIn("Câu trả lời của bạn", html)
        self.assertIn("Ban ghi cau 1", html)
        self.assertNotIn('<p class="text-xs font-bold text-gray-400 uppercase mb-2">Trạng thái</p>', html)

    def test_lecturer_session_detail_renders_transcript_and_working_audio_player_for_mp3_file(self):
        session = self._create_completed_session(
            [
                {
                    "difficulty": DifficultyLevel.EASY,
                    "max_score": 2.0,
                    "raw_score": 8.5,
                    "transcript": "Ban ghi cau 1",
                    "feedback": "Nhan xet cau 1",
                    "audio_name": "sync_audio_q1.mp3",
                },
                {
                    "difficulty": DifficultyLevel.MEDIUM,
                    "max_score": 3.0,
                    "raw_score": 6.0,
                    "transcript": "Ban ghi cau 2",
                    "feedback": "Nhan xet cau 2",
                },
                {
                    "difficulty": DifficultyLevel.HARD,
                    "max_score": 5.0,
                    "raw_score": 9.0,
                    "transcript": "Ban ghi cau 3",
                    "feedback": "Nhan xet cau 3",
                },
            ]
        )

        audio_result = ExamResult.objects.filter(exam_session_id=session).exclude(audio_file="").get()

        self.client.force_login(self.lecturer)
        response = self.client.get(reverse("qna:lecturer_session_detail", args=[session.id]))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertEqual(html.count("<audio controls preload=\"metadata\">"), 1)
        self.assertNotIn("audio-player-icon", html)
        self.assertIn(os.path.basename(audio_result.audio_file.name), html)
        self.assertIn(f'src="{audio_result.audio_file.url}"', html)
        self.assertIn("Transcript câu trả lời", html)
        self.assertIn("Ban ghi cau 1", html)
        self.assertIn('type="audio/mpeg"', html)
        self.assertIn("Nếu player không phát được", html)
        self.assertNotIn('<div class="response-label">Trạng thái</div>', html)

    def test_history_detail_uses_actual_session_duration_instead_of_exam_group_duration(self):
        session = self._create_completed_session(
            [
                {
                    "difficulty": DifficultyLevel.EASY,
                    "max_score": 2.0,
                    "raw_score": 8.5,
                }
            ]
        )

        self.client.force_login(self.student)
        response = self.client.get(reverse("qna:history_detail", args=[session.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["session"].actual_duration_display, "20 phút")
        self.assertContains(response, "Thời gian làm bài: 20 phút")
        self.assertNotContains(response, "Thời gian làm bài: 30 phút")

    def test_lecturer_session_audio_file_url_returns_audio_mpeg_bytes(self):
        session = self._create_completed_session(
            [
                {
                    "difficulty": DifficultyLevel.EASY,
                    "max_score": 2.0,
                    "raw_score": 8.5,
                    "transcript": "Ban ghi cau 1",
                    "feedback": "Nhan xet cau 1",
                    "audio_name": "sync_audio_q1.mp3",
                }
            ]
        )

        audio_result = ExamResult.objects.filter(exam_session_id=session).exclude(audio_file="").get()

        self.client.force_login(self.lecturer)
        response = self.client.get(audio_result.audio_file.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "audio/mpeg")
        self.assertEqual(b"".join(response.streaming_content), b"fake-audio-data")

    def test_history_detail_scales_question_scores_and_final_total_from_real_max_scores(self):
        session = self._create_completed_session(
            [
                {
                    "difficulty": DifficultyLevel.EASY,
                    "max_score": 2.0,
                    "raw_score": 8.5,
                },
                {
                    "difficulty": DifficultyLevel.MEDIUM,
                    "max_score": 3.0,
                    "raw_score": 6.0,
                },
                {
                    "difficulty": DifficultyLevel.HARD,
                    "max_score": 5.0,
                    "raw_score": 9.0,
                },
            ]
        )

        self.client.force_login(self.student)
        response = self.client.get(reverse("qna:history_detail", args=[session.id]))

        self.assertEqual(response.status_code, 200)
        question_details = response.context["question_details"]
        self.assertEqual([item.question_max_score for item in question_details], [2.0, 3.0, 5.0])
        self.assertEqual([item.display_score_suffix for item in question_details], ["/ 2.0", "/ 3.0", "/ 5.0"])
        self.assertEqual([round(item.display_score_value, 2) for item in question_details], [1.7, 1.8, 4.5])

        session_context = response.context["session"]
        self.assertEqual(round(sum(item.display_score_value for item in question_details), 2), 8.0)
        self.assertEqual(session_context.final_display_score, 8.0)
        self.assertEqual(session_context.final_display_score_suffix, "/ 10")
        self.assertContains(response, "/ 2.0")
        self.assertContains(response, "/ 10")

    def test_student_and_lecturer_detail_use_same_question_details_when_some_questions_unanswered(self):
        session = self._create_completed_session(
            [
                {
                    "difficulty": DifficultyLevel.EASY,
                    "max_score": 2.0,
                    "raw_score": 8.5,
                    "transcript": "Tra loi cau 1",
                    "feedback": "Nhan xet cau 1",
                },
                {
                    "difficulty": DifficultyLevel.MEDIUM,
                    "max_score": 3.0,
                    "raw_score": 6.0,
                    "transcript": "Tra loi cau 2",
                    "feedback": "Nhan xet cau 2",
                },
                {
                    "difficulty": DifficultyLevel.HARD,
                    "max_score": 5.0,
                    "raw_score": None,
                },
            ]
        )

        self.client.force_login(self.student)
        student_response = self.client.get(reverse("qna:history_detail", args=[session.id]))

        self.client.force_login(self.lecturer)
        lecturer_response = self.client.get(reverse("qna:lecturer_session_detail", args=[session.id]))

        self.assertEqual(student_response.status_code, 200)
        self.assertEqual(lecturer_response.status_code, 200)
        self.assertEqual(len(student_response.context["question_details"]), 3)
        self.assertEqual(len(lecturer_response.context["question_details"]), 3)
        self.assertEqual(self._question_snapshot(student_response), self._question_snapshot(lecturer_response))
        self.assertEqual(student_response.context["question_details"][2].status_label, "Chưa trả lời")
        self.assertEqual(lecturer_response.context["question_details"][2].status_label, "Chưa trả lời")
        self.assertEqual(student_response.context["session"].final_display_score, 3.5)
        self.assertEqual(lecturer_response.context["session"].final_display_score, 3.5)

    def test_non_ten_total_scale_is_synced_between_student_and_lecturer(self):
        session = self._create_completed_session(
            [
                {
                    "difficulty": DifficultyLevel.EASY,
                    "max_score": 2.0,
                    "raw_score": 8.5,
                },
                {
                    "difficulty": DifficultyLevel.HARD,
                    "max_score": 2.0,
                    "raw_score": 7.5,
                },
                {
                    "difficulty": DifficultyLevel.MEDIUM,
                    "max_score": 3.0,
                    "raw_score": 6.0,
                },
            ]
        )

        self.client.force_login(self.student)
        student_response = self.client.get(reverse("qna:history_detail", args=[session.id]))

        self.client.force_login(self.lecturer)
        lecturer_response = self.client.get(reverse("qna:lecturer_session_detail", args=[session.id]))

        self.assertEqual(student_response.context["session"].final_display_score, 5.0)
        self.assertEqual(lecturer_response.context["session"].final_display_score, 5.0)
        self.assertEqual(student_response.context["session"].final_display_score_suffix, "/ 7")
        self.assertEqual(lecturer_response.context["session"].final_display_score_suffix, "/ 7")
        self.assertEqual(student_response.context["session"].score_scale_suffix, "/7")
        self.assertEqual(lecturer_response.context["session"].score_scale_suffix, "/7")

    def test_templates_no_longer_hardcode_score_suffixes_or_student_audio_block(self):
        lecturer_template_path = os.path.join(
            os.path.dirname(__file__),
            "templates",
            "qna",
            "lecturer",
            "lecturer_session_detail.html",
        )
        with open(lecturer_template_path, "r", encoding="utf-8") as template_file:
            lecturer_template = template_file.read()

        self.assertNotIn("/10", lecturer_template)
        self.assertNotIn("/3.0", lecturer_template)
        self.assertNotIn("/3.5", lecturer_template)
        self.assertIn("{{ item.display_score_suffix }}", lecturer_template)
        self.assertIn("{{ session.final_display_score_suffix }}", lecturer_template)
        self.assertNotIn("audio-player-icon", lecturer_template)
        self.assertIn("item.result.audio_display_name", lecturer_template)
        self.assertIn("item.result.audio_mime_type", lecturer_template)
        self.assertIn("Transcript câu trả lời", lecturer_template)
        self.assertIn("item.result.transcript", lecturer_template)
        self.assertNotIn('<div class="response-label">Trạng thái</div>', lecturer_template)

        history_template_path = os.path.join(
            os.path.dirname(__file__),
            "templates",
            "qna",
            "student",
            "history_detail.html",
        )
        with open(history_template_path, "r", encoding="utf-8") as template_file:
            history_template = template_file.read()

        self.assertNotIn("<audio", history_template)
        self.assertNotIn("audio_file.url", history_template)
        self.assertIn("Câu trả lời của bạn", history_template)
        self.assertIn("item.result.transcript", history_template)
        self.assertNotIn('>Trạng thái</p>', history_template)
        self.assertIn("session.actual_duration_display", history_template)
        self.assertNotIn("duration_minutes", history_template)


class VerificationImagesPermissionTests(TestCase):
    def setUp(self):
        self.user_model = get_user_model()
        self.owner = self.user_model.objects.create_user(username="student_verify_owner", password="Password123!")
        UserProfile.objects.create(
            user=self.owner,
            is_lecturer=False,
            full_name="Sinh Vien Chu Session",
            student_id="SVVERIFY01",
        )

        self.lecturer = self.user_model.objects.create_user(username="lecturer_verify_ok", password="Password123!")
        self.other_lecturer = self.user_model.objects.create_user(
            username="lecturer_verify_blocked",
            password="Password123!",
        )
        self.lecturer_profile = UserProfile.objects.create(user=self.lecturer, is_lecturer=True)
        self.other_lecturer_profile = UserProfile.objects.create(user=self.other_lecturer, is_lecturer=True)

        self.subject = Subject.objects.create(name="Bao mat anh xac thuc", subject_code="VERIFY401")
        self.other_subject = Subject.objects.create(name="Mon khac", subject_code="VERIFY402")
        self.lecturer_profile.subjects_taught.add(self.subject)
        self.other_lecturer_profile.subjects_taught.add(self.other_subject)

        self.exam_group = ExamSessionGroup.objects.create(
            subject=self.subject,
            group_name="Ca xac thuc",
            academic_year="2025-2026",
            semester="HK1",
            exam_date=timezone.now() - timedelta(hours=1),
            duration_minutes=30,
            status="SCHEDULED",
            created_by=self.lecturer,
        )
        self.session = ExamSession.objects.create(
            user=self.owner,
            subject=self.subject,
            exam_session_group_id=self.exam_group,
            session_status="COMPLETED",
            face_image_blob=b"face-bytes",
            face_image_mime="image/jpeg",
            id_card_image_blob=b"card-bytes",
            id_card_image_mime="image/png",
            verification_status="ALLOW",
        )

    def test_owner_student_can_view_verification_images(self):
        self.client.force_login(self.owner)

        response = self.client.get(reverse("qna:verification_images", args=[self.session.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "success")
        self.assertIn("data:image/jpeg;base64", payload["face_image"])
        self.assertIn("data:image/png;base64", payload["id_card_image"])

    def test_lecturer_of_same_subject_can_view_verification_images(self):
        self.client.force_login(self.lecturer)

        response = self.client.get(reverse("qna:verification_images", args=[self.session.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")

    def test_lecturer_of_other_subject_is_forbidden_from_verification_images(self):
        self.client.force_login(self.other_lecturer)

        response = self.client.get(reverse("qna:verification_images", args=[self.session.id]))

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "error")
        self.assertNotIn("face_image", payload)


@override_settings(CACHES=TEST_CACHES)
class ExamWindowBoundaryTests(TestCase):
    def setUp(self):
                self.user_model = get_user_model()
                self.lecturer = self.user_model.objects.create_user(username="lecturer_boundary", password="testpass")
                self.lecturer_profile = UserProfile.objects.create(user=self.lecturer, is_lecturer=True)
                self.student = self.user_model.objects.create_user(username="SVBOUND01", password="testpass")
                UserProfile.objects.create(
                    user=self.student,
                    is_lecturer=False,
                    full_name="Sinh Vien Boundary",
                    student_id="SVBOUND01",
                )
                self.subject = Subject.objects.create(name="Mon thi boundary", subject_code="DS550")
                self.lecturer_profile.subjects_taught.add(self.subject)
                self.bank = QuestionBank.objects.create(subject=self.subject, name="Ngan hang boundary")

                for index, difficulty in enumerate(
                        [DifficultyLevel.EASY, DifficultyLevel.MEDIUM, DifficultyLevel.HARD],
                        start=1,
                ):
                    Question.objects.create(
                        subject=self.subject,
                        bank=self.bank,
                        question_text=f"Cau hoi boundary {index}",
                        question_id_in_barem=f"QB_{index}",
                        difficulty=difficulty,
                    )

                self.exam_group = ExamSessionGroup.objects.create(
                    subject=self.subject,
                    group_name="Ca thi boundary",
                    academic_year="2025-2026",
                    semester="HK1",
                    exam_date=timezone.now() + timedelta(minutes=15),
                    duration_minutes=30,
                    status="SCHEDULED",
                    created_by=self.lecturer,
                    configuration_data={"rooms": [], "rosters": []},
                )

                self.room = self.exam_group.session_rooms.create(
                    room_name="Phong Boundary",
                    expected_students=1,
                    room_password="ABCDE",
                    display_order=1,
                )
                self.room.students.add(self.student)
                self.seb_headers = {
                    "HTTP_USER_AGENT": "SEB",
                    "HTTP_X_SAFEEXAMBROWSER_REQUESTHASH": "test-hash",
                }

    def _move_exam_window_to_exact_end(self):
                fixed_now = timezone.now().replace(microsecond=0)
                self.exam_group.exam_date = fixed_now - timedelta(minutes=30)
                self.exam_group.duration_minutes = 30
                self.exam_group.status = "SCHEDULED"
                self.exam_group.save(update_fields=["exam_date", "duration_minutes", "status", "updated_at"])
                return fixed_now

    def test_verify_exam_password_accepts_matching_room_password_at_exact_end_boundary(self):
                fixed_now = self._move_exam_window_to_exact_end()
                self.client.force_login(self.student)

                with patch("qna.models.timezone.now", return_value=fixed_now):
                    response = self.client.post(
                        reverse("qna:verify_exam_password", args=[self.subject.subject_code]),
                        {"password": "ABCDE"},
                    )

                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.url,
                                     reverse("qna:pre_exam_verification", args=[self.subject.subject_code]))

    def test_draft_exam_group_is_not_treated_as_open(self):
                from .exam_guards import subject_has_active_exam_group
                from .exam_runtime import is_exam_group_open

                self.exam_group.exam_date = timezone.now() - timedelta(minutes=5)
                self.exam_group.duration_minutes = 30
                self.exam_group.status = "DRAFT"
                self.exam_group.save(update_fields=["exam_date", "duration_minutes", "status", "updated_at"])

                self.assertFalse(is_exam_group_open(self.exam_group))
                self.assertFalse(subject_has_active_exam_group(self.subject))

    def test_exam_sessions_list_renders_realtime_schedule_dataset(self):
                    self.client.force_login(self.lecturer)

                    response = self.client.get(reverse("qna:lecturer_exam_sessions_list"))

                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, "data-start=")
                    self.assertContains(response, "data-end=")
                    self.assertContains(response, "data-status-note")

    def test_delete_exam_group_is_blocked_while_window_hits_exact_end_boundary(self):
                    fixed_now = self._move_exam_window_to_exact_end()
                    self.client.force_login(self.lecturer)

                    with patch("qna.models.timezone.now", return_value=fixed_now):
                        response = self.client.post(
                            reverse("qna:lecturer_delete_exam_group", args=[self.exam_group.id]),
                            follow=True,
                        )

                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(ExamSessionGroup.objects.filter(pk=self.exam_group.id).exists())

    def test_create_manual_question_is_blocked_while_exam_is_ongoing(self):
                    fixed_now = self._move_exam_window_to_exact_end()
                    self.client.force_login(self.lecturer)

                    with patch("qna.views.timezone.now", return_value=fixed_now):
                        response = self.client.post(
                            reverse("qna:api_create_manual_question"),
                            data=json.dumps(
                                {
                                    "subject_id": str(self.subject.id),
                                    "question_text": "Cau hoi moi khi dang thi",
                                    "difficulty": DifficultyLevel.EASY,
                                }
                            ),
                            content_type="application/json",
                        )

                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["status"], "FAIL")
                    self.assertTrue(response.json()["message"])

    def test_create_exam_set_is_blocked_while_exam_is_ongoing(self):
                    fixed_now = self._move_exam_window_to_exact_end()
                    self.client.force_login(self.lecturer)

                    with patch("qna.exam_management.timezone.now", return_value=fixed_now):
                        response = self.client.post(
                            reverse("qna:lecturer_create_exam_set"),
                            data=json.dumps(
                                {
                                    "subject_id": str(self.subject.id),
                                    "academic_year": "2025-2026",
                                    "semester": "HK1",
                                    "number_of_versions": 1,
                                    "easy_count": 1,
                                    "medium_count": 1,
                                    "hard_count": 1,
                                    "easy_score": 2,
                                    "medium_score": 3,
                                    "hard_score": 5,
                                    "allow_duplicate_questions": False,
                                    "source_bank_ids": [self.bank.id],
                                }
                            ),
                            content_type="application/json",
                        )

                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["status"], "FAIL")
                    self.assertTrue(response.json()["message"])

    def test_import_students_to_room_is_blocked_while_exam_is_ongoing(self):
                    fixed_now = self._move_exam_window_to_exact_end()
                    self.client.force_login(self.lecturer)
                    upload = SimpleUploadedFile(
                        "boundary_students.csv",
                        "username\nSVBOUND01\n".encode("utf-8-sig"),
                        content_type="text/csv",
                    )

                    with patch("qna.models.timezone.now", return_value=fixed_now):
                        response = self.client.post(
                            reverse("qna:lecturer_import_students_to_room", args=[self.room.id]),
                            {"csv_file": upload},
                        )

                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["status"], "FAIL")
                    self.assertIn("đang diễn ra", response.json()["message"])

    def test_random_assign_students_is_blocked_while_exam_is_ongoing(self):
                    fixed_now = self._move_exam_window_to_exact_end()
                    self.client.force_login(self.lecturer)

                    with patch("qna.models.timezone.now", return_value=fixed_now):
                        response = self.client.post(
                            reverse("qna:lecturer_random_assign_students", args=[self.exam_group.id]),
                        )

                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["status"], "FAIL")
                    self.assertIn("đang diễn ra", response.json()["message"])

class LecturerExamManagementTests(LecturerExamManagementTests):
    def test_used_exam_code_cannot_be_deleted(self):
                    exam_set = self._create_exam_set(number_of_versions=1)
                    exam_code = exam_set.exam_codes.get()
                    self._link_exam_code(exam_code)

                    response = self.client.post(
                        reverse("qna:lecturer_delete_exam_code", args=[exam_code.id])
                    )

                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["status"], "FAIL")
                    self.assertEqual(response.json()["message"], "Mã đề đã được sử dụng trong ca thi nên không thể xóa.")
                    self.assertTrue(ExamCode.objects.filter(pk=exam_code.id).exists())

    def test_used_exam_code_cannot_be_edited_via_legacy_update_endpoint(self):
                    exam_set = self._create_exam_set(number_of_versions=1)
                    exam_code = exam_set.exam_codes.get()
                    self._link_exam_code(exam_code)

                    response = self.client.post(
                        reverse("qna:lecturer_update_exam_code_question", args=[exam_code.id]),
                        {"difficulty": "EASY"},
                    )

                    self.assertEqual(response.status_code, 410)
                    self.assertFalse(response.json()["success"])

    def test_used_exam_code_cannot_be_edited_via_legacy_text_endpoint(self):
                    exam_set = self._create_exam_set(number_of_versions=1)
                    exam_code = exam_set.exam_codes.get()
                    self._link_exam_code(exam_code)

                    response = self.client.post(
                        reverse("qna:lecturer_edit_exam_code_question", args=[exam_code.id]),
                        {"difficulty": "EASY", "new_text": "Noi dung moi"},
                    )

                    self.assertEqual(response.status_code, 410)
                    self.assertFalse(response.json()["success"])

    def test_used_exam_set_cannot_be_published(self):
                    exam_set = self._create_exam_set(number_of_versions=1)
                    exam_code = exam_set.exam_codes.get()
                    exam_code.is_approved = True
                    exam_code.save(update_fields=["is_approved", "updated_at"])
                    self._link_exam_code(exam_code)

                    response = self.client.post(
                        reverse("qna:lecturer_publish_exam_set", args=[exam_set.id])
                    )

                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["status"], "FAIL")
                    exam_set.refresh_from_db()
                    self.assertEqual(exam_set.status, ExamSetStatus.DRAFT)

    def test_create_exam_set_allows_zero_counts_for_unused_difficulty_buckets(self):
                    response = self._post_json(
                        "qna:lecturer_create_exam_set",
                        {
                            "subject_id": str(self.subject.id),
                            "academic_year": "2025-2026",
                            "semester": "HK1",
                            "number_of_versions": 1,
                            "easy_count": 0,
                            "medium_count": 2,
                            "hard_count": 0,
                            "easy_score": 0,
                            "medium_score": 5,
                            "hard_score": 0,
                            "shuffle_question_order": False,
                            "allow_duplicate_questions": False,
                            "source_bank_ids": [self.bank.id],
                        },
                    )

                    self.assertEqual(response.status_code, 200)
                    exam_set = ExamSet.objects.get(pk=response.json()["exam_set_id"])
                    exam_code = exam_set.exam_codes.get()
                    items = list(exam_code.exam_code_questions.select_related("question_id").all())

                    self.assertEqual(exam_set.easy_pool_size, 0)
                    self.assertEqual(exam_set.medium_pool_size, 2)
                    self.assertEqual(exam_set.hard_pool_size, 0)
                    self.assertEqual(len(items), 2)
                    self.assertTrue(all(item.difficulty == DifficultyLevel.MEDIUM for item in items))

    def test_update_exam_code_content_uses_item_ids_not_payload_order(self):
                    exam_set = ExamSet.objects.create(
                        subject=self.subject,
                        name=self.subject.name,
                        academic_year="2025-2026",
                        semester="HK1",
                        number_of_versions=1,
                        easy_pool_size=1,
                        medium_pool_size=1,
                        hard_pool_size=1,
                        created_by=self.lecturer,
                    )
                    exam_set.question_banks.add(self.bank)

                    exam_code = ExamCode.objects.create(
                        subject=self.subject,
                        exam_set=exam_set,
                        code_name="101",
                        code_number=101,
                        source_material=self.bank.name,
                    )

                    medium_question = Question.objects.create(
                        subject=self.subject,
                        bank=self.bank,
                        question_text="Medium goc",
                        question_id_in_barem="EC_MEDIUM",
                        difficulty=DifficultyLevel.MEDIUM,
                        is_exam_clone=True,
                    )
                    easy_question = Question.objects.create(
                        subject=self.subject,
                        bank=self.bank,
                        question_text="Easy goc",
                        question_id_in_barem="EC_EASY",
                        difficulty=DifficultyLevel.EASY,
                        is_exam_clone=True,
                    )
                    hard_question = Question.objects.create(
                        subject=self.subject,
                        bank=self.bank,
                        question_text="Hard goc",
                        question_id_in_barem="EC_HARD",
                        difficulty=DifficultyLevel.HARD,
                        is_exam_clone=True,
                    )

                    medium_item = ExamCodeQuestion.objects.create(
                        exam_code=exam_code,
                        question=medium_question,
                        difficulty=DifficultyLevel.MEDIUM,
                        display_order=1,
                        score=3,
                    )
                    easy_item = ExamCodeQuestion.objects.create(
                        exam_code=exam_code,
                        question=easy_question,
                        difficulty=DifficultyLevel.EASY,
                        display_order=2,
                        score=2,
                    )
                    hard_item = ExamCodeQuestion.objects.create(
                        exam_code=exam_code,
                        question=hard_question,
                        difficulty=DifficultyLevel.HARD,
                        display_order=3,
                        score=5,
                    )

                    response = self._post_json(
                        "qna:lecturer_update_exam_code_content",
                        {
                            "items": [
                                {
                                    "id": easy_item.id,
                                    "display_order": easy_item.display_order,
                                    "difficulty": easy_item.difficulty,
                                    "content": "Easy da sua",
                                },
                                {
                                    "id": medium_item.id,
                                    "display_order": medium_item.display_order,
                                    "difficulty": medium_item.difficulty,
                                    "content": "Medium da sua",
                                },
                                {
                                    "id": hard_item.id,
                                    "display_order": hard_item.display_order,
                                    "difficulty": hard_item.difficulty,
                                    "content": "Hard da sua",
                                },
                            ]
                        },
                        args=[exam_code.id],
                    )

                    self.assertEqual(response.status_code, 200)
                    easy_question.refresh_from_db()
                    medium_question.refresh_from_db()
                    hard_question.refresh_from_db()

                    self.assertEqual(easy_question.question_text, "Easy da sua")
                    self.assertEqual(medium_question.question_text, "Medium da sua")
                    self.assertEqual(hard_question.question_text, "Hard da sua")

    def test_create_exam_set_rejects_non_numeric_counts(self):
                    response = self._post_json(
                        "qna:lecturer_create_exam_set",
                        {
                            "subject_id": str(self.subject.id),
                            "academic_year": "2025-2026",
                            "semester": "HK1",
                            "number_of_versions": "abc",
                            "easy_count": 1,
                            "medium_count": 1,
                            "hard_count": 1,
                            "source_bank_ids": [self.bank.id],
                        },
                    )

                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["status"], "FAIL")

@override_settings(CACHES=TEST_CACHES, CHANNEL_LAYERS=TEST_CHANNEL_LAYERS)
class ExamSubmissionRegressionTests(TestCase):
    def setUp(self):
                    self.media_root = tempfile.mkdtemp(dir=os.path.dirname(__file__))
                    self.media_override = override_settings(MEDIA_ROOT=self.media_root)
                    self.media_override.enable()

                    self.user_model = get_user_model()
                    self.lecturer = self.user_model.objects.create_user(
                        username="lecturer-regression",
                        password="Password123!",
                    )
                    self.student = self.user_model.objects.create_user(
                        username="SVREG001",
                        password="Password123!",
                    )
                    self.other_student = self.user_model.objects.create_user(
                        username="SVREG002",
                        password="Password123!",
                    )

                    self.lecturer_profile = UserProfile.objects.create(
                        user=self.lecturer,
                        is_lecturer=True,
                        full_name="Giang Vien Regression",
                    )
                    UserProfile.objects.create(
                        user=self.student,
                        is_lecturer=False,
                        full_name="Sinh Vien Regression",
                        student_id="SVREG001",
                    )
                    UserProfile.objects.create(
                        user=self.other_student,
                        is_lecturer=False,
                        full_name="Sinh Vien Khac",
                        student_id="SVREG002",
                    )

                    self.subject = Subject.objects.create(
                        name="Mon hoi quy",
                        subject_code="REG401",
                    )
                    self.lecturer_profile.subjects_taught.add(self.subject)

                    self.exam_group = ExamSessionGroup.objects.create(
                        subject=self.subject,
                        group_name="Ca thi hoi quy",
                        academic_year="2025-2026",
                        semester="HK1",
                        exam_date=timezone.now() - timedelta(minutes=5),
                        duration_minutes=30,
                        status="SCHEDULED",
                        created_by=self.lecturer,
                        configuration_data={"rooms": [], "rosters": []},
                    )
                    self.room = self.exam_group.session_rooms.create(
                        room_name="Phong Regression",
                        expected_students=1,
                        room_password="ABCDE",
                        display_order=1,
                    )
                    self.room.students.add(self.student)

                    self.question = Question.objects.create(
                        subject=self.subject,
                        question_text="Trinh bay noi dung hoi quy.",
                        question_id_in_barem="REG_Q1",
                        difficulty=DifficultyLevel.EASY,
                    )
                    self.session = ExamSession.objects.create(
                        user=self.student,
                        subject=self.subject,
                        exam_session_group_id=self.exam_group,
                    )
                    self.session.questions.add(self.question)

                    self.client.force_login(self.student)

    def _start_session(self):
                    self.session.started_at = timezone.now()
                    self.session.session_status = "STARTED"
                    self.session.verification_status = "ALLOW"
                    self.session.save(update_fields=["started_at", "session_status", "verification_status"])
                    return self.session

    def _move_exam_group_end_into_submission_grace_window(self):
                    self.exam_group.exam_date = timezone.now() - timedelta(
                        minutes=self.exam_group.duration_minutes,
                        seconds=5,
                    )
                    self.exam_group.save(update_fields=["exam_date", "updated_at"])
                    return self.exam_group

    def tearDown(self):
                    self.media_override.disable()
                    shutil.rmtree(self.media_root, ignore_errors=True)

    def test_exam_consumer_rejects_session_of_other_user(self):
                    from channels.routing import URLRouter
                    from .routing import websocket_urlpatterns

                    communicator = WebsocketCommunicator(
                        URLRouter(websocket_urlpatterns),
                        f"/ws/exam/{self.session.id}/",
                    )
                    communicator.scope["user"] = self.other_student

                    connected, close_code = async_to_sync(communicator.connect)()

                    self.assertFalse(connected)
                    self.assertEqual(close_code, 4403)

    def test_exam_consumer_rejects_session_that_has_not_started(self):
                    from channels.routing import URLRouter
                    from .routing import websocket_urlpatterns

                    communicator = WebsocketCommunicator(
                        URLRouter(websocket_urlpatterns),
                        f"/ws/exam/{self.session.id}/",
                    )
                    communicator.scope["user"] = self.student

                    connected, close_code = async_to_sync(communicator.connect)()

                    self.assertFalse(connected)
                    self.assertEqual(close_code, 4408)

    def test_worker_main_question_is_idempotent_for_same_session_question(self):
                    from .management.commands import run_workers

                    first = async_to_sync(run_workers._create_main_exam_result)(
                        self.session.id,
                        self.question.id,
                        "Ban ghi lan 1",
                        7.5,
                        "Nhan xet lan 1",
                    )
                    second = async_to_sync(run_workers._create_main_exam_result)(
                        self.session.id,
                        self.question.id,
                        "Ban ghi lan 2",
                        8.5,
                        "Nhan xet lan 2",
                    )

                    self.assertTrue(first["created"])
                    self.assertFalse(second["created"])
                    self.assertEqual(
                        ExamResult.objects.filter(exam_session_id=self.session, question_id=self.question).count(),
                        1,
                    )
                    result = ExamResult.objects.get(exam_session_id=self.session, question_id=self.question)
                    self.assertEqual(result.transcript, "Ban ghi lan 1")
                    self.assertEqual(result.score, 7.5)
                    self.assertEqual(result.feedback, "Nhan xet lan 1")

    def test_save_exam_result_returns_audio_url_when_audio_uploaded(self):
                    self._start_session()
                    result = ExamResult.objects.create(
                        exam_session_id=self.session,
                        question_id=self.question,
                        transcript="Server transcript",
                        score=6.0,
                        feedback="Server feedback",
                        analysis=["server-analysis"],
                    )

                    with patch(
                        "qna.views._convert_uploaded_audio_to_mp3",
                        return_value={
                            "content": b"fake-mp3-data",
                            "stored_name": f"audio_{self.session.id}_{self.question.id}.mp3",
                            "source_extension": ".webm",
                            "source_content_type": "audio/webm",
                        },
                    ):
                        response = self.client.post(
                            reverse("qna:save_exam_result"),
                            {
                                "session_id": self.session.id,
                                "question_id": self.question.id,
                                "score": 99,
                                "transcript": "Browser transcript",
                                "feedback": "Browser feedback",
                                "analysis": json.dumps(["browser-analysis"]),
                                "audio_file": SimpleUploadedFile(
                                    "answer.webm",
                                    b"fake-webm-data",
                                    content_type="audio/webm",
                                ),
                            },
                        )

                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    self.assertEqual(payload["status"], "ok")
                    self.assertTrue(payload["audio_url"])
                    self.assertIn("student_audio/", payload["audio_url"])
                    self.assertTrue(payload["audio_url"].endswith(".mp3"))

                    audio_response = self.client.get(payload["audio_url"])
                    self.assertEqual(audio_response.status_code, 200)
                    self.assertEqual(audio_response["Content-Type"], "audio/mpeg")
                    self.assertEqual(b"".join(audio_response.streaming_content), b"fake-mp3-data")

                    result.refresh_from_db()
                    self.assertEqual(result.transcript, "Server transcript")
                    self.assertEqual(result.score, 6.0)
                    self.assertEqual(result.feedback, "Server feedback")
                    self.assertEqual(result.analysis, ["server-analysis"])
                    self.assertTrue(result.audio_file)
                    self.assertTrue(result.audio_file.name.endswith(".mp3"))
                    self.assertEqual(result.audio_mime_type, "audio/mpeg")

    def test_save_exam_result_converts_mp4_upload_to_mp3_filename(self):
                    self._start_session()
                    result = ExamResult.objects.create(
                        exam_session_id=self.session,
                        question_id=self.question,
                        transcript="Server transcript",
                        score=6.0,
                        feedback="Server feedback",
                    )

                    with patch(
                        "qna.views._convert_uploaded_audio_to_mp3",
                        return_value={
                            "content": b"fake-mp3-data",
                            "stored_name": f"audio_{self.session.id}_{self.question.id}.mp3",
                            "source_extension": ".m4a",
                            "source_content_type": "audio/mp4",
                        },
                    ) as convert_audio:
                        response = self.client.post(
                            reverse("qna:save_exam_result"),
                            {
                                "session_id": self.session.id,
                                "question_id": self.question.id,
                                "audio_file": SimpleUploadedFile(
                                    "answer.m4a",
                                    b"fake-m4a-data",
                                    content_type="audio/mp4",
                                ),
                            },
                        )

                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    self.assertTrue(payload["audio_url"].endswith(".mp3"))
                    self.assertEqual(convert_audio.call_args.kwargs["audio_file"].content_type, "audio/mp4")

                    result.refresh_from_db()
                    self.assertTrue(result.audio_file.name.endswith(".mp3"))
                    self.assertEqual(result.audio_mime_type, "audio/mpeg")

    def test_save_exam_result_returns_error_when_mp3_conversion_fails(self):
                    self._start_session()
                    result = ExamResult.objects.create(
                        exam_session_id=self.session,
                        question_id=self.question,
                        transcript="Server transcript",
                        score=6.0,
                        feedback="Server feedback",
                    )

                    with patch("qna.views._convert_uploaded_audio_to_mp3", side_effect=RuntimeError("ffmpeg_failed")):
                        response = self.client.post(
                            reverse("qna:save_exam_result"),
                            {
                                "session_id": self.session.id,
                                "question_id": self.question.id,
                                "audio_file": SimpleUploadedFile(
                                    "answer.webm",
                                    b"fake-webm-data",
                                    content_type="audio/webm",
                                ),
                            },
                        )

                    self.assertEqual(response.status_code, 500)
                    self.assertEqual(response.json()["status"], "error")
                    self.assertIn("MP3", response.json()["message"])

                    result.refresh_from_db()
                    self.assertFalse(bool(result.audio_file))

    def test_save_exam_result_returns_error_when_converted_mp3_is_empty(self):
                    self._start_session()
                    result = ExamResult.objects.create(
                        exam_session_id=self.session,
                        question_id=self.question,
                        transcript="Server transcript",
                        score=6.0,
                        feedback="Server feedback",
                    )

                    with patch("qna.views._convert_uploaded_audio_to_mp3", side_effect=RuntimeError("mp3_output_empty")):
                        response = self.client.post(
                            reverse("qna:save_exam_result"),
                            {
                                "session_id": self.session.id,
                                "question_id": self.question.id,
                                "audio_file": SimpleUploadedFile(
                                    "answer.webm",
                                    b"fake-webm-data",
                                    content_type="audio/webm",
                                ),
                            },
                        )

                    self.assertEqual(response.status_code, 500)
                    self.assertEqual(response.json()["status"], "error")
                    self.assertIn("không hợp lệ", response.json()["message"])

                    result.refresh_from_db()
                    self.assertFalse(bool(result.audio_file))

    def test_validate_generated_mp3_file_accepts_positive_duration_and_size(self):
                    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir=self.media_root) as handle:
                        handle.write(b"fake-mp3-data")
                        audio_path = handle.name

                    try:
                        with patch("qna.views._probe_audio_duration_seconds", return_value=1.75) as probe_duration:
                            metadata = views._validate_generated_mp3_file(
                                audio_path,
                                session_id=str(self.session.id),
                                question_id=str(self.question.id),
                            )
                    finally:
                        if os.path.exists(audio_path):
                            os.remove(audio_path)

                    self.assertEqual(metadata["output_size"], len(b"fake-mp3-data"))
                    self.assertEqual(metadata["duration_seconds"], 1.75)
                    probe_duration.assert_called_once()

    def test_save_exam_result_allows_timeout_flush_within_grace_window(self):
                    self._start_session()
                    ExamResult.objects.create(
                        exam_session_id=self.session,
                        question_id=self.question,
                        transcript="Server transcript",
                        score=6.0,
                        feedback="Server feedback",
                    )
                    self._move_exam_group_end_into_submission_grace_window()

                    with patch(
                        "qna.views._convert_uploaded_audio_to_mp3",
                        return_value={
                            "content": b"fake-mp3-data",
                            "stored_name": f"audio_{self.session.id}_{self.question.id}.mp3",
                            "source_extension": ".webm",
                            "source_content_type": "audio/webm",
                        },
                    ):
                        response = self.client.post(
                            reverse("qna:save_exam_result"),
                            {
                                "session_id": self.session.id,
                                "question_id": self.question.id,
                                "allow_after_expiry": "1",
                                "audio_file": SimpleUploadedFile(
                                    "answer.webm",
                                    b"fake-webm-data",
                                    content_type="audio/webm",
                                ),
                            },
                        )

                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json()["audio_url"].endswith(".mp3"))

    def test_save_exam_result_rejects_when_result_does_not_exist(self):
                    self._start_session()

                    response = self.client.post(
                        reverse("qna:save_exam_result"),
                        {
                            "session_id": self.session.id,
                            "question_id": self.question.id,
                            "score": 10,
                            "transcript": "Browser transcript",
                            "audio_file": SimpleUploadedFile(
                                "answer.webm",
                                b"fake-webm-data",
                                content_type="audio/webm",
                            ),
                        },
                    )

                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["status"], "error")
                    self.assertIn("kết quả server", response.json()["message"])
                    self.assertEqual(
                        ExamResult.objects.filter(exam_session_id=self.session, question_id=self.question).count(), 0)

    def test_update_profile_image_returns_forbidden(self):
                    response = self.client.post(
                        reverse("qna:update_profile_image"),
                        {
                            "profile_image": SimpleUploadedFile(
                                "profile.png",
                                b"profile-image-data",
                                content_type="image/png",
                            )
                        },
                    )
                    self.assertEqual(response.status_code, 403)
                    payload = response.json()
                    self.assertFalse(payload["success"])
                    self.assertIn("Người dùng không có quyền", payload["error"])

    def test_lecturer_student_review_screen_does_not_assign_readonly_property(self):
                    self.client.force_login(self.lecturer)
                    response = self.client.get(
                        reverse("qna:lecturer_student_review_screen"),
                        {"subject_id": self.subject.id},
                    )
                    self.assertEqual(response.status_code, 200)

    def test_finalize_session_is_idempotent(self):
                    first = self.client.post(reverse("qna:finalize_session", args=[self.session.id]))
                    second = self.client.post(reverse("qna:finalize_session", args=[self.session.id]))

                    self.assertEqual(first.status_code, 200)
                    self.assertEqual(second.status_code, 200)
                    self.assertFalse(first.json()["already_finalized"])
                    self.assertTrue(second.json()["already_finalized"])

    def test_exam_session_display_status_marks_never_started_closed_session_as_absent(self):
                    self.exam_group.exam_date = timezone.now() - timedelta(hours=2)
                    self.exam_group.duration_minutes = 30
                    self.exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])

                    self.session.refresh_from_db()
                    self.assertEqual(self.session.display_status, "ABSENT")

    def test_exam_session_display_status_marks_started_session_without_answers_as_completed(self):
                    self._start_session()

                    self.session.refresh_from_db()
                    self.assertEqual(self.session.display_status, "IN_PROGRESS")

    def test_save_exam_result_rejects_after_finalize(self):
                    ExamResult.objects.create(
                        exam_session_id=self.session,
                        question_id=self.question,
                        transcript="Server transcript",
                        score=7.0,
                        feedback="Server feedback",
                    )
                    self.session.session_status = "COMPLETED"
                    self.session.completed_at = timezone.now()
                    self.session.save(update_fields=["session_status", "completed_at"])

                    response = self.client.post(
                        reverse("qna:save_exam_result"),
                        {
                            "session_id": self.session.id,
                            "question_id": self.question.id,
                            "audio_file": SimpleUploadedFile(
                                "answer.webm",
                                b"fake-webm-data",
                                content_type="audio/webm",
                            ),
                        },
                    )

                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["status"], "error")

    def test_worker_context_allows_timeout_flush_within_grace_window(self):
                    from .management.commands import run_workers

                    self._start_session()
                    self._move_exam_group_end_into_submission_grace_window()

                    blocked_context = async_to_sync(run_workers._load_main_question_context)(
                        self.session.id,
                        self.question.id,
                    )
                    grace_context = async_to_sync(run_workers._load_main_question_context)(
                        self.session.id,
                        self.question.id,
                        allow_after_expiry=True,
                    )

                    self.assertFalse(blocked_context["submission_window_open"])
                    self.assertFalse(blocked_context["session_is_active"])
                    self.assertFalse(grace_context["exam_is_open"])
                    self.assertTrue(grace_context["submission_window_open"])
                    self.assertTrue(grace_context["session_is_active"])

    def test_websocket_no_face_violation_does_not_mark_session_cheating(self):
        from .consumers import ExamConsumer

        self._start_session()
        consumer = ExamConsumer()
        consumer.scope = {"user": self.student}
        consumer.session = self.session
        consumer.session_id = self.session.id

        async_to_sync(consumer.save_violation_image)("NO_FACE", "data:image/jpeg;base64,ZmFrZQ==")
        async_to_sync(consumer.mark_session_cheating)("NO_FACE")

        self.session.refresh_from_db()
        self.assertFalse(self.session.cheating_flag)
        self.assertEqual(self.session.violation_images.filter(violation_type="NO_FACE").count(), 1)

    def test_exam_consumer_forwards_progress_event_to_browser(self):
        from .consumers import ExamConsumer

        consumer = ExamConsumer()
        consumer.scope = {"user": self.student}
        consumer.session_id = self.session.id
        consumer.send = AsyncMock()

        payload = {
            "stage": "transcript_ready",
            "session_id": self.session.id,
            "question_id": self.question.id,
            "detail": "Transcript: xin chao",
            "transcript": "xin chao",
        }

        async_to_sync(consumer.exam_progress)({"message": payload})

        consumer.send.assert_awaited_once()
        sent_payload = json.loads(consumer.send.await_args.kwargs["text_data"])
        self.assertEqual(sent_payload["type"], "exam.progress")
        self.assertEqual(sent_payload["message"], payload)

    def test_exam_consumer_forwards_allow_after_expiry_and_group_metadata_to_worker(self):
        from types import SimpleNamespace

        from .consumers import ExamConsumer

        consumer = ExamConsumer()
        consumer.scope = {"user": self.student}
        consumer.channel_name = "test-reply-channel"
        consumer.channel_layer = SimpleNamespace(send=AsyncMock())
        consumer.session_id = self.session.id
        consumer.room_group_name = f"exam_{self.session.id}"
        consumer.audio_stream_started = False
        consumer.audio_chunk_count = 0
        consumer.current_audio_question_id = None

        async_to_sync(consumer.receive)(
            text_data=json.dumps(
                {
                    "action": "start_stream",
                    "session_id": self.session.id,
                    "question_id": self.question.id,
                    "audio_mime_type": "audio/webm",
                    "allow_after_expiry": True,
                }
            )
        )
        async_to_sync(consumer.receive)(
            text_data=json.dumps(
                {
                    "action": "end_stream",
                    "session_id": self.session.id,
                    "question_id": self.question.id,
                    "audio_mime_type": "audio/webm",
                    "allow_after_expiry": True,
                }
            )
        )

        start_payload = consumer.channel_layer.send.await_args_list[0].args[1]
        end_payload = consumer.channel_layer.send.await_args_list[1].args[1]
        self.assertTrue(start_payload["allow_after_expiry"])
        self.assertTrue(end_payload["allow_after_expiry"])
        self.assertEqual(start_payload["reply_group"], f"exam_{self.session.id}")
        self.assertEqual(end_payload["reply_group"], f"exam_{self.session.id}")

    def test_exam_grading_status_returns_cached_result_for_reconnect_flow(self):
        from .exam_runtime import EXAM_GRADING_STATUS_RESULT, clear_exam_grading_state, store_exam_grading_state

        self._start_session()
        clear_exam_grading_state(self.session.id, self.question.id)
        result_payload = {
            "type": "main_question_complete",
            "data": {
                "result_id": 123,
                "score": 8.5,
                "question_id": self.question.id,
                "transcript": "Ban ghi reconnect",
                "feedback": "Nhan xet reconnect",
            },
        }
        store_exam_grading_state(
            self.session.id,
            self.question.id,
            status=EXAM_GRADING_STATUS_RESULT,
            result=result_payload,
        )

        response = self.client.get(
            reverse("qna:exam_grading_status", args=[self.session.id, self.question.id])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["grading_state"]["status"], "RESULT")
        self.assertEqual(payload["grading_state"]["result"], result_payload)

    def test_worker_no_chunk_path_persists_error_state(self):
        from types import SimpleNamespace

        from .exam_runtime import clear_exam_grading_state, get_exam_grading_state
        from .management.commands import run_workers

        command = run_workers.Command.__new__(run_workers.Command)
        command.channel_layer = SimpleNamespace(group_send=AsyncMock(), send=AsyncMock())
        clear_exam_grading_state(self.session.id, self.question.id)

        result = async_to_sync(command.process_audio_and_transcribe)(
            "test-reply-channel",
            f"exam_{self.session.id}",
            [],
            session_id=self.session.id,
            question_id=self.question.id,
            audio_mime_type="audio/webm",
        )

        self.assertIsNone(result)
        grading_state = get_exam_grading_state(self.session.id, self.question.id)
        self.assertEqual(grading_state["status"], "ERROR")
        self.assertIn("Không nhận được dữ liệu âm thanh", grading_state["error_message"])

@override_settings(CACHES=TEST_CACHES, CHANNEL_LAYERS=TEST_CHANNEL_LAYERS)
class LecturerGenerateQuestionsTests(TestCase):
    def setUp(self):
                    views.QUESTION_JOB_LOCAL_FALLBACK.clear()

                    self.user_model = get_user_model()
                    self.lecturer = self.user_model.objects.create_user(
                        username="lecturer-generate",
                        password="Password123!",
                    )
                    self.profile = UserProfile.objects.create(
                        user=self.lecturer,
                        is_lecturer=True,
                        full_name="Giang Vien Tao Cau Hoi",
                    )
                    self.subject = Subject.objects.create(
                        name="Tri tue nhan tao",
                        subject_code="AI401",
                    )
                    self.profile.subjects_taught.add(self.subject)
                    self.material = LectureMaterial.objects.create(
                        subject=self.subject,
                        title="Bai giang chuong 1",
                        file_path="materials/chuong_1.txt",
                        file_type="text/plain",
                        workspace_id="ws_test",
                    )
                    self.client.force_login(self.lecturer)

    def tearDown(self):
                    views.QUESTION_JOB_LOCAL_FALLBACK.clear()

    @patch("qna.views.cache.get", side_effect=ConnectionError("redis down"))
    @patch("qna.views.cache.set", side_effect=ConnectionError("redis down"))
    @patch("qna.views._generate_questions_from_ai")
    def test_generate_questions_uses_local_job_fallback_when_cache_is_unavailable(
                        self,
                        mock_generate_questions,
                        mock_cache_set,
                        mock_cache_get,
                ):
                    mock_generate_questions.return_value = [
                        {
                            "content": "Trinh bay cac thanh phan cua mot he thong AI.",
                            "difficulty": "MEDIUM",
                            "source": "Bai giang chuong 1",
                        }
                    ]

                    response = self.client.post(
                        reverse("qna:api_generate_questions"),
                        data=json.dumps(
                            {
                                "subject_id": self.subject.id,
                                "document_ids": [self.material.id],
                                "total_count": 1,
                                "level_config": {"easy": 0, "medium": 1, "hard": 0},
                                "workspace_id": "ws_test",
                            }
                        ),
                        content_type="application/json",
                        HTTP_ACCEPT="application/json",
                    )

                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    self.assertEqual(payload["status"], "SUCCESS")
                    self.assertEqual(len(payload["questions"]), 1)
                    self.assertEqual(payload["summary"]["medium"], 1)
                    self.assertEqual(Question.objects.filter(subject_id=self.subject).count(), 1)
                    self.assertTrue(
                        Question.objects.get(subject_id=self.subject).question_id_in_barem.startswith("DRAFT_ws_test_")
                    )

                    status_response = self.client.get(
                        reverse("qna:api_generate_questions_status"),
                        {"job_id": payload["job_id"]},
                        HTTP_ACCEPT="application/json",
                    )
                    self.assertEqual(status_response.status_code, 200)
                    status_payload = status_response.json()
                    self.assertEqual(status_payload["status"], "SUCCESS")
                    self.assertEqual(status_payload["process_status"], "COMPLETE")
                    self.assertEqual(status_payload["summary"]["medium"], 1)

    @patch("qna.views._generate_questions_from_ai")
    def test_generate_questions_returns_friendly_message_when_api_key_is_invalid(self,
                                                                                             mock_generate_questions):
                    from httpx import Request, Response
                    from openai import AuthenticationError

                    mock_generate_questions.side_effect = AuthenticationError(
                        "Incorrect API key provided",
                        response=Response(401, request=Request("POST", "https://api.openai.com/v1/chat/completions")),
                        body={
                            "error": {
                                "message": "Incorrect API key provided",
                                "code": "invalid_api_key",
                            }
                        },
                    )

                    response = self.client.post(
                        reverse("qna:api_generate_questions"),
                        data=json.dumps(
                            {
                                "subject_id": self.subject.id,
                                "document_ids": [self.material.id],
                                "total_count": 1,
                                "level_config": {"easy": 0, "medium": 1, "hard": 0},
                                "workspace_id": "ws_test",
                            }
                        ),
                        content_type="application/json",
                        HTTP_ACCEPT="application/json",
                    )

                    self.assertEqual(response.status_code, 400)
                    payload = response.json()
                    self.assertEqual(payload["status"], "FAIL")
                    self.assertIn("OpenAI API key không hợp lệ", payload["message"])
                    self.assertNotIn("Incorrect API key", payload["message"])
                    self.assertEqual(Question.objects.filter(subject_id=self.subject).count(), 0)

    @patch("qna.views._generate_questions_from_ai")
    def test_generate_questions_regenerates_when_ai_returns_duplicate_items(self, mock_generate_questions):
                    Question.objects.create(
                        subject=self.subject,
                        question_text="Cau trung lap",
                        question_id_in_barem="MAN_EXISTING_DUP",
                        difficulty=DifficultyLevel.MEDIUM,
                    )
                    mock_generate_questions.side_effect = [
                        [
                            {
                                "content": "Cau trung lap",
                                "difficulty": "MEDIUM",
                                "source": "Bai giang chuong 1",
                            },
                            {
                                "content": "Cau moi hop le",
                                "difficulty": "HARD",
                                "source": "Bai giang chuong 1",
                            },
                        ],
                        [
                            {
                                "content": "Cau moi bo sung",
                                "difficulty": "MEDIUM",
                                "source": "Bai giang chuong 1",
                            },
                        ],
                    ]

                    response = self.client.post(
                        reverse("qna:api_generate_questions"),
                        data=json.dumps(
                            {
                                "subject_id": self.subject.id,
                                "document_ids": [self.material.id],
                                "total_count": 2,
                                "level_config": {"easy": 0, "medium": 1, "hard": 1},
                                "workspace_id": "ws_test",
                            }
                        ),
                        content_type="application/json",
                        HTTP_ACCEPT="application/json",
                    )

                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    self.assertEqual(payload["created_count"], 2)
                    self.assertEqual(payload["requested_count"], 2)
                    self.assertEqual(payload["skipped_duplicates"], 1)
                    self.assertIn("tự sinh bù 1 câu bị trùng", payload["message"])


class StudentEnrollmentTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(dir=os.path.dirname(__file__))
        self.override = override_settings(
            MEDIA_ROOT=self.media_root,
            CACHES=TEST_CACHES,
            CHANNEL_LAYERS=TEST_CHANNEL_LAYERS,
        )
        self.override.enable()

        self.user_model = get_user_model()
        self.lecturer = self.user_model.objects.create_user(
            username="lecturer01",
            password="Password123!",
        )
        self.lecturer_profile = UserProfile.objects.create(
            user=self.lecturer,
            is_lecturer=True,
            full_name="Giang Vien",
        )
        self.subject_a = Subject.objects.create(
            name="Mon A",
            subject_code="MA001",
        )
        self.subject_b = Subject.objects.create(
            name="Mon B",
            subject_code="MB001",
        )
        self.lecturer_profile.subjects_taught.add(self.subject_a, self.subject_b)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_student_dashboard_shows_only_enrolled_subjects(self):
        # Tạo student user
        student = self.user_model.objects.create_user(
            username="student01",
            password="Password123!",
        )
        student_profile = UserProfile.objects.create(
            user=student,
            is_lecturer=False,
            full_name="Sinh Vien",
        )
        student_profile.subjects_enrolled.add(self.subject_a)
        self.client.force_login(student)

        response = self.client.get(reverse("qna:dashboard"))
        self.assertEqual(response.status_code, 200)

        # Chỉ thấy subject_a
        self.assertContains(response, self.subject_a.name)
        self.assertNotContains(response, self.subject_b.name)

    def test_import_new_student_creates_account_and_enrolls_in_subject(self):
        self.client.force_login(self.lecturer)

        # Upload roster cho subject_a
        roster = StudentRosterUpload.objects.create(
            subject_id=self.subject_a,
            academic_year="2024-2025",
            semester="HK1",
            title="Roster A",
            total_students=1,
        )
        StudentRosterStudent.objects.create(
            student_roster_upload_id=roster,
            row_number=1,
            student_code="SV001",
            full_name="Sinh Vien Moi",
            class_name="L01",
        )

        # Create accounts
        response = self.client.post(
            reverse("qna:lecturer_student_list_create_accounts", args=[roster.id]),
            {
                "default_password": "Password123!",
                "confirm_password": "Password123!",
            },
        )
        self.assertEqual(response.status_code, 302)

        # Check user created
        user = self.user_model.objects.get(username="SV001")
        profile = UserProfile.objects.get(user_id=user)
        self.assertIn(self.subject_a, profile.subjects_enrolled.all())

        # Check student dashboard
        self.client.force_login(user)
        response = self.client.get(reverse("qna:dashboard"))
        self.assertContains(response, self.subject_a.name)
        self.assertNotContains(response, self.subject_b.name)

    def test_import_existing_student_enrolls_in_new_subject_without_creating_duplicate_account(self):
        self.client.force_login(self.lecturer)

        # Tạo user cũ enrolled in subject_a
        existing_user = self.user_model.objects.create_user(
            username="SV002",
            password="Password123!",
        )
        existing_profile = UserProfile.objects.create(
            user=existing_user,
            is_lecturer=False,
            full_name="Sinh Vien Cu",
        )
        existing_profile.subjects_enrolled.add(self.subject_a)

        # Upload roster cho subject_b
        roster_b = StudentRosterUpload.objects.create(
            subject_id=self.subject_b,
            academic_year="2024-2025",
            semester="HK1",
            title="Roster B",
            total_students=1,
        )
        StudentRosterStudent.objects.create(
            student_roster_upload_id=roster_b,
            row_number=1,
            student_code="SV002",
            full_name="Sinh Vien Cu",
            class_name="L01",
        )

        # Create accounts
        response = self.client.post(
            reverse("qna:lecturer_student_list_create_accounts", args=[roster_b.id]),
            {
                "default_password": "Password123!",
                "confirm_password": "Password123!",
            },
        )
        self.assertEqual(response.status_code, 302)

        # Check no new user created
        self.assertEqual(self.user_model.objects.filter(username="SV002").count(), 1)

        # Check enrolled in both subjects
        existing_profile.refresh_from_db()
        self.assertIn(self.subject_a, existing_profile.subjects_enrolled.all())
        self.assertIn(self.subject_b, existing_profile.subjects_enrolled.all())

        # Check student dashboard
        self.client.force_login(existing_user)
        response = self.client.get(reverse("qna:dashboard"))
        self.assertContains(response, self.subject_a.name)
        self.assertContains(response, self.subject_b.name)

    def test_import_student_again_does_not_duplicate_enrollment(self):
        self.client.force_login(self.lecturer)

        # Upload roster cho subject_a
        roster_a1 = StudentRosterUpload.objects.create(
            subject_id=self.subject_a,
            academic_year="2024-2025",
            semester="HK1",
            title="Roster A1",
            total_students=1,
        )
        StudentRosterStudent.objects.create(
            student_roster_upload_id=roster_a1,
            row_number=1,
            student_code="SV003",
            full_name="Sinh Vien Test",
            class_name="L01",
        )

        # Create accounts first time
        self.client.post(
            reverse("qna:lecturer_student_list_create_accounts", args=[roster_a1.id]),
            {
                "default_password": "Password123!",
                "confirm_password": "Password123!",
            },
        )

        # Upload roster cho subject_a again
        roster_a2 = StudentRosterUpload.objects.create(
            subject_id=self.subject_a,
            academic_year="2024-2025",
            semester="HK1",
            title="Roster A2",
            total_students=1,
        )
        StudentRosterStudent.objects.create(
            student_roster_upload_id=roster_a2,
            row_number=1,
            student_code="SV003",
            full_name="Sinh Vien Test",
            class_name="L01",
        )

        # Create accounts second time
        self.client.post(
            reverse("qna:lecturer_student_list_create_accounts", args=[roster_a2.id]),
            {
                "default_password": "Password123!",
                "confirm_password": "Password123!",
            },
        )

        # Check user created only once
        self.assertEqual(self.user_model.objects.filter(username="SV003").count(), 1)

        # Check enrolled only once
        user = self.user_model.objects.get(username="SV003")
        profile = UserProfile.objects.get(user_id=user)
        self.assertEqual(profile.subjects_enrolled.filter(id=self.subject_a.id).count(), 1)

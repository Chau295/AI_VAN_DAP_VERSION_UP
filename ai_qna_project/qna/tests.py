import json
import os
import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import views
from .models import (
    DifficultyLevel,
    ExamAppeal,
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

    def disabled_ajax_upload_reports_skipped_rows_legacy_fixture(self):
        upload = SimpleUploadedFile(
            "Danh_sach_L02.csv",
            (
                "MÃƒÂ£ sÃ¡Â»â€˜ sinh viÃƒÂªn,HÃ¡Â»Â vÃƒÂ  tÃƒÂªn,GiÃ¡Â»â€ºi tÃƒÂ­nh,NgÃƒÂ y sinh,LÃ¡Â»â€ºp,Email\n"
                "SV010,Nguyen Van Ten,Nam,12/05/2003,CNT01,sv010@example.com\n"
                "SV011,,NÃ¡Â»Â¯,24/08/2003,CNT01,sv011@example.com\n"
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
        self.assertIn("Dong 3", payload["results"][0]["warnings"][0]["message"])
        self.assertIn("Dong 4", payload["results"][0]["warnings"][1]["message"])

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
            group_name="Ca thi dang dien ra",
            academic_year="2025-2026",
            semester="HK1",
            exam_date=timezone.now() - timedelta(minutes=5),
            duration_minutes=30,
            status="SCHEDULED",
            created_by=self.lecturer,
            configuration_data={"rooms": [], "rosters": []},
        )

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
            question_text="Cau clone khong duoc hien",
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
            name="Mon khac",
            subject_code="OT401",
        )
        other_bank = QuestionBank.objects.create(
            subject=other_subject,
            name="Bank ngoai pham vi",
        )

        response = self._post_json(
            "qna:api_create_manual_question",
            {
                "subject_id": str(self.subject.id),
                "real_bank_id": str(other_bank.id),
                "view_type": "bank",
                "difficulty": DifficultyLevel.MEDIUM,
                "content": "Cau hoi khong hop le.",
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
            question_text="Cau hoi dang duoc khoa",
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

    def _create_exam_set(self, number_of_versions=2):
        response = self._post_json(
            "qna:lecturer_create_exam_set",
            {
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
            },
        )
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
            group_name="Ca thi dang dien ra",
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

    def test_create_exam_set_ignores_shuffle_question_order_payload(self):
        response = self._post_json(
            "qna:lecturer_create_exam_set",
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
                "shuffle_question_order": False,
                "allow_duplicate_questions": False,
                "source_bank_ids": [self.bank.id],
            },
        )

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
        self.assertNotIn("M?c ?? c?u h?i", content)
        self.assertNotIn("c?u h?i", content)
        self.assertNotContains(response, 'class="stepper-input"')
        self.assertNotContains(response, 'type="hidden" id="easyCount"')

    def test_legacy_create_session_route_redirects_to_session_wizard(self):
        response = self.client.get(
            reverse("qna:lecturer_create_exam_session", args=[self.subject.subject_code])
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('qna:lecturer_create_session_screen')}?subject_id={self.subject.id}",
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

    def test_exam_set_detail_hides_regenerate_action_and_shows_set_code(self):
        exam_set = self._create_exam_set(number_of_versions=1)

        response = self.client.get(
            reverse("qna:lecturer_exam_set_detail_screen", args=[exam_set.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"BD-{exam_set.id:04d}")
        self.assertNotContains(response, "fa-rotate-right")
        self.assertNotContains(response, "Sinh láº¡i")

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
        UserProfile.objects.create(user=self.student, is_lecturer=False, full_name="Sinh Vien Exam", student_id="SVEXAM01")
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

        self.seb_headers = {
            "HTTP_USER_AGENT": "SEB",
            "HTTP_X_SAFEEXAMBROWSER_REQUESTHASH": "test-hash",
        }

    def _open_exam_window(self):
        self.exam_group.exam_date = timezone.now() - timedelta(minutes=5)
        self.exam_group.duration_minutes = 30
        self.exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])

    def _verify_room_password(self, password="ABCDE"):
        return self.client.post(
            reverse("qna:verify_exam_password", args=[self.subject.subject_code]),
            {"password": password},
            **self.seb_headers,
        )

    def _verify_face(self):
        return self.client.post(
            reverse("qna:verify_face"),
            {
                "face_image": "data:image/jpeg;base64,ZmFrZQ==",
                "subject_code": self.subject.subject_code,
            },
            **self.seb_headers,
        )

    def _complete_pre_exam_flow(self):
        password_response = self._verify_room_password()
        self.assertEqual(password_response.status_code, 302)
        face_response = self._verify_face()
        self.assertEqual(face_response.status_code, 200)
        return face_response

    def test_student_dashboard_disables_exam_button_when_exam_not_open(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("qna:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Chua the vao thi")
        self.assertContains(response, "Chua den gio thi")

    def test_exam_password_rejects_access_outside_exam_window(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("qna:exam_password", args=[self.subject.subject_code]), **self.seb_headers)
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

        response = self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]), **self.seb_headers)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("qna:exam_password", args=[self.subject.subject_code]))
        self.assertFalse(ExamSession.objects.filter(subject_id=self.subject, exam_session_group_id=self.exam_group).exists())

    def test_exam_page_rejects_when_face_verification_missing(self):
        self._open_exam_window()
        self.client.force_login(self.student)
        self._verify_room_password()

        response = self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]), **self.seb_headers)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("qna:pre_exam_verification", args=[self.subject.subject_code]))
        self.assertFalse(ExamSession.objects.filter(subject_id=self.subject, exam_session_group_id=self.exam_group).exists())

    def test_exam_page_rejects_when_entry_token_missing(self):
        self._open_exam_window()
        self.client.force_login(self.student)
        self._complete_pre_exam_flow()

        session_store = self.client.session
        session_store.pop(views._student_exam_entry_session_key(self.exam_group.pk), None)
        session_store.save()

        response = self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]), **self.seb_headers)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("qna:pre_exam_verification", args=[self.subject.subject_code]))
        self.assertFalse(ExamSession.objects.filter(subject_id=self.subject, exam_session_group_id=self.exam_group).exists())
    def test_student_dashboard_enables_exam_button_during_open_window_without_room_password(self):
        self.exam_group.exam_date = timezone.now() - timedelta(minutes=5)
        self.exam_group.duration_minutes = 30
        self.exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])
        self.room.room_password = ""
        self.room.save(update_fields=["room_password"])
        self.client.force_login(self.student)

        response = self.client.get(reverse("qna:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dang trong ca thi")
        self.assertContains(response, "Vao thi")
        self.assertNotContains(response, "Thieu mat khau")

    def test_exam_password_redirects_directly_to_verification_when_room_has_no_password(self):
        self.exam_group.exam_date = timezone.now() - timedelta(minutes=5)
        self.exam_group.duration_minutes = 30
        self.exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])
        self.room.room_password = ""
        self.room.save(update_fields=["room_password"])
        self.client.force_login(self.student)

        response = self.client.get(reverse("qna:exam_password", args=[self.subject.subject_code]), **self.seb_headers)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("qna:pre_exam_verification", args=[self.subject.subject_code]))

    def test_exam_page_rejects_direct_url_after_exam_window_closes(self):
        self._open_exam_window()
        Question.objects.create(
            subject=self.subject,
            question_text="Cau hoi dong bo thoi gian",
            question_id_in_barem="Q_LOCK_TIME",
            difficulty=DifficultyLevel.EASY,
        )
        self.client.force_login(self.student)

        verify_response = self._complete_pre_exam_flow()
        self.assertEqual(verify_response.status_code, 200)

        self.exam_group.exam_date = timezone.now() - timedelta(hours=2)
        self.exam_group.duration_minutes = 30
        self.exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])

        response = self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]), **self.seb_headers)

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

        response = self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]), **self.seb_headers)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "const SERVER_NOW_MS =")
        self.assertContains(response, "const EXAM_END_AT_MS =")
        self.assertEqual(ExamSession.objects.filter(subject_id=self.subject, exam_session_group_id=self.exam_group).count(), 1)
    def test_student_dashboard_shows_finished_exam_as_outside_exam_window(self):
        self.exam_group.exam_date = timezone.now() - timedelta(hours=2)
        self.exam_group.duration_minutes = 30
        self.exam_group.save(update_fields=["exam_date", "duration_minutes", "updated_at"])
        self.client.force_login(self.student)

        response = self.client.get(reverse("qna:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ngoai thoi gian thi")
        self.assertContains(response, "Chua the vao thi")
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

        self.assertContains(response, 'data-entry-lock-state="STARTED"')
        self.assertContains(response, "Khong the vao lai")
        self.assertContains(response, "Da bat dau ca thi")
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
        self.assertIn("thoi gian thi", response.json()["message"].lower())
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

        first_entry = self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]), **self.seb_headers)
        self.assertEqual(first_entry.status_code, 200)

        second_entry = self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]), **self.seb_headers)
        self.assertEqual(second_entry.status_code, 302)
        self.assertEqual(second_entry.url, reverse("qna:dashboard"))
        self.assertEqual(ExamSession.objects.filter(subject_id=self.subject, exam_session_group_id=self.exam_group).count(), 1)
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
        self.client.get(reverse("qna:exam_page", args=[self.subject.subject_code]), **self.seb_headers)

        self._verify_room_password()
        reentry_response = self._verify_face()

        self.assertEqual(reentry_response.status_code, 400)
        self.assertEqual(reentry_response.json()["status"], "error")
        self.assertIn("1 lan", reentry_response.json()["message"])
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
            "description": "Ca thi duoc tao tu wizard moi",
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
        self.assertNotIn('placeholder="Nh?p 5 k? t?"', content)
        self.assertNotIn('data-room-step="increase"', content)
        self.assertNotIn('${assignedCount}/${room.student_count || 0} SV', content)
        self.assertNotIn('Giảng viên tự nhập đúng 5 ký tự cho phòng thi.', content)
        self.assertNotIn(': "Chưa gắn bộ đề";', content)
        self.assertNotIn(': "Chưa gắn";', content)
        self.assertNotIn('Vui lÃ²ng', content)
        self.assertNotIn('GiÃ¡Âº', content)

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
        self.assertEqual(exam_group.description, "Ca thi duoc tao tu wizard moi")
        self.assertEqual(exam_group.status, "SCHEDULED")
        self.assertEqual(exam_group.exam_codes.count(), 2)
        self.assertEqual(exam_group.session_rooms.count(), 2)
        self.assertEqual(exam_group.session_rooms.first().exam_set, self.exam_set)
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
        self.assertContains(response, "XÃ³a ca thi thÃ nh cÃ´ng")

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
        self.assertContains(response, "Äang diá»…n ra")
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
        self.assertContains(response, "Váº¯ng thi")

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
        self.assertNotIn("KhÃ´ng phÃ¡t hiá»‡n khuÃ´n máº·t trong khung hÃ¬nh", html)
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

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["status"], "FAIL")

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
                **self.seb_headers,
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("qna:pre_exam_verification", args=[self.subject.subject_code]))

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
        self.assertIn("Ä‘ang diá»…n ra", response.json()["message"])

    def test_random_assign_students_is_blocked_while_exam_is_ongoing(self):
        fixed_now = self._move_exam_window_to_exact_end()
        self.client.force_login(self.lecturer)

        with patch("qna.models.timezone.now", return_value=fixed_now):
            response = self.client.post(
                reverse("qna:lecturer_random_assign_students", args=[self.exam_group.id]),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["status"], "FAIL")
        self.assertIn("Ä‘ang diá»…n ra", response.json()["message"])


class LecturerExamManagementTests(LecturerExamManagementTests):
    def test_used_exam_code_cannot_be_deleted(self):
        exam_set = self._create_exam_set(number_of_versions=1)
        exam_code = exam_set.exam_codes.get()
        self._link_exam_code(exam_code)

        response = self.client.post(
            reverse("qna:lecturer_delete_exam_code", args=[exam_code.id])
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["status"], "FAIL")
        self.assertTrue(ExamCode.objects.filter(pk=exam_code.id).exists())

    def test_used_exam_code_cannot_be_edited_via_legacy_update_endpoint(self):
        exam_set = self._create_exam_set(number_of_versions=1)
        exam_code = exam_set.exam_codes.get()
        self._link_exam_code(exam_code)

        response = self.client.post(
            reverse("qna:lecturer_update_exam_code_question", args=[exam_code.id]),
            {"difficulty": "EASY"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])

    def test_used_exam_code_cannot_be_edited_via_legacy_text_endpoint(self):
        exam_set = self._create_exam_set(number_of_versions=1)
        exam_code = exam_set.exam_codes.get()
        self._link_exam_code(exam_code)

        response = self.client.post(
            reverse("qna:lecturer_edit_exam_code_question", args=[exam_code.id]),
            {"difficulty": "EASY", "new_text": "Noi dung moi"},
        )

        self.assertEqual(response.status_code, 400)
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
                "easy_score": 2,
                "medium_score": 3,
                "hard_score": 5,
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

        result.refresh_from_db()
        self.assertEqual(result.transcript, "Server transcript")
        self.assertEqual(result.score, 6.0)
        self.assertEqual(result.feedback, "Server feedback")
        self.assertEqual(result.analysis, ["server-analysis"])
        self.assertTrue(result.audio_file)

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
        self.assertIn("Chua co ket qua", response.json()["message"])
        self.assertEqual(ExamResult.objects.filter(exam_session_id=self.session, question_id=self.question).count(), 0)
    def test_submit_exam_appeal_allows_within_window(self):
        self.session.session_status = "COMPLETED"
        self.session.completed_at = timezone.now() - timedelta(minutes=5)
        self.session.final_score = 8.0
        self.session.save(update_fields=["session_status", "completed_at", "final_score"])

        response = self.client.post(
            reverse("qna:submit_exam_appeal", args=[self.session.id]),
            data=json.dumps({"reason": "Toi muon phuc khao lai ket qua bai thi."}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "success")
        self.assertTrue(payload["created"])
        self.assertTrue(ExamAppeal.objects.filter(exam_session_id=self.session).exists())

    def test_submit_exam_appeal_rejects_after_window(self):
        self.session.session_status = "COMPLETED"
        self.session.completed_at = timezone.now() - views.APPEAL_WINDOW_DELTA - timedelta(seconds=1)
        self.session.final_score = 8.0
        self.session.save(update_fields=["session_status", "completed_at", "final_score"])

        response = self.client.post(
            reverse("qna:submit_exam_appeal", args=[self.session.id]),
            data=json.dumps({"reason": "Toi muon phuc khao lai ket qua bai thi."}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "error")
        self.assertIn("gian", payload["message"])
        self.assertFalse(ExamAppeal.objects.filter(exam_session_id=self.session).exists())

    def test_update_profile_image_accepts_profile_image_and_avatar(self):
        first_response = self.client.post(
            reverse("qna:update_profile_image"),
            {
                "profile_image": SimpleUploadedFile(
                    "profile.png",
                    b"profile-image-data",
                    content_type="image/png",
                )
            },
        )
        self.assertEqual(first_response.status_code, 200)
        first_payload = first_response.json()
        self.assertTrue(first_payload["success"])
        self.assertTrue(first_payload["image_data_url"])
        self.assertEqual(first_payload["image_data_url"], first_payload["avatar_url"])

        second_response = self.client.post(
            reverse("qna:update_profile_image"),
            {
                "avatar": SimpleUploadedFile(
                    "avatar.png",
                    b"avatar-image-data",
                    content_type="image/png",
                )
            },
        )
        self.assertEqual(second_response.status_code, 200)
        second_payload = second_response.json()
        self.assertTrue(second_payload["success"])
        self.assertTrue(second_payload["image_data_url"])
        self.assertEqual(second_payload["image_data_url"], second_payload["avatar_url"])

        profile = self.student.userprofile
        profile.refresh_from_db()
        self.assertEqual(profile.profile_image_blob, b"avatar-image-data")


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
    def test_generate_questions_returns_friendly_message_when_api_key_is_invalid(self, mock_generate_questions):
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
        self.assertIn("OpenAI API key khÃ´ng há»£p lá»‡", payload["message"])
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
        self.assertIn("tá»± sinh bÃ¹ 1 cÃ¢u bá»‹ trÃ¹ng", payload["message"])
















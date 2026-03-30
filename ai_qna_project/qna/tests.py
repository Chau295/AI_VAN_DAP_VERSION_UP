import json
import os
import shutil
import tempfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    DifficultyLevel,
    ExamCode,
    ExamCodeQuestion,
    ExamSessionGroup,
    ExamSet,
    ExamSetStatus,
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
                "Mã số sinh viên,Họ và tên,Giới tính,Ngày sinh,Lớp,Email\n"
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
        return StudentRosterUpload.objects.get(subject=self.subject)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_ajax_upload_creates_roster_and_students(self):
        upload = SimpleUploadedFile(
            "Danh_sach_L01.csv",
            (
                "Mã số sinh viên,Họ và tên,Giới tính,Ngày sinh,Lớp,Email\n"
                "SV001,Nguyen Van A,Nam,12/05/2003,CNT01,sv001@example.com\n"
                "SV002,Tran Thi B,Nữ,24/08/2003,CNT01,sv002@example.com\n"
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

        roster = StudentRosterUpload.objects.get(subject=self.subject)
        self.assertEqual(roster.academic_year, "2024-2025")
        self.assertEqual(roster.semester, "HK1")
        self.assertEqual(roster.total_students, 2)
        self.assertEqual(StudentRosterStudent.objects.filter(roster=roster).count(), 2)
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

        management_response = self.client.get(
            reverse("qna:lecturer_student_list_management", args=[self.subject.subject_code])
        )
        self.assertContains(management_response, "1/1 sinh viên đã có tài khoản")

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
            "SV005,Sinh Vien Chua Co,Nữ,24/08/2003,CNT01,sv005@example.com",
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
        self.assertContains(management_response, "Xóa bộ lọc")
        self.assertContains(management_response, "1 có sẵn")
        self.assertContains(management_response, "0 mới tạo")

        detail_response = self.client.get(
            reverse("qna:lecturer_student_list_detail", args=[roster.id]),
            {"q": "SV004"},
        )
        self.assertContains(detail_response, "Xóa bộ lọc")

        full_detail_response = self.client.get(
            reverse("qna:lecturer_student_list_detail", args=[roster.id])
        )
        self.assertContains(full_detail_response, "ĐÃ CÓ SẴN")
        self.assertContains(full_detail_response, "CHƯA CÓ")

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
            "SV003,Sinh Vien Moi,Nữ,24/08/2003,CNT01,sv003@example.com",
        ])

        response = self.client.post(
            reverse("qna:lecturer_student_list_create_accounts", args=[roster.id]),
            {
                "default_password": "BatchPass123!",
                "confirm_password": "BatchPass123!",
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

        management_response = self.client.get(
            reverse("qna:lecturer_student_list_management", args=[self.subject.subject_code])
        )
        self.assertContains(management_response, "2/2 sinh viên đã có tài khoản")
        self.assertContains(management_response, "1 có sẵn")
        self.assertContains(management_response, "1 mới tạo")

        workspace_response = self.client.get(
            reverse("qna:lecturer_subject_workspace", args=[self.subject.subject_code])
        )
        self.assertContains(workspace_response, self.subject.name)
        self.assertNotContains(workspace_response, "Tiến độ tạo tài khoản sinh viên")
        self.assertNotContains(workspace_response, "Tien do tao tai khoan sinh vien")

        detail_response = self.client.get(
            reverse("qna:lecturer_student_list_detail", args=[roster.id])
        )
        self.assertContains(detail_response, "ĐÃ CÓ SẴN")
        self.assertContains(detail_response, "MỚI TẠO")
        self.assertContains(detail_response, "Đã có sẵn: 1")
        self.assertContains(detail_response, "Mới tạo: 1")

        self.client.logout()
        self.assertTrue(self.client.login(username="SV002", password="LegacyPass123!"))
        self.assertFalse(self.client.login(username="SV002", password="BatchPass123!"))
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
                "MÃ£ sá»‘ sinh viÃªn,Há» vÃ  tÃªn,Giá»›i tÃ­nh,NgÃ y sinh,Lá»›p,Email\n"
                "SV010,Nguyen Van Ten,Nam,12/05/2003,CNT01,sv010@example.com\n"
                "SV011,,Ná»¯,24/08/2003,CNT01,sv011@example.com\n"
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
        self.assertIn("Dong 3", payload["results"][0]["warnings"][0]["message"])
        self.assertIn("Dong 4", payload["results"][0]["warnings"][1]["message"])

    def test_invalid_batch_password_keeps_create_modal_open_with_inline_error(self):
        roster = self._upload_roster([
            "SV001,Nguyen Van A,Nam,12/05/2003,CNT01,sv001@example.com",
        ])

        response = self.client.post(
            reverse("qna:lecturer_student_list_create_accounts", args=[roster.id]),
            {
                "default_password": "12345678",
                "confirm_password": "12345678",
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

        self.assertEqual(draft_one.bank_id, int(new_bank_id))
        self.assertEqual(draft_two.bank_id, int(new_bank_id))
        self.assertTrue(draft_one.question_id_in_barem.startswith("SAVED_"))
        self.assertTrue(draft_two.question_id_in_barem.startswith("SAVED_"))
        self.assertFalse(Question.objects.filter(pk=draft_three.id).exists())


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
        return exam_group

    def test_create_exam_set_generates_exam_codes_and_clone_questions(self):
        exam_set = self._create_exam_set(number_of_versions=2)

        self.assertEqual(exam_set.status, ExamSetStatus.DRAFT)
        self.assertEqual(exam_set.exam_codes.count(), 2)
        self.assertEqual(exam_set.question_banks.count(), 1)

        for code in exam_set.exam_codes.all():
            items = list(code.exam_code_questions.select_related("question").all())
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
                "shuffle_question_order": True,
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

        self.assertFalse(exam_set.shuffle_question_order)
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
        self.assertContains(response, 'id="easyCount" class="stepper-input" type="number"')
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
        self.assertContains(response, "<th style=\"text-align: center;\">Mã bộ đề</th>", html=True)
        self.assertContains(response, "<th>Môn học / Học phần</th>", html=True)
        self.assertNotContains(response, "Mã bộ đề / Môn học")
        self.assertContains(response, f"BD-{exam_set.id:04d}")
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
        self.assertNotContains(response, "Sinh lại")

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
        items = list(exam_code.exam_code_questions.select_related("question").all())
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
        items = list(exam_code.exam_code_questions.select_related("question").all())

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

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "FAIL")


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
        self.exam_codes = list(self.exam_set.exam_codes.order_by("code_number", "id"))

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
        return {
            "subject_id": self.subject.id,
            "group_name": "Thi giua ky DS403",
            "academic_year": "2025-2026",
            "semester": "HK1",
            "exam_date": "2026-04-20",
            "start_time": "08:00",
            "end_time": "09:30",
            "description": "Ca thi duoc tao tu wizard moi",
            "exam_password": "EXAM123",
            "roster_ids": [self.roster.id],
            "submit_action": submit_action,
            "rooms": [
                {
                    "client_id": "room-1",
                    "room_name": "Phong A101",
                    "student_count": 1,
                    "password": "CK_A101",
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
                    "password": "CK_A102",
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
        self.assertContains(response, "CK_A101")

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

        self.assertEqual(response.status_code, 400)
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
        items = list(exam_code.exam_code_questions.select_related("question").all())

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

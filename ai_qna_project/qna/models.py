from datetime import timedelta
import re
import unicodedata

from django.conf import settings
from django.db import models
from django.utils import timezone


class DifficultyLevel(models.TextChoices):
    EASY = "EASY", "Dễ"
    MEDIUM = "MEDIUM", "Trung bình"
    HARD = "HARD", "Khó"


class SemesterChoices(models.TextChoices):
    HK1 = "HK1", "Học kỳ 1"
    HK2 = "HK2", "Học kỳ 2"
    SUMMER = "SUMMER", "Học kỳ hè"


class ExamSetStatus(models.TextChoices):
    DRAFT = "DRAFT", "Chưa duyệt"
    APPROVED = "APPROVED", "Đã duyệt"


class StudentListUploadStatus(models.TextChoices):
    PENDING = "PENDING", "Chưa tạo"
    CREATED = "CREATED", "Đã tạo"


class StudentRosterAccountStatus(models.TextChoices):
    PENDING = "PENDING", "Chưa có"
    EXISTING = "EXISTING", "Đã có sẵn"
    CREATED = "CREATED", "Mới tạo"


def normalize_question_text(value: str) -> str:
    text = (value or "").strip().lower()
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text)
    return text


class Subject(models.Model):
    subject_id = models.AutoField(primary_key=True, db_column="subject_id")
    name = models.CharField(max_length=255, verbose_name="Tên môn học")
    subject_code = models.CharField(max_length=20, unique=True, verbose_name="Mã môn học")
    quiz_data_file = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Ví dụ: data_analysis_quiz.json",
    )
    exam_password = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name="Mật khẩu bài thi",
        help_text="Mật khẩu để sinh viên vào bài thi (để trống nếu không cần)",
    )

    class Meta:
        db_table = "subject"
        ordering = ["name"]
        verbose_name = "Môn học"
        verbose_name_plural = "Môn học"

    def __str__(self):
        return self.name

    @property
    def id(self):
        return self.subject_id


class UserProfile(models.Model):
    user_profile_id = models.AutoField(primary_key=True, db_column="user_profile_id")
    user_id = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        db_column="user_id",
        related_name="userprofile",
    )
    is_lecturer = models.BooleanField(default=False)
    subjects_taught = models.ManyToManyField(
        Subject,
        blank=True,
        related_name="lecturers",
    )
    full_name = models.CharField(max_length=150, blank=True, default="")
    class_name = models.CharField(max_length=100, blank=True, default="")
    student_id = models.CharField(max_length=150, blank=True, default="")
    profile_image_blob = models.BinaryField(null=True, blank=True)
    profile_image_mime = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        db_table = "user_profile"
        verbose_name = "Hồ sơ người dùng"
        verbose_name_plural = "Hồ sơ người dùng"

    def __str__(self):
        return self.user_id.username

    @property
    def id(self):
        return self.user_profile_id

    @property
    def user(self):
        return self.user_id

    @user.setter
    def user(self, value):
        self.user_id = value


class QuestionBank(models.Model):
    question_bank_id = models.AutoField(primary_key=True, db_column="question_bank_id")
    subject_id = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        db_column="subject_id",
        related_name="question_banks",
        verbose_name="Môn học",
    )
    name = models.CharField(max_length=255, verbose_name="Tên ngân hàng")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Ngày tạo")

    class Meta:
        db_table = "question_bank"
        ordering = ["-created_at"]
        verbose_name = "Ngân hàng câu hỏi"
        verbose_name_plural = "Ngân hàng câu hỏi"

    def __str__(self):
        return f"{self.name} - {self.subject_id.name}"

    @property
    def id(self):
        return self.question_bank_id

    @property
    def subject(self):
        return self.subject_id

    @subject.setter
    def subject(self, value):
        self.subject_id = value


class Question(models.Model):
    question_id = models.AutoField(primary_key=True, db_column="question_id")
    subject_id = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        db_column="subject_id",
        related_name="questions",
        verbose_name="Môn học",
    )
    question_bank_id = models.ForeignKey(
        QuestionBank,
        on_delete=models.CASCADE,
        db_column="question_bank_id",
        null=True,
        blank=True,
        related_name="questions",
        verbose_name="Ngân hàng câu hỏi",
    )
    question_text = models.TextField(verbose_name="Nội dung câu hỏi")
    question_text_normalized = models.TextField(
        verbose_name="Câu hỏi chuẩn hóa",
        blank=True,
        null=True,
    )
    question_id_in_barem = models.CharField(
        max_length=50,
        verbose_name="ID câu hỏi trong tệp barem",
        help_text="Ví dụ: Q1, Q2...",
    )
    difficulty = models.CharField(
        max_length=10,
        choices=DifficultyLevel.choices,
        default=DifficultyLevel.EASY,
        verbose_name="Độ khó",
    )
    is_exam_clone = models.BooleanField(default=False, verbose_name="Bản sao dùng cho mã đề")
    is_draft = models.BooleanField(default=False, verbose_name="Bản nháp chưa lưu")
    created_at = models.DateTimeField(auto_now_add=True, null=True, verbose_name="Ngày tạo")

    class Meta:
        db_table = "question"
        ordering = ["question_id_in_barem", "question_id"]
        verbose_name = "Câu hỏi"
        verbose_name_plural = "Câu hỏi"
        constraints = [
            models.UniqueConstraint(
                fields=["subject_id", "question_text_normalized"],
                name="uq_question_subject_normalized_text",
            )
        ]

    def save(self, *args, **kwargs):
        if self.is_exam_clone:
            import uuid
            self.question_text_normalized = f"clone_{uuid.uuid4().hex}"
        else:
            self.question_text_normalized = normalize_question_text(self.question_text)

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.subject_id.subject_code} - {self.question_text[:50]}..."

    @property
    def short_text(self):
        return self.question_text[:80]

    @property
    def id(self):
        return self.question_id

    @property
    def subject(self):
        return self.subject_id

    @subject.setter
    def subject(self, value):
        self.subject_id = value

    @property
    def bank(self):
        return self.question_bank_id

    @bank.setter
    def bank(self, value):
        self.question_bank_id = value


class ExamSet(models.Model):
    exam_set_id = models.AutoField(primary_key=True, db_column="exam_set_id")
    subject_id = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        db_column="subject_id",
        related_name="exam_sets",
        verbose_name="Môn học",
    )
    name = models.CharField(max_length=255, blank=True, default="", verbose_name="Tên bộ đề")
    academic_year = models.CharField(max_length=20, verbose_name="Năm học")
    semester = models.CharField(
        max_length=10,
        choices=SemesterChoices.choices,
        default=SemesterChoices.HK1,
        verbose_name="Học kỳ",
    )
    number_of_versions = models.PositiveIntegerField(default=1, verbose_name="Số mã đề")
    easy_pool_size = models.PositiveIntegerField(default=1, verbose_name="Số câu dễ trong ma trận")
    medium_pool_size = models.PositiveIntegerField(default=1, verbose_name="Số câu trung bình trong ma trận")
    hard_pool_size = models.PositiveIntegerField(default=1, verbose_name="Số câu khó trong ma trận")
    easy_score = models.DecimalField(max_digits=5, decimal_places=1, default=2.0, verbose_name="Điểm câu dễ")
    medium_score = models.DecimalField(max_digits=5, decimal_places=1, default=2.5, verbose_name="Điểm câu trung bình")
    hard_score = models.DecimalField(max_digits=5, decimal_places=1, default=3.0, verbose_name="Điểm câu khó")
    shuffle_question_order = models.BooleanField(default=True, verbose_name="Tự động xáo trộn thứ tự câu hỏi")
    allow_duplicate_questions = models.BooleanField(
        default=False,
        verbose_name="Cho phép câu hỏi trùng lặp giữa các mã đề",
    )
    status = models.CharField(
        max_length=20,
        choices=ExamSetStatus.choices,
        default=ExamSetStatus.DRAFT,
        verbose_name="Trạng thái",
    )
    created_by_user_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        db_column="created_by_user_id",
        null=True,
        blank=True,
        related_name="created_exam_sets",
        verbose_name="Người tạo",
    )
    question_banks = models.ManyToManyField(
        QuestionBank,
        blank=True,
        related_name="exam_sets",
        verbose_name="Nguồn câu hỏi",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Ngày tạo")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Ngày cập nhật")

    class Meta:
        db_table = "exam_set"
        ordering = ["-created_at"]
        verbose_name = "Bộ đề thi"
        verbose_name_plural = "Bộ đề thi"

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        return self.name or self.subject_id.name

    @property
    def total_questions(self):
        return self.easy_pool_size + self.medium_pool_size + self.hard_pool_size

    @property
    def total_score(self):
        return (
            self.easy_pool_size * float(self.easy_score)
            + self.medium_pool_size * float(self.medium_score)
            + self.hard_pool_size * float(self.hard_score)
        )

    @property
    def approved_codes_count(self):
        return self.exam_codes.filter(is_approved=True).count()

    @property
    def is_linked(self):
        return self.exam_codes.filter(exam_session_groups__isnull=False).exists()

    @property
    def linked_status_label(self):
        return "Đã dùng" if self.is_linked else "Chưa dùng"

    @property
    def status_label(self):
        return self.get_status_display()

    @property
    def id(self):
        return self.exam_set_id

    @property
    def subject(self):
        return self.subject_id

    @subject.setter
    def subject(self, value):
        self.subject_id = value

    @property
    def created_by(self):
        return self.created_by_user_id

    @created_by.setter
    def created_by(self, value):
        self.created_by_user_id = value


class ExamCode(models.Model):
    exam_code_id = models.AutoField(primary_key=True, db_column="exam_code_id")
    subject_id = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        db_column="subject_id",
        related_name="exam_codes",
        verbose_name="Môn học",
    )
    exam_set_id = models.ForeignKey(
        ExamSet,
        on_delete=models.CASCADE,
        db_column="exam_set_id",
        null=True,
        blank=True,
        related_name="exam_codes",
        verbose_name="Bộ đề thi",
    )
    code_name = models.CharField(max_length=100, verbose_name="Tên mã đề")
    code_number = models.PositiveIntegerField(default=0, verbose_name="Số thứ tự mã đề")
    source_material = models.TextField(blank=True, null=True, verbose_name="Nguồn tài liệu")
    is_approved = models.BooleanField(default=False, verbose_name="Đã duyệt")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Ngày tạo")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Ngày cập nhật")

    class Meta:
        db_table = "exam_code"
        ordering = ["code_number", "created_at"]
        verbose_name = "Mã đề thi"
        verbose_name_plural = "Mã đề thi"

    def __str__(self):
        return f"{self.code_name} - {self.subject_id.name}"

    @property
    def ordered_questions(self):
        return list(
            self.exam_code_questions.select_related("question_id").order_by("display_order", "exam_code_question_id")
        )

    @property
    def total_questions(self):
        return self.exam_code_questions.count()

    @property
    def is_linked(self):
        return self.exam_session_groups.exists()

    @property
    def id(self):
        return self.exam_code_id

    @property
    def subject(self):
        return self.subject_id

    @subject.setter
    def subject(self, value):
        self.subject_id = value

    @property
    def exam_set(self):
        return self.exam_set_id

    @exam_set.setter
    def exam_set(self, value):
        self.exam_set_id = value


class ExamCodeQuestion(models.Model):
    exam_code_question_id = models.AutoField(primary_key=True, db_column="exam_code_question_id")
    exam_code_id = models.ForeignKey(
        ExamCode,
        on_delete=models.CASCADE,
        db_column="exam_code_id",
        related_name="exam_code_questions",
        verbose_name="Mã đề",
    )
    question_id = models.ForeignKey(
        Question,
        on_delete=models.CASCADE,
        db_column="question_id",
        related_name="exam_code_items",
        verbose_name="Câu hỏi",
    )
    difficulty = models.CharField(
        max_length=10,
        choices=DifficultyLevel.choices,
        verbose_name="Độ khó",
    )
    display_order = models.PositiveIntegerField(default=1, verbose_name="Thứ tự hiển thị")
    score = models.DecimalField(max_digits=5, decimal_places=2, default=1, verbose_name="Điểm")

    class Meta:
        db_table = "exam_code_question"
        ordering = ["display_order", "exam_code_question_id"]
        verbose_name = "Câu hỏi trong mã đề"
        verbose_name_plural = "Câu hỏi trong mã đề"

    def __str__(self):
        return f"{self.exam_code_id.code_name} - Câu {self.display_order}"

    @property
    def id(self):
        return self.exam_code_question_id

    @property
    def exam_code(self):
        return self.exam_code_id

    @exam_code.setter
    def exam_code(self, value):
        self.exam_code_id = value

    @property
    def question(self):
        return self.question_id

    @question.setter
    def question(self, value):
        self.question_id = value


class LectureMaterial(models.Model):
    lecture_material_id = models.AutoField(primary_key=True, db_column="lecture_material_id")
    subject_id = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        db_column="subject_id",
        related_name="lecture_materials",
        verbose_name="Môn học",
    )
    question_bank_id = models.ForeignKey(
        QuestionBank,
        on_delete=models.CASCADE,
        db_column="question_bank_id",
        null=True,
        blank=True,
        related_name="materials",
        verbose_name="Ngân hàng câu hỏi",
    )
    title = models.CharField(max_length=255, verbose_name="Tiêu đề tài liệu")
    file_path = models.CharField(max_length=500, verbose_name="Đường dẫn file")
    file_type = models.CharField(max_length=50, verbose_name="Loại file")
    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name="Ngày upload")
    workspace_id = models.CharField(max_length=100, blank=True, null=True, verbose_name="Phi làm việc")

    class Meta:
        db_table = "lecture_material"
        ordering = ["-uploaded_at"]
        verbose_name = "Tài liệu bài giảng"
        verbose_name_plural = "Tài liệu bài giảng"

    def __str__(self):
        return f"{self.title} - {self.subject_id.name}"

    @property
    def filename(self):
        import os
        return os.path.basename(self.file_path or "")

    @property
    def extension(self):
        name = self.filename.lower()
        return name.split(".")[-1] if "." in name else ""

    @property
    def id(self):
        return self.lecture_material_id

    @property
    def subject(self):
        return self.subject_id

    @subject.setter
    def subject(self, value):
        self.subject_id = value

    @property
    def bank(self):
        return self.question_bank_id

    @bank.setter
    def bank(self, value):
        self.question_bank_id = value


class ExamRoom(models.Model):
    exam_room_id = models.AutoField(primary_key=True, db_column="exam_room_id")
    room_name = models.CharField(max_length=100, verbose_name="Tên phòng")
    room_code = models.CharField(max_length=50, unique=True, verbose_name="Mã phòng")
    capacity = models.PositiveIntegerField(verbose_name="Sức chứa")

    class Meta:
        db_table = "exam_room"
        ordering = ["room_code"]
        verbose_name = "Phòng thi"
        verbose_name_plural = "Phòng thi"

    def __str__(self):
        return f"{self.room_name} ({self.room_code})"

    @property
    def id(self):
        return self.exam_room_id


class ExamSessionGroup(models.Model):
    STATUS_CHOICES = [
        ("DRAFT", "Nháp"),
        ("SCHEDULED", "Đã lên lịch"),
        ("ONGOING", "Đang diễn ra"),
        ("COMPLETED", "Đã kết thúc"),
        ("CANCELLED", "Đã hủy"),
    ]

    exam_session_group_id = models.AutoField(primary_key=True, db_column="exam_session_group_id")
    subject_id = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        db_column="subject_id",
        related_name="exam_session_groups",
        verbose_name="Môn học",
    )
    group_name = models.CharField(max_length=200, verbose_name="Tên ca thi")
    academic_year = models.CharField(max_length=20, blank=True, default="", verbose_name="Năm học")
    semester = models.CharField(
        max_length=10,
        choices=SemesterChoices.choices,
        default=SemesterChoices.HK1,
        verbose_name="Học kỳ",
    )
    description = models.TextField(blank=True, default="", verbose_name="Mô tả kỳ thi")
    exam_date = models.DateTimeField(verbose_name="Ngày giờ thi")
    duration_minutes = models.PositiveIntegerField(default=60, verbose_name="Thời gian làm bài (phút)")
    exam_password = models.CharField(max_length=100, blank=True, null=True, verbose_name="Mật khẩu bài thi")
    configuration_data = models.JSONField(default=dict, blank=True, verbose_name="Cấu hình ca thi")
    exam_codes = models.ManyToManyField(
        ExamCode,
        related_name="exam_session_groups",
        verbose_name="Mã đề thi",
    )
    rooms = models.ManyToManyField(
        ExamRoom,
        through="ExamSessionRoom",
        verbose_name="Phòng thi",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="DRAFT", verbose_name="Trạng thái")
    created_by_user_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        db_column="created_by_user_id",
        related_name="created_exam_groups",
        verbose_name="Người tạo",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Ngày tạo")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Ngày cập nhật")

    class Meta:
        db_table = "exam_session_group"
        ordering = ["-exam_date"]
        verbose_name = "Ca thi"
        verbose_name_plural = "Ca thi"

    def __str__(self):
        return f"{self.group_name} - {self.subject_id.name} ({self.exam_date.strftime('%d/%m/%Y %H:%M')})"

    @property
    def start_at(self):
        return self.exam_date

    @property
    def end_at(self):
        return self.exam_date + timedelta(minutes=self.duration_minutes or 0)

    @property
    def computed_status(self):
        if self.status == "CANCELLED":
            return "CANCELLED"
        if self.status == "DRAFT":
            return "DRAFT"

        now = timezone.now()
        if now < self.start_at:
            return "SCHEDULED"
        if self.start_at <= now < self.end_at:
            return "ONGOING"
        return "COMPLETED"

    @property
    def status_label(self):
        status_map = {
            "DRAFT": "Lưu nháp",
            "SCHEDULED": "Sắp diễn ra",
            "ONGOING": "Đang diễn ra",
            "COMPLETED": "Đã kết thúc",
            "CANCELLED": "Đã hủy",
        }
        return status_map.get(self.computed_status, self.get_status_display())

    @property
    def room_configs(self):
        config = self.configuration_data or {}
        rooms = config.get("rooms") or []
        return rooms if isinstance(rooms, list) else []

    @property
    def roster_source_configs(self):
        config = self.configuration_data or {}
        rosters = config.get("rosters") or []
        return rosters if isinstance(rosters, list) else []

    @property
    def room_count(self):
        if self.room_configs:
            return len(self.room_configs)
        return self.session_rooms.count()

    @property
    def total_students_count(self):
        if self.room_configs:
            return sum(len((room.get("assignments") or [])) for room in self.room_configs)
        return self.get_total_students()

    @property
    def is_locked(self):
        if self.exam_sessions.exists() or self.status == "CANCELLED":
            return True
        if self.status != "DRAFT" and timezone.now() >= self.start_at:
            return True
        return False

    @property
    def can_modify(self):
        return not self.is_locked

    def get_total_students(self):
        if self.room_configs:
            return self.total_students_count
        return ExamSessionRoom.objects.filter(exam_session_group_id=self).aggregate(
            total=models.Count("students")
        )["total"] or 0

    def get_completed_students(self):
        return ExamSession.objects.filter(exam_session_group_id=self, is_completed=True).count()

    def get_absent_students(self):
        return max(0, self.get_total_students() - self.get_completed_students())

    @property
    def id(self):
        return self.exam_session_group_id

    @property
    def subject(self):
        return self.subject_id

    @subject.setter
    def subject(self, value):
        self.subject_id = value

    @property
    def created_by(self):
        return self.created_by_user_id

    @created_by.setter
    def created_by(self, value):
        self.created_by_user_id = value


class ExamSessionRoom(models.Model):
    exam_session_room_id = models.AutoField(primary_key=True, db_column="exam_session_room_id")
    exam_session_group_id = models.ForeignKey(
        ExamSessionGroup,
        on_delete=models.CASCADE,
        db_column="exam_session_group_id",
        related_name="session_rooms",
    )
    exam_room_id = models.ForeignKey(
        ExamRoom,
        on_delete=models.SET_NULL,
        db_column="exam_room_id",
        null=True,
        blank=True,
        related_name="session_rooms",
    )
    room_name = models.CharField(max_length=100, blank=True, default="", verbose_name="Tên phòng hiển thị")
    expected_students = models.PositiveIntegerField(default=0, verbose_name="Số lượng sinh viên dự kiến")
    room_password = models.CharField(max_length=100, blank=True, default="", verbose_name="Mật khẩu phòng")
    exam_set_id = models.ForeignKey(
        ExamSet,
        on_delete=models.SET_NULL,
        db_column="exam_set_id",
        null=True,
        blank=True,
        related_name="session_rooms",
        verbose_name="Bộ đề gắn cho phòng",
    )
    exam_codes = models.ManyToManyField(
        ExamCode,
        blank=True,
        related_name="session_rooms",
        verbose_name="Mã đề gắn cho phòng",
    )
    display_order = models.PositiveIntegerField(default=1, verbose_name="Thứ tự hiển thị")
    students = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="exam_rooms",
        blank=True,
        verbose_name="Sinh viên",
    )

    class Meta:
        db_table = "exam_session_room"
        ordering = ["display_order", "exam_session_room_id"]
        verbose_name = "Phòng thi trong ca thi"
        verbose_name_plural = "Phòng thi trong ca thi"

    def __str__(self):
        return f"{self.exam_session_group_id.group_name} - {self.display_room_name}"

    @property
    def display_room_name(self):
        return self.room_name or (self.exam_room_id.room_name if self.exam_room_id_id else "")

    @property
    def id(self):
        return self.exam_session_room_id

    @property
    def exam_group(self):
        return self.exam_session_group_id

    @exam_group.setter
    def exam_group(self, value):
        self.exam_session_group_id = value

    @property
    def room(self):
        return self.exam_room_id

    @room.setter
    def room(self, value):
        self.exam_room_id = value

    @property
    def exam_set(self):
        return self.exam_set_id

    @exam_set.setter
    def exam_set(self, value):
        self.exam_set_id = value


class ExamSession(models.Model):
    exam_session_id = models.AutoField(primary_key=True, db_column="exam_session_id")
    user_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        db_column="user_id",
        related_name="exam_sessions",
    )
    subject_id = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        db_column="subject_id",
    )
    exam_session_group_id = models.ForeignKey(
        ExamSessionGroup,
        on_delete=models.SET_NULL,
        db_column="exam_session_group_id",
        null=True,
        blank=True,
        related_name="exam_sessions",
        verbose_name="Ca thi",
    )
    questions = models.ManyToManyField(Question, related_name="exam_sessions")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Ngày thi")
    is_completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name="Thời gian hoàn thành")
    final_score = models.FloatField(null=True, blank=True)

    face_image_blob = models.BinaryField(null=True, blank=True, verbose_name="Ảnh khuôn mặt chụp từ webcam")
    face_image_mime = models.CharField(max_length=100, blank=True, default="", verbose_name="MIME type ảnh khuôn mặt")
    id_card_image_blob = models.BinaryField(null=True, blank=True, verbose_name="Ảnh thẻ sinh viên")
    id_card_image_mime = models.CharField(max_length=100, blank=True, default="", verbose_name="MIME type ảnh thẻ")
    verification_score = models.FloatField(null=True, blank=True, verbose_name="Điểm tương đồng (0-1)")
    verification_status = models.CharField(
        max_length=20,
        choices=[
            ("PENDING", "Chờ xác thực"),
            ("ALLOW", "Cho phép thi"),
            ("WARNING_ALLOW", "Cho phép thi (cần kiểm tra lại)"),
            ("BLOCK", "Chặn thi"),
        ],
        default="PENDING",
        verbose_name="Trạng thái xác thực",
    )
    needs_manual_review = models.BooleanField(default=False, verbose_name="Cần kiểm tra thủ công")
    cheating_flag = models.BooleanField(default=False, verbose_name="Cảnh báo gian lận") # Đã thêm đánh dấu gian lận

    class Meta:
        db_table = "exam_session"
        verbose_name = "Phiên thi"
        verbose_name_plural = "Phiên thi"

    def __str__(self):
        return f"Bài thi môn {self.subject_id.name} của {self.user_id.username}"

    @property
    def id(self):
        return self.exam_session_id

    @property
    def user(self):
        return self.user_id

    @user.setter
    def user(self, value):
        self.user_id = value

    @property
    def subject(self):
        return self.subject_id

    @subject.setter
    def subject(self, value):
        self.subject_id = value

    @property
    def exam_group(self):
        return self.exam_session_group_id

    @exam_group.setter
    def exam_group(self, value):
        self.exam_session_group_id = value


class ExamResult(models.Model):
    exam_result_id = models.AutoField(primary_key=True, db_column="exam_result_id")
    exam_session_id = models.ForeignKey(
        ExamSession,
        on_delete=models.CASCADE,
        db_column="exam_session_id",
        related_name="results",
    )
    question_id = models.ForeignKey(
        Question,
        on_delete=models.CASCADE,
        db_column="question_id",
    )
    transcript = models.TextField(verbose_name="Nội dung trả lời")
    score = models.FloatField(verbose_name="Điểm số")
    feedback = models.TextField(verbose_name="Nhận xét của AI", null=True, blank=True)
    analysis = models.JSONField(verbose_name="Phân tích chi tiết", null=True, blank=True)
    audio_file = models.FileField(
        upload_to="student_audio/%Y/%m/",
        null=True,
        blank=True,
        verbose_name="File ghi âm MP3"
    ) # Đã thêm file ghi âm
    answered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "exam_result"
        verbose_name = "Kết quả thi"
        verbose_name_plural = "Kết quả thi"
        constraints = [
            models.UniqueConstraint(
                fields=["exam_session_id", "question_id"],
                name="uq_exam_result_session_question",
            )
        ]

    def __str__(self):
        return f"Kết quả câu hỏi {self.question_id.question_id} của {self.exam_session_id.user_id.username}"

    @property
    def id(self):
        return self.exam_result_id

    @property
    def session(self):
        return self.exam_session_id

    @session.setter
    def session(self, value):
        self.exam_session_id = value

    @property
    def question(self):
        return self.question_id

    @question.setter
    def question(self, value):
        self.question_id = value


class ViolationImage(models.Model):
    """Lưu hình ảnh gian lận (vd: TWO_FACES) trong quá trình thi"""
    violation_image_id = models.AutoField(primary_key=True, db_column="violation_image_id")
    exam_session_id = models.ForeignKey(
        ExamSession,
        on_delete=models.CASCADE,
        db_column="exam_session_id",
        related_name="violation_images",
        verbose_name="Phiên thi"
    )
    image_blob = models.BinaryField(verbose_name="Ảnh gian lận")
    image_mime = models.CharField(max_length=100, default="image/jpeg", verbose_name="MIME type ảnh")
    violation_type = models.CharField(max_length=50, default="TWO_FACES", verbose_name="Loại gian lận")
    timestamp = models.DateTimeField(auto_now_add=True, verbose_name="Thời điểm ghi nhận")

    class Meta:
        db_table = "violation_image"
        ordering = ["-timestamp"]
        verbose_name = "Hình ảnh gian lận"
        verbose_name_plural = "Hình ảnh gian lận"

    def __str__(self):
        return f"Gian lận {self.violation_type} - Session {self.exam_session_id.exam_session_id}"

    @property
    def id(self):
        return self.violation_image_id

    @property
    def session(self):
        return self.exam_session_id

    @session.setter
    def session(self, value):
        self.exam_session_id = value

    @property
    def get_image_data_url(self):
        from base64 import b64encode
        if self.image_blob:
            return f"data:{self.image_mime};base64,{b64encode(self.image_blob).decode('ascii')}"
        return ""


class StudentRosterUpload(models.Model):
    student_roster_upload_id = models.AutoField(primary_key=True, db_column="student_roster_upload_id")
    subject_id = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        db_column="subject_id",
        related_name="student_roster_uploads",
        verbose_name="Môn học",
    )
    academic_year = models.CharField(max_length=20, verbose_name="Năm học")
    semester = models.CharField(
        max_length=10,
        choices=SemesterChoices.choices,
        default=SemesterChoices.HK1,
        verbose_name="Học kỳ",
    )
    title = models.CharField(max_length=255, verbose_name="Tên hiển thị")
    original_file_name = models.CharField(max_length=255, verbose_name="Tên file gốc")
    uploaded_file = models.FileField(
        upload_to="student_lists/%Y/%m/",
        null=True,
        blank=True,
        verbose_name="Tệp danh sách",
    )
    total_students = models.PositiveIntegerField(default=0, verbose_name="Số lượng sinh viên")
    status = models.CharField(
        max_length=20,
        choices=StudentListUploadStatus.choices,
        default=StudentListUploadStatus.PENDING,
        verbose_name="Trạng thái",
    )
    created_by_user_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        db_column="created_by_user_id",
        null=True,
        blank=True,
        related_name="student_roster_uploads",
        verbose_name="Người tải lên",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Ngày tải lên")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Ngày cập nhật")
    account_created_at = models.DateTimeField(null=True, blank=True, verbose_name="Ngày tạo tài khoản")

    class Meta:
        db_table = "student_roster_upload"
        ordering = ["-created_at"]
        verbose_name = "Danh sách lớp tải lên"
        verbose_name_plural = "Danh sách lớp tải lên"

    def __str__(self):
        return f"{self.title} - {self.subject_id.subject_code}"

    @property
    def created_accounts_count(self):
        return self.students.filter(account_created=True).count()

    @property
    def pending_accounts_count(self):
        return max(0, self.total_students - self.created_accounts_count)

    @property
    def existing_accounts_count(self):
        return self.students.filter(account_status=StudentRosterAccountStatus.EXISTING).count()

    @property
    def new_accounts_count(self):
        return self.students.filter(account_status=StudentRosterAccountStatus.CREATED).count()

    def refresh_status(self, save=True):
        total = self.students.count()
        created = self.students.filter(account_created=True).count()
        self.total_students = total
        self.status = (
            StudentListUploadStatus.CREATED
            if total and created == total
            else StudentListUploadStatus.PENDING
        )
        self.account_created_at = timezone.now() if self.status == StudentListUploadStatus.CREATED else None
        if save:
            self.save(update_fields=["total_students", "status", "account_created_at", "updated_at"])

    @property
    def id(self):
        return self.student_roster_upload_id

    @property
    def subject(self):
        return self.subject_id

    @subject.setter
    def subject(self, value):
        self.subject_id = value

    @property
    def created_by(self):
        return self.created_by_user_id

    @created_by.setter
    def created_by(self, value):
        self.created_by_user_id = value


class StudentRosterStudent(models.Model):
    student_roster_student_id = models.AutoField(primary_key=True, db_column="student_roster_student_id")
    student_roster_upload_id = models.ForeignKey(
        StudentRosterUpload,
        on_delete=models.CASCADE,
        db_column="student_roster_upload_id",
        related_name="students",
        verbose_name="Tệp danh sách",
    )
    row_number = models.PositiveIntegerField(default=1, verbose_name="STT")
    student_code = models.CharField(max_length=50, db_index=True, verbose_name="Mã số sinh viên")
    full_name = models.CharField(max_length=255, verbose_name="Họ và tên")
    gender = models.CharField(max_length=20, blank=True, default="", verbose_name="Giới tính")
    date_of_birth = models.DateField(null=True, blank=True, verbose_name="Ngày sinh")
    class_name = models.CharField(max_length=100, blank=True, default="", verbose_name="Lớp")
    email = models.EmailField(blank=True, default="", verbose_name="Email")
    linked_user_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        db_column="linked_user_id",
        null=True,
        blank=True,
        related_name="student_list_rows",
        verbose_name="Tài khoản liên kết",
    )
    account_created = models.BooleanField(default=False, verbose_name="Đã có tài khoản")
    account_status = models.CharField(
        max_length=20,
        choices=StudentRosterAccountStatus.choices,
        default=StudentRosterAccountStatus.PENDING,
        verbose_name="Nguồn trạng thái tài khoản",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Ngày tạo")

    class Meta:
        db_table = "student_roster_student"
        ordering = ["row_number", "student_roster_student_id"]
        verbose_name = "Sinh viên trong danh sách"
        verbose_name_plural = "Sinh viên trong danh sách"
        constraints = [
            models.UniqueConstraint(
                fields=["student_roster_upload_id", "student_code"],
                name="unique_student_code_per_roster",
            )
        ]

    def __str__(self):
        return f"{self.student_code} - {self.full_name}"

    @property
    def id(self):
        return self.student_roster_student_id

    @property
    def roster(self):
        return self.student_roster_upload_id

    @roster.setter
    def roster(self, value):
        self.student_roster_upload_id = value

    @property
    def linked_user(self):
        return self.linked_user_id

    @linked_user.setter
    def linked_user(self, value):
        self.linked_user_id = value
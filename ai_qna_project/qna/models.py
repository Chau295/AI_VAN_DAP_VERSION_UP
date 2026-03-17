from django.conf import settings
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class DifficultyLevel(models.TextChoices):
    EASY = "EASY", "Dễ"
    MEDIUM = "MEDIUM", "Trung bình"
    HARD = "HARD", "Khó"


class SemesterChoices(models.TextChoices):
    HK1 = "HK1", "HK1"
    HK2 = "HK2", "HK2"
    SUMMER = "SUMMER", "HK hè"


class ExamSetStatus(models.TextChoices):
    DRAFT = "DRAFT", "Chưa duyệt"
    APPROVED = "APPROVED", "Đã duyệt"


class Subject(models.Model):
    name = models.CharField(max_length=255, verbose_name="Tên môn học")
    subject_code = models.CharField(max_length=20, unique=True, verbose_name="Mã môn học")
    quiz_data_file = models.CharField(max_length=255, blank=True, null=True, help_text="Ví dụ: data_analysis_quiz.json")
    exam_password = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name="Mật khẩu bài thi",
        help_text="Mật khẩu để sinh viên vào bài thi (để trống nếu không cần)",
    )

    def __str__(self):
        return self.name


class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    is_lecturer = models.BooleanField(default=False)
    subjects_taught = models.ManyToManyField(Subject, blank=True, related_name="lecturers")
    full_name = models.CharField(max_length=150, blank=True, default="")
    class_name = models.CharField(max_length=100, blank=True, default="")
    student_id = models.CharField(max_length=150, blank=True, default="")

    profile_image_blob = models.BinaryField(null=True, blank=True)
    profile_image_mime = models.CharField(max_length=100, blank=True, default="")

    def __str__(self):
        return self.user.username


# ==========================================
# BẢNG NGÂN HÀNG CÂU HỎI (MỚI)
# ==========================================
class QuestionBank(models.Model):
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name='question_banks', verbose_name="Môn học")
    name = models.CharField(max_length=255, verbose_name="Tên ngân hàng")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Ngày tạo")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Ngân hàng câu hỏi"
        verbose_name_plural = "Ngân hàng câu hỏi"

    def __str__(self):
        return f"{self.name} - {self.subject.name}"


class Question(models.Model):
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="questions", verbose_name="Môn học")
    bank = models.ForeignKey(QuestionBank, on_delete=models.CASCADE, null=True, blank=True, related_name="questions", verbose_name="Ngân hàng câu hỏi")
    question_text = models.TextField(verbose_name="Nội dung câu hỏi")
    question_id_in_barem = models.CharField(
        max_length=20,
        verbose_name="ID câu hỏi trong tệp barem",
        help_text="Ví dụ: Q1, Q2...",
    )
    difficulty = models.CharField(
        max_length=10,
        choices=DifficultyLevel.choices,
        default=DifficultyLevel.EASY,
        verbose_name="Độ khó",
    )
    is_supplementary = models.BooleanField(default=False, verbose_name="Là câu hỏi phụ")
    is_exam_clone = models.BooleanField(default=False, verbose_name="Bản sao dùng cho mã đề")
    created_at = models.DateTimeField(auto_now_add=True, null=True, verbose_name="Ngày tạo")

    class Meta:
        ordering = ["question_id_in_barem"]

    def __str__(self):
        return f"{self.subject.subject_code} - {self.question_text[:50]}..."

    @property
    def short_text(self):
        return self.question_text[:80]


class ExamSet(models.Model):
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="exam_sets", verbose_name="Môn học")
    name = models.CharField(max_length=255, blank=True, default="", verbose_name="Tên bộ đề")
    academic_year = models.CharField(max_length=20, verbose_name="Năm học")
    semester = models.CharField(max_length=10, choices=SemesterChoices.choices, default=SemesterChoices.HK1, verbose_name="Học kỳ")
    number_of_versions = models.PositiveIntegerField(default=1, verbose_name="Số mã đề")
    easy_pool_size = models.PositiveIntegerField(default=1, verbose_name="Số câu dễ trong ma trận")
    medium_pool_size = models.PositiveIntegerField(default=1, verbose_name="Số câu trung bình trong ma trận")
    hard_pool_size = models.PositiveIntegerField(default=1, verbose_name="Số câu khó trong ma trận")
    easy_score = models.DecimalField(max_digits=5, decimal_places=1, default=2.0, verbose_name="Điểm câu dễ")
    medium_score = models.DecimalField(max_digits=5, decimal_places=1, default=2.5, verbose_name="Điểm câu trung bình")
    hard_score = models.DecimalField(max_digits=5, decimal_places=1, default=3.0, verbose_name="Điểm câu khó")
    shuffle_question_order = models.BooleanField(default=True, verbose_name="Tự động xáo trộn thứ tự câu hỏi")
    allow_duplicate_questions = models.BooleanField(default=False, verbose_name="Cho phép câu hỏi trùng lặp giữa các mã đề")
    status = models.CharField(max_length=20, choices=ExamSetStatus.choices, default=ExamSetStatus.DRAFT, verbose_name="Trạng thái")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_exam_sets", verbose_name="Người tạo")
    question_banks = models.ManyToManyField(QuestionBank, blank=True, related_name="exam_sets", verbose_name="Nguồn câu hỏi")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Ngày tạo")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Ngày cập nhật")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Bộ đề thi"
        verbose_name_plural = "Bộ đề thi"

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        return self.name or self.subject.name

    @property
    def total_questions(self):
        return self.easy_pool_size + self.medium_pool_size + self.hard_pool_size

    @property
    def total_score(self):
        return (self.easy_pool_size * float(self.easy_score)) + (self.medium_pool_size * float(self.medium_score)) + (self.hard_pool_size * float(self.hard_score))

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


class ExamCode(models.Model):
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name="exam_codes",
        verbose_name="Môn học"
    )
    exam_set = models.ForeignKey(
        ExamSet,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="exam_codes",
        verbose_name="Bộ đề thi"
    )
    code_name = models.CharField(max_length=100, verbose_name="Tên mã đề")
    code_number = models.PositiveIntegerField(default=0, verbose_name="Số thứ tự mã đề")
    source_material = models.TextField(blank=True, null=True, verbose_name="Nguồn tài liệu")
    is_approved = models.BooleanField(default=False, verbose_name="Đã duyệt")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Ngày tạo")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Ngày cập nhật")

    class Meta:
        ordering = ["code_number", "created_at"]
        verbose_name = "Mã đề thi"
        verbose_name_plural = "Mã đề thi"

    def __str__(self):
        return f"{self.code_name} - {self.subject.name}"

    @property
    def ordered_questions(self):
        return list(
            self.exam_code_questions.select_related("question")
            .order_by("display_order", "id")
        )

    @property
    def total_questions(self):
        return self.exam_code_questions.count()

    @property
    def is_linked(self):
        return self.exam_session_groups.exists()

class ExamCodeQuestion(models.Model):
    exam_code = models.ForeignKey(
        ExamCode,
        on_delete=models.CASCADE,
        related_name="exam_code_questions",
        verbose_name="Mã đề"
    )
    question = models.ForeignKey(
        Question,
        on_delete=models.CASCADE,
        related_name="exam_code_items",
        verbose_name="Câu hỏi"
    )
    difficulty = models.CharField(
        max_length=10,
        choices=DifficultyLevel.choices,
        verbose_name="Độ khó"
    )
    display_order = models.PositiveIntegerField(default=1, verbose_name="Thứ tự hiển thị")
    score = models.DecimalField(max_digits=5, decimal_places=2, default=1, verbose_name="Điểm")

    class Meta:
        ordering = ["display_order", "id"]
        verbose_name = "Câu hỏi trong mã đề"
        verbose_name_plural = "Câu hỏi trong mã đề"

    def __str__(self):
        return f"{self.exam_code.code_name} - Câu {self.display_order}"


class LectureMaterial(models.Model):
    """Tài liệu bài giảng được upload bởi giảng viên"""
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name="lecture_materials",
        verbose_name="Môn học",
    )
    bank = models.ForeignKey(
        QuestionBank,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="materials",
        verbose_name="Ngân hàng câu hỏi"
    )
    title = models.CharField(max_length=255, verbose_name="Tiêu đề tài liệu")
    file_path = models.CharField(max_length=500, verbose_name="Đường dẫn file")
    file_type = models.CharField(max_length=50, verbose_name="Loại file")
    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name="Ngày upload")

    workspace_id = models.CharField(max_length=100, blank=True, null=True, verbose_name="Phiên làm việc")

    class Meta:
        ordering = ["-uploaded_at"]
        verbose_name = "Tài liệu bài giảng"
        verbose_name_plural = "Tài liệu bài giảng"

    def __str__(self):
        return f"{self.title} - {self.subject.name}"

    @property
    def filename(self):
        import os
        return os.path.basename(self.file_path or "")

    @property
    def extension(self):
        name = self.filename.lower()
        return name.split(".")[-1] if "." in name else ""


class ExamRoom(models.Model):
    """Phòng thi trong một ca thi"""
    room_name = models.CharField(max_length=100, verbose_name="Tên phòng")
    room_code = models.CharField(max_length=50, unique=True, verbose_name="Mã phòng")
    capacity = models.PositiveIntegerField(verbose_name="Sức chứa")

    class Meta:
        ordering = ["room_code"]
        verbose_name = "Phòng thi"
        verbose_name_plural = "Phòng thi"

    def __str__(self):
        return f"{self.room_name} ({self.room_code})"


class ExamSessionGroup(models.Model):
    """Ca thi - Nhóm các phiên thi theo mã đề và phòng"""
    STATUS_CHOICES = [
        ("DRAFT", "Nháp"),
        ("SCHEDULED", "Đã lên lịch"),
        ("ONGOING", "Đang diễn ra"),
        ("COMPLETED", "Đã kết thúc"),
        ("CANCELLED", "Đã hủy"),
    ]

    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name="exam_session_groups",
        verbose_name="Môn học",
    )
    group_name = models.CharField(max_length=200, verbose_name="Tên ca thi")

    exam_date = models.DateTimeField(verbose_name="Ngày giờ thi")
    duration_minutes = models.PositiveIntegerField(default=60, verbose_name="Thời gian làm bài (phút)")
    exam_password = models.CharField(max_length=100, blank=True, null=True, verbose_name="Mật khẩu bài thi")

    exam_codes = models.ManyToManyField(ExamCode, related_name="exam_session_groups", verbose_name="Mã đề thi")
    rooms = models.ManyToManyField(ExamRoom, through="ExamSessionRoom", verbose_name="Phòng thi")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="DRAFT", verbose_name="Trạng thái")

    created_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="created_exam_groups",
        verbose_name="Người tạo",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Ngày tạo")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Ngày cập nhật")

    class Meta:
        ordering = ["-exam_date"]
        verbose_name = "Ca thi"
        verbose_name_plural = "Ca thi"

    def __str__(self):
        return f"{self.group_name} - {self.subject.name} ({self.exam_date.strftime('%d/%m/%Y %H:%M')})"

    def get_total_students(self):
        return ExamSessionRoom.objects.filter(exam_group=self).aggregate(
            total=models.Count("students")
        )["total"] or 0

    def get_completed_students(self):
        return ExamSession.objects.filter(exam_group=self, is_completed=True).count()

    def get_absent_students(self):
        return max(0, self.get_total_students() - self.get_completed_students())


class ExamSessionRoom(models.Model):
    """Liên kết giữa Ca thi và Phòng thi"""
    exam_group = models.ForeignKey(ExamSessionGroup, on_delete=models.CASCADE, related_name="session_rooms")
    room = models.ForeignKey(ExamRoom, on_delete=models.CASCADE, related_name="session_rooms")
    students = models.ManyToManyField(User, related_name="exam_rooms", verbose_name="Sinh viên")

    class Meta:
        verbose_name = "Phòng thi trong ca thi"
        verbose_name_plural = "Phòng thi trong ca thi"

    def __str__(self):
        return f"{self.exam_group.group_name} - {self.room.room_name}"


class ExamSession(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="exam_sessions")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE)
    exam_group = models.ForeignKey(
        ExamSessionGroup,
        on_delete=models.SET_NULL,
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

    def __str__(self):
        return f"Bài thi môn {self.subject.name} của {self.user.username}"


class ExamResult(models.Model):
    session = models.ForeignKey(ExamSession, on_delete=models.CASCADE, related_name="results")
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    transcript = models.TextField(verbose_name="Nội dung trả lời")
    score = models.FloatField(verbose_name="Điểm số")
    feedback = models.TextField(verbose_name="Nhận xét của AI", null=True, blank=True)
    analysis = models.JSONField(verbose_name="Phân tích chi tiết", null=True, blank=True)
    answered_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Kết quả câu hỏi {self.question.id} của {self.session.user.username}"


class SupplementaryResult(models.Model):
    session = models.ForeignKey(ExamSession, on_delete=models.CASCADE, related_name="supplementary_results")
    question_text = models.TextField()
    transcript = models.TextField(blank=True, null=True)
    score = models.FloatField(default=0.0)
    max_score = models.FloatField(default=1.0)
    feedback = models.TextField(blank=True, null=True)
    analysis = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Supplementary result for {self.session.user.username} - Score: {self.score}/{self.max_score}"
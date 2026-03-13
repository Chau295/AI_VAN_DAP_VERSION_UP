from django.conf import settings
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class DifficultyLevel(models.TextChoices):
    EASY = "EASY", "Dễ"
    MEDIUM = "MEDIUM", "Trung bình"
    HARD = "HARD", "Khó"


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
    created_at = models.DateTimeField(auto_now_add=True, null=True, verbose_name="Ngày tạo")

    class Meta:
        ordering = ["question_id_in_barem"]

    def __str__(self):
        return f"{self.subject.subject_code} - {self.question_text[:50]}..."

    @property
    def short_text(self):
        return self.question_text[:80]


class ExamCode(models.Model):
    """Mã đề thi - Chứa 3 câu hỏi với 3 mức độ khó"""
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="exam_codes", verbose_name="Môn học")
    code_name = models.CharField(max_length=100, verbose_name="Tên mã đề")

    question_easy = models.ForeignKey(
        Question,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="exam_codes_easy",
        verbose_name="Câu hỏi Dễ",
    )
    question_medium = models.ForeignKey(
        Question,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="exam_codes_medium",
        verbose_name="Câu hỏi Trung bình",
    )
    question_hard = models.ForeignKey(
        Question,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="exam_codes_hard",
        verbose_name="Câu hỏi Khó",
    )

    source_material = models.TextField(blank=True, null=True, verbose_name="Nguồn tài liệu")
    is_approved = models.BooleanField(default=False, verbose_name="Đã duyệt")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Ngày tạo")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Ngày cập nhật")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Mã đề thi"
        verbose_name_plural = "Mã đề thi"

    def __str__(self):
        return f"{self.code_name} - {self.subject.name}"


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

    # ---- DÒNG NÀY ĐỂ QUẢN LÝ PHIÊN TẢI TÀI LIỆU ----
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
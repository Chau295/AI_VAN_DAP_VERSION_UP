from datetime import timedelta
import re
import unicodedata

from django.conf import settings
from django.db import models
from django.utils import timezone


class DifficultyLevel(models.TextChoices):
    EASY = "EASY", "Dá»…"
    MEDIUM = "MEDIUM", "Trung bÃ¬nh"
    HARD = "HARD", "KhÃ³"


class SemesterChoices(models.TextChoices):
    HK1 = "HK1", "Há»c ká»³ 1"
    HK2 = "HK2", "Há»c ká»³ 2"
    SUMMER = "SUMMER", "Há»c ká»³ hÃ¨"


class ExamSetStatus(models.TextChoices):
    DRAFT = "DRAFT", "ChÆ°a duyá»‡t"
    APPROVED = "APPROVED", "ÄÃ£ duyá»‡t"


class StudentListUploadStatus(models.TextChoices):
    PENDING = "PENDING", "ChÆ°a táº¡o"
    CREATED = "CREATED", "ÄÃ£ táº¡o"


class StudentRosterAccountStatus(models.TextChoices):
    PENDING = "PENDING", "ChÆ°a cÃ³"
    EXISTING = "EXISTING", "ÄÃ£ cÃ³ sáºµn"
    CREATED = "CREATED", "Má»›i táº¡o"


def normalize_question_text(value: str) -> str:
    text = (value or "").strip().lower()
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text)
    return text


class Subject(models.Model):
    subject_id = models.AutoField(primary_key=True, db_column="subject_id")
    name = models.CharField(max_length=255, verbose_name="TÃªn mÃ´n há»c")
    subject_code = models.CharField(max_length=20, unique=True, verbose_name="MÃ£ mÃ´n há»c")
    quiz_data_file = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="VÃ­ dá»¥: data_analysis_quiz.json",
    )
    exam_password = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name="Máº­t kháº©u bÃ i thi",
        help_text="Máº­t kháº©u Ä‘á»ƒ sinh viÃªn vÃ o bÃ i thi (Ä‘á»ƒ trá»‘ng náº¿u khÃ´ng cáº§n)",
    )

    class Meta:
        db_table = "subject"
        ordering = ["name"]
        verbose_name = "MÃ´n há»c"
        verbose_name_plural = "MÃ´n há»c"

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
        verbose_name = "Há»“ sÆ¡ ngÆ°á»i dÃ¹ng"
        verbose_name_plural = "Há»“ sÆ¡ ngÆ°á»i dÃ¹ng"

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
        verbose_name="MÃ´n há»c",
    )
    name = models.CharField(max_length=255, verbose_name="TÃªn ngÃ¢n hÃ ng")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="NgÃ y táº¡o")

    class Meta:
        db_table = "question_bank"
        ordering = ["-created_at"]
        verbose_name = "NgÃ¢n hÃ ng cÃ¢u há»i"
        verbose_name_plural = "NgÃ¢n hÃ ng cÃ¢u há»i"

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
        verbose_name="MÃ´n há»c",
    )
    question_bank_id = models.ForeignKey(
        QuestionBank,
        on_delete=models.CASCADE,
        db_column="question_bank_id",
        null=True,
        blank=True,
        related_name="questions",
        verbose_name="NgÃ¢n hÃ ng cÃ¢u há»i",
    )
    question_text = models.TextField(verbose_name="Ná»™i dung cÃ¢u há»i")
    question_text_normalized = models.TextField(
        verbose_name="CÃ¢u há»i chuáº©n hÃ³a",
        blank=True,
        null=True,
    )
    question_id_in_barem = models.CharField(
        max_length=50,
        verbose_name="ID cÃ¢u há»i trong tá»‡p barem",
        help_text="VÃ­ dá»¥: Q1, Q2...",
    )
    difficulty = models.CharField(
        max_length=10,
        choices=DifficultyLevel.choices,
        default=DifficultyLevel.EASY,
        verbose_name="Äá»™ khÃ³",
    )
    is_exam_clone = models.BooleanField(default=False, verbose_name="Báº£n sao dÃ¹ng cho mÃ£ Ä‘á»")
    is_draft = models.BooleanField(default=False, verbose_name="Báº£n nhÃ¡p chÆ°a lÆ°u")
    created_at = models.DateTimeField(auto_now_add=True, null=True, verbose_name="NgÃ y táº¡o")

    class Meta:
        db_table = "question"
        ordering = ["question_id_in_barem", "question_id"]
        verbose_name = "CÃ¢u há»i"
        verbose_name_plural = "CÃ¢u há»i"
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
        verbose_name="MÃ´n há»c",
    )
    name = models.CharField(max_length=255, blank=True, default="", verbose_name="TÃªn bá»™ Ä‘á»")
    academic_year = models.CharField(max_length=20, verbose_name="NÄƒm há»c")
    semester = models.CharField(
        max_length=10,
        choices=SemesterChoices.choices,
        default=SemesterChoices.HK1,
        verbose_name="Há»c ká»³",
    )
    number_of_versions = models.PositiveIntegerField(default=1, verbose_name="Sá»‘ mÃ£ Ä‘á»")
    easy_pool_size = models.PositiveIntegerField(default=1, verbose_name="Sá»‘ cÃ¢u dá»… trong ma tráº­n")
    medium_pool_size = models.PositiveIntegerField(default=1, verbose_name="Sá»‘ cÃ¢u trung bÃ¬nh trong ma tráº­n")
    hard_pool_size = models.PositiveIntegerField(default=1, verbose_name="Sá»‘ cÃ¢u khÃ³ trong ma tráº­n")
    easy_score = models.DecimalField(max_digits=5, decimal_places=1, default=2.0, verbose_name="Äiá»ƒm cÃ¢u dá»…")
    medium_score = models.DecimalField(max_digits=5, decimal_places=1, default=2.5, verbose_name="Äiá»ƒm cÃ¢u trung bÃ¬nh")
    hard_score = models.DecimalField(max_digits=5, decimal_places=1, default=3.0, verbose_name="Äiá»ƒm cÃ¢u khÃ³")
    shuffle_question_order = models.BooleanField(default=True, verbose_name="Tá»± Ä‘á»™ng xÃ¡o trá»™n thá»© tá»± cÃ¢u há»i")
    allow_duplicate_questions = models.BooleanField(
        default=False,
        verbose_name="Cho phÃ©p cÃ¢u há»i trÃ¹ng láº·p giá»¯a cÃ¡c mÃ£ Ä‘á»",
    )
    status = models.CharField(
        max_length=20,
        choices=ExamSetStatus.choices,
        default=ExamSetStatus.DRAFT,
        verbose_name="Tráº¡ng thÃ¡i",
    )
    created_by_user_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        db_column="created_by_user_id",
        null=True,
        blank=True,
        related_name="created_exam_sets",
        verbose_name="NgÆ°á»i táº¡o",
    )
    question_banks = models.ManyToManyField(
        QuestionBank,
        blank=True,
        related_name="exam_sets",
        verbose_name="Nguá»“n cÃ¢u há»i",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="NgÃ y táº¡o")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="NgÃ y cáº­p nháº­t")

    class Meta:
        db_table = "exam_set"
        ordering = ["-created_at"]
        verbose_name = "Bá»™ Ä‘á» thi"
        verbose_name_plural = "Bá»™ Ä‘á» thi"

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
        return "ÄÃ£ dÃ¹ng" if self.is_linked else "ChÆ°a dÃ¹ng"

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
        verbose_name="MÃ´n há»c",
    )
    exam_set_id = models.ForeignKey(
        ExamSet,
        on_delete=models.CASCADE,
        db_column="exam_set_id",
        null=True,
        blank=True,
        related_name="exam_codes",
        verbose_name="Bá»™ Ä‘á» thi",
    )
    code_name = models.CharField(max_length=100, verbose_name="TÃªn mÃ£ Ä‘á»")
    code_number = models.PositiveIntegerField(default=0, verbose_name="Sá»‘ thá»© tá»± mÃ£ Ä‘á»")
    source_material = models.TextField(blank=True, null=True, verbose_name="Nguá»“n tÃ i liá»‡u")
    is_approved = models.BooleanField(default=False, verbose_name="ÄÃ£ duyá»‡t")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="NgÃ y táº¡o")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="NgÃ y cáº­p nháº­t")

    class Meta:
        db_table = "exam_code"
        ordering = ["code_number", "created_at"]
        verbose_name = "MÃ£ Ä‘á» thi"
        verbose_name_plural = "MÃ£ Ä‘á» thi"

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
        verbose_name="MÃ£ Ä‘á»",
    )
    question_id = models.ForeignKey(
        Question,
        on_delete=models.CASCADE,
        db_column="question_id",
        related_name="exam_code_items",
        verbose_name="CÃ¢u há»i",
    )
    difficulty = models.CharField(
        max_length=10,
        choices=DifficultyLevel.choices,
        verbose_name="Äá»™ khÃ³",
    )
    display_order = models.PositiveIntegerField(default=1, verbose_name="Thá»© tá»± hiá»ƒn thá»‹")
    score = models.DecimalField(max_digits=5, decimal_places=2, default=1, verbose_name="Äiá»ƒm")

    class Meta:
        db_table = "exam_code_question"
        ordering = ["display_order", "exam_code_question_id"]
        verbose_name = "CÃ¢u há»i trong mÃ£ Ä‘á»"
        verbose_name_plural = "CÃ¢u há»i trong mÃ£ Ä‘á»"

    def __str__(self):
        return f"{self.exam_code_id.code_name} - CÃ¢u {self.display_order}"

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
        verbose_name="MÃ´n há»c",
    )
    question_bank_id = models.ForeignKey(
        QuestionBank,
        on_delete=models.CASCADE,
        db_column="question_bank_id",
        null=True,
        blank=True,
        related_name="materials",
        verbose_name="NgÃ¢n hÃ ng cÃ¢u há»i",
    )
    title = models.CharField(max_length=255, verbose_name="TiÃªu Ä‘á» tÃ i liá»‡u")
    file_path = models.CharField(max_length=500, verbose_name="ÄÆ°á»ng dáº«n file")
    file_type = models.CharField(max_length=50, verbose_name="Loáº¡i file")
    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name="NgÃ y upload")
    workspace_id = models.CharField(max_length=100, blank=True, null=True, verbose_name="Phi lÃ m viá»‡c")

    class Meta:
        db_table = "lecture_material"
        ordering = ["-uploaded_at"]
        verbose_name = "TÃ i liá»‡u bÃ i giáº£ng"
        verbose_name_plural = "TÃ i liá»‡u bÃ i giáº£ng"

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
    room_name = models.CharField(max_length=100, verbose_name="TÃªn phÃ²ng")
    room_code = models.CharField(max_length=50, unique=True, verbose_name="MÃ£ phÃ²ng")
    capacity = models.PositiveIntegerField(verbose_name="Sá»©c chá»©a")

    class Meta:
        db_table = "exam_room"
        ordering = ["room_code"]
        verbose_name = "PhÃ²ng thi"
        verbose_name_plural = "PhÃ²ng thi"

    def __str__(self):
        return f"{self.room_name} ({self.room_code})"

    @property
    def id(self):
        return self.exam_room_id


class ExamSessionGroup(models.Model):
    STATUS_CHOICES = [
        ("DRAFT", "NhÃ¡p"),
        ("SCHEDULED", "ÄÃ£ lÃªn lá»‹ch"),
        ("ONGOING", "Äang diá»…n ra"),
        ("COMPLETED", "ÄÃ£ káº¿t thÃºc"),
        ("CANCELLED", "ÄÃ£ há»§y"),
    ]

    exam_session_group_id = models.AutoField(primary_key=True, db_column="exam_session_group_id")
    subject_id = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        db_column="subject_id",
        related_name="exam_session_groups",
        verbose_name="MÃ´n há»c",
    )
    group_name = models.CharField(max_length=200, verbose_name="TÃªn ca thi")
    academic_year = models.CharField(max_length=20, blank=True, default="", verbose_name="NÄƒm há»c")
    semester = models.CharField(
        max_length=10,
        choices=SemesterChoices.choices,
        default=SemesterChoices.HK1,
        verbose_name="Há»c ká»³",
    )
    description = models.TextField(blank=True, default="", verbose_name="MÃ´ táº£ ká»³ thi")
    exam_date = models.DateTimeField(verbose_name="NgÃ y giá» thi")
    duration_minutes = models.PositiveIntegerField(default=60, verbose_name="Thá»i gian lÃ m bÃ i (phÃºt)")
    exam_password = models.CharField(max_length=100, blank=True, null=True, verbose_name="Máº­t kháº©u bÃ i thi")
    configuration_data = models.JSONField(default=dict, blank=True, verbose_name="Cáº¥u hÃ¬nh ca thi")
    exam_codes = models.ManyToManyField(
        ExamCode,
        related_name="exam_session_groups",
        verbose_name="MÃ£ Ä‘á» thi",
    )
    rooms = models.ManyToManyField(
        ExamRoom,
        through="ExamSessionRoom",
        verbose_name="PhÃ²ng thi",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="DRAFT", verbose_name="Tráº¡ng thÃ¡i")
    created_by_user_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        db_column="created_by_user_id",
        related_name="created_exam_groups",
        verbose_name="NgÆ°á»i táº¡o",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="NgÃ y táº¡o")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="NgÃ y cáº­p nháº­t")

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
    def is_entry_open(self):
        if self.status == "CANCELLED":
            return False

        start_at = self.start_at
        end_at = self.end_at
        if not start_at or not end_at:
            return False

        now = timezone.now()
        return start_at <= now <= end_at

    @property
    def computed_status(self):
        if self.status == "CANCELLED":
            return "CANCELLED"

        now = timezone.now()
        if self.start_at <= now <= self.end_at:
            return "ONGOING"
        if now > self.end_at:
            return "COMPLETED"
        if self.status == "DRAFT":
            return "DRAFT"
        return "SCHEDULED"

    @property
    def status_label(self):
        status_map = {
            "DRAFT": "LÆ°u nhÃ¡p",
            "SCHEDULED": "Sáº¯p diá»…n ra",
            "ONGOING": "Äang diá»…n ra",
            "COMPLETED": "ÄÃ£ káº¿t thÃºc",
            "CANCELLED": "ÄÃ£ há»§y",
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
        if self.exam_sessions.exists():
            return True
        if self.computed_status in ["ONGOING", "COMPLETED", "CANCELLED"]:
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
        return ExamSession.objects.filter(exam_session_group_id=self, session_status="COMPLETED").count()

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
    room_name = models.CharField(max_length=100, blank=True, default="", verbose_name="TÃªn phÃ²ng hiá»ƒn thá»‹")
    expected_students = models.PositiveIntegerField(default=0, verbose_name="Sá»‘ lÆ°á»£ng sinh viÃªn dá»± kiáº¿n")
    room_password = models.CharField(max_length=100, blank=True, default="", verbose_name="Máº­t kháº©u phÃ²ng")
    exam_set_id = models.ForeignKey(
        ExamSet,
        on_delete=models.SET_NULL,
        db_column="exam_set_id",
        null=True,
        blank=True,
        related_name="session_rooms",
        verbose_name="Bá»™ Ä‘á» gáº¯n cho phÃ²ng",
    )
    exam_codes = models.ManyToManyField(
        ExamCode,
        blank=True,
        related_name="session_rooms",
        verbose_name="MÃ£ Ä‘á» gáº¯n cho phÃ²ng",
    )
    display_order = models.PositiveIntegerField(default=1, verbose_name="Thá»© tá»± hiá»ƒn thá»‹")
    students = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="exam_rooms",
        blank=True,
        verbose_name="Sinh viÃªn",
    )

    class Meta:
        db_table = "exam_session_room"
        ordering = ["display_order", "exam_session_room_id"]
        verbose_name = "PhÃ²ng thi trong ca thi"
        verbose_name_plural = "PhÃ²ng thi trong ca thi"

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
    STATUS_CHOICES = [
        ("PENDING", "Chá»"),
        ("STARTED", "ÄÃ£ báº¯t Ä‘áº§u"),
        ("COMPLETED", "HoÃ n thÃ nh"),
        ("CANCELLED", "Há»§y"),
    ]

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
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="NgÃ y thi")
    started_at = models.DateTimeField(null=True, blank=True, verbose_name="Thá»i gian báº¯t Ä‘áº§u lÃ m bÃ i")
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name="Thá»i gian hoÃ n thÃ nh")
    session_status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="PENDING",
        verbose_name="Tráº¡ng thÃ¡i phiÃªn thi",
    )
    final_score = models.FloatField(null=True, blank=True)

    face_image_blob = models.BinaryField(null=True, blank=True, verbose_name="áº¢nh khuÃ´n máº·t chá»¥p tá»« webcam")
    face_image_mime = models.CharField(max_length=100, blank=True, default="", verbose_name="MIME type áº£nh khuÃ´n máº·t")
    id_card_image_blob = models.BinaryField(null=True, blank=True, verbose_name="áº¢nh tháº» sinh viÃªn")
    id_card_image_mime = models.CharField(max_length=100, blank=True, default="", verbose_name="MIME type áº£nh tháº»")
    verification_score = models.FloatField(null=True, blank=True, verbose_name="Äiá»ƒm tÆ°Æ¡ng Ä‘á»“ng (0-1)")
    verification_status = models.CharField(
        max_length=20,
        choices=[
            ("PENDING", "Chá» xÃ¡c thá»±c"),
            ("ALLOW", "Cho phÃ©p thi"),
            ("WARNING_ALLOW", "Cho phÃ©p thi (cáº§n kiá»ƒm tra láº¡i)"),
            ("BLOCK", "Cháº·n thi"),
        ],
        default="PENDING",
        verbose_name="Tráº¡ng thÃ¡i xÃ¡c thá»±c",
    )
    needs_manual_review = models.BooleanField(default=False, verbose_name="Cáº§n kiá»ƒm tra thá»§ cÃ´ng")
    cheating_flag = models.BooleanField(default=False, verbose_name="Cáº£nh bÃ¡o gian láº­n") # ÄÃ£ thÃªm Ä‘Ã¡nh dáº¥u gian láº­n

    class Meta:
        db_table = "exam_session"
        verbose_name = "PhiÃªn thi"
        verbose_name_plural = "PhiÃªn thi"

    def __str__(self):
        return f"BÃ i thi mÃ´n {self.subject_id.name} cá»§a {self.user_id.username}"

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
    def is_completed(self):
        return self.session_status == "COMPLETED"

    @property
    def exam_group(self):
        return self.exam_session_group_id

    @exam_group.setter
    def exam_group(self, value):
        self.exam_session_group_id = value

    @property
    def display_status(self):
        if self.session_status == "COMPLETED":
            return "COMPLETED"

        exam_group = getattr(self, "exam_session_group_id", None) or getattr(self, "exam_group", None)
        if exam_group and getattr(exam_group, "computed_status", None) == "COMPLETED":
            answered_count = self.results.count() if hasattr(self, "results") else 0
            return "ENDED" if answered_count > 0 else "ABSENT"

        return "IN_PROGRESS"

    @property
    def calculated_final_score(self):
        return float(self.final_score or 0)


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
    transcript = models.TextField(verbose_name="Ná»™i dung tráº£ lá»i")
    score = models.FloatField(verbose_name="Äiá»ƒm sá»‘")
    feedback = models.TextField(verbose_name="Nháº­n xÃ©t cá»§a AI", null=True, blank=True)
    analysis = models.JSONField(verbose_name="PhÃ¢n tÃ­ch chi tiáº¿t", null=True, blank=True)
    audio_file = models.FileField(
        upload_to="student_audio/%Y/%m/",
        null=True,
        blank=True,
        verbose_name="File ghi Ã¢m MP3"
    ) # ÄÃ£ thÃªm file ghi Ã¢m
    answered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "exam_result"
        verbose_name = "Káº¿t quáº£ thi"
        verbose_name_plural = "Káº¿t quáº£ thi"
        constraints = [
            models.UniqueConstraint(
                fields=["exam_session_id", "question_id"],
                name="uq_exam_result_session_question",
            )
        ]

    def __str__(self):
        return f"Káº¿t quáº£ cÃ¢u há»i {self.question_id.question_id} cá»§a {self.exam_session_id.user_id.username}"

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


class ExamAppeal(models.Model):
    exam_appeal_id = models.AutoField(primary_key=True, db_column="exam_appeal_id")
    exam_session_id = models.OneToOneField(
        ExamSession,
        on_delete=models.CASCADE,
        db_column="exam_session_id",
        related_name="appeal",
        verbose_name="PhiÃªn thi",
    )
    request_content = models.TextField(verbose_name="Ná»™i dung phÃºc kháº£o")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="NgÃ y táº¡o")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="NgÃ y cáº­p nháº­t")

    class Meta:
        db_table = "exam_appeal"
        verbose_name = "YÃªu cáº§u phÃºc kháº£o"
        verbose_name_plural = "YÃªu cáº§u phÃºc kháº£o"

    def __str__(self):
        return f"PhÃºc kháº£o phiÃªn thi {self.exam_session_id_id}"

    @property
    def id(self):
        return self.exam_appeal_id

    @property
    def session(self):
        return self.exam_session_id

    @session.setter
    def session(self, value):
        self.exam_session_id = value


class ViolationImage(models.Model):
    """LÆ°u hÃ¬nh áº£nh gian láº­n (vd: TWO_FACES) trong quÃ¡ trÃ¬nh thi"""
    violation_image_id = models.AutoField(primary_key=True, db_column="violation_image_id")
    exam_session_id = models.ForeignKey(
        ExamSession,
        on_delete=models.CASCADE,
        db_column="exam_session_id",
        related_name="violation_images",
        verbose_name="PhiÃªn thi"
    )
    image = models.ImageField(upload_to='violations/', null=True, blank=True, verbose_name="áº¢nh gian láº­n")
    image_mime = models.CharField(max_length=100, default="image/jpeg", verbose_name="MIME type áº£nh")
    violation_type = models.CharField(max_length=50, default="TWO_FACES", verbose_name="Loáº¡i gian láº­n")
    timestamp = models.DateTimeField(auto_now_add=True, verbose_name="Thá»i Ä‘iá»ƒm ghi nháº­n")

    class Meta:
        db_table = "violation_image"
        ordering = ["-timestamp"]
        verbose_name = "HÃ¬nh áº£nh gian láº­n"
        verbose_name_plural = "HÃ¬nh áº£nh gian láº­n"

    def __str__(self):
        return f"Gian láº­n {self.violation_type} - Session {self.exam_session_id.exam_session_id}"

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
        if self.image:
            return self.image.url
        return ""


class StudentRosterUpload(models.Model):
    student_roster_upload_id = models.AutoField(primary_key=True, db_column="student_roster_upload_id")
    subject_id = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        db_column="subject_id",
        related_name="student_roster_uploads",
        verbose_name="MÃ´n há»c",
    )
    academic_year = models.CharField(max_length=20, verbose_name="NÄƒm há»c")
    semester = models.CharField(
        max_length=10,
        choices=SemesterChoices.choices,
        default=SemesterChoices.HK1,
        verbose_name="Há»c ká»³",
    )
    title = models.CharField(max_length=255, verbose_name="TÃªn hiá»ƒn thá»‹")
    original_file_name = models.CharField(max_length=255, verbose_name="TÃªn file gá»‘c")
    uploaded_file = models.FileField(
        upload_to="student_lists/%Y/%m/",
        null=True,
        blank=True,
        verbose_name="Tá»‡p danh sÃ¡ch",
    )
    total_students = models.PositiveIntegerField(default=0, verbose_name="Sá»‘ lÆ°á»£ng sinh viÃªn")
    status = models.CharField(
        max_length=20,
        choices=StudentListUploadStatus.choices,
        default=StudentListUploadStatus.PENDING,
        verbose_name="Tráº¡ng thÃ¡i",
    )
    created_by_user_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        db_column="created_by_user_id",
        null=True,
        blank=True,
        related_name="student_roster_uploads",
        verbose_name="NgÆ°á»i táº£i lÃªn",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="NgÃ y táº£i lÃªn")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="NgÃ y cáº­p nháº­t")
    account_created_at = models.DateTimeField(null=True, blank=True, verbose_name="NgÃ y táº¡o tÃ i khoáº£n")

    class Meta:
        db_table = "student_roster_upload"
        ordering = ["-created_at"]
        verbose_name = "Danh sÃ¡ch lá»›p táº£i lÃªn"
        verbose_name_plural = "Danh sÃ¡ch lá»›p táº£i lÃªn"

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
        verbose_name="Tá»‡p danh sÃ¡ch",
    )
    row_number = models.PositiveIntegerField(default=1, verbose_name="STT")
    student_code = models.CharField(max_length=50, db_index=True, verbose_name="MÃ£ sá»‘ sinh viÃªn")
    full_name = models.CharField(max_length=255, verbose_name="Há» vÃ  tÃªn")
    gender = models.CharField(max_length=20, blank=True, default="", verbose_name="Giá»›i tÃ­nh")
    date_of_birth = models.DateField(null=True, blank=True, verbose_name="NgÃ y sinh")
    class_name = models.CharField(max_length=100, blank=True, default="", verbose_name="Lá»›p")
    email = models.EmailField(blank=True, default="", verbose_name="Email")
    linked_user_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        db_column="linked_user_id",
        null=True,
        blank=True,
        related_name="student_list_rows",
        verbose_name="TÃ i khoáº£n liÃªn káº¿t",
    )
    account_created = models.BooleanField(default=False, verbose_name="ÄÃ£ cÃ³ tÃ i khoáº£n")
    account_status = models.CharField(
        max_length=20,
        choices=StudentRosterAccountStatus.choices,
        default=StudentRosterAccountStatus.PENDING,
        verbose_name="Nguá»“n tráº¡ng thÃ¡i tÃ i khoáº£n",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="NgÃ y táº¡o")

    class Meta:
        db_table = "student_roster_student"
        ordering = ["row_number", "student_roster_student_id"]
        verbose_name = "Sinh viÃªn trong danh sÃ¡ch"
        verbose_name_plural = "Sinh viÃªn trong danh sÃ¡ch"
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


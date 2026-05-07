from django.core.management.base import BaseCommand
from django.db.models import Sum, Count, Max

from qna.models import AIUsageLog


def n(value):
    return "{:,}".format(int(value or 0)).replace(",", ".")


def sec_ms(value):
    return "{:.1f}s".format(int(value or 0) / 1000)


def sec(value):
    return "{:.1f}s".format(float(value or 0))


class Command(BaseCommand):
    help = "Kiểm tra token AI cho tạo câu hỏi và phiên thi"

    def add_arguments(self, parser):
        parser.add_argument(
            "mode",
            nargs="?",
            default="all",
            choices=["questions", "exams", "all"],
            help="questions = tạo câu hỏi, exams = phiên thi, all = cả hai",
        )

    def handle(self, *args, **options):
        mode = options["mode"]

        if mode in ["questions", "all"]:
            self.show_question_generation_report()

        if mode in ["exams", "all"]:
            self.show_exam_attempt_report()

    def show_question_generation_report(self):
        rows = list(
            AIUsageLog.objects
            .filter(group_id__startswith="question_generation_")
            .values("group_id")
            .annotate(
                requests=Count("id"),
                prompt=Sum("prompt_tokens"),
                output=Sum("completion_tokens"),
                total=Sum("total_tokens"),
                time=Sum("latency_ms"),
                latest=Max("created_at"),
            )
            .order_by("-latest")[:10]
        )

        self.stdout.write("")
        self.stdout.write("=" * 90)
        self.stdout.write("TOKEN - TAO CAU HOI")
        self.stdout.write("=" * 90)

        if not rows:
            self.stdout.write("Chua co log tao cau hoi.")
            return

        self.stdout.write(
            "{:<4} {:<58} {:>4} {:>10} {:>10} {:>10} {:>8}".format(
                "STT", "GROUP_ID", "REQ", "PROMPT", "OUTPUT", "TOTAL", "TIME"
            )
        )
        self.stdout.write("-" * 120)

        for index, row in enumerate(rows, 1):
            self.stdout.write(
                "{:<4} {:<58} {:>4} {:>10} {:>10} {:>10} {:>8}".format(
                    index,
                    row["group_id"],
                    row["requests"],
                    n(row["prompt"]),
                    n(row["output"]),
                    n(row["total"]),
                    sec_ms(row["time"]),
                )
            )

        latest_group_id = rows[0]["group_id"]
        logs = AIUsageLog.objects.filter(group_id=latest_group_id).order_by("created_at")

        total_prompt = sum(item.prompt_tokens for item in logs)
        total_output = sum(item.completion_tokens for item in logs)
        total_token = sum(item.total_tokens for item in logs)
        total_time = sum(item.latency_ms for item in logs)

        self.stdout.write("")
        self.stdout.write("--- CHI TIET LAN TAO CAU HOI MOI NHAT ---")
        self.stdout.write(f"GROUP_ID        : {latest_group_id}")
        self.stdout.write(f"Tong request AI : {logs.count()}")
        self.stdout.write(f"Tong prompt     : {n(total_prompt)} tokens")
        self.stdout.write(f"Tong output     : {n(total_output)} tokens")
        self.stdout.write(f"Tong token      : {n(total_token)} tokens")
        self.stdout.write(f"Tong thoi gian  : {sec_ms(total_time)}")

        self.stdout.write("")
        self.stdout.write("Chi tiet tung batch:")
        self.stdout.write(
            "{:<18} {:<30} {:<14} {:>10} {:>10} {:>10} {:>8}".format(
                "STEP", "FEATURE", "MODEL", "PROMPT", "OUTPUT", "TOTAL", "TIME"
            )
        )
        self.stdout.write("-" * 120)

        for log in logs:
            self.stdout.write(
                "{:<18} {:<30} {:<14} {:>10} {:>10} {:>10} {:>8}".format(
                    log.step_name,
                    log.feature,
                    log.model_name,
                    n(log.prompt_tokens),
                    n(log.completion_tokens),
                    n(log.total_tokens),
                    sec_ms(log.latency_ms),
                )
            )

    def show_exam_attempt_report(self):
        rows = list(
            AIUsageLog.objects
            .filter(group_id__startswith="exam_attempt_")
            .values("group_id")
            .annotate(
                requests=Count("id"),
                prompt=Sum("prompt_tokens"),
                output=Sum("completion_tokens"),
                total=Sum("total_tokens"),
                audio=Sum("audio_duration_seconds"),
                time=Sum("latency_ms"),
                latest=Max("created_at"),
            )
            .order_by("-latest")[:10]
        )

        self.stdout.write("")
        self.stdout.write("=" * 90)
        self.stdout.write("TOKEN - PHIEN THI / SINH VIEN THI")
        self.stdout.write("=" * 90)

        if not rows:
            self.stdout.write("Chua co log phien thi. Hay cho sinh vien tra loi mot cau de test.")
            return

        self.stdout.write(
            "{:<4} {:<40} {:>4} {:>10} {:>10} {:>10} {:>8} {:>8}".format(
                "STT", "GROUP_ID", "REQ", "PROMPT", "OUTPUT", "TOTAL", "AUDIO", "TIME"
            )
        )
        self.stdout.write("-" * 130)

        for index, row in enumerate(rows, 1):
            self.stdout.write(
                "{:<4} {:<40} {:>4} {:>10} {:>10} {:>10} {:>8} {:>8}".format(
                    index,
                    row["group_id"],
                    row["requests"],
                    n(row["prompt"]),
                    n(row["output"]),
                    n(row["total"]),
                    sec(row["audio"]),
                    sec_ms(row["time"]),
                )
            )

        latest_group_id = rows[0]["group_id"]
        logs = AIUsageLog.objects.filter(group_id=latest_group_id).order_by("created_at")

        total_prompt = sum(item.prompt_tokens for item in logs)
        total_output = sum(item.completion_tokens for item in logs)
        total_token = sum(item.total_tokens for item in logs)
        total_audio = sum(item.audio_duration_seconds for item in logs)
        total_time = sum(item.latency_ms for item in logs)

        self.stdout.write("")
        self.stdout.write("--- CHI TIET PHIEN THI MOI NHAT ---")
        self.stdout.write(f"GROUP_ID        : {latest_group_id}")
        self.stdout.write(f"Tong request AI : {logs.count()}")
        self.stdout.write(f"Tong prompt     : {n(total_prompt)} tokens")
        self.stdout.write(f"Tong output     : {n(total_output)} tokens")
        self.stdout.write(f"Tong token      : {n(total_token)} tokens")
        self.stdout.write(f"Tong audio      : {sec(total_audio)}")
        self.stdout.write(f"Tong thoi gian  : {sec_ms(total_time)}")

        self.stdout.write("")
        self.stdout.write("Chi tiet tung buoc:")
        self.stdout.write(
            "{:<28} {:<24} {:<14} {:>10} {:>10} {:>10} {:>8} {:>8}".format(
                "STEP", "FEATURE", "MODEL", "PROMPT", "OUTPUT", "TOTAL", "AUDIO", "TIME"
            )
        )
        self.stdout.write("-" * 135)

        for log in logs:
            self.stdout.write(
                "{:<28} {:<24} {:<14} {:>10} {:>10} {:>10} {:>8} {:>8}".format(
                    log.step_name,
                    log.feature,
                    log.model_name,
                    n(log.prompt_tokens),
                    n(log.completion_tokens),
                    n(log.total_tokens),
                    sec(log.audio_duration_seconds),
                    sec_ms(log.latency_ms),
                )
            )
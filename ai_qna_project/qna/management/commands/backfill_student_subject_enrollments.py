from django.core.management.base import BaseCommand
from django.db import transaction

from qna.models import StudentRosterStudent, UserProfile


class Command(BaseCommand):
    help = "Backfill subjects_enrolled từ các StudentRosterStudent đã link tài khoản."

    def handle(self, *args, **options):
        total_links = 0
        created_links = 0

        rows = (
            StudentRosterStudent.objects
            .select_related("linked_user_id", "student_roster_upload_id__subject_id")
            .filter(linked_user_id__isnull=False)
            .order_by("student_roster_student_id")
        )

        with transaction.atomic():
            for row in rows:
                user = row.linked_user_id
                subject = row.student_roster_upload_id.subject_id
                profile, _ = UserProfile.objects.get_or_create(user_id=user)

                total_links += 1
                if not profile.subjects_enrolled.filter(pk=subject.pk).exists():
                    profile.subjects_enrolled.add(subject)
                    created_links += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Backfill xong. Đã kiểm tra {total_links} dòng roster-linked, thêm mới {created_links} liên kết subjects_enrolled."
            )
        )
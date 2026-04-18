from django.db import migrations, models
from django.db.models import Count


def validate_question_bank_name_duplicates(apps, schema_editor):
    QuestionBank = apps.get_model("qna", "QuestionBank")
    Subject = apps.get_model("qna", "Subject")

    duplicate_rows = list(
        QuestionBank.objects.values("subject_id", "name")
        .annotate(total=Count("question_bank_id"))
        .filter(total__gt=1)
        .order_by("subject_id", "name")
    )
    if not duplicate_rows:
        return

    subject_map = Subject.objects.in_bulk([row["subject_id"] for row in duplicate_rows])
    duplicate_details = []
    for row in duplicate_rows[:10]:
        subject = subject_map.get(row["subject_id"])
        subject_code = getattr(subject, "subject_code", "?") if subject is not None else "?"
        subject_name = getattr(subject, "name", "?") if subject is not None else "?"
        bank_name = row["name"] or "<trống>"
        duplicate_details.append(
            f"subject_id={row['subject_id']} ({subject_code} - {subject_name}), name='{bank_name}', count={row['total']}"
        )

    if len(duplicate_rows) > 10:
        duplicate_details.append(f"... và {len(duplicate_rows) - 10} nhóm trùng khác")

    raise RuntimeError(
        "Không thể áp dụng ràng buộc unique QuestionBank(subject, name) vì phát hiện dữ liệu trùng trong cùng môn: "
        + "; ".join(duplicate_details)
    )


class Migration(migrations.Migration):

    dependencies = [
        ("qna", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            validate_question_bank_name_duplicates,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="questionbank",
            constraint=models.UniqueConstraint(
                fields=("subject_id", "name"),
                name="uq_question_bank_subject_name",
            ),
        ),
    ]

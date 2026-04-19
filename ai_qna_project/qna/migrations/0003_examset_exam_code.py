from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("qna", "0002_question_bank_unique_subject_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="examset",
            name="exam_code",
            field=models.CharField(
                blank=True,
                null=True,
                max_length=50,
                verbose_name="Mã bộ đề",
            ),
        ),
    ]

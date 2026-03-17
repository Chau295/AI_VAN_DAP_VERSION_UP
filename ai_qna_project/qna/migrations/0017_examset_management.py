from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('qna', '0016_questionbank_lecturematerial_bank_question_bank'),
    ]

    operations = [
        migrations.AddField(
            model_name='question',
            name='is_exam_clone',
            field=models.BooleanField(default=False, verbose_name='Bản sao dùng cho mã đề'),
        ),
        migrations.CreateModel(
            name='ExamSet',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(blank=True, default='', max_length=255, verbose_name='Tên bộ đề')),
                ('academic_year', models.CharField(max_length=20, verbose_name='Năm học')),
                ('semester', models.CharField(choices=[('HK1', 'HK1'), ('HK2', 'HK2'), ('SUMMER', 'HK hè')], default='HK1', max_length=10, verbose_name='Học kỳ')),
                ('number_of_versions', models.PositiveIntegerField(default=1, verbose_name='Số mã đề')),
                ('easy_pool_size', models.PositiveIntegerField(default=1, verbose_name='Số câu dễ trong ma trận')),
                ('medium_pool_size', models.PositiveIntegerField(default=1, verbose_name='Số câu trung bình trong ma trận')),
                ('hard_pool_size', models.PositiveIntegerField(default=1, verbose_name='Số câu khó trong ma trận')),
                ('easy_score', models.DecimalField(decimal_places=1, default=2.0, max_digits=5, verbose_name='Điểm câu dễ')),
                ('medium_score', models.DecimalField(decimal_places=1, default=2.5, max_digits=5, verbose_name='Điểm câu trung bình')),
                ('hard_score', models.DecimalField(decimal_places=1, default=3.0, max_digits=5, verbose_name='Điểm câu khó')),
                ('shuffle_question_order', models.BooleanField(default=True, verbose_name='Tự động xáo trộn thứ tự câu hỏi')),
                ('allow_duplicate_questions', models.BooleanField(default=False, verbose_name='Cho phép câu hỏi trùng lặp giữa các mã đề')),
                ('status', models.CharField(choices=[('DRAFT', 'Chưa duyệt'), ('APPROVED', 'Đã duyệt')], default='DRAFT', max_length=20, verbose_name='Trạng thái')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Ngày tạo')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Ngày cập nhật')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_exam_sets', to=settings.AUTH_USER_MODEL, verbose_name='Người tạo')),
                ('subject', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='exam_sets', to='qna.subject', verbose_name='Môn học')),
                ('question_banks', models.ManyToManyField(blank=True, related_name='exam_sets', to='qna.questionbank', verbose_name='Nguồn câu hỏi')),
            ],
            options={
                'verbose_name': 'Bộ đề thi',
                'verbose_name_plural': 'Bộ đề thi',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddField(
            model_name='examcode',
            name='code_number',
            field=models.PositiveIntegerField(default=0, verbose_name='Số thứ tự mã đề'),
        ),
        migrations.AddField(
            model_name='examcode',
            name='exam_set',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='exam_codes', to='qna.examset', verbose_name='Bộ đề thi'),
        ),
        migrations.AddField(
            model_name='examcode',
            name='question_order',
            field=models.CharField(default='EASY,MEDIUM,HARD', max_length=32, verbose_name='Thứ tự hiển thị câu hỏi'),
        ),
    ]
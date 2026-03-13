# qna/management/commands/populate_db.py
import json
import os
from django.core.management.base import BaseCommand
from django.conf import settings
from qna.models import Subject, Question

class Command(BaseCommand):
    help = 'Tự động thêm dữ liệu môn học và câu hỏi từ tệp JSON vào cơ sở dữ liệu.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Bắt đầu quá trình thêm dữ liệu...'))

        # --- TẠO MÔN HỌC ---
        subject_name = 'Phân tích và Khai phá dữ liệu'
        subject_code = 'DS401'
        quiz_file_name = 'data_analysis_quiz.json'

        # Sử dụng get_or_create để tránh tạo trùng lặp
        subject, created = Subject.objects.get_or_create(
            subject_code=subject_code,
            defaults={
                'name': subject_name,
                'quiz_data_file': quiz_file_name
            }
        )

        if created:
            self.stdout.write(self.style.SUCCESS(f'Đã tạo môn học mới: "{subject_name}"'))
        else:
            self.stdout.write(self.style.WARNING(f'Môn học "{subject_name}" đã tồn tại.'))

        # --- THÊM CÂU HỎI TỪ TỆP JSON ---
        json_file_path = os.path.join(settings.BASE_DIR, 'quiz_data', quiz_file_name)

        try:
            with open(json_file_path, 'r', encoding='utf-8') as f:
                questions_data = json.load(f)
        except FileNotFoundError:
            self.stderr.write(self.style.ERROR(f'Lỗi: Không tìm thấy tệp {json_file_path}'))
            return
        except json.JSONDecodeError:
            self.stderr.write(self.style.ERROR(f'Lỗi: Tệp {json_file_path} không phải là một file JSON hợp lệ.'))
            return

        questions_created_count = 0
        questions_updated_count = 0

        for question_data in questions_data:
            # Sử dụng update_or_create để vừa có thể thêm mới, vừa có thể cập nhật nếu chạy lại lệnh
            question, created = Question.objects.update_or_create(
                question_id_in_barem=question_data['id'],
                subject=subject,
                defaults={
                    'question_text': question_data['question']
                }
            )
            if created:
                questions_created_count += 1
            else:
                questions_updated_count += 1

        self.stdout.write(self.style.SUCCESS(f'Đã tạo mới {questions_created_count} câu hỏi.'))
        self.stdout.write(self.style.WARNING(f'Đã cập nhật {questions_updated_count} câu hỏi đã có.'))
        self.stdout.write(self.style.SUCCESS('Hoàn tất quá trình thêm dữ liệu!'))
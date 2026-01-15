# -*- coding: utf-8 -*-
"""
Module này chứa tất cả các view và logic xử lý request cho ứng dụng Q&A.

Bao gồm các chức năng chính:
- Đăng ký, đăng nhập, quản lý hồ sơ người dùng.
- Hiển thị dashboard, lịch sử thi.
- Luồng thực hiện một phiên thi (câu hỏi chính và phụ).
- Các API endpoints để lưu kết quả, lấy câu hỏi, và cập nhật avatar.
"""

from __future__ import annotations

import json
import random
import base64
import io
import logging
from base64 import b64encode
from typing import Optional, List, Dict, Any
from uuid import uuid4

logger = logging.getLogger(__name__)

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Sum, Avg, Count
from django.http import (
    JsonResponse,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
)
from django.shortcuts import get_object_or_404, render, redirect
from django.templatetags.static import static
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST, require_GET

from .models import (
    Subject,
    Question,
    ExamSession,
    ExamResult,
    SupplementaryResult,
    UserProfile,
    ExamCode,
    LectureMaterial,
    ExamRoom,
    ExamSessionGroup,
    ExamSessionRoom,
    DifficultyLevel,
)

User = get_user_model()

# ===========================
# GIẢNG VIÊN - NEW INDEPENDENT SCREENS
# ===========================

@login_required
def post_login_redirect(request):
    """
    Kiểm tra vai trò và chuyển hướng người dùng sau khi đăng nhập.
    """
    # Kiểm tra xem user đã có profile chưa, nếu chưa thì tạo
    if not hasattr(request.user, 'userprofile'):
        UserProfile.objects.create(user=request.user)

    if request.user.userprofile.is_lecturer:
        return redirect('qna:lecturer_dashboard')
    else:
        return redirect('qna:dashboard') # Dashboard của sinh viên

# ===========================
# 1. DASHBOARD - System Overview
# ===========================

@login_required
def lecturer_dashboard(request):
    """
    Dashboard hệ thống - Tổng quan toàn hệ thống
    Cho phép lọc theo môn học (không bắt buộc)
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')

    subjects = request.user.userprofile.subjects_taught.all()
    selected_subject_id = request.GET.get('subject_id')
    
    # Thống kê toàn hệ thống
    total_subjects = subjects.count()
    total_sessions = ExamSession.objects.filter(
        subject__in=subjects
    ).count()
    
    # Lọc theo môn học nếu được chọn
    if selected_subject_id:
        try:
            selected_subject = subjects.get(id=selected_subject_id)
            sessions = ExamSession.objects.filter(
                subject=selected_subject
            ).select_related('subject', 'user', 'user__userprofile').order_by('-created_at')[:20]
        except (Subject.DoesNotExist, ValueError):
            selected_subject = None
            sessions = []
    else:
        selected_subject = None
        sessions = ExamSession.objects.filter(
            subject__in=subjects
        ).select_related('subject', 'user', 'user__userprofile').order_by('-created_at')[:20]

    context = {
        'subjects': subjects,
        'selected_subject': selected_subject,
        'total_subjects': total_subjects,
        'total_sessions': total_sessions,
        'recent_sessions': sessions,
    }
    return render(request, 'qna/lecturer/lecturer_dashboard.html', context)


# ===========================
# 2. MÔN HỌC - Subject Management
# ===========================

@login_required
def lecturer_subject_list(request):
    """
    Danh sách môn học - Hiển thị tất cả môn giảng viên phụ trách
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')

    subjects = request.user.userprofile.subjects_taught.all().annotate(
        exam_count=Count('examsession', distinct=True)
    ).order_by('name')

    context = {
        'subjects': subjects,
    }
    return render(request, 'qna/lecturer/lecturer_subject_list.html', context)


@login_required
def lecturer_subject_workspace(request, subject_code):
    """
    Quản lý môn học - Quick workspace cho một môn
    Cho phép thao tác nhanh: tạo đề thi, tạo ca thi, xem bài làm
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')

    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        subject_code=subject_code
    )

    # Lấy thống kê cho môn học
    sessions_in_subject = ExamSession.objects.filter(subject=subject)
    total_exams = sessions_in_subject.count()
    completed_exams = sessions_in_subject.filter(is_completed=True).count()
    
    # Lấy các ca thi sắp tới
    upcoming_groups = ExamSessionGroup.objects.filter(
        subject=subject,
        exam_date__gte=timezone.now()
    ).order_by('exam_date')[:5]
    
    # Lấy các mã đề đã duyệt
    approved_codes = ExamCode.objects.filter(
        subject=subject,
        is_approved=True
    ).count()

    context = {
        'subject': subject,
        'total_exams': total_exams,
        'completed_exams': completed_exams,
        'upcoming_groups': upcoming_groups,
        'approved_codes': approved_codes,
    }
    return render(request, 'qna/lecturer/lecturer_subject_workspace.html', context)


# ===========================
# 3. CÂU HỎI & ĐỀ THI - Question & Exam Code Management
# ===========================

@login_required
def lecturer_questions_screen(request):
    """
    Quản lý câu hỏi - Màn hình độc lập với dropdown chọn môn học
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')

    subjects = request.user.userprofile.subjects_taught.all()
    selected_subject_id = request.GET.get('subject_id')
    
    # Lấy môn học mặc định (lần truy cập gần nhất) hoặc môn đầu tiên
    if not selected_subject_id:
        selected_subject = subjects.first()
    else:
        try:
            selected_subject = subjects.get(id=selected_subject_id)
        except (Subject.DoesNotExist, ValueError):
            selected_subject = subjects.first()
    
    # Bộ lọc
    difficulty = request.GET.get('difficulty')
    topic = request.GET.get('topic', '').strip()
    
    # Lấy danh sách câu hỏi
    questions = Question.objects.filter(subject=selected_subject).order_by('question_id_in_barem')
    
    if difficulty:
        questions = questions.filter(difficulty=difficulty)
    if topic:
        questions = questions.filter(question_text__icontains=topic)
    
    # Lấy danh sách mã đề thi đã duyệt
    exam_codes = ExamCode.objects.filter(
        subject=selected_subject,
        is_approved=True
    ).select_related(
        'question_easy', 'question_medium', 'question_hard'
    ).order_by('-created_at')

    context = {
        'subjects': subjects,
        'selected_subject': selected_subject,
        'questions': questions,
        'exam_codes': exam_codes,
        'selected_difficulty': difficulty,
        'selected_topic': topic,
        'difficulty_choices': DifficultyLevel.choices,
    }
    return render(request, 'qna/lecturer/lecturer_question_management.html', context)


@login_required
@require_POST
def lecturer_create_question(request):
    """
    API để tạo câu hỏi mới
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    subject_id = request.POST.get('subject_id')
    question_text = request.POST.get('question_text', '').strip()
    difficulty = request.POST.get('difficulty')
    
    if not all([subject_id, question_text, difficulty]):
        return JsonResponse({'success': False, 'error': 'Thiếu thông tin bắt buộc'}, status=400)
    
    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        id=subject_id
    )
    
    question = Question.objects.create(
        subject=subject,
        question_text=question_text,
        difficulty=difficulty,
        question_id_in_barem=f'Q_{subject.subject_code}_{uuid4().hex[:8]}',
        is_supplementary=False
    )
    
    messages.success(request, 'Đã tạo câu hỏi thành công.')
    return JsonResponse({'success': True, 'question_id': question.id})


@login_required
@require_POST
def lecturer_update_question(request, question_id):
    """
    API để cập nhật câu hỏi
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    question = get_object_or_404(Question, pk=question_id)
    
    # Kiểm tra quyền truy cập môn học
    if question.subject not in request.user.userprofile.subjects_taught.all():
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập môn học này'}, status=403)
    
    question_text = request.POST.get('question_text', '').strip()
    difficulty = request.POST.get('difficulty')
    
    if question_text:
        question.question_text = question_text
    if difficulty:
        question.difficulty = difficulty
    
    question.save()
    
    messages.success(request, 'Đã cập nhật câu hỏi thành công.')
    return JsonResponse({'success': True})


@login_required
@require_POST
def lecturer_delete_question(request, question_id):
    """
    API để xoá câu hỏi
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    question = get_object_or_404(Question, pk=question_id)
    
    # Kiểm tra quyền truy cập môn học
    if question.subject not in request.user.userprofile.subjects_taught.all():
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập môn học này'}, status=403)
    
    question.delete()
    
    messages.success(request, 'Đã xoá câu hỏi thành công.')
    return JsonResponse({'success': True})


@login_required
@require_POST
def lecturer_import_questions(request):
    """
    API để import câu hỏi từ file
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    subject_id = request.POST.get('subject_id')
    file_obj = request.FILES.get('file')
    
    if not subject_id or not file_obj:
        return JsonResponse({'success': False, 'error': 'Thiếu thông tin'}, status=400)
    
    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        id=subject_id
    )
    
    try:
        import csv
        decoded_file = file_obj.read().decode('utf-8')
        io_string = io.StringIO(decoded_file)
        reader = csv.DictReader(io_string)
        
        imported_count = 0
        for row in reader:
            question_text = row.get('question', '').strip()
            difficulty = row.get('difficulty', 'MEDIUM').strip()
            
            if question_text and difficulty in ['EASY', 'MEDIUM', 'HARD']:
                Question.objects.create(
                    subject=subject,
                    question_text=question_text,
                    difficulty=difficulty,
                    question_id_in_barem=f'IMP_{subject.subject_code}_{uuid4().hex[:8]}',
                    is_supplementary=False
                )
                imported_count += 1
        
        messages.success(request, f'Đã import {imported_count} câu hỏi thành công.')
        return JsonResponse({'success': True, 'imported_count': imported_count})
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


# ===========================
# 4. SINH MÃ ĐỀ BẰNG AI - AI Exam Code Generation
# ===========================

@login_required
def lecturer_generate_codes_screen(request):
    """
    Sinh mã đề bằng AI - Màn hình độc lập với dropdown chọn môn học
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')

    subjects = request.user.userprofile.subjects_taught.all()
    selected_subject_id = request.GET.get('subject_id')
    
    # Lấy môn học mặc định
    if not selected_subject_id:
        selected_subject = subjects.first()
    else:
        try:
            selected_subject = subjects.get(id=selected_subject_id)
        except (Subject.DoesNotExist, ValueError):
            selected_subject = subjects.first()
    
    # Lấy danh sách tài liệu bài giảng
    materials = LectureMaterial.objects.filter(
        subject=selected_subject
    ).order_by('-uploaded_at')
    
    # Lấy danh sách mã đề thi chưa duyệt
    pending_exam_codes = ExamCode.objects.filter(
        subject=selected_subject,
        is_approved=False
    ).select_related(
        'question_easy', 'question_medium', 'question_hard'
    ).order_by('-created_at')
    
    # Lấy danh sách mã đề đã duyệt
    approved_exam_codes = ExamCode.objects.filter(
        subject=selected_subject,
        is_approved=True
    ).select_related(
        'question_easy', 'question_medium', 'question_hard'
    ).order_by('-created_at')

    context = {
        'subjects': subjects,
        'selected_subject': selected_subject,
        'materials': materials,
        'pending_exam_codes': pending_exam_codes,
        'approved_exam_codes': approved_exam_codes,
    }
    return render(request, 'qna/lecturer/lecturer_generate_exam_codes.html', context)


@login_required
@require_POST
def lecturer_upload_material_screen(request):
    """
    API để upload tài liệu bài giảng (đã sửa để không cần subject_code)
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    subject_id = request.POST.get('subject_id')
    title = request.POST.get('title')
    file_obj = request.FILES.get('file')
    
    if not subject_id or not title or not file_obj:
        return JsonResponse({'success': False, 'error': 'Thiếu thông tin'}, status=400)
    
    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        id=subject_id
    )
    
    import os
    from django.conf import settings
    
    upload_dir = os.path.join(settings.MEDIA_ROOT, 'lecture_materials', subject.subject_code)
    os.makedirs(upload_dir, exist_ok=True)
    
    file_path = os.path.join(upload_dir, file_obj.name)
    with open(file_path, 'wb+') as destination:
        for chunk in file_obj.chunks():
            destination.write(chunk)
    
    material = LectureMaterial.objects.create(
        subject=subject,
        title=title,
        file_path=file_path,
        file_type=file_obj.content_type
    )
    
    messages.success(request, 'Đã upload tài liệu thành công.')
    return JsonResponse({'success': True, 'material_id': material.id})


@login_required
@require_POST
def lecturer_generate_codes_with_ai(request):
    """
    API để sinh mã đề thi bằng AI (đã sửa để không cần subject_code)
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    subject_id = request.POST.get('subject_id')
    material_id = request.POST.get('material_id')
    num_codes = int(request.POST.get('num_codes', 4))
    
    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        id=subject_id
    )
    
    if num_codes < 1 or num_codes > 16:
        return JsonResponse({'success': False, 'error': 'Số lượng mã đề phải từ 1 đến 16'}, status=400)
    
    material = get_object_or_404(LectureMaterial, pk=material_id, subject=subject)
    
    try:
        import openai
        client = openai.OpenAI()
        
        with open(material.file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        system_prompt = """Bạn là chuyên gia giáo dục, chuyên tạo câu hỏi thi vấn đáp.
        
        QUY TẮC PHÂN LOẠI ĐỘ KHÓ:
        
        CÂU DỄ:
        - Định nghĩa, khái niệm
        - Nhận biết, mô tả, kể tên
        
        CÂU TRUNG BÌNH:
        - Công thức
        - Nguyên lý hoạt động
        - Giải thích cách vận hành
        
        CÂU KHÓ:
        - Phân tích, so sánh
        - Ứng dụng thực tế
        - Tổng hợp nhiều kiến thức
        
        ĐẦU RA: JSON format với 3 câu hỏi:
        {
            "easy": "câu hỏi dễ",
            "medium": "câu hỏi trung bình",
            "hard": "câu hỏi khó"
        }
        KHÔNG thêm văn bản nào khác ngoài JSON."""
        
        user_prompt = f"""Tạo {num_codes} bộ câu hỏi (mỗi bộ 3 câu hỏi) cho môn học: {subject.name}
        
        NỘI DUNG TÀI LIỆU:
        {content[:5000]}
        
        YÊU CẦU:
        - Tạo {num_codes} bộ câu hỏi khác nhau
        - Mỗi bộ có đúng 3 câu hỏi theo 3 mức độ: Dễ, Trung bình, Khó
        - Câu hỏi phải dựa trên nội dung tài liệu
        - Trả về danh sách JSON: [{{"easy": "...", "medium": "...", "hard": "..."}}, ...]
        """
        
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.7,
            response_format={"type": "json_object"}
        )
        
        questions_list = json.loads(resp.choices[0].message.content)
        
        created_codes = []
        for i, questions in enumerate(questions_list):
            q_easy = Question.objects.create(
                subject=subject,
                question_text=questions['easy'],
                question_id_in_barem=f'AI_EASY_{i}_{uuid4().hex[:8]}',
                difficulty='EASY',
                is_supplementary=False
            )
            
            q_medium = Question.objects.create(
                subject=subject,
                question_text=questions['medium'],
                question_id_in_barem=f'AI_MEDIUM_{i}_{uuid4().hex[:8]}',
                difficulty='MEDIUM',
                is_supplementary=False
            )
            
            q_hard = Question.objects.create(
                subject=subject,
                question_text=questions['hard'],
                question_id_in_barem=f'AI_HARD_{i}_{uuid4().hex[:8]}',
                difficulty='HARD',
                is_supplementary=False
            )
            
            exam_code = ExamCode.objects.create(
                subject=subject,
                code_name=f'Mã đề AI-{i+1}',
                question_easy=q_easy,
                question_medium=q_medium,
                question_hard=q_hard,
                source_material=material.title,
                is_approved=False
            )
            created_codes.append(exam_code.id)
        
        messages.success(request, f'Đã sinh thành công {len(created_codes)} mã đề thi.')
        return JsonResponse({
            'success': True,
            'created_count': len(created_codes),
            'exam_code_ids': created_codes
        })
        
    except Exception as e:
        logger.error(f"Lỗi khi sinh mã đề thi bằng AI: {e}")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


# ===========================
# 5. TẠO CA THI - Create Exam Session
# ===========================

@login_required
def lecturer_create_session_screen(request):
    """
    Tạo ca thi - Màn hình độc lập với dropdown chọn môn học
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')

    subjects = request.user.userprofile.subjects_taught.all()
    selected_subject_id = request.GET.get('subject_id')
    
    if not selected_subject_id:
        selected_subject = subjects.first()
    else:
        try:
            selected_subject = subjects.get(id=selected_subject_id)
        except (Subject.DoesNotExist, ValueError):
            selected_subject = subjects.first()
    
    # Lấy danh sách mã đề thi đã duyệt
    approved_exam_codes = ExamCode.objects.filter(
        subject=selected_subject,
        is_approved=True
    ).select_related(
        'question_easy', 'question_medium', 'question_hard'
    )
    
    # Lấy danh sách phòng thi
    all_rooms = ExamRoom.objects.all()
    
    # Lấy danh sách ca thi đã tạo cho môn học này
    exam_groups = ExamSessionGroup.objects.filter(
        subject=selected_subject
    ).prefetch_related('rooms', 'exam_codes').order_by('-exam_date')

    context = {
        'subjects': subjects,
        'selected_subject': selected_subject,
        'approved_exam_codes': approved_exam_codes,
        'all_rooms': all_rooms,
        'exam_groups': exam_groups,
    }
    return render(request, 'qna/lecturer/lecturer_create_exam_session.html', context)


@login_required
@require_POST
def lecturer_create_exam_group_screen(request):
    """
    API để tạo ca thi mới (đã sửa để không cần subject_code)
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    subject_id = request.POST.get('subject_id')
    group_name = request.POST.get('group_name')
    exam_date_str = request.POST.get('exam_date')
    duration_minutes = int(request.POST.get('duration_minutes', 60))
    exam_password = request.POST.get('exam_password', '').strip()
    exam_code_ids = request.POST.getlist('exam_code_ids')
    room_ids = request.POST.getlist('room_ids')
    
    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        id=subject_id
    )
    
    if not group_name or not exam_date_str:
        return JsonResponse({'success': False, 'error': 'Thiếu thông tin bắt buộc'}, status=400)
    
    from datetime import datetime
    exam_date = datetime.strptime(exam_date_str, '%Y-%m-%dT%H:%M')
    
    exam_group = ExamSessionGroup.objects.create(
        subject=subject,
        group_name=group_name,
        exam_date=exam_date,
        duration_minutes=duration_minutes,
        exam_password=exam_password if exam_password else None,
        status='SCHEDULED',
        created_by=request.user
    )
    
    if exam_code_ids:
        exam_codes = ExamCode.objects.filter(id__in=exam_code_ids, subject=subject)
        exam_group.exam_codes.set(exam_codes)
    
    if room_ids:
        for room_id in room_ids:
            room = ExamRoom.objects.get(pk=room_id)
            ExamSessionRoom.objects.create(
                exam_group=exam_group,
                room=room
            )
    
    messages.success(request, 'Đã tạo ca thi thành công.')
    return JsonResponse({'success': True, 'exam_group_id': exam_group.id})


# ===========================
# 6. DANH SÁCH CA THI - Exam Session List
# ===========================

@login_required
def lecturer_exam_sessions_list(request):
    """
    Danh sách ca thi - Xem tất cả các ca thi với bộ lọc
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')

    subjects = request.user.userprofile.subjects_taught.all()
    selected_subject_id = request.GET.get('subject_id')
    status_filter = request.GET.get('status')
    
    if not selected_subject_id:
        selected_subject = subjects.first()
    else:
        try:
            selected_subject = subjects.get(id=selected_subject_id)
        except (Subject.DoesNotExist, ValueError):
            selected_subject = subjects.first()
    
    # Lọc ca thi
    exam_groups = ExamSessionGroup.objects.filter(
        subject=selected_subject
    ).prefetch_related('rooms', 'exam_codes').order_by('-exam_date')
    
    if status_filter:
        exam_groups = exam_groups.filter(status=status_filter)

    context = {
        'subjects': subjects,
        'selected_subject': selected_subject,
        'exam_groups': exam_groups,
        'selected_status': status_filter,
        'status_choices': ExamSessionGroup.STATUS_CHOICES,
    }
    return render(request, 'qna/lecturer/lecturer_exam_sessions_list.html', context)


# ===========================
# 7. XEM BÀI LÀM SINH VIÊN - Student Review
# ===========================

@login_required
def lecturer_student_review_screen(request):
    """
    Xem bài làm sinh viên - Màn hình độc lập với bộ lọc
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')

    subjects = request.user.userprofile.subjects_taught.all()
    selected_subject_id = request.GET.get('subject_id')
    exam_group_id = request.GET.get('exam_group_id')
    student_filter = request.GET.get('student', '').strip()
    
    if not selected_subject_id:
        selected_subject = subjects.first()
    else:
        try:
            selected_subject = subjects.get(id=selected_subject_id)
        except (Subject.DoesNotExist, ValueError):
            selected_subject = subjects.first()
    
    # Lấy danh sách ca thi của môn học
    exam_groups = ExamSessionGroup.objects.filter(
        subject=selected_subject
    ).order_by('-exam_date')
    
    # Lọc phiên thi
    sessions = ExamSession.objects.filter(
        subject=selected_subject
    ).select_related('user', 'user__userprofile')
    
    if exam_group_id:
        sessions = sessions.filter(exam_group_id=exam_group_id)
    
    if student_filter:
        sessions = sessions.filter(
            user__username__icontains=student_filter
        )
    
    sessions = sessions.order_by('-created_at')

    # Tính điểm cho từng session
    for session in sessions:
        main_avg, supp_sum, final_total = _compute_scores(session)
        session.main_avg = main_avg
        session.supp_sum = supp_sum
        session.calculated_final_score = final_total

    context = {
        'subjects': subjects,
        'selected_subject': selected_subject,
        'exam_groups': exam_groups,
        'selected_exam_group_id': int(exam_group_id) if exam_group_id else None,
        'sessions': sessions,
        'student_filter': student_filter,
    }
    return render(request, 'qna/lecturer/lecturer_student_review.html', context)


# ===========================
# 8. XUẤT BÁO CÁO - Export Reports
# ===========================

@login_required
def lecturer_export_reports_screen(request):
    """
    Xuất báo cáo - Màn hình độc lập với dropdown chọn môn học và ca thi
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')

    subjects = request.user.userprofile.subjects_taught.all()
    selected_subject_id = request.GET.get('subject_id')
    exam_group_id = request.GET.get('exam_group_id')
    
    if not selected_subject_id:
        selected_subject = subjects.first()
    else:
        try:
            selected_subject = subjects.get(id=selected_subject_id)
        except (Subject.DoesNotExist, ValueError):
            selected_subject = subjects.first()
    
    # Lấy danh sách ca thi của môn học
    exam_groups = ExamSessionGroup.objects.filter(
        subject=selected_subject
    ).order_by('-exam_date')

    context = {
        'subjects': subjects,
        'selected_subject': selected_subject,
        'exam_groups': exam_groups,
        'selected_exam_group_id': int(exam_group_id) if exam_group_id else None,
    }
    # Since the export reports screen doesn't have a dedicated template yet,
    # we'll redirect to the student review screen which has similar functionality
    return redirect('qna:lecturer_student_review_screen')


@login_required
def lecturer_export_exam_results_screen(request):
    """
    Xuất báo cáo kết quả thi ra file Excel (đã sửa để không cần subject_code)
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')
    
    subject_id = request.GET.get('subject_id')
    exam_group_id = request.GET.get('exam_group_id')
    
    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        id=subject_id
    )
    
    if exam_group_id:
        exam_group = get_object_or_404(ExamSessionGroup, pk=exam_group_id, subject=subject)
        sessions = ExamSession.objects.filter(
            subject=subject,
            exam_group=exam_group
        ).select_related('user', 'user__userprofile')
    else:
        sessions = ExamSession.objects.filter(
            subject=subject
        ).select_related('user', 'user__userprofile')
    
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
    from datetime import datetime
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Kết quả thi"
    
    headers = ['STT', 'Mã SV', 'Họ tên', 'Lớp', 'Ngày thi', 'Điểm chính', 'Điểm phụ', 'Điểm tổng']
    ws.append(headers)
    
    header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')
    
    for idx, session in enumerate(sessions, 1):
        main_avg, supp_sum, final_total = _compute_scores(session)
        
        profile = session.user.userprofile
        ws.append([
            idx,
            session.user.username,
            profile.full_name,
            profile.class_name,
            session.created_at.strftime('%d/%m/%Y %H:%M'),
            f"{main_avg:.2f}",
            f"{supp_sum:.2f}",
            f"{final_total:.2f}"
        ])
    
    for column in ws.columns:
        max_length = 0
        column_letter = column[0].column_letter
        for cell in column:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = min(max_length + 2, 50)
        ws.column_dimensions[column_letter].width = adjusted_width
    
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    filename = f'Ket_qua_thi_{subject.subject_code}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    wb.save(response)
    return response


@login_required
def lecturer_subject_dashboard(request, subject_code):
    """
    Hiển thị dashboard chi tiết với đầy đủ chức năng và số liệu thật
    cho một môn học cụ thể.
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')

    # Lấy thông tin môn học và đảm bảo giảng viên có quyền truy cập
    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        subject_code=subject_code
    )

    # Lấy tất cả các phiên thi liên quan đến môn học này
    sessions_in_subject = ExamSession.objects.filter(subject=subject)

    # 1. TÍNH TOÁN CÁC SỐ LIỆU THỐNG KÊ
    # Đếm tổng số kỳ thi (phiên thi) đã được tạo
    total_exams = sessions_in_subject.count()

    # Đếm số lượng sinh viên duy nhất đã tham gia thi môn này
    total_students = sessions_in_subject.values('user').distinct().count()

    # Đếm số lượt thi đã hoàn thành
    completed_exams = sessions_in_subject.filter(is_completed=True).count()

    # Lấy 10 kỳ thi gần nhất để hiển thị trong bảng
    recent_exams = sessions_in_subject.select_related('user', 'user__userprofile').order_by('-created_at')[:10]

    # (Tương lai) Đếm số yêu cầu phúc khảo
    # appeal_requests = Appeal.objects.filter(exam_result__session__subject=subject).count()

    context = {
        'subject': subject,
        'total_exams': total_exams,
        'total_students': total_students,
        'completed_exams': completed_exams,
        'recent_exams': recent_exams,
        # 'appeal_requests': appeal_requests, # Sẽ dùng trong tương lai
    }

    return render(request, 'qna/lecturer/lecturer_subject_dashboard.html', context)


@login_required
def update_exam_password(request, subject_code):
    """
    Cập nhật mật khẩu bài thi cho một môn học.
    Chỉ giảng viên được phân công môn học mới có quyền thay đổi.
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')

    if request.method == 'POST':
        subject = get_object_or_404(
            request.user.userprofile.subjects_taught,
            subject_code=subject_code
        )
        
        password = request.POST.get('exam_password', '').strip()
        
        # Nếu password rỗng, xóa mật khẩu (không cần password)
        if not password:
            subject.exam_password = None
        else:
            subject.exam_password = password
        
        subject.save()
        
        messages.success(request, 'Đã cập nhật mật khẩu bài thi thành công.')
    
    return redirect('qna:lecturer_subject_dashboard', subject_code=subject_code)


@login_required
def exam_password_view(request, subject_code):
    """
    Hiển thị trang nhập mật khẩu bài thi.
    Được gọi khi môn học có đặt mật khẩu.
    """
    subject = get_object_or_404(Subject, subject_code=subject_code)
    
    # Nếu môn học không có mật khẩu, chuyển thẳng đến trang xác thực khuôn mặt
    if not subject.exam_password:
        return redirect('qna:pre_exam_verification', subject_code=subject_code)
    
    return render(request, 'qna/student/exam_password.html', {
        'subject': subject,
        'subject_code': subject_code
    })


@login_required
def verify_exam_password(request, subject_code):
    """
    Xác thực mật khẩu bài thi.
    Nếu đúng, chuyển đến trang xác thực khuôn mặt.
    """
    if request.method == 'POST':
        subject = get_object_or_404(Subject, subject_code=subject_code)
        password = request.POST.get('password', '').strip()
        
        # Nếu môn học không có mật khẩu, cho phép vào
        if not subject.exam_password:
            return redirect('qna:pre_exam_verification', subject_code=subject_code)
        
        # Kiểm tra mật khẩu
        if password == subject.exam_password:
            return redirect('qna:pre_exam_verification', subject_code=subject_code)
        else:
            messages.error(request, 'Mật khẩu không đúng. Vui lòng thử lại.')
            return redirect('qna:exam_password', subject_code=subject_code)
    
    # Nếu không phải POST, chuyển về trang nhập mật khẩu
    return redirect('qna:exam_password', subject_code=subject_code)

# ===========================
# CÁC QUY TẮC VÀ HẰNG SỐ
# ===========================
SUPP_MAX_PER_QUESTION = 1.0  # Điểm tối đa cho mỗi câu hỏi phụ
SUPP_MAX_COUNT = 2  # Số lượng câu hỏi phụ tối đa được tính điểm
FINAL_CAP = 7.0  # Điểm cuối cùng tối đa nếu có trả lời câu hỏi phụ


# ===========================
# CÁC HÀM HỖ TRỢ (HELPERS)
# ===========================

def _json_body(request: HttpRequest) -> Dict[str, Any]:
    """Tải nội dung JSON từ body của request một cách an toàn."""
    try:
        if request.body:
            return json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return {}


def _ensure_owner(session: ExamSession, user: User) -> None:
    """Kiểm tra người dùng hiện tại có phải là chủ sở hữu của phiên thi không."""
    if session.user_id != getattr(user, "id", None):
        raise PermissionDenied("Bạn không có quyền truy cập phiên thi này.")


def _compute_scores(session: ExamSession) -> tuple[float, float, float]:
    """
    Tính toán và trả về bộ 3 điểm của một phiên thi.
    Xử lý phòng thủ với dữ liệu điểm không nhất quán từ DB.
    """
    # 1. Tính điểm trung bình phần chính (Logic không đổi)
    total_main_questions = session.questions.count()
    if total_main_questions == 0:
        total_main_questions = 3

    main_results = ExamResult.objects.filter(session=session)
    sum_of_scores = main_results.aggregate(total=Sum('score'))['total'] or 0.0
    main_avg = sum_of_scores / total_main_questions

    # 2. Tính tổng điểm câu hỏi phụ (lấy 2 câu cao nhất sau khi khử lặp và làm sạch dữ liệu)
    supp_results_qs = SupplementaryResult.objects.filter(session=session)
    best_scores_by_text = {}
    for result in supp_results_qs:
        key = (result.question_text or "").strip()
        if not key:
            continue

        score = float(result.score or 0.0)

        # ▼▼▼ LOGIC LÀM SẠCH DỮ LIỆU ĐƯỢC THÊM VÀO ▼▼▼
        # Nếu điểm > 1.0, giả định đó là thang 10 và quy đổi về thang 1
        if score > SUPP_MAX_PER_QUESTION:
            score /= 10.0
        # ▲▲▲ KẾT THÚC LOGIC LÀM SẠCH ▲▲▲

        current_score = max(0.0, min(score, SUPP_MAX_PER_QUESTION))

        if key not in best_scores_by_text or current_score > best_scores_by_text[key]:
            best_scores_by_text[key] = current_score

    unique_supp_scores = list(best_scores_by_text.values())
    unique_supp_scores.sort(reverse=True)

    supp_sum = sum(unique_supp_scores[:SUPP_MAX_COUNT])

    # 3. Tính điểm cuối cùng (Logic không đổi)
    if supp_sum > 0:
        final_total = min(FINAL_CAP, main_avg + supp_sum)
    else:
        final_total = min(10.0, main_avg)

    return main_avg, supp_sum, final_total


# qna/views.py

def _dedupe_supp_for_display(qs: SupplementaryResult) -> List[SupplementaryResult]:
    """
    Khử lặp các câu hỏi phụ và làm sạch điểm để hiển thị.
    """
    best_by_text = {}
    for result in qs:
        key = (result.question_text or "").strip()
        if not key:
            continue

        # ▼▼▼ LOGIC LÀM SẠCH DỮ LIỆU ĐƯỢC THÊM VÀO ▼▼▼
        # Tạo một bản sao để không thay đổi đối tượng gốc trong queryset
        cleaned_result = result
        score = float(cleaned_result.score or 0.0)

        # Nếu điểm > 1.0, giả định đó là thang 10 và quy đổi về thang 1
        if score > SUPP_MAX_PER_QUESTION:
            cleaned_result.score = score / 10.0
        # ▲▲▲ KẾT THÚC LOGIC LÀM SẠCH ▲▲▲

        current_best = best_by_text.get(key)
        if current_best is None or float(cleaned_result.score or 0) > float(current_best.score or 0):
            best_by_text[key] = cleaned_result

    items = list(best_by_text.values())
    items.sort(key=lambda x: float(x.score or 0), reverse=True)
    return items[:SUPP_MAX_COUNT]

# ===========================
# FORM ĐĂNG KÝ
# ===========================

class RegistrationForm(forms.Form):
    """Form xử lý việc đăng ký tài khoản mới."""
    full_name = forms.CharField(
        label=mark_safe('Họ và tên <span class="text-red-500">*</span>'),
        max_length=150,
        widget=forms.TextInput(attrs={"placeholder": "VD: Nguyễn Văn A", "autocomplete": "name"})
    )
    username = forms.CharField(
        label=mark_safe('Tên đăng nhập (Mã SV) <span class="text-red-500">*</span>'),
        max_length=150,
        widget=forms.TextInput(attrs={"placeholder": "Mã SV hoặc tên đăng nhập", "autocomplete": "username"}),
        help_text="Phải là duy nhất."
    )
    class_name = forms.CharField(
        label=mark_safe('Lớp <span class="text-red-500">*</span>'),
        max_length=100,
        widget=forms.TextInput(attrs={"placeholder": "VD: K25CNTT", "autocomplete": "organization"})
    )
    email = forms.EmailField(
        label='Email',
        required=False,
        widget=forms.EmailInput(attrs={"placeholder": "ten@sv.duytan.edu.vn", "autocomplete": "email"}),
        help_text="(Không bắt buộc)"
    )
    faculty = forms.CharField(
        label='Khoa',
        required=False,
        max_length=150,
        widget=forms.TextInput(attrs={"placeholder": "VD: Công nghệ thông tin"}),
        help_text="(Không bắt buộc)"
    )
    password = forms.CharField(
        label=mark_safe('Mật khẩu <span class="text-red-500">*</span>'),
        strip=False,
        widget=forms.PasswordInput(attrs={"placeholder": "••••••••", "autocomplete": "new-password"}),
        help_text="Tối thiểu 8 ký tự."
    )
    password2 = forms.CharField(
        label=mark_safe('Nhập lại mật khẩu <span class="text-red-500">*</span>'),
        strip=False,
        widget=forms.PasswordInput(attrs={"placeholder": "••••••••", "autocomplete": "new-password"}),
        help_text="Phải trùng với mật khẩu."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({
                "class": "appearance-none border rounded w-full py-2 px-3 text-gray-700 leading-tight focus:outline-none focus:ring-2 focus:ring-blue-400"
            })

    def clean_username(self):
        """Kiểm tra tên đăng nhập không được trống và phải là duy nhất."""
        username = self.cleaned_data.get("username", "").strip()
        if not username:
            raise ValidationError("Tên đăng nhập là bắt buộc.")
        if User.objects.filter(username=username).exists():
            raise ValidationError("Tên đăng nhập này đã tồn tại.")
        return username

    def clean(self):
        """Kiểm tra mật khẩu và các ràng buộc toàn form."""
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        password2 = cleaned_data.get("password2")

        if password and password2 and password != password2:
            self.add_error("password2", "Mật khẩu nhập lại không khớp.")

        if password:
            try:
                validate_password(password, user=User(username=cleaned_data.get("username")))
            except ValidationError as e:
                self.add_error("password", e)

        return cleaned_data


# ===========================
# CÁC VIEW HIỂN THỊ TRANG (PAGES)
# ===========================

@login_required
def dashboard_view(request: HttpRequest) -> HttpResponse:
    """Hiển thị trang dashboard chính sau khi đăng nhập."""
    subjects = Subject.objects.all().order_by("name")
    recent_sessions = (
        ExamSession.objects.filter(user=request.user)
        .select_related("subject")
        .order_by("-created_at")[:5]
    )
    profile, _ = UserProfile.objects.get_or_create(user=request.user)

    return render(request, "qna/student/dashboard.html", {
        "subjects": subjects,
        "recent_sessions": recent_sessions,
        "full_name": profile.full_name or request.user.first_name,
    })


# qna/views.py

@login_required
def history_view(request: HttpRequest) -> HttpResponse:
    """Hiển thị trang lịch sử các phiên thi của người dùng."""
    sessions = (
        ExamSession.objects.filter(user=request.user)
        .select_related("subject")
        .order_by("-created_at")
    )
    for s in sessions:
        main_avg, supp_sum, final_total = _compute_scores(s)
        s.main_avg = main_avg
        s.supp_sum = supp_sum
        # ▼▼▼ THAY ĐỔI Ở ĐÂY ▼▼▼
        # Đổi tên thuộc tính để tránh xung đột và làm rõ ý nghĩa.
        s.calculated_final_score = final_total
        # ▲▲▲ KẾT THÚC THAY ĐỔI ▲▲▲

    return render(request, "qna/student/history.html", {"sessions": sessions})

@login_required
def history_detail_view(request: HttpRequest, session_id: int) -> HttpResponse:
    """Hiển thị chi tiết kết quả của một phiên thi."""
    session = get_object_or_404(
        ExamSession.objects.select_related("subject", "user"),
        pk=session_id,
    )
    _ensure_owner(session, request.user)

    main_results = (
        ExamResult.objects.filter(session=session)
        .select_related("question")
        .order_by("question_id")
    )
    supp_results_qs = SupplementaryResult.objects.filter(session=session)
    supp_results_display = _dedupe_supp_for_display(supp_results_qs)

    main_avg, supp_sum, final_total = _compute_scores(session)

    return render(request, "qna/student/history_detail.html", {
        "session": session,
        "results": main_results,
        "supp_results": supp_results_display,
        "main_avg": main_avg,
        "supp_sum": supp_sum,
        "final_total": final_total,
    })


@login_required
def pre_exam_verification_view(request: HttpRequest, subject_code: str) -> HttpResponse:
    """Trang xác thực trước khi thi."""
    subject = get_object_or_404(Subject, subject_code=subject_code)
    
    return render(request, 'qna/student/pre_exam_verification.html', {
        'subject': subject,
        'subject_code': subject_code
    })


@login_required
def exam_view(request: HttpRequest, subject_code: str) -> HttpResponse:
    """Bắt đầu một phiên thi mới cho một môn học."""
    
    # ========================================
    # CẤU HÌNH BẢO MẬT
    # ========================================
    # Đặt True để bật kiểm tra SEB (môi trường production)
    # Đặt False để tắt kiểm tra SEB (môi trường development/testing)
    SEB_CHECK_ENABLED = False
    
    # Giải pháp 2: Kiểm tra Safe Exam Browser (SEB)
    if SEB_CHECK_ENABLED:
        user_agent = request.META.get('HTTP_USER_AGENT', '')
        
        # Kiểm tra xem có phải là Safe Exam Browser không
        # SEB User-Agent thường chứa chuỗi "SEB"
        if 'SEB' not in user_agent:
            # Nếu không phải SEB, chặn truy cập và hướng dẫn tải
            return render(request, 'qna/student/seb_required.html', {
                'message': 'Bài thi này yêu cầu sử dụng Safe Exam Browser.',
                'download_url': 'https://safeexambrowser.org/download_en.html'
            })
    
    subject = get_object_or_404(Subject, subject_code=subject_code)

    main_questions = list(
        Question.objects.filter(subject=subject, is_supplementary=False).order_by("?")[:3]
    )

    if not main_questions:
        messages.error(request, f"Môn {subject.name} chưa có câu hỏi. Vui lòng liên hệ quản trị viên.")
        return redirect("qna:dashboard")

    session = ExamSession.objects.create(user=request.user, subject=subject)
    session.questions.set(main_questions)

    remaining_main_qs = Question.objects.filter(
        subject=subject, is_supplementary=False
    ).exclude(id__in=[q.id for q in main_questions])

    barem = [{"id": q.id, "question": q.question_text} for q in remaining_main_qs]

    return render(request, "qna/student/exam.html", {
        "subject": subject,
        "selected_questions": main_questions,
        "session": session,
        "barem_json": json.dumps(barem, ensure_ascii=False),
    })


# ===========================
# HỒ SƠ VÀ AVATAR
# ===========================

def _get_avatar_data_url(profile: UserProfile) -> str:
    """Tạo chuỗi data URL cho avatar từ DB blob hoặc trả về ảnh mặc định."""
    if profile.profile_image_blob:
        try:
            mime = profile.profile_image_mime or "image/jpeg"
            encoded_blob = b64encode(profile.profile_image_blob).decode("ascii")
            return f"data:{mime};base64,{encoded_blob}"
        except Exception:
            pass
    return static("images/default_avatar.png")


@login_required
def profile_view(request: HttpRequest) -> HttpResponse:
    # Lấy UserProfile liên kết với người dùng đang đăng nhập.
    # Sử dụng try-except để xử lý trường hợp hiếm gặp khi profile chưa được tạo.
    try:
        profile = request.user.userprofile
    except UserProfile.DoesNotExist:
        # Lý tưởng nhất, mỗi User nên có một UserProfile được tạo tự động.
        # Đây là phương án dự phòng.
        profile = None

    context = {
        'user': request.user,
        'profile': profile,
    }
    return render(request, 'qna/student/profile.html', context)


@login_required
@require_POST
def update_profile_image(request: HttpRequest) -> JsonResponse:
    """API để tải lên và cập nhật ảnh đại diện mới, lưu dưới dạng blob."""
    file_obj = request.FILES.get('profile_image')
    if not file_obj:
        return JsonResponse({"success": False, "error": "Không tìm thấy file ảnh."}, status=400)

    if file_obj.size > 5 * 1024 * 1024:  # Giới hạn 5MB
        return JsonResponse({"success": False, "error": "Kích thước ảnh không được vượt quá 5MB."}, status=400)

    content = file_obj.read()
    mime = file_obj.content_type

    profile, _ = UserProfile.objects.get_or_create(user=request.user)

    profile.profile_image_blob = content
    profile.profile_image_mime = mime
    profile.save()

    data_url = f"data:{mime};base64,{b64encode(content).decode('ascii')}"
    return JsonResponse({"success": True, "image_data_url": data_url})


# ===========================
# CÁC API CHO LUỒNG THI
# ===========================

@login_required
@require_POST
def save_exam_result(request: HttpRequest) -> JsonResponse:
    """API để lưu kết quả của một câu hỏi chính."""
    data = _json_body(request)
    session_id = data.get("session_id")
    question_id = data.get("question_id")
    score = data.get("score")

    if not all([session_id, question_id, score is not None]):
        return HttpResponseBadRequest("Thiếu các tham số bắt buộc (session_id, question_id, score).")

    session = get_object_or_404(ExamSession, pk=session_id)
    _ensure_owner(session, request.user)
    question = get_object_or_404(Question, pk=question_id, is_supplementary=False)

    result, created = ExamResult.objects.update_or_create(
        session=session,
        question=question,
        defaults={
            "transcript": data.get("transcript", ""),
            "score": float(score),
            "feedback": data.get("feedback"),
            "analysis": data.get("analysis"),
            "answered_at": timezone.now(),
        }
    )
    return JsonResponse({"status": "ok", "created": created, "result_id": result.id})


@login_required
@require_POST
def get_supplementary_for_session(request: HttpRequest, session_id: int) -> JsonResponse:
    """API để lấy ngẫu nhiên 2 câu hỏi phụ cho một phiên thi."""
    session = get_object_or_404(ExamSession.objects.select_related("subject"), pk=session_id)
    _ensure_owner(session, request.user)

    supp_pool = list(Question.objects.filter(subject=session.subject, is_supplementary=True))
    random.shuffle(supp_pool)

    picked_questions = supp_pool[:SUPP_MAX_COUNT]
    items = [{"id": q.id, "question": q.question_text} for q in picked_questions]

    return JsonResponse({"status": "ok", "items": items})


@login_required
@require_POST
def save_supplementary_result(request: HttpRequest) -> JsonResponse:
    """API để lưu kết quả của một câu hỏi phụ."""
    data = _json_body(request)
    session_id = data.get("session_id")
    question_text = (data.get("question_text") or "").strip()
    raw_score = data.get("score")

    if not all([session_id, question_text, raw_score is not None]):
        return HttpResponseBadRequest("Thiếu tham số (session_id, question_text, score).")

    session = get_object_or_404(ExamSession, pk=session_id)
    _ensure_owner(session, request.user)

    if SupplementaryResult.objects.filter(session=session).count() >= SUPP_MAX_COUNT:
        return JsonResponse(
            {"status": "error", "message": f"Đã đạt số lượng câu hỏi phụ tối đa ({SUPP_MAX_COUNT})."},
            status=400,
        )

    try:
        score_val = float(raw_score)
        max_score_val = float(data.get("max_score", 10.0))
        if max_score_val <= 0: max_score_val = 10.0
    except (TypeError, ValueError):
        return HttpResponseBadRequest("Giá trị điểm không hợp lệ.")

    normalized_score = (score_val / max_score_val) * SUPP_MAX_PER_QUESTION
    final_score = max(0.0, min(normalized_score, SUPP_MAX_PER_QUESTION))

    sr = SupplementaryResult.objects.create(
        session=session,
        question_text=question_text,
        transcript=data.get("transcript", ""),
        score=final_score,
        max_score=SUPP_MAX_PER_QUESTION,
        feedback=data.get("feedback"),
        analysis=data.get("analysis"),
    )

    main_avg, supp_sum, final_total = _compute_scores(session)
    return JsonResponse({
        "status": "ok",
        "supplementary_result_id": sr.id,
        "main_avg": main_avg,
        "supp_sum": supp_sum,
        "final_total": final_total,
    })


@login_required
@require_POST
def finalize_session_view(request: HttpRequest, session_id: int) -> JsonResponse:
    """API để hoàn thành một phiên thi."""
    session = get_object_or_404(ExamSession, pk=session_id)
    _ensure_owner(session, request.user)

    # Dùng hàm tính điểm đã được cập nhật
    main_avg, _, total_score = _compute_scores(session)

    session.is_completed = True
    session.completed_at = timezone.now()
    session.final_score = total_score
    session.save(update_fields=["is_completed", "completed_at", "final_score"])

    return JsonResponse({"status": "success", "final_score": session.final_score})


# ===========================
# FACE VERIFICATION
# ===========================

@login_required
@require_POST
def verify_student_face(request: HttpRequest) -> JsonResponse:
    """
    API để xác thực khuôn mặt sinh viên trước khi thi.
    Nhận ảnh từ webcam (base64) và lưu vào database.
    """
    face_image_data = request.POST.get('face_image')  # Base64 từ webcam
    subject_code = request.POST.get('subject_code')  # Mã môn học
    
    if not face_image_data:
        return JsonResponse({
            'status': 'error',
            'message': 'Thiếu ảnh khuôn mặt.'
        }, status=400)
    
    if not subject_code:
        return JsonResponse({
            'status': 'error',
            'message': 'Thiếu mã môn học.'
        }, status=400)
    
    try:
        # Lấy môn học
        subject = get_object_or_404(Subject, subject_code=subject_code)
        
        # Xử lý ảnh webcam (Base64 -> bytes)
        format, imgstr = face_image_data.split(';base64,')
        ext = format.split('/')[-1]
        face_image_bytes = base64.b64decode(imgstr)
        face_mime = f"image/{ext}"
        
        # Tạo ExamSession tạm thời để lưu thông tin xác thực
        session = ExamSession.objects.create(
            user=request.user,
            subject=subject,
            face_image_blob=face_image_bytes,
            face_image_mime=face_mime,
            verification_status='ALLOW'
        )
        
        # Trả về thông tin session để client có thể tiếp tục
        return JsonResponse({
            'status': 'success',
            'session_id': session.id,
            'message': 'Đã lưu ảnh xác thực thành công.'
        })
        
    except Exception as e:
        return JsonResponse({
            'status': 'error',
            'message': f'Lỗi khi xử lý ảnh: {str(e)}'
        }, status=500)


@login_required
def get_verification_images(request: HttpRequest, session_id: int) -> HttpResponse:
    """
    API để lấy ảnh xác thực của một phiên thi (cho giảng viên xem).
    """
    session = get_object_or_404(ExamSession, pk=session_id)
    
    # Kiểm tra quyền: chỉ giảng viên hoặc chủ sở hữu session mới được xem
    if not (request.user.userprofile.is_lecturer or session.user == request.user):
        raise PermissionDenied("Bạn không có quyền xem ảnh xác thực này.")
    
    face_data_url = None
    id_card_data_url = None
    
    if session.face_image_blob and session.face_image_mime:
        face_encoded = b64encode(session.face_image_blob).decode('ascii')
        face_data_url = f"data:{session.face_image_mime};base64,{face_encoded}"
    
    if session.id_card_image_blob and session.id_card_image_mime:
        id_card_encoded = b64encode(session.id_card_image_blob).decode('ascii')
        id_card_data_url = f"data:{session.id_card_image_mime};base64,{id_card_encoded}"
    
    return JsonResponse({
        'status': 'success',
        'face_image': face_data_url,
        'id_card_image': id_card_data_url,
        'verification_score': session.verification_score,
        'verification_status': session.verification_status,
        'needs_manual_review': session.needs_manual_review
    })


# ===========================
# GIẢNG VIÊN - QUẢN LÝ CÂU HỎI & MÃ ĐỀ THI
# ===========================

@login_required
def lecturer_question_management(request, subject_code):
    """
    Trang quản lý câu hỏi và mã đề thi cho một môn học.
    Giảng viên có thể: xem, chỉnh sửa, xoá câu hỏi trong mã đề thi.
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')
    
    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        subject_code=subject_code
    )
    
    # Lấy danh sách mã đề thi đã duyệt cho môn học này
    exam_codes = ExamCode.objects.filter(
        subject=subject,
        is_approved=True
    ).select_related(
        'question_easy', 'question_medium', 'question_hard'
    ).order_by('-created_at')
    
    # Lấy danh sách câu hỏi có sẵn trong môn học
    all_questions = Question.objects.filter(subject=subject).order_by('question_id_in_barem')
    
    context = {
        'subject': subject,
        'exam_codes': exam_codes,
        'all_questions': all_questions,
    }
    return render(request, 'qna/lecturer/lecturer_question_management.html', context)


@login_required
@require_POST
def lecturer_update_exam_code_question(request, exam_code_id):
    """
    API để cập nhật câu hỏi trong một mã đề thi.
    Giảng viên có thể thay đổi câu hỏi cho từng mức độ khó.
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    exam_code = get_object_or_404(ExamCode, pk=exam_code_id)
    
    # Kiểm tra quyền truy cập môn học
    if exam_code.subject not in request.user.userprofile.subjects_taught.all():
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập môn học này'}, status=403)
    
    difficulty = request.POST.get('difficulty')  # EASY, MEDIUM, HARD
    question_id = request.POST.get('question_id')
    
    if difficulty not in ['EASY', 'MEDIUM', 'HARD']:
        return JsonResponse({'success': False, 'error': 'Mức độ khó không hợp lệ'}, status=400)
    
    if question_id:
        question = get_object_or_404(Question, pk=question_id, subject=exam_code.subject)
    else:
        question = None
    
    # Cập nhật câu hỏi tương ứng
    if difficulty == 'EASY':
        exam_code.question_easy = question
    elif difficulty == 'MEDIUM':
        exam_code.question_medium = question
    elif difficulty == 'HARD':
        exam_code.question_hard = question
    
    exam_code.save()
    
    messages.success(request, 'Đã cập nhật câu hỏi thành công.')
    return JsonResponse({'success': True})


@login_required
@require_POST
def lecturer_delete_exam_code(request, exam_code_id):
    """
    API để xoá một mã đề thi.
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    exam_code = get_object_or_404(ExamCode, pk=exam_code_id)
    
    # Kiểm tra quyền truy cập môn học
    if exam_code.subject not in request.user.userprofile.subjects_taught.all():
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập môn học này'}, status=403)
    
    exam_code.delete()
    messages.success(request, 'Đã xoá mã đề thi thành công.')
    return JsonResponse({'success': True})


# ===========================
# GIẢNG VIÊN - IMPORT BÀI GIẢNG & SINH MÃ ĐỀ BẰNG AI
# ===========================

@login_required
def lecturer_generate_exam_codes(request, subject_code):
    """
    Trang sinh mã đề thi bằng AI từ tài liệu bài giảng.
    Giảng viên có thể: upload tài liệu, quy định số lượng mã đề, duyệt mã đề.
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')
    
    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        subject_code=subject_code
    )
    
    # Lấy danh sách tài liệu bài giảng
    materials = LectureMaterial.objects.filter(subject=subject).order_by('-uploaded_at')
    
    # Lấy danh sách mã đề thi chưa duyệt
    pending_exam_codes = ExamCode.objects.filter(
        subject=subject,
        is_approved=False
    ).select_related(
        'question_easy', 'question_medium', 'question_hard'
    ).order_by('-created_at')
    
    context = {
        'subject': subject,
        'materials': materials,
        'pending_exam_codes': pending_exam_codes,
    }
    return render(request, 'qna/lecturer/lecturer_generate_exam_codes.html', context)


@login_required
@require_POST
def lecturer_upload_material(request, subject_code):
    """
    API để upload tài liệu bài giảng.
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        subject_code=subject_code
    )
    
    title = request.POST.get('title')
    file_obj = request.FILES.get('file')
    
    if not title or not file_obj:
        return JsonResponse({'success': False, 'error': 'Thiếu thông tin'}, status=400)
    
    # Lưu file
    import os
    from django.conf import settings
    
    upload_dir = os.path.join(settings.MEDIA_ROOT, 'lecture_materials', subject_code)
    os.makedirs(upload_dir, exist_ok=True)
    
    file_path = os.path.join(upload_dir, file_obj.name)
    with open(file_path, 'wb+') as destination:
        for chunk in file_obj.chunks():
            destination.write(chunk)
    
    material = LectureMaterial.objects.create(
        subject=subject,
        title=title,
        file_path=file_path,
        file_type=file_obj.content_type
    )
    
    messages.success(request, 'Đã upload tài liệu thành công.')
    return JsonResponse({'success': True, 'material_id': material.id})


@login_required
@require_POST
def lecturer_generate_codes_with_ai(request, subject_code):
    """
    API để sinh mã đề thi bằng AI từ tài liệu.
    Sử dụng OpenAI API để tạo câu hỏi theo 3 mức độ khó.
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        subject_code=subject_code
    )
    
    material_id = request.POST.get('material_id')
    num_codes = int(request.POST.get('num_codes', 4))
    
    if num_codes < 1 or num_codes > 16:
        return JsonResponse({'success': False, 'error': 'Số lượng mã đề phải từ 1 đến 16'}, status=400)
    
    material = get_object_or_404(LectureMaterial, pk=material_id, subject=subject)
    
    try:
        import openai
        client = openai.OpenAI()
        
        # Đọc nội dung tài liệu (giả sử là text file)
        with open(material.file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Prompt để sinh câu hỏi theo 3 mức độ khó
        system_prompt = """Bạn là chuyên gia giáo dục, chuyên tạo câu hỏi thi vấn đáp.
        
        QUY TẮC PHÂN LOẠI ĐỘ KHÓ:
        
        CÂU DỄ:
        - Định nghĩa, khái niệm
        - Nhận biết, mô tả, kể tên
        - Ví dụ: "Hãy định nghĩa Khai phá dữ liệu"
        
        CÂU TRUNG BÌNH:
        - Công thức
        - Nguyên lý hoạt động
        - Giải thích cách vận hành
        - Ví dụ: "Trình bày nguyên lý hoạt động của thuật toán Apriori"
        
        CÂU KHÓ:
        - Phân tích, so sánh
        - Ứng dụng thực tế
        - Tổng hợp nhiều kiến thức
        - Ví dụ: "So sánh Apriori và FP-Growth"
        
        ĐẦU RA: JSON format với 3 câu hỏi:
        {
            "easy": "câu hỏi dễ",
            "medium": "câu hỏi trung bình",
            "hard": "câu hỏi khó"
        }
        KHÔNG thêm văn bản nào khác ngoài JSON."""
        
        user_prompt = f"""Tạo {num_codes} bộ câu hỏi (mỗi bộ 3 câu hỏi) cho môn học: {subject.name}
        
        NỘI DUNG TÀI LIỆU:
        {content[:5000]}  # Giới hạn 5000 ký tự để tránh token limit
        
        YÊU CẦU:
        - Tạo {num_codes} bộ câu hỏi khác nhau
        - Mỗi bộ có đúng 3 câu hỏi theo 3 mức độ: Dễ, Trung bình, Khó
        - Câu hỏi phải dựa trên nội dung tài liệu
        - Trả về danh sách JSON: [{{"easy": "...", "medium": "...", "hard": "..."}}, ...]
        """
        
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.7,
            response_format={"type": "json_object"}
        )
        
        questions_list = json.loads(resp.choices[0].message.content)
        
        # Tạo mã đề thi và câu hỏi
        created_codes = []
        for i, questions in enumerate(questions_list):
            # Tạo 3 câu hỏi mới
            q_easy = Question.objects.create(
                subject=subject,
                question_text=questions['easy'],
                question_id_in_barem=f'AI_EASY_{i}_{uuid4().hex[:8]}',
                is_supplementary=False
            )
            
            q_medium = Question.objects.create(
                subject=subject,
                question_text=questions['medium'],
                question_id_in_barem=f'AI_MEDIUM_{i}_{uuid4().hex[:8]}',
                is_supplementary=False
            )
            
            q_hard = Question.objects.create(
                subject=subject,
                question_text=questions['hard'],
                question_id_in_barem=f'AI_HARD_{i}_{uuid4().hex[:8]}',
                is_supplementary=False
            )
            
            # Tạo mã đề thi
            exam_code = ExamCode.objects.create(
                subject=subject,
                code_name=f'Mã đề AI-{i+1}',
                question_easy=q_easy,
                question_medium=q_medium,
                question_hard=q_hard,
                source_material=material.title,
                is_approved=False  # Cần giảng viên duyệt
            )
            created_codes.append(exam_code.id)
        
        messages.success(request, f'Đã sinh thành công {len(created_codes)} mã đề thi.')
        return JsonResponse({
            'success': True,
            'created_count': len(created_codes),
            'exam_code_ids': created_codes
        })
        
    except Exception as e:
        logger.error(f"Lỗi khi sinh mã đề thi bằng AI: {e}")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@require_POST
def lecturer_approve_exam_code(request, exam_code_id):
    """
    API để duyệt một mã đề thi.
    Sau khi duyệt, mã đề có thể được sử dụng cho ca thi.
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    exam_code = get_object_or_404(ExamCode, pk=exam_code_id)
    
    # Kiểm tra quyền truy cập môn học
    if exam_code.subject not in request.user.userprofile.subjects_taught.all():
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập môn học này'}, status=403)
    
    exam_code.is_approved = True
    exam_code.save()
    
    messages.success(request, 'Đã duyệt mã đề thi thành công.')
    return JsonResponse({'success': True})


@login_required
@require_POST
def lecturer_edit_exam_code_question(request, exam_code_id):
    """
    API để chỉnh sửa nội dung câu hỏi trong mã đề thi.
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    exam_code = get_object_or_404(ExamCode, pk=exam_code_id)
    
    # Kiểm tra quyền truy cập môn học
    if exam_code.subject not in request.user.userprofile.subjects_taught.all():
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập môn học này'}, status=403)
    
    difficulty = request.POST.get('difficulty')  # EASY, MEDIUM, HARD
    new_text = request.POST.get('new_text', '').strip()
    
    if difficulty not in ['EASY', 'MEDIUM', 'HARD']:
        return JsonResponse({'success': False, 'error': 'Mức độ khó không hợp lệ'}, status=400)
    
    if not new_text:
        return JsonResponse({'success': False, 'error': 'Nội dung câu hỏi không được để trống'}, status=400)
    
    # Cập nhật nội dung câu hỏi
    question = None
    if difficulty == 'EASY':
        question = exam_code.question_easy
    elif difficulty == 'MEDIUM':
        question = exam_code.question_medium
    elif difficulty == 'HARD':
        question = exam_code.question_hard
    
    if question:
        question.question_text = new_text
        question.save()
    else:
        # Tạo câu hỏi mới nếu chưa có
        question = Question.objects.create(
            subject=exam_code.subject,
            question_text=new_text,
            question_id_in_barem=f'AI_EDITED_{difficulty}_{uuid4().hex[:8]}',
            is_supplementary=False
        )
        
        if difficulty == 'EASY':
            exam_code.question_easy = question
        elif difficulty == 'MEDIUM':
            exam_code.question_medium = question
        elif difficulty == 'HARD':
            exam_code.question_hard = question
        
        exam_code.save()
    
    messages.success(request, 'Đã chỉnh sửa câu hỏi thành công.')
    return JsonResponse({'success': True})


# ===========================
# GIẢNG VIÊN - TẠO CA THI MỚI
# ===========================

@login_required
def lecturer_create_exam_session(request, subject_code):
    """
    Trang tạo ca thi mới.
    Giảng viên có thể: chọn môn học, thời gian, mã đề thi, phòng thi, import sinh viên.
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')
    
    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        subject_code=subject_code
    )
    
    # Lấy danh sách mã đề thi đã duyệt
    approved_exam_codes = ExamCode.objects.filter(
        subject=subject,
        is_approved=True
    ).select_related(
        'question_easy', 'question_medium', 'question_hard'
    )
    
    # Lấy danh sách phòng thi
    all_rooms = ExamRoom.objects.all()
    
    # Lấy danh sách ca thi đã tạo
    exam_groups = ExamSessionGroup.objects.filter(
        subject=subject
    ).prefetch_related('rooms', 'exam_codes').order_by('-exam_date')
    
    context = {
        'subject': subject,
        'approved_exam_codes': approved_exam_codes,
        'all_rooms': all_rooms,
        'exam_groups': exam_groups,
    }
    return render(request, 'qna/lecturer/lecturer_create_exam_session.html', context)


@login_required
@require_POST
def lecturer_create_exam_group(request, subject_code):
    """
    API để tạo ca thi mới.
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        subject_code=subject_code
    )
    
    group_name = request.POST.get('group_name')
    exam_date_str = request.POST.get('exam_date')
    duration_minutes = int(request.POST.get('duration_minutes', 60))
    exam_password = request.POST.get('exam_password', '').strip()
    exam_code_ids = request.POST.getlist('exam_code_ids')
    room_ids = request.POST.getlist('room_ids')
    
    if not group_name or not exam_date_str:
        return JsonResponse({'success': False, 'error': 'Thiếu thông tin bắt buộc'}, status=400)
    
    from datetime import datetime
    exam_date = datetime.strptime(exam_date_str, '%Y-%m-%dT%H:%M')
    
    # Tạo ca thi
    exam_group = ExamSessionGroup.objects.create(
        subject=subject,
        group_name=group_name,
        exam_date=exam_date,
        duration_minutes=duration_minutes,
        exam_password=exam_password if exam_password else None,
        status='SCHEDULED',
        created_by=request.user
    )
    
    # Thêm mã đề thi
    if exam_code_ids:
        exam_codes = ExamCode.objects.filter(id__in=exam_code_ids, subject=subject)
        exam_group.exam_codes.set(exam_codes)
    
    # Thêm phòng thi
    if room_ids:
        for room_id in room_ids:
            room = ExamRoom.objects.get(pk=room_id)
            ExamSessionRoom.objects.create(
                exam_group=exam_group,
                room=room
            )
    
    messages.success(request, 'Đã tạo ca thi thành công.')
    return JsonResponse({'success': True, 'exam_group_id': exam_group.id})


@login_required
@require_POST
def lecturer_update_exam_group(request, exam_group_id):
    """
    API để cập nhật thông tin ca thi.
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    exam_group = get_object_or_404(ExamSessionGroup, pk=exam_group_id)
    
    # Kiểm tra quyền truy cập
    if exam_group.subject not in request.user.userprofile.subjects_taught.all():
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập môn học này'}, status=403)
    
    # Không cho phép sửa khi ca thi đã kết thúc
    if exam_group.status == 'COMPLETED':
        return JsonResponse({'success': False, 'error': 'Không thể sửa ca thi đã kết thúc'}, status=400)
    
    # Cập nhật các trường
    if 'group_name' in request.POST:
        exam_group.group_name = request.POST['group_name']
    if 'exam_date' in request.POST:
        from datetime import datetime
        exam_group.exam_date = datetime.strptime(request.POST['exam_date'], '%Y-%m-%dT%H:%M')
    if 'duration_minutes' in request.POST:
        exam_group.duration_minutes = int(request.POST['duration_minutes'])
    if 'exam_password' in request.POST:
        password = request.POST['exam_password'].strip()
        exam_group.exam_password = password if password else None
    
    exam_group.save()
    
    messages.success(request, 'Đã cập nhật ca thi thành công.')
    return JsonResponse({'success': True})


@login_required
@require_POST
def lecturer_import_students_to_room(request, session_room_id):
    """
    API để import danh sách sinh viên vào phòng thi.
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    session_room = get_object_or_404(ExamSessionRoom, pk=session_room_id)
    
    # Kiểm tra quyền truy cập
    if session_room.exam_group.subject not in request.user.userprofile.subjects_taught.all():
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập môn học này'}, status=403)
    
    import csv
    import io
    
    csv_file = request.FILES.get('csv_file')
    if not csv_file:
        return JsonResponse({'success': False, 'error': 'Thiếu file CSV'}, status=400)
    
    try:
        decoded_file = csv_file.read().decode('utf-8')
        io_string = io.StringIO(decoded_file)
        reader = csv.DictReader(io_string)
        
        imported_count = 0
        for row in reader:
            username = row.get('username', '').strip()
            if username:
                try:
                    user = User.objects.get(username=username)
                    session_room.students.add(user)
                    imported_count += 1
                except User.DoesNotExist:
                    continue
        
        messages.success(request, f'Đã import {imported_count} sinh viên thành công.')
        return JsonResponse({'success': True, 'imported_count': imported_count})
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@require_POST
def lecturer_random_assign_students(request, exam_group_id):
    """
    API để random sinh viên vào các phòng thi.
    Quy tắc: 1 sinh viên chỉ thuộc 1 phòng thi.
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    exam_group = get_object_or_404(ExamSessionGroup, pk=exam_group_id)
    
    # Kiểm tra quyền truy cập
    if exam_group.subject not in request.user.userprofile.subjects_taught.all():
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập môn học này'}, status=403)
    
    # Lấy tất cả sinh viên từ tất cả các phòng
    all_students = []
    for session_room in exam_group.session_rooms.all():
        all_students.extend(list(session_room.students.all()))
    
    # Xoá tất cả sinh viên khỏi phòng
    for session_room in exam_group.session_rooms.all():
        session_room.students.clear()
    
    # Random sinh viên
    random.shuffle(all_students)
    
    # Chia sinh viên vào các phòng
    session_rooms = list(exam_group.session_rooms.all())
    for i, student in enumerate(all_students):
        room_index = i % len(session_rooms)
        session_rooms[room_index].students.add(student)
    
    messages.success(request, 'Đã random sinh viên vào các phòng thi thành công.')
    return JsonResponse({'success': True})


# ===========================
# GIẢNG VIÊN - XEM CHI TIẾT BÀI LÀM CỦA SINH VIÊN
# ===========================

@login_required
def lecturer_student_review(request, subject_code):
    """
    Trang xem chi tiết bài làm của sinh viên.
    Giảng viên có thể: xem câu trả lời, nghe lại bản ghi âm.
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')
    
    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        subject_code=subject_code
    )
    
    # Lấy danh sách các phiên thi của môn học
    sessions = ExamSession.objects.filter(
        subject=subject
    ).select_related('user', 'user__userprofile').prefetch_related(
        'results__question'
    ).order_by('-created_at')
    
    context = {
        'subject': subject,
        'sessions': sessions,
    }
    return render(request, 'qna/lecturer/lecturer_student_review.html', context)


@login_required
def lecturer_session_detail(request, session_id):
    """
    Trang chi tiết một phiên thi.
    Hiển thị câu trả lời và bản ghi âm của sinh viên.
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')
    
    session = get_object_or_404(
        ExamSession.objects.select_related('subject', 'user', 'user__userprofile'),
        pk=session_id
    )
    
    # Kiểm tra quyền truy cập
    if session.subject not in request.user.userprofile.subjects_taught.all():
        raise PermissionDenied("Bạn không có quyền xem phiên thi này.")
    
    # Lấy kết quả thi
    main_results = (
        ExamResult.objects.filter(session=session)
        .select_related("question")
        .order_by("question_id")
    )
    
    supp_results_qs = SupplementaryResult.objects.filter(session=session)
    supp_results_display = _dedupe_supp_for_display(supp_results_qs)
    
    main_avg, supp_sum, final_total = _compute_scores(session)
    
    context = {
        'session': session,
        'results': main_results,
        'supp_results': supp_results_display,
        'main_avg': main_avg,
        'supp_sum': supp_sum,
        'final_total': final_total,
    }
    return render(request, 'qna/lecturer/lecturer_session_detail.html', context)


# ===========================
# GIẢNG VIÊN - XUẤT BÁO CÁO KẾT QUẢ THI
# ===========================

@login_required
def lecturer_export_exam_results(request, subject_code, exam_group_id=None):
    """
    Xuất báo cáo kết quả thi ra file Excel.
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')
    
    subject = get_object_or_404(
        request.user.userprofile.subjects_taught,
        subject_code=subject_code
    )
    
    # Lọc theo ca thi nếu có
    if exam_group_id:
        exam_group = get_object_or_404(ExamSessionGroup, pk=exam_group_id, subject=subject)
        sessions = ExamSession.objects.filter(
            subject=subject,
            exam_group=exam_group
        ).select_related('user', 'user__userprofile')
    else:
        sessions = ExamSession.objects.filter(
            subject=subject
        ).select_related('user', 'user__userprofile')
    
    # Tạo file Excel
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
    from django.http import HttpResponse
    from datetime import datetime
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Kết quả thi"
    
    # Header
    headers = ['STT', 'Mã SV', 'Họ tên', 'Lớp', 'Ngày thi', 'Điểm chính', 'Điểm phụ', 'Điểm tổng']
    ws.append(headers)
    
    # Style header
    header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')
    
    # Data
    for idx, session in enumerate(sessions, 1):
        main_avg, supp_sum, final_total = _compute_scores(session)
        
        profile = session.user.userprofile
        ws.append([
            idx,
            session.user.username,
            profile.full_name,
            profile.class_name,
            session.created_at.strftime('%d/%m/%Y %H:%M'),
            f"{main_avg:.2f}",
            f"{supp_sum:.2f}",
            f"{final_total:.2f}"
        ])
    
    # Auto-fit columns
    for column in ws.columns:
        max_length = 0
        column_letter = column[0].column_letter
        for cell in column:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = min(max_length + 2, 50)
        ws.column_dimensions[column_letter].width = adjusted_width
    
    # Response
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    filename = f'Ket_qua_thi_{subject.subject_code}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    wb.save(response)
    return response


# ===========================
# GIẢNG VIÊN - QUẢN LÝ PHÒNG THI
# ===========================

@login_required
def lecturer_manage_rooms(request):
    """
    Trang quản lý phòng thi.
    """
    if not request.user.userprofile.is_lecturer:
        return redirect('qna:dashboard')
    
    rooms = ExamRoom.objects.all().order_by('room_code')
    
    context = {
        'rooms': rooms,
    }
    return render(request, 'qna/lecturer/lecturer_manage_rooms.html', context)


@login_required
@require_POST
def lecturer_create_room(request):
    """
    API để tạo phòng thi mới.
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    room_name = request.POST.get('room_name')
    room_code = request.POST.get('room_code')
    capacity = int(request.POST.get('capacity', 30))
    
    if not room_name or not room_code:
        return JsonResponse({'success': False, 'error': 'Thiếu thông tin bắt buộc'}, status=400)
    
    room = ExamRoom.objects.create(
        room_name=room_name,
        room_code=room_code,
        capacity=capacity
    )
    
    messages.success(request, 'Đã tạo phòng thi thành công.')
    return JsonResponse({'success': True, 'room_id': room.id})


@login_required
@require_POST
def lecturer_delete_room(request, room_id):
    """
    API để xoá phòng thi.
    """
    if not request.user.userprofile.is_lecturer:
        return JsonResponse({'success': False, 'error': 'Không có quyền truy cập'}, status=403)
    
    room = get_object_or_404(ExamRoom, pk=room_id)
    room.delete()
    
    messages.success(request, 'Đã xoá phòng thi thành công.')
    return JsonResponse({'success': True})

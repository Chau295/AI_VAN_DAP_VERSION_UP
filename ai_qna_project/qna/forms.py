from django import forms
from .models import DifficultyLevel


class QuestionForm(forms.Form):
    question = forms.CharField(
        label="Câu hỏi của bạn",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Bạn muốn hỏi gì về Django?"}),
    )


class LecturerManualQuestionForm(forms.Form):
    subject_id = forms.IntegerField(required=True)
    question_text = forms.CharField(
        required=True,
        max_length=5000,
        widget=forms.Textarea(attrs={"rows": 5}),
    )
    difficulty = forms.ChoiceField(
        required=True,
        choices=DifficultyLevel.choices,
    )

    def clean_question_text(self):
        value = (self.cleaned_data.get("question_text") or "").strip()
        if not value:
            raise forms.ValidationError("Nội dung câu hỏi không được để trống.")
        return value


class LecturerQuestionImportForm(forms.Form):
    subject_id = forms.IntegerField(required=True)
    file = forms.FileField(required=True)


class LecturerMaterialUploadForm(forms.Form):
    subject_id = forms.IntegerField(required=True)
    title = forms.CharField(required=True, max_length=255)
    file = forms.FileField(required=True)

    def clean_title(self):
        value = (self.cleaned_data.get("title") or "").strip()
        if not value:
            raise forms.ValidationError("Tiêu đề tài liệu không được để trống.")
        return value

    def clean_file(self):
        file_obj = self.cleaned_data.get("file")
        if not file_obj:
            raise forms.ValidationError("Vui lòng chọn file.")
        if file_obj.size <= 0:
            raise forms.ValidationError("File được chọn không có dữ liệu.")
        if file_obj.size > 50 * 1024 * 1024:
            raise forms.ValidationError("Dung lượng file vượt quá 50MB. Vui lòng kiểm tra lại.")

        allowed_ext = {"pdf", "docx", "txt"}
        name = file_obj.name.lower()
        ext = name.split(".")[-1] if "." in name else ""
        if ext not in allowed_ext:
            raise forms.ValidationError("Định dạng file không hợp lệ.")
        return file_obj
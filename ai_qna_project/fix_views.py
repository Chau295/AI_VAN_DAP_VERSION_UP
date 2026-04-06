from pathlib import Path
import sys
import ftfy

INPUT_PATH = "qna/views.py"

try:
    text = Path(INPUT_PATH).read_text(encoding="utf-8")
except FileNotFoundError:
    raise SystemExit(f"Không tìm thấy file: {INPUT_PATH}")

fixed = ftfy.fix_text(text)

replacements = {
    "KhÃ´ng": "Không",
    "Táº£i lÃªn": "Tải lên",
    "DÃ²ng": "Dòng",
    "thiáº¿u": "thiếu",
    "trÃ¹ng": "trùng",
    "há» tÃªn": "họ tên",
    "ngÃ¢n hÃ ng": "ngân hàng",
    "cÃ¢u há»i": "câu hỏi",
    "khÃ´ng há»£p lá»‡": "không hợp lệ",
    "KhÃ´ng thá»ƒ": "Không thể",
    "xÃ³a": "xóa",
    "sá»­a": "sửa",
    "Äá»‹nh dáº¡ng file": "Định dạng file",
    "há»‡ thá»‘ng": "hệ thống",
    "cháº¥p nháº­n": "chấp nhận",
    "Dung lÆ°á»£ng": "Dung lượng",
    "vÆ°á»£t quÃ¡": "vượt quá",
    "Vui lÃ²ng": "Vui lòng",
    "kiá»ƒm tra láº¡i": "kiểm tra lại",
    "Thiáº¿u": "Thiếu",
    "TÃªn file": "Tên file",
    "TÃ i liá»‡u": "Tài liệu",
    "MÃ£ Ä‘á»": "Mã đề",
    "bá»™ Ä‘á»": "bộ đề",
    "giáº£ng viÃªn": "giảng viên",
    "sinh viÃªn": "sinh viên",
    "NÄƒm há»c": "Năm học",
    "MÃ´n há»c": "Môn học",
    "Äáº·t": "Đặt",
    "ÄÃ£": "Đã",
    "thÃ nh cÃ´ng": "thành công",
    "tháº¥t báº¡i": "thất bại",
    "Äá»c": "Đọc",
    "KhÃ´ng tÃ¬m tháº¥y": "Không tìm thấy",
    "MÃ´i trÆ°á»ng": "Môi trường",
    "thá»­ láº¡i sau": "thử lại sau",
}

for old, new in replacements.items():
    fixed = fixed.replace(old, new)

compile(fixed, INPUT_PATH, "exec")
Path(INPUT_PATH).write_text(fixed, encoding="utf-8")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

print(f"Đã sửa xong: {INPUT_PATH}")

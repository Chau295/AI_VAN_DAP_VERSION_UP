# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from django.core.files.storage import default_storage


def build_draft_audio_file(*, session_id: int, question_id: int) -> str | None:
    """
    Ghép các chunk audio đã upload thành một file .webm tạm.
    Không đụng database.
    """
    chunk_dir = f"exam_recording_drafts/session_{session_id}/question_{question_id}"

    try:
        _, chunk_names = default_storage.listdir(chunk_dir)
    except Exception:
        return None

    chunk_names = sorted(
        name for name in chunk_names
        if name.startswith("chunk_") and name.endswith(".webm")
    )

    if not chunk_names:
        return None

    temp_dir = tempfile.mkdtemp(prefix="exam_draft_audio_")
    output_path = os.path.join(temp_dir, f"draft_{session_id}_{question_id}.webm")

    with open(output_path, "wb") as output:
        for chunk_name in chunk_names:
            chunk_path = f"{chunk_dir}/{chunk_name}"
            with default_storage.open(chunk_path, "rb") as chunk_file:
                output.write(chunk_file.read())

    if not os.path.exists(output_path) or os.path.getsize(output_path) <= 0:
        return None

    return output_path


def clear_draft_audio_files(*, session_id: int, question_id: int) -> None:
    chunk_dir = f"exam_recording_drafts/session_{session_id}/question_{question_id}"

    try:
        _, chunk_names = default_storage.listdir(chunk_dir)
    except Exception:
        return

    for chunk_name in chunk_names:
        chunk_path = f"{chunk_dir}/{chunk_name}"
        try:
            default_storage.delete(chunk_path)
        except Exception:
            pass
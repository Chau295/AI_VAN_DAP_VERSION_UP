# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import threading
import time

from django.apps import AppConfig
from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command


class QnaConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "qna"

    def ready(self):
        """
        Tự động chạy bộ kiểm tra bài thi mất heartbeat.
        Không thêm model, không migration.

        Lưu ý:
        - Chỉ bật khi settings.QNA_ENABLE_STALE_EXAM_FINALIZER = True.
        - Dùng cache lock để tránh nhiều thread cùng chạy một lượt.
        """

        if not getattr(settings, "QNA_ENABLE_STALE_EXAM_FINALIZER", False):
            return

        # Tránh chạy 2 lần khi dùng runserver autoreload.
        # Trên Windows/Django dev server, RUN_MAIN=true ở process thật.
        if os.environ.get("RUN_MAIN") not in {"true", "True", None}:
            return

        # Tránh start nhiều thread trong cùng process.
        if getattr(self, "_stale_exam_thread_started", False):
            return

        self._stale_exam_thread_started = True

        thread = threading.Thread(
            target=self._run_stale_exam_finalizer_loop,
            name="stale-exam-finalizer",
            daemon=True,
        )
        thread.start()

    def _run_stale_exam_finalizer_loop(self):
        interval_seconds = int(getattr(settings, "QNA_STALE_EXAM_CHECK_INTERVAL_SECONDS", 15))
        stale_seconds = int(getattr(settings, "QNA_STALE_EXAM_SECONDS", 45))

        while True:
            try:
                # Lock ngắn để nếu Django bị chạy nhiều process thì hạn chế trùng lượt quét.
                lock_acquired = cache.add(
                    "qna:stale_exam_finalizer:lock",
                    "1",
                    timeout=max(interval_seconds - 1, 5),
                )

                if lock_acquired:
                    call_command(
                        "finalize_stale_exam_sessions",
                        stale_seconds=stale_seconds,
                        verbosity=0,
                    )

            except Exception as exc:
                print(f"[stale-exam-finalizer] error: {exc}")

            time.sleep(interval_seconds)
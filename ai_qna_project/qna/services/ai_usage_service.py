import time
from typing import Any

from qna.models import AIUsageLog


def extract_openai_usage(usage: Any) -> dict:
    if usage is None:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
        }

    prompt_tokens = (
        getattr(usage, "prompt_tokens", None)
        or getattr(usage, "input_tokens", None)
        or 0
    )

    completion_tokens = (
        getattr(usage, "completion_tokens", None)
        or getattr(usage, "output_tokens", None)
        or 0
    )

    total_tokens = getattr(usage, "total_tokens", None) or (
        int(prompt_tokens or 0) + int(completion_tokens or 0)
    )

    cached_tokens = 0

    prompt_details = getattr(usage, "prompt_tokens_details", None)
    if prompt_details is not None:
        cached_tokens = getattr(prompt_details, "cached_tokens", 0) or 0

    input_details = getattr(usage, "input_tokens_details", None)
    if input_details is not None:
        cached_tokens = getattr(input_details, "cached_tokens", cached_tokens) or 0

    return {
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total_tokens or 0),
        "cached_tokens": int(cached_tokens or 0),
    }


def record_ai_usage(
    *,
    group_id: str,
    feature: str,
    step_name: str,
    model_name: str,
    usage=None,
    user=None,
    subject=None,
    exam_session=None,
    exam_result=None,
    question=None,
    latency_ms: int = 0,
    audio_duration_seconds: float = 0,
    success: bool = True,
    error_message: str = "",
    metadata: dict | None = None,
):
    token_data = extract_openai_usage(usage)

    try:
        return AIUsageLog.objects.create(
            group_id=group_id,
            feature=feature,
            step_name=step_name,
            user=user if getattr(user, "is_authenticated", False) else None,
            subject=subject,
            exam_session=exam_session,
            exam_result=exam_result,
            question=question,
            model_name=model_name,
            prompt_tokens=token_data["prompt_tokens"],
            completion_tokens=token_data["completion_tokens"],
            total_tokens=token_data["total_tokens"],
            cached_tokens=token_data["cached_tokens"],
            audio_duration_seconds=audio_duration_seconds or 0,
            latency_ms=latency_ms or 0,
            success=success,
            error_message=error_message or "",
            metadata=metadata or {},
        )
    except Exception:
        # Không để việc ghi log token làm hỏng chức năng chính.
        return None


def summarize_ai_usage(group_id: str) -> dict:
    logs = AIUsageLog.objects.filter(group_id=group_id)

    return {
        "group_id": group_id,
        "request_count": logs.count(),
        "prompt_tokens": sum(item.prompt_tokens for item in logs),
        "completion_tokens": sum(item.completion_tokens for item in logs),
        "total_tokens": sum(item.total_tokens for item in logs),
        "cached_tokens": sum(item.cached_tokens for item in logs),
        "audio_duration_seconds": sum(item.audio_duration_seconds for item in logs),
        "latency_ms": sum(item.latency_ms for item in logs),
        "by_feature": {
            feature: {
                "request_count": logs.filter(feature=feature).count(),
                "prompt_tokens": sum(item.prompt_tokens for item in logs.filter(feature=feature)),
                "completion_tokens": sum(item.completion_tokens for item in logs.filter(feature=feature)),
                "total_tokens": sum(item.total_tokens for item in logs.filter(feature=feature)),
                "audio_duration_seconds": sum(item.audio_duration_seconds for item in logs.filter(feature=feature)),
                "latency_ms": sum(item.latency_ms for item in logs.filter(feature=feature)),
            }
            for feature in logs.values_list("feature", flat=True).distinct()
        },
    }
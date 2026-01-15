# Fix for Audio Hallucination Issue

## Problem
The AI (Whisper) was hallucinating text responses even when there was no audio or only silence. For example, it would return text like "Các bạn hãy đăng ký cho kênh lalaschool Để không bỏ lỡ những video hấp dẫn" when the student didn't say anything.

## Solution
Implemented audio detection logic in `qna/management/commands/run_workers.py` to check for actual sound before sending audio to Whisper for transcription.

## Changes Made

### File: `qna/management/commands/run_workers.py`

In the `process_audio_and_transcribe()` function, added two layers of audio detection:

1. **RMS (Root Mean Square) Threshold Check**
   - Calculates the RMS (volume level) of the audio
   - If RMS < 50.0, considers it as silence/background noise
   - Returns error message: "Không có âm thanh được phát hiện."

2. **Empty Transcript Check**
   - After Whisper transcription, checks if result is empty string
   - If empty, returns error message: "Không có âm thanh được phát hiện."

### Code Changes

```python
# Kiểm tra RMS để phát hiện silence (không có âm thanh thực)
# Ngưỡng RMS < 50.0 thường là silence hoặc nhiễu nền
RMS_THRESHOLD = 50.0
if rms < RMS_THRESHOLD:
    logger.info(f"Phát hiện silence (RMS={rms:.1f} < {RMS_THRESHOLD}). Trả về 'Không có âm thanh'.")
    await self.channel_layer.send(reply_channel, 
        {'type': 'exam.error', 'message': 'Không có âm thanh được phát hiện.'})
    return None

# ... after Whisper transcription ...

# Nếu Whisper trả về chuỗi rỗng, coi như không có âm thanh
if not raw:
    logger.info("Whisper trả về chuỗi rỗng. Trả về 'Không có âm thanh'.")
    await self.channel_layer.send(reply_channel, 
        {'type': 'exam.error', 'message': 'Không có âm thanh được phát hiện.'})
    return None
```

## How It Works

1. **Audio Recording**: Student records audio through the browser
2. **Audio Processing**: Audio is converted to WAV format
3. **RMS Calculation**: System calculates the audio volume level (RMS)
4. **Silence Detection**: If RMS < 50.0, system rejects the audio as silence
5. **Transcription**: Only if audio has sufficient volume, it's sent to Whisper
6. **Empty Check**: If Whisper returns empty text, system rejects it
7. **User Feedback**: User sees alert "Không có âm thanh được phát hiện" and can retry

## RMS Threshold Explanation

- **RMS (Root Mean Square)**: Measures the average power/volume of audio
- **Threshold of 50.0**: Experimentally determined to distinguish between:
  - Silence/background noise (RMS < 50.0)
  - Actual speech (RMS >= 50.0)
- This threshold can be adjusted if needed

## Benefits

1. **Prevents Hallucination**: Whisper no longer generates random text from silence
2. **Accurate Transcription**: Only processes actual spoken content
3. **Better User Experience**: Clear feedback when no audio is detected
4. **Reliable Scoring**: Scores are based on actual student responses, not hallucinations

## Testing

To verify the fix works:

1. Start an exam
2. Click "Bắt đầu" (Start recording)
3. Immediately click "Dừng & Nộp bài" (Stop & Submit) without speaking
4. Expected result: Alert message "Không có âm thanh được phát hiện."
5. No hallucinated text should appear

## Future Improvements

- Consider adjusting RMS threshold based on microphone sensitivity
- Add audio visualization to help users see their audio levels
- Allow user to listen back to their recording before submission

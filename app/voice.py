"""Локальная транскрибация (faster-whisper, без облака) + простые голосовые признаки для оценки тона."""
import math
import tempfile

from . import config

_model = None


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _get():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model


def transcribe(data: bytes, suffix: str = ".webm") -> dict:
    from faster_whisper.audio import decode_audio
    with tempfile.NamedTemporaryFile(suffix=suffix) as f:
        f.write(data)
        f.flush()
        audio = decode_audio(f.name)
    segs, info = _get().transcribe(audio, language="ru", vad_filter=True, beam_size=5)
    segs = list(segs)
    text = " ".join(s.text.strip() for s in segs).strip()
    speech = sum(s.end - s.start for s in segs) or 1e-9
    rms = math.sqrt(float((audio ** 2).mean())) if len(audio) else 0.0
    return {"text": text, "duration_s": round(info.duration, 1), "words_per_sec": round(len(text.split()) / speech, 2),
            "loudness_db": round(20 * math.log10(rms + 1e-9), 1), "model": config.WHISPER_MODEL}

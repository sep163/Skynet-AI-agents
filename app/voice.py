"""Локальная транскрибация (faster-whisper, без облака) + простые голосовые признаки для оценки тона."""
import math
import tempfile

import numpy as np

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


def prosody(audio, sr: int = 16000) -> dict:
    """Громкость и высота тона по кадрам 40 мс: средний F0 и его разброс в полутонах (насколько «скачет» голос)."""
    a = np.asarray(audio, dtype=np.float32)
    if len(a) < sr // 4:
        return {}
    fl, hop = int(0.04 * sr), int(0.02 * sr)
    frames = np.lib.stride_tricks.sliding_window_view(a, fl)[::hop]
    rms = np.sqrt((frames ** 2).mean(axis=1)) + 1e-9
    voiced = frames[rms >= max(np.percentile(rms, 60), 1e-3)]
    lo, hi = sr // 400, sr // 70
    f0 = []
    for fr in voiced[:600]:
        fr = fr - fr.mean()
        ac = np.correlate(fr, fr, "full")[fl - 1:]
        if ac[0] <= 0:
            continue
        lag = lo + int(np.argmax(ac[lo:hi]))
        if ac[lag] / ac[0] > 0.3:
            f0.append(sr / lag)
    st = 12 * np.log2(np.array(f0) / np.median(f0)) if len(f0) > 5 else None
    loud = 20 * np.log10(rms[rms >= np.percentile(rms, 60)])
    return {"loudness_db": round(float(np.mean(loud)), 1), "loudness_peak_db": round(float(np.percentile(loud, 95)), 1),
            "pitch_hz": round(float(np.median(f0)), 0) if st is not None else None, "pitch_std_st": round(float(np.std(st)), 2) if st is not None else None}


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
    out = {"text": text, "duration_s": round(info.duration, 1), "words_per_sec": round(len(text.split()) / speech, 2),
           "loudness_db": round(20 * math.log10(rms + 1e-9), 1), "model": config.WHISPER_MODEL}
    out |= prosody(audio)
    out["segments"] = [{"start": round(s.start, 1), "end": round(s.end, 1), "text": s.text.strip(),
                        **{k: v for k, v in prosody(audio[int(s.start * 16000):int(s.end * 16000)]).items() if k in ("loudness_db", "pitch_std_st")}} for s in segs]
    return out


def transcribe_call(data: bytes, suffix: str = ".wav", client_channel: str = "left") -> dict:
    """Стереозапись колл-центра: клиент и оператор в разных каналах. Каждый канал расшифровывается отдельно,
    реплики сводятся по времени — роли известны без модели распознавания говорящих."""
    from faster_whisper.audio import decode_audio
    with tempfile.NamedTemporaryFile(suffix=suffix) as f:
        f.write(data)
        f.flush()
        try:
            left, right = decode_audio(f.name, split_stereo=True)
        except Exception:
            return {"error": "запись не стерео: разметьте реплики вручную строками «К:» и «О:»"}
    if len(left) and float(np.corrcoef(left[: 16000 * 30], right[: 16000 * 30])[0, 1]) > 0.98:
        return {"error": "каналы одинаковые (моно-запись): разметьте реплики вручную строками «К:» и «О:»"}
    chans = {"client": left, "operator": right} if client_channel == "left" else {"client": right, "operator": left}
    utt = []
    for role, audio in chans.items():
        segs, _ = _get().transcribe(audio, language="ru", vad_filter=True, beam_size=5)
        for s in segs:
            t = s.text.strip()
            if not t:
                continue
            dur = max(s.end - s.start, 0.3)
            utt.append({"role": role, "start": round(s.start, 1), "end": round(s.end, 1), "text": t, "words_per_sec": round(len(t.split()) / dur, 2),
                        **prosody(audio[int(s.start * 16000):int(s.end * 16000)])})
    utt.sort(key=lambda u: u["start"])
    return {"utterances": utt, "dialog": [["client" if u["role"] == "client" else "agent", u["text"]] for u in utt],
            "duration_s": round(len(left) / 16000, 1), "model": config.WHISPER_MODEL}

"""Deterministic single-speaker acoustic measurements for decoded audio."""

from dataclasses import dataclass
from io import BytesIO

import librosa
import numpy as np


@dataclass(frozen=True)
class AudioMetrics:
    average_pitch_hz: float | None
    estimated_snr_db: float | None


def analyze_audio(audio_bytes: bytes) -> AudioMetrics:
    """Measure mean voiced pitch and estimate SNR from short-time energy.

    The SNR estimate treats the lowest-energy 20% of non-silent frames
    as a background-noise proxy and the highest-energy 20% as speech plus
    noise. We subtract estimated noise power before taking their ratio.
    This is not ground-truth SNR and assumes a reasonably stationary noise
    floor with both quieter and voiced portions present.
    """
    try:
        waveform, sample_rate = librosa.load(BytesIO(audio_bytes), sr=None, mono=True)
    except Exception as exc:  # decoding backends raise format-specific exceptions
        raise ValueError("Unable to decode audio bytes") from exc

    samples = np.asarray(waveform, dtype=np.float64)
    if samples.size == 0 or not np.all(np.isfinite(samples)):
        return AudioMetrics(None, None)
    if np.max(np.abs(samples)) < 1e-8:
        return AudioMetrics(None, None)

    pitch = None
    try:
        frame_length = 2048
        if samples.size >= frame_length and sample_rate > 2 * librosa.note_to_hz("C2"):
            f0, voiced, _ = librosa.pyin(
                samples, sr=sample_rate, fmin=librosa.note_to_hz("C2"),
                fmax=min(librosa.note_to_hz("C7"), sample_rate * 0.45),
                frame_length=frame_length,
            )
            valid = f0[np.asarray(voiced, dtype=bool) & np.isfinite(f0)]
            if valid.size:
                pitch = float(np.mean(valid))
    except (ValueError, librosa.ParameterError):
        pass

    frame_length = max(256, min(2048, samples.size))
    hop_length = max(1, frame_length // 4)
    rms = librosa.feature.rms(\n        y=samples, frame_length=frame_length, hop_length=hop_length, center=False\n    )[0]
    power = np.square(rms)
    power = power[np.isfinite(power)]
    snr = None
    if power.size >= 5:
        noise = float(np.quantile(power, 0.2))
        speech = float(np.quantile(power, 0.8))
        if noise > 1e-12 and speech > noise * 1.05:
            ratio = (speech - noise) / noise
            candidate = 10.0 * np.log10(ratio)
            if np.isfinite(candidate):
                snr = float(candidate)

    return AudioMetrics(pitch, snr)

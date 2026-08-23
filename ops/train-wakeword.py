#!/usr/bin/env python3
"""Train a reproducible Miso openWakeWord head from bilingual synthetic speech."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
from onnx import TensorProto, helper, numpy_helper
from openwakeword.utils import AudioFeatures
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler


SAMPLE_RATE = 16_000
CLIP_SAMPLES = 32_000
FEATURE_FRAMES = 16
FEATURE_WIDTH = 96


@dataclass(frozen=True, slots=True)
class Voice:
    name: str
    language: str


TRAIN_VOICES = (
    Voice("Daniel", "en"),
    Voice("Karen", "en"),
    Voice("Moira", "en"),
    Voice("Rishi", "en"),
    Voice("Samantha", "en"),
    Voice("Tessa", "en"),
    Voice("Fred", "en"),
    Voice("Kathy", "en"),
    Voice("Mónica", "es"),
    Voice("Paulina", "es"),
    Voice("Flo (Spanish (Spain))", "es"),
    Voice("Flo (Spanish (Mexico))", "es"),
    Voice("Reed (Spanish (Spain))", "es"),
    Voice("Reed (Spanish (Mexico))", "es"),
)

TEST_VOICES = (
    Voice("Eddy (English (UK))", "en"),
    Voice("Eddy (English (US))", "en"),
    Voice("Grandma (English (UK))", "en"),
    Voice("Grandpa (English (US))", "en"),
    Voice("Sandy (English (UK))", "en"),
    Voice("Shelley (English (US))", "en"),
    Voice("Eddy (Spanish (Spain))", "es"),
    Voice("Eddy (Spanish (Mexico))", "es"),
    Voice("Grandma (Spanish (Spain))", "es"),
    Voice("Grandpa (Spanish (Mexico))", "es"),
    Voice("Sandy (Spanish (Spain))", "es"),
    Voice("Shelley (Spanish (Mexico))", "es"),
)

POSITIVE_PHRASES = {
    "en": ("Miso", "Hey Miso", "Okay Miso", "Miso please"),
    "es": ("Miso", "Hola Miso", "Oye Miso", "Miso por favor"),
}

NEGATIVE_PHRASES = {
    "en": (
        "Milo",
        "Mia",
        "Mika",
        "Misha",
        "Mason",
        "missile",
        "missing",
        "music",
        "me so happy",
        "miso soup",
        "permission",
        "assistant",
        "set a timer",
        "turn on the kitchen light",
        "add milk to the shopping list",
        "what time is it",
    ),
    "es": (
        "Milo",
        "Mia",
        "mismo",
        "misa",
        "piso",
        "quiso",
        "hizo",
        "aviso",
        "permiso",
        "preciso",
        "sumiso",
        "sopa de miso",
        "asistente",
        "pon un temporizador",
        "enciende la luz de la cocina",
        "añade leche a la lista de compras",
    ),
}


@dataclass(frozen=True, slots=True)
class BaseClip:
    path: Path
    language: str
    phrase: str


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--negative-features", type=Path, required=True)
    parser.add_argument("--positive-train", type=int, default=20_000)
    parser.add_argument("--confusable-train", type=int, default=10_000)
    parser.add_argument("--general-negative-train", type=int, default=20_000)
    parser.add_argument("--positive-test", type=int, default=2_000)
    parser.add_argument("--confusable-test", type=int, default=2_000)
    parser.add_argument("--seed", type=int, default=20260823)
    return parser.parse_args()


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL)


def _slug(voice: Voice, rate: int, phrase: str) -> str:
    digest = hashlib.sha256(
        f"{voice.name}\0{voice.language}\0{rate}\0{phrase}".encode()
    ).hexdigest()[:16]
    return f"{voice.language}-{digest}"


def synthesize_bases(
    root: Path,
    voices: tuple[Voice, ...],
    rates: tuple[int, ...],
    phrases: dict[str, tuple[str, ...]],
) -> list[BaseClip]:
    root.mkdir(parents=True, exist_ok=True)
    result: list[BaseClip] = []
    pending: list[tuple[Voice, int, str, Path]] = []
    for voice in voices:
        for rate in rates:
            for phrase in phrases[voice.language]:
                name = _slug(voice, rate, phrase)
                wav_path = root / f"{name}.wav"
                if not wav_path.is_file():
                    pending.append((voice, rate, phrase, wav_path))
                result.append(BaseClip(wav_path, voice.language, phrase))
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_synthesize_one, root, voice, rate, phrase, wav_path)
            for voice, rate, phrase, wav_path in pending
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            future.result()
            if completed % 50 == 0 or completed == len(futures):
                print(
                    f"synthesized {completed}/{len(futures)} new clips "
                    f"in {root.name}",
                    flush=True,
                )
    return result


def _synthesize_one(
    root: Path,
    voice: Voice,
    rate: int,
    phrase: str,
    wav_path: Path,
) -> None:
    aiff_path = root / f"{wav_path.stem}.aiff"
    _run(
        [
            "/usr/bin/say",
            "-v",
            voice.name,
            "-r",
            str(rate),
            "-o",
            str(aiff_path),
            phrase,
        ]
    )
    _run(
        [
            "/opt/homebrew/bin/ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(aiff_path),
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ]
    )
    aiff_path.unlink()


def load_trimmed(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as source:
        if (
            source.getframerate() != SAMPLE_RATE
            or source.getnchannels() != 1
            or source.getsampwidth() != 2
        ):
            raise ValueError(f"invalid synthesized WAV: {path}")
        pcm = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
    peak = int(np.max(np.abs(pcm.astype(np.int32)))) if pcm.size else 0
    threshold = max(100, round(peak * 0.0125))
    active = np.flatnonzero(np.abs(pcm.astype(np.int32)) >= threshold)
    if not active.size:
        raise ValueError(f"synthesized WAV is silent: {path}")
    padding = round(SAMPLE_RATE * 0.04)
    start = max(0, int(active[0]) - padding)
    end = min(len(pcm), int(active[-1]) + padding + 1)
    return pcm[start:end].astype(np.float32)


def augment(
    signal: np.ndarray,
    rng: np.random.Generator,
    *,
    far_field: bool = False,
) -> np.ndarray:
    maximum = round(SAMPLE_RATE * 1.55)
    if len(signal) > maximum:
        signal = signal[:maximum]
    value = signal.copy()
    if rng.random() < 0.85:
        reverbed = value.copy()
        for _ in range(int(rng.integers(1, 4))):
            delay = int(rng.integers(240, 2_000))
            gain = float(rng.uniform(0.08, 0.35 if not far_field else 0.5))
            if delay < len(value):
                reverbed[delay:] += value[:-delay] * gain
        value = reverbed
    gain = float(rng.uniform(0.2, 0.65) if far_field else rng.uniform(0.45, 1.15))
    value *= gain
    clip = np.zeros(CLIP_SAMPLES, dtype=np.float32)
    desired_end = int(
        rng.uniform(1.58, 1.88 if not far_field else 1.82) * SAMPLE_RATE
    )
    start = max(0, desired_end - len(value))
    value = value[-desired_end:] if len(value) > desired_end else value
    clip[start : start + len(value)] = value
    signal_rms = math.sqrt(float(np.mean(value * value))) if value.size else 1.0
    snr_db = float(rng.uniform(6, 20) if far_field else rng.uniform(12, 38))
    noise_rms = max(8.0, signal_rms / (10 ** (snr_db / 20)))
    noise = rng.normal(0, noise_rms, CLIP_SAMPLES).astype(np.float32)
    if rng.random() < 0.7:
        noise = np.convolve(noise, np.ones(5, dtype=np.float32) / 5, mode="same")
        current = math.sqrt(float(np.mean(noise * noise)))
        noise *= noise_rms / max(current, 1e-6)
    clip += noise
    return np.clip(np.rint(clip), -32_768, 32_767).astype(np.int16)


def make_feature_set(
    preprocessor: AudioFeatures,
    bases: list[BaseClip],
    count: int,
    rng: np.random.Generator,
    *,
    batch_size: int = 128,
) -> tuple[np.ndarray, np.ndarray]:
    loaded = {base.path: load_trimmed(base.path) for base in bases}
    features = np.empty((count, FEATURE_FRAMES, FEATURE_WIDTH), dtype=np.float32)
    languages = np.empty(count, dtype="U2")
    completed = 0
    started = time.monotonic()
    while completed < count:
        size = min(batch_size, count - completed)
        clips = np.empty((size, CLIP_SAMPLES), dtype=np.int16)
        for index in range(size):
            base = bases[int(rng.integers(0, len(bases)))]
            clips[index] = augment(loaded[base.path], rng)
            languages[completed + index] = base.language
        features[completed : completed + size] = preprocessor.embed_clips(
            clips, batch_size=batch_size, ncpu=1
        )
        completed += size
        if completed % 1_024 == 0 or completed == count:
            elapsed = max(0.001, time.monotonic() - started)
            print(
                f"embedded {completed}/{count} clips "
                f"({completed / elapsed:.1f} clips/s)",
                flush=True,
            )
    return features, languages


def general_negative_windows(
    path: Path,
    count: int,
    rng: np.random.Generator,
    *,
    train_fraction: float = 0.8,
) -> tuple[np.ndarray, np.ndarray]:
    frames = np.load(path, mmap_mode="r")
    if frames.ndim != 2 or frames.shape[1] != FEATURE_WIDTH:
        raise ValueError("general negative features must have shape (frames, 96)")
    split = int(len(frames) * train_fraction)
    starts = rng.integers(0, split - FEATURE_FRAMES, size=count)
    offsets = np.arange(FEATURE_FRAMES)
    train = np.asarray(frames[starts[:, None] + offsets], dtype=np.float32)
    return train, frames[split:]


def export_onnx(
    path: Path,
    scaler: StandardScaler,
    classifier: MLPClassifier,
) -> None:
    if len(classifier.coefs_) != 2 or classifier.coefs_[1].shape[1] != 1:
        raise ValueError("expected a one-hidden-layer binary MLP")
    initializers = [
        numpy_helper.from_array(scaler.mean_.astype(np.float32), "scale_mean"),
        numpy_helper.from_array(scaler.scale_.astype(np.float32), "scale_std"),
        numpy_helper.from_array(classifier.coefs_[0].astype(np.float32), "weight_1"),
        numpy_helper.from_array(classifier.intercepts_[0].astype(np.float32), "bias_1"),
        numpy_helper.from_array(classifier.coefs_[1].astype(np.float32), "weight_2"),
        numpy_helper.from_array(classifier.intercepts_[1].astype(np.float32), "bias_2"),
    ]
    nodes = [
        helper.make_node("Flatten", ["audio_features"], ["flat"], axis=1),
        helper.make_node("Sub", ["flat", "scale_mean"], ["centered"]),
        helper.make_node("Div", ["centered", "scale_std"], ["scaled"]),
        helper.make_node(
            "Gemm", ["scaled", "weight_1", "bias_1"], ["hidden_linear"]
        ),
        helper.make_node("Relu", ["hidden_linear"], ["hidden"]),
        helper.make_node(
            "Gemm", ["hidden", "weight_2", "bias_2"], ["output_linear"]
        ),
        helper.make_node("Sigmoid", ["output_linear"], ["miso"]),
    ]
    graph = helper.make_graph(
        nodes,
        "miso_openwakeword",
        [
            helper.make_tensor_value_info(
                "audio_features",
                TensorProto.FLOAT,
                [1, FEATURE_FRAMES, FEATURE_WIDTH],
            )
        ],
        [helper.make_tensor_value_info("miso", TensorProto.FLOAT, [1, 1])],
        initializer=initializers,
    )
    model = helper.make_model(
        graph,
        producer_name="miso-local-wake-trainer",
        opset_imports=[helper.make_opsetid("", 13)],
    )
    model.ir_version = 8
    onnx.checker.check_model(model)
    onnx.save(model, path)


def predict(
    classifier: MLPClassifier, scaler: StandardScaler, x: np.ndarray
) -> np.ndarray:
    flat = x.reshape(len(x), -1)
    return classifier.predict_proba(scaler.transform(flat))[:, 1]


def heldout_negative_scores(
    classifier: MLPClassifier,
    scaler: StandardScaler,
    frames: np.ndarray,
    *,
    batch_size: int = 4_096,
) -> np.ndarray:
    count = len(frames) - FEATURE_FRAMES + 1
    scores = np.empty(count, dtype=np.float32)
    offsets = np.arange(FEATURE_FRAMES)
    for start in range(0, count, batch_size):
        size = min(batch_size, count - start)
        indices = np.arange(start, start + size)[:, None] + offsets
        windows = np.asarray(frames[indices], dtype=np.float32)
        scores[start : start + size] = predict(classifier, scaler, windows)
    return scores


def activation_count(
    scores: np.ndarray,
    threshold: float,
    activation_frames: int,
    cooldown_seconds: float,
) -> int:
    cooldown_frames = math.ceil(cooldown_seconds / 0.08)
    cooldown_until = 0
    streak = 0
    activations = 0
    for index, score in enumerate(scores):
        if score < threshold:
            streak = 0
            continue
        streak += 1
        if streak < activation_frames:
            continue
        streak = 0
        if index < cooldown_until:
            continue
        activations += 1
        cooldown_until = index + cooldown_frames
    return activations


def evaluate(
    positive_scores: np.ndarray,
    positive_languages: np.ndarray,
    confusable_scores: np.ndarray,
    general_scores: np.ndarray,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    negative_hours = len(general_scores) * 0.08 / 3_600
    candidates: list[dict[str, object]] = []
    thresholds = np.concatenate(
        (
            np.arange(0.1, 0.951, 0.025),
            np.arange(0.96, 0.991, 0.01),
            np.asarray((0.995, 0.9975, 0.999, 0.9995, 0.9999)),
        )
    )
    for threshold in thresholds:
        # Independent positive clips validate a single score window. Consecutive
        # frame policies require streaming-WAV validation and must not be chosen
        # from this feature-only matrix.
        for frames in (1,):
            recall = float(np.mean(positive_scores >= threshold))
            language_recall = {
                language: float(
                    np.mean(
                        positive_scores[positive_languages == language] >= threshold
                    )
                )
                for language in ("en", "es")
            }
            activations = activation_count(
                general_scores, float(threshold), frames, 2.0
            )
            candidates.append(
                {
                    "threshold": round(float(threshold), 3),
                    "activation_frames": frames,
                    "recall": round(recall, 4),
                    "language_recall": {
                        key: round(value, 4) for key, value in language_recall.items()
                    },
                    "confusable_false_positive_rate": round(
                        float(np.mean(confusable_scores >= threshold)), 4
                    ),
                    "general_false_activations": activations,
                    "general_negative_hours": round(negative_hours, 4),
                    "false_activations_per_hour": round(
                        activations / negative_hours, 4
                    ),
                }
            )
    passing = [
        item
        for item in candidates
        if item["recall"] >= 0.8
        and min(item["language_recall"].values()) >= 0.8
        and item["false_activations_per_hour"] <= 0.5
    ]
    if passing:
        selected = max(
            passing,
            key=lambda item: (
                min(item["language_recall"].values()),
                item["recall"],
                -item["activation_frames"],
                -item["threshold"],
            ),
        )
    else:
        selected = max(
            candidates,
            key=lambda item: (
                min(item["language_recall"].values())
                - min(1.0, item["false_activations_per_hour"] / 10),
                -item["confusable_false_positive_rate"],
            ),
        )
    return selected, candidates


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    options = arguments()
    root = options.output_directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    positive_train_bases = synthesize_bases(
        root / "bases" / "positive-train",
        TRAIN_VOICES,
        (150, 175, 200, 225),
        POSITIVE_PHRASES,
    )
    negative_train_bases = synthesize_bases(
        root / "bases" / "negative-train",
        TRAIN_VOICES,
        (160, 195, 230),
        NEGATIVE_PHRASES,
    )
    positive_test_bases = synthesize_bases(
        root / "bases" / "positive-test",
        TEST_VOICES,
        (162, 187, 212),
        POSITIVE_PHRASES,
    )
    negative_test_bases = synthesize_bases(
        root / "bases" / "negative-test",
        TEST_VOICES,
        (168, 205),
        NEGATIVE_PHRASES,
    )
    print(
        json.dumps(
            {
                "positive_train_bases": len(positive_train_bases),
                "negative_train_bases": len(negative_train_bases),
                "positive_test_bases": len(positive_test_bases),
                "negative_test_bases": len(negative_test_bases),
            }
        ),
        flush=True,
    )

    preprocessor = AudioFeatures(inference_framework="onnx")
    positive_train, _ = make_feature_set(
        preprocessor,
        positive_train_bases,
        options.positive_train,
        np.random.default_rng(options.seed + 1),
    )
    confusable_train, _ = make_feature_set(
        preprocessor,
        negative_train_bases,
        options.confusable_train,
        np.random.default_rng(options.seed + 2),
    )
    positive_test, positive_languages = make_feature_set(
        preprocessor,
        positive_test_bases,
        options.positive_test,
        np.random.default_rng(options.seed + 3),
    )
    confusable_test, _ = make_feature_set(
        preprocessor,
        negative_test_bases,
        options.confusable_test,
        np.random.default_rng(options.seed + 4),
    )
    general_train, general_heldout = general_negative_windows(
        options.negative_features,
        options.general_negative_train,
        np.random.default_rng(options.seed + 5),
    )

    x = np.concatenate((positive_train, confusable_train, general_train))
    y = np.concatenate(
        (
            np.ones(len(positive_train), dtype=np.uint8),
            np.zeros(len(confusable_train) + len(general_train), dtype=np.uint8),
        )
    )
    order = np.random.default_rng(options.seed + 6).permutation(len(x))
    x = x[order].reshape(len(x), -1)
    y = y[order]
    scaler = StandardScaler()
    x = scaler.fit_transform(x).astype(np.float32, copy=False)
    classifier = MLPClassifier(
        hidden_layer_sizes=(64,),
        activation="relu",
        solver="adam",
        batch_size=256,
        learning_rate_init=0.001,
        max_iter=100,
        early_stopping=True,
        validation_fraction=0.1,
        n_iter_no_change=8,
        random_state=options.seed,
        verbose=True,
    )
    classifier.fit(x, y)
    del x, y, positive_train, confusable_train, general_train

    model_path = root / "miso.onnx"
    export_onnx(model_path, scaler, classifier)
    positive_scores = predict(classifier, scaler, positive_test)
    confusable_scores = predict(classifier, scaler, confusable_test)
    general_scores = heldout_negative_scores(classifier, scaler, general_heldout)
    selected, candidates = evaluate(
        positive_scores,
        positive_languages,
        confusable_scores,
        general_scores,
    )

    session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    comparison = session.run(
        None, {"audio_features": positive_test[:1].astype(np.float32)}
    )[0][0][0]
    expected = positive_scores[0]
    maximum_delta = abs(float(comparison) - float(expected))
    if maximum_delta > 1e-4:
        raise RuntimeError(f"ONNX export differs from sklearn by {maximum_delta}")

    metadata = {
        "model": model_path.name,
        "sha256": sha256(model_path),
        "openwakeword_version": "0.6.0",
        "seed": options.seed,
        "feature_shape": [FEATURE_FRAMES, FEATURE_WIDTH],
        "training": {
            "positive_samples": options.positive_train,
            "confusable_negative_samples": options.confusable_train,
            "general_negative_samples": options.general_negative_train,
            "positive_base_clips": len(positive_train_bases),
            "confusable_base_clips": len(negative_train_bases),
            "iterations": classifier.n_iter_,
            "loss": round(float(classifier.loss_), 6),
        },
        "heldout": {
            "positive_samples": options.positive_test,
            "confusable_negative_samples": options.confusable_test,
            "general_negative_frames": len(general_scores),
            "general_negative_hours": round(len(general_scores) * 0.08 / 3_600, 4),
            "voices": [voice.name for voice in TEST_VOICES],
        },
        "selected": selected,
        "acceptance_passed": (
            selected["recall"] >= 0.8
            and min(selected["language_recall"].values()) >= 0.8
            and selected["false_activations_per_hour"] <= 0.5
        ),
        "onnx_sklearn_maximum_delta": maximum_delta,
        "threshold_candidates": candidates,
    }
    (root / "metrics.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(metadata["selected"], indent=2, sort_keys=True))
    print(f"model: {model_path}")
    print(f"sha256: {metadata['sha256']}")
    return 0 if metadata["acceptance_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

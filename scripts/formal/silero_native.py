"""Compare the production Silero adapter with direct bundled ONNX execution."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys


def run_native() -> dict:
    import numpy as np
    from faster_whisper.vad import get_vad_model
    from server.audio.vad import SileroVAD

    session = get_vad_model().session
    inputs = [(item.name, item.shape, item.type) for item in session.get_inputs()]
    outputs = [(item.name, item.shape, item.type) for item in session.get_outputs()]
    expected_inputs = [
        ("input", ["seq_len", 576], "tensor(float)"),
        ("h", [1, 1, 128], "tensor(float)"),
        ("c", [1, 1, 128], "tensor(float)"),
    ]
    if inputs != expected_inputs:
        raise RuntimeError(f"Silero ONNX input contract changed: {inputs}")
    if [item[0] for item in outputs] != ["speech_probs", "hn", "cn"]:
        raise RuntimeError(f"Silero ONNX output names changed: {outputs}")
    if any(item[2] != "tensor(float)" for item in outputs):
        raise RuntimeError(f"Silero ONNX output dtype changed: {outputs}")
    if any(item[1] != [1, 1, 128] for item in outputs[1:]):
        raise RuntimeError(f"Silero ONNX state shape changed: {outputs}")

    ramp = np.linspace(-32768, 32767, 512).astype(np.int16)
    boundaries = np.resize(np.array([-32768, -1, 0, 1, 32767], dtype=np.int16), 512)
    wave = (12000 * np.sin(np.arange(512) * 2 * np.pi * 440 / 16000)).astype(np.int16)
    frames = [np.zeros(512, dtype=np.int16), ramp, boundaries, wave,
              np.full(512, 32767, dtype=np.int16),
              np.full(512, -32768, dtype=np.int16), np.zeros(512, dtype=np.int16)]
    adapter = SileroVAD(0.5)
    observations = []
    first_pass = None
    for replay in range(2):
        if replay:
            adapter.reset()
            for name in ("_h", "_c", "_context"):
                value = getattr(adapter, name)
                if value.dtype != np.float32 or np.any(value):
                    raise AssertionError(f"native_reset: {name} was not zeroed")
        h = np.zeros((1, 1, 128), dtype=np.float32)
        c = np.zeros((1, 1, 128), dtype=np.float32)
        context = np.zeros(64, dtype=np.float32)
        probabilities = []
        for index, frame in enumerate(frames):
            normalized = frame.astype(np.float32) * np.float32(1 / 32768)
            model_input = np.empty((1, 576), dtype=np.float32)
            model_input[0, :64] = context
            model_input[0, 64:] = normalized
            probability, h, c = session.run(
                ["speech_probs", "hn", "cn"], {"input": model_input, "h": h, "c": c},
            )
            if probability.size != 1 or not np.isfinite(probability).all():
                raise AssertionError(f"native_probability: frame {index}")
            expected = float(probability.reshape(-1)[0])
            if not 0 <= expected <= 1:
                raise AssertionError(f"native_probability_range: {expected}")
            actual = adapter.is_speech(frame)
            if actual != (expected > 0.5):
                raise AssertionError(f"native_classification: frame {index}")
            context = normalized[448:].copy()
            for name, wanted in (("_h", h), ("_c", c), ("_context", context)):
                value = getattr(adapter, name)
                if value.dtype != np.float32 or value.shape != wanted.shape:
                    raise AssertionError(f"native_state_contract: {name}, frame {index}")
                if not np.isfinite(value).all() or not np.array_equal(value, wanted):
                    raise AssertionError(f"native_state_value: {name}, frame {index}")
            probabilities.append(expected)
        if first_pass is None:
            first_pass = probabilities
        elif probabilities != first_pass:
            raise AssertionError("native_reset_replay: probability sequence changed")
        observations.append({"frames": len(frames), "probabilities": probabilities})

    model_path = Path(session._model_path)
    return {
        "status": "PASS",
        "versions": {name: importlib.metadata.version(name)
                     for name in ("numpy", "faster-whisper", "onnxruntime")},
        "model": model_path.name,
        "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "inputs": inputs,
        "outputs": outputs,
        "replays": observations,
        "reset_replay_equal": True,
        "scope": "14 fixed synthetic frames; adapter equivalence, not speech accuracy or runtime performance",
    }


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    print(json.dumps(run_native(), ensure_ascii=False, indent=2))

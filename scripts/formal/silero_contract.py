"""Check the real Silero adapter against an independent ONNX input ledger."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import itertools
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import numpy as np

from lesson_liveness import Violation


def require(condition, property_name, expected, actual, trace):
    if not condition:
        raise Violation(property_name, expected, actual, list(trace))


def normalized(frame):
    result = np.zeros(512, dtype=np.float32)
    count = min(len(frame), 512)
    result[:count] = np.asarray(frame[:count], dtype=np.float32) / np.float32(32768)
    return result


class ContractSession:
    def __init__(self, expected_frames=None, probability=None, trace=None):
        self.expected_frames = expected_frames if expected_frames is not None else deque()
        self.probability = probability
        self.trace = trace if trace is not None else []
        self.calls = 0
        self.reset_expected()

    def expect(self, frame):
        self.expected_frames.append(np.array(frame, dtype=np.int16, copy=True))

    def reset_expected(self):
        self.expected_h = np.zeros((1, 1, 128), dtype=np.float32)
        self.expected_c = np.zeros((1, 1, 128), dtype=np.float32)
        self.expected_context = np.zeros(64, dtype=np.float32)

    def _array(self, property_name, expected, actual):
        valid = (isinstance(actual, np.ndarray) and actual.shape == expected.shape
                 and actual.dtype == expected.dtype and np.array_equal(actual, expected))
        if valid:
            return
        require(False, property_name,
                dict(shape=list(expected.shape), dtype=str(expected.dtype), head=expected.flatten()[:8].tolist()),
                dict(shape=list(np.shape(actual)), dtype=str(getattr(actual, "dtype", None)),
                     head=np.asarray(actual).flatten()[:8].tolist()), self.trace)

    def run(self, output_names, inputs):
        require(bool(self.expected_frames), "silero_frame_delivery", "a ledger frame for every ONNX call",
                dict(calls=self.calls + 1, pending=len(self.expected_frames)), self.trace)
        require(output_names is None and set(inputs) == {"input", "h", "c"},
                "silero_frame_delivery", "input, h, c and all outputs", list(inputs), self.trace)
        frame = self.expected_frames.popleft()
        audio = normalized(frame)
        model_input = inputs["input"]
        require(isinstance(model_input, np.ndarray) and model_input.shape == (1, 576)
                and model_input.dtype == np.float32, "silero_normalization",
                "float32 input with shape (1, 576)",
                dict(shape=list(np.shape(model_input)), dtype=str(getattr(model_input, "dtype", None))),
                self.trace)
        self._array("silero_normalization", audio, model_input[0, 64:])
        self._array("silero_context", self.expected_context, model_input[0, :64])
        self._array("silero_state_h", self.expected_h, inputs["h"])
        self._array("silero_state_c", self.expected_c, inputs["c"])
        probability = self.probability
        if probability is None:
            probability = 0.9 if np.any(audio) else 0.1
        elif callable(probability):
            probability = probability(frame)
        self.calls += 1
        next_h = np.full((1, 1, 128), self.calls + 0.25, dtype=np.float32)
        next_c = np.full((1, 1, 128), -self.calls - 0.75, dtype=np.float32)
        self.expected_h = next_h.copy()
        self.expected_c = next_c.copy()
        self.expected_context = audio[-64:].copy()
        return np.array([[probability]], dtype=np.float32), next_h, next_c


@contextmanager
def model_scope(session):
    package = ModuleType("faster_whisper")
    package.__path__ = []
    vad = ModuleType("faster_whisper.vad")
    vad.get_vad_model = lambda: SimpleNamespace(session=session)
    package.vad = vad
    with patch.dict(sys.modules, {"faster_whisper": package, "faster_whisper.vad": vad}):
        yield


def sample_frame(kind, size=512):
    if kind == "zero":
        return np.zeros(size, dtype=np.int16)
    if kind == "extrema":
        return np.resize(np.array([-32768, -32767, -1, 0, 1, 32766, 32767], dtype=np.int16), size)
    if kind == "tail":
        return ((np.arange(size, dtype=np.int32) * 113 + 19) % 65536 - 32768).astype(np.int16)
    raise ValueError(kind)


def exercise(adapter, session, kind, probability, *, size=512):
    session.trace.append(dict(operation=kind, size=size, probability=probability))
    session.probability = probability
    session.expect(sample_frame(kind, size))
    actual = adapter.is_speech(sample_frame(kind, size))
    require(not session.expected_frames, "silero_frame_delivery", "ledger frame consumed exactly once",
            len(session.expected_frames), session.trace)
    require(actual == (probability > 0.5), "silero_probability", probability > 0.5, actual, session.trace)


def run_contract(depth=4):
    from server.audio.vad import SileroVAD

    candidates = calls = 0
    probabilities = {"zero": 0.49, "extrema": 0.5, "tail": 0.51}
    for length in range(depth + 1):
        for operations in itertools.product((*probabilities, "reset"), repeat=length):
            session = ContractSession()
            with model_scope(session):
                adapter = SileroVAD(0.5)
            for operation in operations:
                if operation == "reset":
                    session.trace.append("reset")
                    adapter.reset()
                    session.reset_expected()
                else:
                    exercise(adapter, session, operation, probabilities[operation])
            exercise(adapter, session, "tail", 0.51)
            calls += session.calls
            candidates += 1
    padding_sizes = (0, 1, 511, 512, 513, 1024)
    for size in padding_sizes:
        session = ContractSession()
        with model_scope(session):
            adapter = SileroVAD(0.5)
        exercise(adapter, session, "extrema", 0.5)
        exercise(adapter, session, "tail", 0.51, size=size)
        exercise(adapter, session, "zero", 0.49)
        calls += session.calls
    return dict(status="PASS", depth=depth, candidates=candidates, calls=calls,
                padding_cases=len(padding_sizes), padding_sizes=list(padding_sizes),
                alphabet=[*probabilities, "reset"],
                properties=["silero_normalization", "silero_context", "silero_state_h",
                            "silero_state_c", "silero_probability", "silero_frame_delivery"])


MUTATIONS = (
    ("skip_model_call", "silero_frame_delivery", "out, self._h, self._c = self._session.run(",
     "return False\n        out, self._h, self._c = self._session.run("),
    ("normalization_scale", "silero_normalization", "frame.astype(np.float32) / 32768.0",
     "frame.astype(np.float32) / 16384.0"),
    ("drop_context_update", "silero_context", "self._context = audio[-self._CONTEXT_SAMPLES:]",
     "self._context = np.zeros(self._CONTEXT_SAMPLES, dtype=np.float32)"),
    ("drop_hidden_update", "silero_state_h", "out, self._h, self._c = self._session.run(",
     "out, discarded_h, self._c = self._session.run("),
    ("drop_cell_update", "silero_state_c", "out, self._h, self._c = self._session.run(",
     "out, self._h, discarded_c = self._session.run("),
    ("include_threshold", "silero_probability", "> self.threshold\n\n\ndef build_frame_vad",
     ">= self.threshold\n\n\ndef build_frame_vad"),
    ("drop_hidden_reset", "silero_state_h",
     "def reset(self) -> None:\n        self._h = np.zeros((1, 1, 128), dtype=np.float32)",
     "def reset(self) -> None:\n        self._h = self._h"),
    ("drop_cell_reset", "silero_state_c",
     "def reset(self) -> None:\n        self._h = np.zeros((1, 1, 128), dtype=np.float32)\n        self._c = np.zeros((1, 1, 128), dtype=np.float32)",
     "def reset(self) -> None:\n        self._h = np.zeros((1, 1, 128), dtype=np.float32)\n        self._c = self._c"),
    ("drop_context_reset", "silero_context",
     "def reset(self) -> None:\n        self._h = np.zeros((1, 1, 128), dtype=np.float32)\n        self._c = np.zeros((1, 1, 128), dtype=np.float32)\n        self._context = np.zeros(self._CONTEXT_SAMPLES, dtype=np.float32)",
     "def reset(self) -> None:\n        self._h = np.zeros((1, 1, 128), dtype=np.float32)\n        self._c = np.zeros((1, 1, 128), dtype=np.float32)\n        self._context = self._context"),
)

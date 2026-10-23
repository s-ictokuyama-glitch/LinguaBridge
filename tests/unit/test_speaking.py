import pytest

from server.speaking import SpeakingSignal


@pytest.mark.parametrize("enabled", [True, False])
def test_activity_and_settled_suppress_duplicates(enabled):
    signal = SpeakingSignal(enabled)
    assert signal.current is False
    assert signal.on_vad_activity() is None
    assert signal.on_lesson_state("live") is None
    assert signal.on_asr_settled(True, True) is None
    assert signal.on_vad_activity() is (True if enabled else None)
    assert signal.current is enabled
    assert signal.on_vad_activity() is None
    assert signal.on_asr_settled(True, False) is None
    assert signal.on_asr_settled(False, True) is None
    assert signal.on_asr_settled(False, False) is (False if enabled else None)
    assert signal.on_asr_settled(False, False) is None
    assert signal.current is False


@pytest.mark.parametrize("state", ["idle", "paused", "ended"])
def test_leaving_live_turns_off_and_ignores_old_asr(state):
    signal = SpeakingSignal(True)
    signal.on_lesson_state("live")
    signal.on_vad_activity()
    assert signal.on_lesson_state(state) is False
    assert signal.on_vad_activity() is None
    assert signal.on_asr_settled(True, True) is None
    assert signal.on_lesson_state(state) is None


def test_capture_gap_resumes_same_utterance_only_after_new_vad_activity():
    signal = SpeakingSignal(True)
    signal.on_lesson_state("live")
    assert signal.on_vad_activity() is True
    assert signal.on_mic_silent() is False
    assert signal.on_mic_silent() is None
    assert signal.on_asr_settled(True, True) is None
    assert signal.current is False
    assert signal.on_vad_activity() is True
    assert signal.on_asr_settled(True, False) is None
    assert signal.on_asr_settled(False, False) is False


def test_disabled_signal_never_emits_even_after_silence_or_pause():
    signal = SpeakingSignal(False)
    for state in ("live", "paused", "live", "ended"):
        assert signal.on_lesson_state(state) is None
        assert signal.on_vad_activity() is None
        assert signal.on_mic_silent() is None
        assert signal.on_asr_settled(False, False) is None
        assert signal.current is False

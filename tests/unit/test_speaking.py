import pytest

from server.speaking import SpeakingSignal


@pytest.mark.parametrize("enabled", [True, False])
def test_activity_and_settled_suppress_duplicates(enabled):
    signal = SpeakingSignal(enabled)
    assert signal.current is False
    assert signal.on_vad_activity() is None
    assert signal.set_accepting_audio(True) is None
    assert signal.on_asr_settled(True, True) is None
    assert signal.on_vad_activity() is (True if enabled else None)
    assert signal.current is enabled
    assert signal.on_vad_activity() is None
    assert signal.on_asr_settled(True, False) is None
    assert signal.on_asr_settled(False, True) is None
    assert signal.on_asr_settled(False, False) is (False if enabled else None)
    assert signal.on_asr_settled(False, False) is None
    assert signal.current is False


def test_disabling_audio_turns_off_and_ignores_old_asr():
    signal = SpeakingSignal(True)
    signal.set_accepting_audio(True)
    signal.on_vad_activity()
    assert signal.set_accepting_audio(False) is False
    assert signal.on_vad_activity() is None
    assert signal.on_asr_settled(True, True) is None
    assert signal.set_accepting_audio(False) is None


def test_capture_gap_resumes_same_utterance_only_after_new_vad_activity():
    signal = SpeakingSignal(True)
    signal.set_accepting_audio(True)
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
    for accepting in (True, False, True, False):
        assert signal.set_accepting_audio(accepting) is None
        assert signal.on_vad_activity() is None
        assert signal.on_mic_silent() is None
        assert signal.on_asr_settled(False, False) is None
        assert signal.current is False

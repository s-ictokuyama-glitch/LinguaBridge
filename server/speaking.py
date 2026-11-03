

class SpeakingSignal:
    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled
        self._accepting_audio = False
        self._current = False

    @property
    def current(self) -> bool:
        return self._current

    def _change(self, on: bool) -> bool | None:
        on = self._enabled and on
        if self.current == on:
            return None
        self._current = on
        return on

    def on_vad_activity(self) -> bool | None:
        return self._change(self._accepting_audio)

    def on_asr_settled(self, open: bool, has_pending: bool) -> bool | None:
        return self._change(self.current and self._accepting_audio and (open or has_pending))

    def set_accepting_audio(self, accepting: bool) -> bool | None:
        self._accepting_audio = accepting
        return None if accepting else self._change(False)

    def on_mic_silent(self) -> bool | None:
        return self._change(False)

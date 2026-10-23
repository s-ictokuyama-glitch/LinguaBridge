

class SpeakingSignal:
    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled
        self._state = "idle"
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
        return self._change(self._state == "live")

    def on_asr_settled(self, open: bool, has_pending: bool) -> bool | None:
        return self._change(self.current and self._state == "live" and (open or has_pending))

    def on_lesson_state(self, state: str) -> bool | None:
        self._state = state
        return None if state == "live" else self._change(False)

    def on_mic_silent(self) -> bool | None:
        return self._change(False)

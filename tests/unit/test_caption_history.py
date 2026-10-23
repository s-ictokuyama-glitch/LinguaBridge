from server.caption_history import CaptionHistory


def test_sequences_history_bound_and_missing_range():
    history = CaptionHistory(maxlen=3)
    assert history.seq_head == 0
    assert history.history_from == 1
    entries = [history.append(t_start=0, t_end=1, text_ja=str(i), asr_ms=1)
               for i in range(5)]
    assert [u.seq for u in entries] == [1, 2, 3, 4, 5]
    assert history.seq_head == 5
    assert history.history_from == 3
    assert history.since(0) == entries[2:]
    assert history.since(3) == entries[3:]
    assert history.since(5) == []
    assert history.since(4)[0] is entries[4]


def test_zero_length_history_still_assigns_sequences():
    history = CaptionHistory(maxlen=0)
    assert history.append(t_start=0, t_end=1, text_ja="一", asr_ms=1).seq == 1
    assert history.history_from == 2
    assert history.since(0) == []

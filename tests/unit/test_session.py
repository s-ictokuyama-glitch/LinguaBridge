"""Session（単一ルーム・コード検証・履歴・アクティブ言語）のユニットテスト。"""

from __future__ import annotations

from server.pipeline import Translation, Utterance
from server.session import Client, Session


def make_session() -> Session:
    return Session(join_code="4831", history_len=50)


def student(cid: str, lang: str) -> Client:
    return Client(id=cid, role="student", lang=lang, ws=None)


def make_utterance(seq: int, text: str, langs: dict[str, str]) -> Utterance:
    utt = Utterance(seq=seq, t_start=0.0, t_end=1.0, text_ja=text, asr_ms=10)
    for lang, translated in langs.items():
        utt.translations[lang] = Translation(lang=lang, text=translated, engine="fake", mt_ms=1)
    return utt


def test_check_code():
    s = make_session()
    assert s.check_code("4831")
    assert not s.check_code("0000")


def test_active_langs_reflects_students_only():
    s = make_session()
    assert s.active_langs() == set()
    s.add_client(student("a", "en"))
    s.add_client(student("b", "zh"))
    s.add_client(Client(id="t", role="teacher", lang=None, ws=None))
    assert s.active_langs() == {"en", "zh"}
    s.remove_client("b")
    assert s.active_langs() == {"en"}


def test_history_since_filters_by_seq_and_lang():
    s = make_session()
    s.add_history(make_utterance(1, "一", {"en": "[en] 一"}))
    s.add_history(make_utterance(2, "二", {"en": "[en] 二", "zh": "[zh] 二"}))
    s.add_history(make_utterance(3, "三", {"zh": "[zh] 三"}))

    en_since_1 = s.history_since(1, "en")
    assert [(u.seq, tr.text) for u, tr in en_since_1] == [(2, "[en] 二")]

    zh_all = s.history_since(0, "zh")
    assert [u.seq for u, _ in zh_all] == [2, 3]

    assert s.history_since(3, "zh") == []


def test_next_seq_monotonic():
    s = make_session()
    assert s.next_seq() == 1
    assert s.next_seq() == 2
    assert s.seq_head == 2

"""授業記録の保存（イシュー#18 / F-10）。

先生トグルがONの間に確定した発話を蓄積し、セッション終了時にローカルへ書き出す。
機械可読な JSONL（発話ごとの原文・全訳文・タイムスタンプ・計測値）と、
閲覧用の Markdown（日本語＋言語別）を出力する。

プライバシー: 記録対象は先生の発話と訳文のみ。生徒に関する情報（接続元・
言語選択等の個人に紐づく情報）は一切含めない（Utterance のフィールドのみ直列化）。
クラウド送信は行わない（ローカルのファイル書き出しのみ）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.pipeline import Utterance


class SessionRecorder:
    """セッション中の発話を蓄積し、終了時にファイル出力する。

    session.history は再送用に直近K件へ丸められるため、記録はここで
    独立に全件を保持する（長い授業でも欠落しない）。
    """

    def __init__(self, out_dir: Path, languages: list[str]) -> None:
        self._out_dir = out_dir
        self._languages = languages
        self._utterances: list[Utterance] = []
        self._written = False

    def add(self, utterance: Utterance) -> None:
        """記録ON中に確定した発話を蓄積する（訳文は後から同じオブジェクトに入る）。"""
        self._utterances.append(utterance)

    @property
    def has_entries(self) -> bool:
        return bool(self._utterances)

    def _entry(self, utt: Utterance) -> dict:
        # 生徒情報は一切含めない。Utterance の内容のみ
        return {
            "seq": utt.seq,
            "created_at": utt.created_at.isoformat(),
            "t_start": round(utt.t_start, 2),
            "t_end": round(utt.t_end, 2),
            "ja": utt.text_ja,
            "asr_ms": utt.asr_ms,
            "translations": {
                lang: {"text": tr.text, "engine": tr.engine, "mt_ms": tr.mt_ms}
                for lang, tr in sorted(utt.translations.items())
            },
        }

    def write(self, started_at: datetime) -> Path | None:
        """蓄積した発話を started_at 名のフォルダに書き出す。空なら None（何も書かない）。"""
        if self._written or not self._utterances:
            return None
        self._written = True
        session_dir = self._out_dir / started_at.strftime("%Y-%m-%d_%H%M")
        session_dir.mkdir(parents=True, exist_ok=True)

        with (session_dir / "transcript.jsonl").open("w", encoding="utf-8") as f:
            for utt in self._utterances:
                f.write(json.dumps(self._entry(utt), ensure_ascii=False) + "\n")

        self._write_markdown(
            session_dir / "transcript.ja.md", "日本語（原文）", lambda u: u.text_ja
        )
        for lang in self._languages:
            # その言語の訳文が1件も無ければファイルを作らない（選択者0名の言語）
            if not any(lang in u.translations for u in self._utterances):
                continue
            self._write_markdown(
                session_dir / f"transcript.{lang}.md",
                lang,
                lambda u, lang=lang: (
                    u.translations[lang].text if lang in u.translations else None
                ),
            )
        return session_dir

    def _write_markdown(self, path: Path, title: str, getter) -> None:
        lines = [f"# 授業記録（{title}）", ""]
        for utt in self._utterances:
            text = getter(utt)
            if not text:
                continue
            ts = utt.created_at.strftime("%H:%M:%S")
            lines.append(f"- **{ts}** {text}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

# 先生の授業操作

先生はマイクを許可して開始し、一時停止、再開、終了を操作する。生徒のバナーが同じ状態へ変化する。

## Sub-features

- `lesson-start` は開始から配信中へ進む。
- `lesson-pause` は一時停止と再開を行う。
- `lesson-end-live` と `lesson-end-paused` は両状態から終了する。
- `lesson-monitoring` はマイク入力メーター、生徒数・言語内訳、遅延の内訳・翻訳キャッシュ、無音・過負荷警告を扱う。
- `lesson-partial` は任意設定の先生専用暫定文字起こしを扱う。
- `lesson-microphone` はマイク選択、拒否後の再試行を扱う。
- `lesson-teacher-reconnect` は先生切断時の自動一時停止と、先生復帰時の自動再開を扱う。

## How to get to it (user POV)

このPCの `/teacher` を開き、開始、一時停止、終了を押す。入力機器は `#mic-select` で選ぶ。

## Driving it with Playwright

Preconditions:

- 独立した先生・生徒contextが参加済み。実経路ではマイクと検証用発話が必要。

- **開始。** `teacher.locator('#start-btn').click()`。マイク許可後、先生 `#session-state` が `配信中`、英語生徒 `#session-banner` が `Live`。発話後の字幕まで確認する。
- **停止と再開。** `teacher.locator('#pause-btn').click()`。先生は `一時停止中`、生徒は `Paused`。`#pause-btn` がdisabled、`#start-btn` が「再開」になったことを確認し、`teacher.locator('#start-btn').click()` で再開する。停止直前の未確定発話は確定字幕になる場合がある。停止後の音声が字幕へ追加されず、再開後の新しい発話が届くことを確認する。
- **終了。** `teacher.locator('#end-btn').click()`。先生は `終了`、生徒は `Class has ended`。配信中と一時停止中の各入口を別runで実行する。
- **先生の切断と復帰。** 英語生徒が `Live` の間に先生だけのcontextを `set_offline(True)` にし、`teacher.evaluate('state.ws.close(1000, "verification outage")')` で既存接続を閉じる。生徒が `Paused`、先生の操作ボタンがdisabledになることを確認する。`set_offline(False)` 後、先生の自動再接続と生徒の `Live` 復帰を待つ。先生が手動で一時停止した授業は自動再開の対象にしない。補助ケースは `.venv/Scripts/python.exe scripts/qa_native_browser.py --scenario native_teacher_disconnect_flush_and_resume`。
- **補助ケース。** `.venv/Scripts/python.exe scripts/qa_native_browser.py --story TEACH-03 --story TEACH-04` は停止・終了のサーバー契約を確認する。`.venv/Scripts/python.exe scripts/qa_native_browser.py --scenario native_mic_denied_same_tab_retry_to_caption` は権限拒否と再試行を模擬する。
- **ブラウザー音声API。** `.venv/Scripts/python.exe scripts/qa_native_browser.py --scenario native_http_audio_worklet` は実getUserMedia・AudioWorklet・WSへChromiumの合成入力を通す。
- **状況と警告。** 配信中に `#mic-meter-bar`、`#stat-students`、`#stat-langs` と遅延内訳を読み、入力と参加者の変化に追従することを確認する。補助ケース `.venv/Scripts/python.exe scripts/qa_native_browser.py --scenario native_silence_warning_lifecycle_and_swap --scenario native_overload_audio_and_mt_then_clear` で無音・過負荷警告の表示と解除を確認する。閾値は実効 `monitoring` 設定を読む。
- **暫定文字起こし。** `partial.enabled` がONの設定と、確定前に暫定認識できる音声が必要。先生の `#partial` が更新され、確定・停止・終了時に消えることを確認する。既定OFFでは到達できない。`.venv/Scripts/python.exe scripts/qa_native_browser.py --scenario native_partial_on_replaces_final_pause --scenario native_partial_off_is_default` はfake ASRによる表示契約と既定設定の補助確認で、実認識の証明ではない。

## Gotchas

- 先生を別タブで開くと後勝ちになり、古いタブはcode 4000で切断される。単なる観測用に先生contextを増やさない。
- マイクにはsecure contextが必要。このPCのloopback HTTPか、証明書を準備したHTTPSを使う。
- 合成入力のケースは物理マイクや実ASR精度を証明しない。開始表示だけで収音成功としない。

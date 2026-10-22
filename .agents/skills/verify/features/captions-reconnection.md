# 字幕と接続復元

生徒は選択言語の字幕を受信する。切断時には再接続表示が出て、復帰後は欠けた字幕を順序通りに受け取る。

## Sub-features

- `caption-delivery` は先生の発話から生徒の訳文を表示する。
- `caption-speaking` は生徒の発話中表示を扱う。
- `caption-delay` は処理遅延が5,000ms以上のライブ字幕に遅延タグを表示する。
- `caption-follow` はスクロールによる追従停止と最新への復帰を行う。
- `caption-reconnect` は切断中の字幕を復元し、重複を作らない。

## How to get to it (user POV)

生徒が参加し、先生が配信を始める。字幕欄を上へスクロールすると追従が止まり、「最新へ」で復帰する。
Wi-Fi切断から戻ると再接続する。

## Driving it with Playwright

Preconditions:

- 実経路ではマイク、複数の検証用発話、独立したen/zh生徒が必要。長文の追従試験には欄を越える字幕数が必要。

- **配信。** 開始後に発話し、`teacher.locator('#transcript li').last` と `student.locator('#cards .text').last` を読む。日本語の原文と選択言語の訳文が対応し、`.card` の `data-seq` が単調に増えることを確認する。
- **発話中表示。** 実効 `partial.speaking_indicator` がONなら、発話中に `#speaking-indicator` が表示される。発話終了、一時停止、授業終了、接続切断で消えることを確認する。補助ケース `.venv/Scripts/python.exe scripts/qa_native_browser.py --scenario native_speaking_indicator_lifecycle --scenario native_speaking_after_capture_gap` は途中参加と収音ギャップ後も確認する。
- **遅延表示。** ライブ字幕の `delay_ms` が5,000ms以上なら `.delay-tag` が表示される。復元字幕は遅延0扱いで、切断時間を理由にタグを付けない。境界値の補助確認は `.venv/Scripts/python.exe scripts/qa_browser.py --story STU-09`。HTTP・WSを置換するUI試験なので実モデルの処理時間の証明には使わない。
- **追従。** `#cards` の `scrollHeight > clientHeight + 40` を確認する。viewportを変えた場合は一度最下部へスクロールしてから、`student.locator('#cards').evaluate('(el) => { el.scrollTop = 0; }')` で上へ戻し、`#latest-btn` の表示を待つ。`student.locator('#latest-btn').click()` 後、最新カードが見えてボタンが隠れる。
- **切断。** 生徒だけのcontextで `set_offline(True)` を設定する。このEdgeでは既存WebSocketが残るため、`student.evaluate('state.ws.close(1000, "verification outage")')` で実接続を閉じる。アプリのseqや参加状態、タイマーは書き換えない。`Reconnecting…` を待ち、先生が別の発話を行う。`set_offline(False)` 後、最大25秒待って `Live` に戻り、欠けた字幕がseq順に復元され、重複が無いことを確認する。先生contextまで切断しない。
- **補助ケース。** `.venv/Scripts/python.exe scripts/qa_native_browser.py --scenario native_audio_to_caption --scenario native_reconnect_missing_translation`。これは定数PCM・fake ASR/MTと再接続timer制御の経路。追従UIの補助は `.venv/Scripts/python.exe scripts/qa_browser.py --story STU-02`。

## Gotchas

- Edgeでは `set_offline(True)` だけでは既存WebSocketが切れず、字幕を受け続ける場合がある。切断バナーを観測しないまま復元成功としない。上の操作は接続障害の模擬で、学校LANの断線を証明しない。
- ネットワークをOS全体で切ると先生と他runにも影響する。ブラウザーcontext単位で切断を模擬する。
- 履歴は既定で直近50件。実効設定の `history_resend` を読み、その件数を超える長期切断は全履歴復元の合格条件にしない。
- 字幕が不足してscrollできない場合は前提不足を報告する。ボタン可視性だけを書き換えない。
- 実翻訳の意味精度は文面を読んで判定する。fakeの `[en] 日本語` を翻訳の成功としない。

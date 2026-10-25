# 全機能の検証台帳

状態の正本は [feature-status.xlsx](feature-status.xlsx) の **Stories 1シート**。96行のユーザーストーリーに期待する挙動、前提、操作手順、要件、実装根拠、検証状態、証跡、残作業を対応付けた。

[stories.tsv](stories.tsv) はレビュー可能なストーリー定義であり、状態の二重管理には使わない。[HTML](feature-status.html) と [プレビュー画像](feature-status-preview.png) は台帳から生成した閲覧補助。ExcelではA/B列と1〜6行を固定し、6行目のフィルター、E/F列の入力リストを利用できる。

[checkpoint.json](checkpoint.json) は次回作業者向けに手動更新する非権威のスナップショットである。内容が矛盾する場合は、検証済みの `feature-status.xlsx` を優先する。

## 対象と判定

`server/` の認識・区切り・翻訳・配信・記録・通信防御、`web/` の先生/生徒画面・音声Worklet・翻訳辞書、`config.yaml`、初回セットアップと起動スクリプトを読んで機能を整理した。graphifyの関係図を入口に、現在のソースを根拠にした。README、plan.md、現場検証チェックリスト、既存テストも照合した。

ベンチ・集計用スクリプトは性能要件の検証手段として扱う。外部ライブラリと同梱QRライブラリ内部の全実装レビューは対象外。将来の追加言語と翻訳対訳辞書は現行機能と混同せず、FUT-01/02を未実装とした。

- 合格：当該行の合格条件を確認済み。自動同期で合格にするのは、全条件と実際のassertの対応を監査した `AUDITED_AUTOMATIC` / `AUDITED_BROWSER` の行のみ。ブラウザは必要なシナリオ集合と、実装・テストハーネス・ストーリー定義の現在のハッシュも照合する。OPS-09のような複合条件はpytestと実Edgeの両証跡が揃った場合だけ合格にする。
- 部分確認：関連テストは成功したが、条件の未監査部分、実端末、実音声品質、長時間試験などが残る。通信や実音声を前提とする行は、入力だけを模擬した画面試験ではこの区分。
- 不合格：再現可能な反例がある。別の成功テストでは既知の不合格を解除しない。
- 未実施／実行不可／未実装：それぞれ未着手、前提不足などで実行できない、対応する実装がない。スキップは合格に数えない。

実装状態と検証状態は独立。ソースが存在しても合格とは限らない。過去の開発機レポート、fakeエンジンの成功、集計関数の単体テストを学校実機の合格証拠にはしない。

## 実行済みの証跡

初期の全Python試験の確定結果は [20260906-224957](runs/20260906-224957/pytest.log)：488件中484成功・4失敗。既存の480件（通常461、実ASR13、実MT6）は成功し、追加境界試験8件中4件が失敗した。モデル初期化を含む試験のプロセス打切りを避けるため `--timeout=120` を指定。授業遅延の合格基準を緩めたものではない。

ブラウザは実EdgeでHTML/CSS/JSを動かし、WebSocketとマイクを決定的な代替にした。最初の [36シナリオ](runs/20260906-223938-browser/browser.json) は31成功・5失敗。[小画面の追加1件](runs/20260906-225403-browser/browser.json) は成功。実マイク、iOS Safari、学校LANを検証した結果ではない。失敗画像は同じ実行フォルダに保存。

不具合の再現と影響は [findings.md](findings.md)。テスト対象のアプリ本体は変更していない。

続く [20260906-230707](runs/20260906-230707/pytest.log) は通常テスト＋追加QAの490件中486成功・既知4失敗。実モデル19件は先の実行の証跡を参照する。新しい `test_pipeline_contracts.py` の21件は、幻覚除外後の採番/配信/翻訳/記録、閾値の等号、停止/終了での保留確定、出荷設定の12Segment/30秒/1000ms、翻訳APIへ渡す出力上限とトークン取得失敗の代用を確認した。ASR-05・TURN-01/02/03/04・MT-04の全条件の対応監査も完了。

[音声Workletの追加試験](runs/20260906-230224-browser/browser.json) は5件中3成功・2失敗（うち1件は既存ケースの再試行）。44.1/48kHzのサンプル数ドリフトを再現し、MIC-05を不合格に更新した。初回の2秒入力で送信チャンク数に幅を許した検査では、このずれを見落としていた。

[字幕画面の条件別再試験](runs/20260906-231340-browser/browser.json) は7シナリオ成功。STU-01/03/04/05/08/09は画面内の挙動が合格条件なので、実DOM・CSS・localStorage・再読み込みで確認した範囲を合格とした。STU-05には先生画面の保存禁止も含む。模擬入力を使ったことは証跡に残し、Safariや学校端末まで合格としたものではない。

[実HTTP/WebSocket＋実Edgeの再試験](runs/20260906-232105-native-browser/browser.json) は4シナリオ成功。先生画面から合成PCMを送って生徒へ字幕を届け、言語変更と拒否時のUI復帰、参加/言語変更/退出の2秒周期統計、記録表示と途中参加を確認した。MON-04/REC-02は全合格条件を確認。TEACH-01/STU-06は残る条件を含め部分確認を維持。音声機器とASR/MTだけは模擬であり、初回モデル試験や90分試験と区別する。

実通信試験の初稿 `20260906-231920-native-browser` は、先生画面の配信開始を確認する前に試験側が音声を一括投入していた。開始確認を挟んだ再実行では4件成功し、初稿の2失敗を製品不具合へ分類していない。シナリオごとに別のループバック・動的ポートを使い、90分試験のサーバーには参加していない。

[一時停止・終了・復帰の実通信試験](runs/20260906-232857-native-browser/browser.json) は4件成功。TEACH-03/04は保留発話の確定、停止中のブラウザ送信停止とサーバー側入力遮断、再開を確認して合格。CONN-02も未翻訳の差分復元を確認し、[既存の復元9件](runs/20260906-233049/pytest.log)と合格条件を照合した。

[復元混雑の実通信試験](runs/20260906-233622-native-browser/browser.json) は1成功・1失敗。標準しきい値128では翻訳済み差分35件の復元が成功したが、明示設定2では負荷解消後5回再試行しても字幕が進まなかった。CONN-04を不合格へ更新。[独立した境界試験](runs/20260906-234154/pytest.log)も標準128/50件と設定32/31・32件が成功、設定32/33件が失敗。初稿`233804`は試験側が標準値を32と誤認したため不採用。`233426-native-browser`は再試行間の待機条件を強めた`233622`で再確認した。

[証明書なしHTTPの音声試験](runs/20260906-234441-native-browser/browser.json)は成功。Edge本来のgetUserMedia・MediaStream・48kHz AudioContext・AudioWorkletを使い、3200バイトPCMフレームを10回以上送信した。Chromiumの合成キャプチャ機器を使っているので、物理マイクやCLIのURL案内までは証明しない。OPS-09を部分確認へ更新。

[先生タブ交代試験](runs/20260906-234556-native-browser/browser.json)では、旧画面の切断と再接続防止は成功するが、新画面がマイク未準備のまま「配信中」となり開始ボタンも押せない。CONN-09を不合格へ更新。単なるサーバー状態の継続と、ブラウザからの音声継続を区別した。

[言語切替の競合試験](runs/20260906-234807-native-browser/browser.json)は2件成功。拒否時の選択復帰に加え、再接続の復元翻訳を保留中にzh→en→zhと切り替え、既存字幕は不変、復元分と新着はzh、連続seqも正常となることを確認しSTU-06を合格にした。

[モニターの実通信試験](runs/20260906-235158-native-browser/browser.json)は4件成功。MON-01の直近50行・最新追従、MON-05の認識待ち/処理中/翻訳待ち/排出後の値とDOM、MON-06の3回キャッシュヒットと推論中央値の維持、MON-07の無音警告・発話で解除・再警告・停止/再開・先生交代を確認し合格にした。無音閾値は既定30秒を確認した上でテスト設定のみ3秒へ短縮。先の`235013-native-browser`の2件成功も保持し、先生交代を追加した再試験を最終根拠とした。

[過負荷の実通信試験](runs/20260906-235408-native-browser/browser.json)も成功。ASR側の10秒超滞留、MT側の2件/3件、解消時をそれぞれ実際に作り、警告の表示/非表示を確認。[境界の単体6件](runs/20260906-235418/pytest.log)は9.99/10.00/10.01秒およびMT2/3/4件の等号も確認した。MON-08の合格は出荷閾値の挙動に対する判定で、学校PCでの閾値の妥当性評価とは異なる。

製品コードに対する直前の全スイート [20260908-034706](runs/20260908-034706/pytest.log) は、専用実モデル19件とOPS-06実モデル直積1件を含む654件中642成功・再現済み11失敗・物理マイクopt-in 1件スキップで、新規回帰は0件。失敗はCONN-04が1件、REC-05が1件、REC-07が2件、TEACH-05が1件、OPS-03が1件、OPS-08が1件、OPS-13が1件、OPS-14が3件で、8ストーリーに対応する。物理マイクのスキップは後続の実行で成功を確認した。QA証跡基盤とテストは強化したが、テスト対象の製品コードは変更していない。

[暫定字幕と先生切断の実通信試験](runs/20260907-000047-native-browser/browser.json)は3件成功。partial ONでは同一turnのrevision 1→2、確定時と一時停止時の消去、生徒へpartialが届かないことを確認し、既定OFFでもpartialが出ないことを確認。先生の同一タブ切断では残音声を確定してpausedとなり、再接続後は自動liveへ戻って同じ音声経路から次の字幕を送れた。MON-02/CONN-06を合格にした。

[partial隔離の追加25件](runs/20260907-000523/pytest.log)は全成功。うち新規3件は、partialが実JSONL・履歴・採番・翻訳・生徒配信へ混入しないこと、確定待ち中のinterimを省略すること、古いturnの結果を破棄して次のpartialへ復帰することを直接確認し、MON-03を合格にした。

[発話中表示の再試験](runs/20260907-000428-native-browser/browser.json)では、途中参加・停止・終了・切断は成功したが、長時間の入力途絶後に音声が戻っても再表示されない。[追加状態プローブ](runs/20260907-002952-native-browser/browser.json)で、入力送信・live状態・破棄なしを確認した一方、VAD区間がopenのまま発話開始数が増えず、通知列がON→OFFで止まることを確認。STU-10を不合格にした。初稿`000342-native-browser`の終了文言のテスト誤りは採用していない。

[境界・復旧の追加一括試験](runs/20260907-005215/pytest.log)は526件中521成功・既知5失敗。欠損/閾値直下の全モデルとfactoryのfail-closed、同一WebSocketでの言語再選択、低速クライアントだけの1013切断、不正メッセージ後の再利用、ASR/MTタイムアウト後のworker継続、保留MT中の2秒以内shutdownを直接確認した。これによりOPS-05、JOIN-05/06、ASR-04、MT-02、REC-03、SAFE-01/02/03/04/09、OPS-11の監査を具体化した。

[参加・通信・マイクの再試験](runs/20260907-005138-browser/browser.json)、[QR参加](runs/20260907-005948-browser/browser.json)、[マイク拒否後の同一タブ再試行](runs/20260907-005916-native-browser/browser.json)はいずれも成功。JOIN-02/03、CONN-01/03、MIC-02/04、TEACH-02を合格にした。`005913-browser`のJOIN-02失敗は、別ストーリーJOIN-01の既知表示不具合まで誤ってassertしたテスト設計ミスなので採用していない。

最終の画面契約 [20260907-014145](runs/20260907-014145-browser/browser.json) はSTU-02、MIC-01、MIC-03の3件すべて成功。最下部追従の停止/復帰、許可前後のマイクラベルと実際の接続先、切替待機中・成功後・NotReadableError後の旧入力接続を実Edgeで確認した。[証明書なしHTTP](runs/20260907-014150-native-browser/browser.json)も、空の一時証明書ディレクトリ、実Edgeの合成入力、48kHz AudioContext、AudioWorklet、実WebSocketの3200バイトPCM 10フレーム以上で成功した。

OPS-09は起動案内・HTTP単独uvicorn構成のpytestと前記Edge証跡を合成して合格。[現行証跡基盤の17件](runs/20260907-014328/pytest.log)はOPS-09/11と、古いsource manifestの拒否、監査集合の排他、部分実行時の完全証跡保持、セル内証跡の圧縮を確認した。Immutableな全履歴は各runフォルダへ残し、台帳セルには現在のpytest、直近のbrowser成功/失敗、soakだけを保持する。

最終ブラウザ一括試験 [20260908-112500-browser](runs/20260908-112500-browser/browser.json) は43件中35成功・既知8失敗で、新規回帰はない。実Edge一括試験 [20260908-112624-native-browser](runs/20260908-112624-native-browser/browser.json) は、OPS-10を含む26件中23成功・既知3失敗。SAFE-08では実HTTP/WSの参加、字幕、記録、切断、復元、終了を通して外向きHTTP通信0・全WS接続先loopbackを確認した。OPS-10では現PCの非loopback NIC `192.168.1.6` に `0.0.0.0` の動的ポートで待ち受け、teacher-info・画面表示・OpenCV QR復号値の一致、同URLからの実Edge HTTP 200・実WebSocket参加・字幕seq 1まで通した。別端末到達、実DHCP変更、スマホカメラ読取は未検証なので部分確認を維持する。

STU-11は実Edgeのmobile/touch模擬で、文字L（25.6px）、自然文長文6カード、375×667/667×375の縦横、信頼済みtouch scroll、Latest復帰、visibility hidden→visible模擬、再接続時の`last_seq=6`、字幕seq 1〜8の順序・欠落なしまで確認した。一方、200% reflow相当の188×334では全6カードが横overflow（card client/scroll 158/184px、本文130/170px）となり、可読条件への再現可能な反例なので不合格とした。実iOS Safari、ChromeOS Chrome、実browser zoom、30秒OSロックはこの模擬で代替しない。

[物理マイクと字幕経路の複合試験](runs/20260908-112900/pytest.log)は2件とも成功した。Windowsの既定物理マイクから収音したPCMフレームをloopback WebSocketへ送り、別の統合試験で先生発話が選択言語の生徒字幕へ到達することも確認し、TEACH-01の合格を現行ソースで再確認した。`20260908-045745`はサンドボックス下のEdgeが暗号化ストア/GPUキャッシュへアクセスできずCDP初期化で停止した環境失敗であり、製品判定には採用していない。

[証跡基盤と品質TSVの最終集中回帰](runs/20260908-034547/pytest.log)は74件すべて成功した。pytest source manifestへルートの運用ファイル、QAスクリプト、fixture、ストーリー定義、新設した品質validatorを含めること、native browserの環境skipをgreen扱いせず台帳無変更で止めることを確認した。QUAL-01は30行・10列・連番・異なる原文・必須翻訳・整数評点・領域/音響条件・中国語平均3.5境界を同じPython実装で検証し、否定ラベルを含む不正入力18種、CLI契約、source証拠とTSVの自己参照もfail-closedにした。実授業由来、評価者資格、人手による意味評価は未検証なので部分確認を維持する。OPS-02では実 `download_models.py` の中断、完成済みpayload保持、同一格納先での再試行を確認したが、実数GBの転送中断とPowerShell連鎖は隔離Windowsでの現地条件として残る。

[最新の全pytest](runs/20260908-112049/pytest.log)は、専用実モデル19件とOPS-06実モデル直積1件を含む887件中875成功・既知11失敗・物理マイク1スキップで、新規回帰は0。[証跡・品質・現地性能validatorの集中回帰](runs/20260908-111918/pytest.log)も323件すべて成功した。[最新カバレッジ監査](runs/20260908-112934-coverage-audit/audit.json)では証拠失効による降格0件で、PERF-03/04/05の保存済みsoak/scaleも現行sourceに対して再監査した。現地性能validatorはschema v2で、Windows APIから搭載RAMとOS可視RAMのraw bytes、build、product typeを、レジストリからCPU manufacturer/nameを採取し、sealed reportのCPU・core/thread数・RAM表示と照合する。搭載RAMは8〜16GiBのraw境界、Windows 11 client、GenuineIntel Core i5を満たし、run終了後15分以内に採取した場合だけ資格ありとする。bool/float型偽装、欠損、取得失敗、hardware/hash片側改変、report不一致、2run間のhardware・hostname・時系列不一致は受領ゲートの実行試験でもfail-closedを確認した。学校実機run自体は未実施なのでPERF-02は部分確認のままである。

追加の実モデル試験では、sherpa/faster-whisper × Hy-MT2/NLLBの全組合せでja→en/zh配信を確認してOPS-06を合格にした。実日本語音声のja→en/zh配信と、既知誤認識「原子の風」をhotwordで「原子の数」へ補正する経路も確認し、ASR-01/06を合格にした。一方、未準備のまま60秒後に先生画面を開くOPS-03、旧Wi-Fiと典型的ホットスポットIPが併存すると旧IPを選ぶOPS-13、powercfg失敗・既存ファイアウォール規則の内容・非管理者向け手動手順を確認しないOPS-14を反例として追加した。

OPS-12追加試験前の中間台帳は96件中、合格67・不合格14・部分確認12・未実施1・未実装2だった。現行集計は後述の「長時間・スケール試験完了、学校実機待ち」を参照。

中断した初回全試験 `20260906-223421`（NLLBロード中20秒制限）と追加試験初稿 `20260906-224702`（試験側が存在しない応答を待った）はJUnit未生成。判定に使わずログを保持し、修正した試験を新しい実行フォルダで再実行した。初回の台帳同期では式キャッシュが空になったため、OfficeCLIで再計算する処理を加え、検証済み。

## テストループの再実行

依存：既存 `.venv`、OfficeCLI。ブラウザ試験にはEdgeと `.qa-tools` のPlaywrightが必要。

```powershell
$env:PYTHONIOENCODING = 'utf-8'
# 既存＋追加境界試験。既知の不具合がある間は終了コード1が正しい。
.venv/Scripts/python.exe scripts/story_tracker.py run --pytest-arg=tests --pytest-arg=qa_checks --pytest-arg=--timeout=120

# ブラウザ試験 → 出力されたフォルダを指定して台帳へ同期
.venv/Scripts/python.exe scripts/qa_browser.py
.venv/Scripts/python.exe scripts/qa_native_browser.py
.venv/Scripts/python.exe scripts/story_tracker.py --run-dir docs/qa/runs/<実行フォルダ> sync-browser

# 現地wizardが生成・封印した8件を全artifactの目視レビュー後に取込
.venv/Scripts/python.exe scripts/story_tracker.py sync-field --run-dir docs/field/evidence/<実施日> --reviewer <実施者> --approve-field-candidates

# スキーマ、全ID/文面、式エラー、集計キャッシュとHTMLの確認
.venv/Scripts/python.exe scripts/story_tracker.py check
.venv/Scripts/python.exe scripts/qa_browser.py --preview-tracker
```

ブラウザ起動や依存の読取りをサンドボックスが拒否する環境では、承認された権限で実行する。権限エラーを機能不合格として扱わない。`--story STU-11` でブラウザ試験を絞れる。`--no-sync` で証跡収集と台帳書込みを分離できる。

台帳の同時編集中に同期しない。通常同期はIDで行を解決し、対象行のF/M/Oのみ更新して、それ以外の手入力を保持する。`sync-field` は対象8行のF/M/N/Oを更新するが、既存の不合格findingは自動解除しない。全同期は書込み直前に対象storyの行位置と対象セルを再読込し、途中で台帳が変わっていれば一括書込み前に停止する。同期前コピーは各run配下に保存。同じ実行の再同期は拒否する。pytest/browserとも、実装・ハーネス・ストーリー定義のmanifestが現行と完全一致しないrunは台帳へ触れる前に拒否する。`init` は既存台帳を上書きしない。新しい試験を追加したらTSVと台帳のL列を合わせ、条件の監査後に合格対象を更新する。

ループは「未実施・部分確認・不合格を抽出 → 前提を満たして試験 → 証跡保存 → 同じ行へ反映」。不具合の修正、GitHub issue作成、OS設定変更はこの検証作業では行っていない。修正後は失敗を再現した同じシナリオと周辺回帰試験を実行し、根拠を添えて不合格を解除する。

現地証拠の自動import基盤は実装済みである。wizardはOPS-01/02/10/12、STU-11、PERF-01/02、QUAL-01の生gate値を日付固定の `field-story-evidence.json` へ保存し、同sidecarを外側SHA-256 manifestへ封印する。`sync-field` は受領側で全gate、全artifact、学校PCで試験前後に取得したstart/end source snapshot、通常cache／cache無効の性能2run、PERF-01の全final表示遅延package、QUAL-01の品質TSV／source／evaluator provenance packageを再計算し、実施者名と明示承認がある場合だけreview receiptと更新前backupを作って台帳へ反映する。Markdown reportは表示物でありimport元にはしない。後続のfull/browser/soak/scale/auditは今回の補助runに含まれない行も含め、全field合格のreceipt story anchorと現行証拠を再監査する。有効な合格は保持し、新しい明示的失敗または証拠失効を優先する。現地証拠自体はまだ未取得なので、現在の7件は部分確認のままである。STU-11には既知の横overflow反例があり、現地証拠を合格レビューしても既存不合格は自動解除しない。実装修正後に同じ拡大相当シナリオとChromeOS Chrome/iOS Safariを再試験する。

フィールド証拠は `docs/field/evidence/<実施日>/` に集約し、symlink・空ファイルを含まないdir全体のSHA-256一覧を再計算照合する。試験前の `school-source-start.json` と全試験後の `school-source-end.json` は、source集合・SHA-256・session・学校PC hostname fingerprintを結び、`end.predecessor_sha256` でstart artifactのbytesを直接参照し、性能2runとOPS-12を時刻上で囲み、受領checkoutとも完全一致する場合だけ採用する。学校PCの45分性能runは、同じWindows PC・checkout・`.venv`で切断／partial注入なしの通常cache／cache無効を連続実行し、各run終了後15分以内に実機情報を採取し、通常run終了後4時間以内にcache無効runを開始する。run名にはtrimしたhostnameのUTF-8 SHA-256先頭16桁を含める。移送前に `scripts/validate_field_performance.py` でsealed JSON、現行source/input/runtime、Windows system/hostname fingerprintとrun名のbinding、完全送出・drain・連続観測を検証する。移送後は元音声・モデル・学校PCのPython実体を要求せず、sealed input/runtime snapshotのhashを保持したまま、コピー済みrun/manifest/全artifact/effective config/reportを再ハッシュする。さらに受領側の現行checkoutとsource manifestのsoak必須source key集合を完全一致させ、全source bytesを再ハッシュする。

PERF-01はschema v2の固定run JSONと保存validationを外側manifestへ封印し、単体・受領側の双方で `scripts/perf01_evidence.py` を再実行する。単体validator自身が固定start snapshotのexact schema・実施日・session・host・canonical source SHA-256をrunへ結び、全final JSONLの任意の正の先頭seqからの連番、各seqに対するen/zhの厳密な2N行、発話終了・transcript生成・代表端末DOM表示の時刻、10台の言語内訳、45分、中央値5秒以下、最大8秒以下を丸め前の値で照合する。実授業WAVは同じopen handleでstream hash・非圧縮PCM header・宣言frame全読込・実時間・前後file identityを確認し、artifact/run/snapshotのhardlink aliasと検証中変更を拒否する。受領側は `start < measurement < audio end <= observation end <= end snapshot <= bundle` も検証する。QUAL-01は固定TSVと `quality-provenance.json` を `scripts/quality_evidence.py package` で再検証し、PCM WAVの実payload、30 source segment、sealed transcriptとの日英中本文一致、宣言されたrole/language/qualification kindと証拠に結ばれた90 evaluator assignment、session/host/実施日/時系列を結ぶ。wizardの10/15項目shell出力は実Bash関数へ偽出力を流す動的陰性試験でもfail-closedを確認した。物理10台のidentity・同時接続・45分録画・全final注釈・共通clock、snapshot現場真正性、実授業由来、transcriptの監査対象run由来、音声区間の意味対応、評価者identity/資格・attestation・意味評価の真正性は機械判定できないため、scopeをfalseにしたまま最終reviewerが原証拠を確認する。詳細は [実地検証チェックリスト](../field-verification-checklist.md) を参照。

## 長時間・スケール試験完了、学校実機待ち

[残件の実行可能性監査](runs/20260907-214305-capability-audit/environment-audit.md)で、現行PCのOS・仮想化・接続端末・ネットワークと残る実地条件を突合した。追加の[OPS-12 loopback実Edge試験](runs/20260907-212418-ops12-loopback/evidence.json)では、10個の独立Edge contextが参加URLから3.580秒でjoinし、実HTTP/WS経由で全員が字幕を受信した。学校LAN・投影QR・物理端末・人間操作は未検証なのでOPS-12は部分確認を維持する。最新の全スイート、通常／native Edge、物理マイク、保存済み性能証跡の再監査を同期した現行台帳は、合格71・不合格16・部分確認7・未実施0・実行不可0・未実装2である。

source manifest付きの[最終スケール試験](runs/20260907-055300-scale-final/2026-09-07-scaling.md)は、1/10/20/40人 × 1/2/3/5言語の16点を各120秒＋drain 20秒で完走した。全点で実sherpa ASRを20回実行し、1/2言語は実Hy-MT2、3/5言語は明示したfake MTを使用。ASR/MT件数は生徒数に依存せず、配信欠落・破棄・切断・エラーは0で、[ストーリー別監査](runs/20260907-055300-scale-final/story-evidence.json)は `qualified=true`、`passed=true`、`healthy=true`。これをPERF-04の最終根拠とする。

[最終90分試験](runs/20260907-095700-soak-final/2026-09-07-acceptance.md)は、実sherpa＋Hy-MT2、合成拡張音源、擬似生徒10名、60秒周期の切断注入で5400秒＋drain 20.062秒を完走した。字幕1,846件、遅延中央値0.11秒・p95 0.27秒・最大3.67秒、RSSは開始1,739MB・終了1,688MB・ピーク2,553MB。切断89回は全件復旧し、全10人が各923件を欠落なく受信、最大復旧1.531秒だった。クラッシュ・推論失敗・容量破棄・再接続失敗・復元欠落・時間窓欠落はいずれも0で、[ストーリー別監査](runs/20260907-095700-soak-final/story-evidence.json)はPERF-03/05を資格あり・合格と判定した。

```powershell
.venv/Scripts/python.exe scripts/replay_client.py --minutes 90 --students 10 --corpus ja_ext --engine hy-mt2 --asr sherpa --disconnect-every-s 60 --drain-seconds 20 --port <空きポート> --out-dir docs/qa/runs/<新規フォルダ> --no-config-update
```

これらは開発PC・合成音源・Python WebSocket生徒での安定性を示す証跡である。`--no-config-update` により出荷設定は書き換えていない。旧runは履歴として保持するが、PERF-03/04/05の現行根拠には上記2件だけを使用する。PERF-01は指定学校PC・実授業音源・物理ブラウザ10台での画面到達測定、PERF-02は指定学校Windows 11/Core i5/8〜16GB級PC・実授業音源・Python擬似生徒10接続でのプロセスツリーRSS測定が必要なため、どちらも部分確認を維持する。PERF-02は物理ブラウザを接続しない性能試験であり、物理ブラウザは合格条件に含めない。

学校PC、学校LANの端末10台、iPhone/Chromebook、実授業音源と内容評価者、初回セットアップを試せる隔離Windows環境が残る。既存ホストの証明書・モデルを削除したり、ファイアウォール/電源設定を変更して代用しない。

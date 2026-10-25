# ストーリー試験で再現した問題

状態の正本は [台帳](feature-status.xlsx)。以下は反例の説明であり、修正は未実施。台帳で不合格の16ストーリーはすべて決定的な反例と実装上の原因まで確認した。最新browser/native-browser証跡のsource hashは現行checkoutと一致している。

| ストーリー | 観測した問題 | 影響・根拠 |
|---|---|---|
| JOIN-01 | 参加成功後も参加フォームが表示される | 字幕画面とフォームが同時表示。`hidden` が `#join-screen { display:flex }` に負ける。`web/student.css:5`。[画像](runs/20260906-223938-browser/JOIN-01-join_hides_entry_screen.png)、[最新ブラウザ証跡](runs/20260908-112500-browser/browser.json)。 |
| STU-07 | 英語UIでも記録表示が「● 記録中」 | 生徒向けの記録状態表示が翻訳されない。`web/index.html:25` の固定文言に`data-i18n`がなく、`web/i18n.js`にも対応キーがない。[最新ブラウザ証跡](runs/20260908-112500-browser/browser.json)。 |
| CONN-05 | サーバー再起動後、新コードで入り直しても旧字幕/seqが残る | 新授業のseq=1が重複扱いされ、旧授業だけが表示される。終端的な`join_rejected`で画面だけ戻し、`lastSeq`、`pendingSeqs`、旧カードを初期化しない（`web/student.js:247-252`）。[画像](runs/20260906-223938-browser/CONN-05-new_server_does_not_reuse_old_cards.png)、[最新ブラウザ証跡](runs/20260908-112500-browser/browser.json)。 |
| TEACH-05 | 終了→再開始後、生徒が切断すると復帰しない | `session:ended`で立てた`state.ended`を`session:live`で戻さないため、close処理が再接続を予約せず終了する（`web/student.js:204-212,261-264`）。[最新ブラウザ証跡](runs/20260908-112500-browser/browser.json)。 |
| CONN-08 | 先生の最初の再接続が失敗すると再試行が止まる | 最初のcloseで`joined=false`にした後、未参加のまま失敗した次のcloseが早期returnする（`web/teacher.js:234-247`）。[最新ブラウザ証跡](runs/20260908-112500-browser/browser.json)。 |
| MIC-05 | 44.1/48kHzの連続入力で16kHz出力の時間がずれる | 60秒＝960,000サンプルを期待する入力で、44.1kHzは961,484、48kHzは967,500サンプルを生成（送信前残りも含む）。128サンプルのブロック末尾を読出し位置が越すと、`slice(keepFrom)`が空になりながら越境位相も0へ戻る（`web/audio-worklet.js:39-41`）。[最新ブラウザ証跡](runs/20260908-112500-browser/browser.json)。16kHz、無音、クリップ、全送信チャンク長は成功。 |
| REC-05 | 最初の認識結果を待っている終了処理が即座に戻る | `finalize_recording()` がASR/MTキュー排出前に`has_entries`を判定する（`server/pipeline.py:856-860`）。空record＋ASR待ち1件では1 event-loop tickで`None`となる一方、先に1件追加した対照では`task_done()`まで待つ。[最新pytest証跡](runs/20260908-112049/pytest.log)。 |
| REC-07 | 書込失敗後の再試行が無視される | I/O成功前に`_written=True`となり、例外後も取り消されない（`server/recorder.py:69-75`）。一度だけ`mkdir`を失敗させると再試行は`None`。[最新pytest証跡](runs/20260908-112049/pytest.log)。 |
| REC-07 | 保存失敗を先生へ通知して再試行する操作経路がない | 終了制御は`finalize_recording()`の例外を先生向け`ErrorMsg`へ変換せず（`server/main.py:341-349`）、WebSocket受信ループも保存例外を回復処理へつながない。再試行可能な内部状態に加え、内容を保持したまま先生へ失敗を知らせ、明示的に再実行できる境界が必要。 |
| REC-07 | 同一分の別授業が同じ保存先を上書きする | 保存先が分単位で、既存ディレクトリを`exist_ok=True`で再利用し、JSONLを`"w"`で開く（`server/recorder.py:72-75`）。2つのrecorderが同じpathを返し、後者の内容だけが残る。[最新pytest証跡](runs/20260908-112049/pytest.log)。 |
| TEACH-05 | 終了→開始→終了した2回目の授業が記録に残らない | recorderはPipeline生成時の1回しか作られず（`server/pipeline.py:249-251`）、開始・終了制御にもrotationがない（`server/main.py:334-349`）。2巡後も同一recorderは`_written=True`、メモリ2件に対しディスクはseq 1だけ。recorder交換だけでは同一分衝突でseq 2がseq 1を上書きするため、REC-07の一意保存先修正も必要。[最新pytest証跡](runs/20260908-112049/pytest.log)。 |
| CONN-04 | 復元しきい値を小さく設定すると、負荷解消後も差分復元が進まない | `replay_history()`が同じevent-loop turnで復元ジョブをしきい値まで積み、次件で1013切断する。切断時に対象clientを先に除くため、積んだジョブも配信されず、次回も同じ`last_seq`から全件を積み直す。設定32では31/32件成功、翻訳済み33件だけが決定的に失敗する（`server/pipeline.py:648-653,766-803,954-969`）。[最新pytest証跡](runs/20260908-112049/pytest.log)、[最新実HTTP/WS＋Edge証跡](runs/20260908-112624-native-browser/browser.json)。標準128・履歴50件は成功。 |
| CONN-09 | 新しい先生タブが「配信中」でも音声経路が準備されない | 旧タブは4000で正常切断・理由表示・再接続なし。新タブは`micReady=false`、Workletなし、開始ボタン無効となり音声を送れない。`ensureMic()`は開始ボタンだけから呼ばれ、joined時にliveだと`setButtons()`がそのボタンも無効にする（`web/teacher.js:197-207,252-256,340-345,369-414`）。[最新実HTTP/WS＋Edge証跡](runs/20260908-112624-native-browser/browser.json)。入力機器と認識/翻訳は模擬。 |
| STU-10 | 長時間の音声入力途絶後、音声が戻っても「発話中」が再表示されない | 無フレーム状態が警告閾値を越えると`_speaking`だけOFFにするが、VAD発話区間はopenのまま（`server/pipeline.py:912-933`）。再入力時も`utterances_opened`が増えないためON通知が出ず、受信列は`[true,false]`で止まる（`server/pipeline.py:419-439`）。WS送信は6フレーム増加、live、破棄0を確認。[最新実HTTP/WS＋Edge証跡](runs/20260908-112624-native-browser/browser.json)。通常の無音PCM、停止・終了・切断は別経路で正常。 |
| STU-11 | 大文字・長文を200% reflow相当の狭幅で表示するとカード内が横overflowする | 実Edge mobile/touch模擬の188×334 CSS px・文字L（25.6px）で、全6カードがclient/scroll幅158/184px、本文が130/170px。本文に長い英単語を狭幅で折り返す指定がない（`web/student.css:42-57`）。375×667と667×375はoverflow 0で、touch scroll、Latest、模擬ロック復帰、連続復元も成功したため、今回の模擬試験での失敗は200% reflow相当へ限定された。[最新ブラウザ証跡](runs/20260908-112500-browser/browser.json)。実iOS Safari、ChromeOS Chrome、実browser zoom、30秒OSロックは未検証。 |
| OPS-03 | モデル準備が完了しなくても待機上限後に先生画面を開く | `pipeline.ready=false`を保った決定的試験で、600回の待機後に`webbrowser.open()`が呼ばれた。openがループ外にあり、待機終了後の再判定がない（`server/main.py:463-467`）。準備完了へ遷移した場合に1回だけ開く正常系は成功。[最新pytest証跡](runs/20260908-112049/pytest.log)。 |
| OPS-08 | 証明書のSANが旧LAN IPのままでも、現在IPのHTTPS先生URLを「別端末可」と案内する | `ServerConfig.tls_ready()`は証明書・鍵の存在だけを確認し、起動表示は現在IPとSANを照合しない。証明書生成時には生成時点のLAN IPがSANへ記録され、既存証明書は`--force`なしでは更新されない。旧IP `192.0.2.10` の証明書と現IP `192.0.2.11` を与えた決定的試験で、SAN不一致の`https://192.0.2.11:18448/teacher`を案内する反例を再現。`server/config.py:33-34`、`server/main.py:550-565`、`scripts/make_cert.py:29-40,60,90-92`。[最新pytest証跡](runs/20260908-112049/pytest.log)。 |
| OPS-13 | 旧Wi-Fi候補と典型的なWindows hotspot候補が併存すると、旧Wi-Fi側IPを参加URLに選ぶ | 旧Wi-Fi `192.168.1.34` の順位は`(0,0)`、典型的なWindows hotspot `192.168.137.1`は末尾`.1`の減点で`(0,1)`となり、候補順を逆にしても旧IPを選ぶ（`server/main.py:117-154`）。IP文字列だけのAPIではアダプタ役割を確実に判別できない。[最新pytest証跡](runs/20260908-112049/pytest.log)。 |
| OPS-14 | セットアップ完了表示がOS設定の成功を保証しない | `setup.ps1:89-100`は2つの`powercfg`終了コードを個別確認せず、native commandの非zero終了を検出できないため完了マーカーへ進み得る。`:76-87`は同名ファイアウォール規則の存在だけで、有効・Inbound・Allow・TCP・対象portを再検証しない。`README.md:66-70,103-115`にも非管理者が後で実行できる具体手順がない。[最新pytest証跡](runs/20260908-112049/pytest.log)。 |

## 修正後の再試験順

16件は責務境界ごとに19個の独立した修正単位として扱う。特に `OPS-13 → OPS-08`、`CONN-08 → CONN-09`、`REC-05 + REC-07 → TEACH-05（記録）` の依存順を守る。

| 順序 | 修正単位 | 修正後に必須の回帰ゲート |
|---|---|---|
| 1. 運用 | `OPS-03`、`OPS-13`、`OPS-08`、`OPS-14a powercfg`、`OPS-14b firewall`、`OPS-14c README` | 各 `test_ops03_*` / `test_ops13_*` / `test_ops08_*` / `test_ops14_*`、`tests/unit/test_lan_ip.py`、`tests/integration/test_ops_certificates.py`、launcher/offline invariants。`tls_ready()`と「現在IPをSANが覆うか」は別判定にする。 |
| 2. 小規模UI | `JOIN-01`、`STU-07`、`STU-11` | join成功・拒否・再参加時のcomputed display、記録中のja/en/zh切替とfallback、188px相当の長単語・URL状文字列・日本語、および通常portrait/landscape/touch。 |
| 3. 接続状態 | `TEACH-05（browser）`、`CONN-05`、`CONN-08`、`CONN-09` | 授業再開後の生徒再接続、新授業epochでは初期化・同一授業再接続では保持、先生のjoin前連続失敗からの復帰、4000では再試行禁止、代替先生の自動mic復帰とuser-gesture fallback。 |
| 4. 音声・復元 | `MIC-05`、`STU-10`、`CONN-04` | 16/44.1/48kHz・127/128/129境界・異なるchunk分割、capture gap後の`true,false,true`、replayしきい値`N-1/N/N+1`、cached/uncached/mixedの単調なseq前進と順序維持。 |
| 5. 記録 | `REC-05`、`REC-07（transactional storage）`、`TEACH-05（recording）` | ASR/MT drain後の空判定、失敗後も内容と保存先を保った再試行、同一分の排他的な一意保存、終了→開始→終了、重複end、default-off。 |

一括修正で因果を曖昧にしないため、新授業epoch初期化と同一授業再接続、teacher再試行とmic準備、Worklet再標本化とserver側VAD、replay schedulingと表示状態、queue drainと永続化とrecorder rotationは別の修正・判定単位に保つ。

記録系4反例の実行コードは [qa_checks/test_story_edges.py](../../qa_checks/test_story_edges.py)、確定証跡は [pytest.log](runs/20260906-224957/pytest.log) と [JUnit](runs/20260906-224957/pytest.xml)。`qa_checks` は既存テスト群とは別の明示実行対象で、失敗をxfailにして隠していない。ファイル保全の一部はF-10から導出した期待であり、既存仕様の明記と区別して台帳に記した。

ブラウザ反例は実Edge＋模擬通信/模擬マイク。再接続の障害経過を決定的に与えた結果で、学校LANの実切断やiOSで実測したという意味ではない。

MIC-05の追加試験は時間待ちをせず、実Workletソースに60秒分の合成サンプルを128サンプルずつ入力する。結果を1600サンプルの送信単位に丸める前の残りも数えるため、単なる最終チャンクの端数ではない。既存90分リプレイはPCMを直接WSへ送るため、このブラウザ変換経路は試験しない。今回の不具合を90分試験の成功で解除してはならない。

CONN-04の独立した境界試験は [test_replay_large_gap.py](../../qa_checks/test_replay_large_gap.py)。標準しきい値128・履歴50件は成功。明示設定32では31/32件は成功、翻訳済み33件で1013切断した。[3成功・1失敗](runs/20260906-234154/pytest.log)。復元投入時にキューがしきい値へ達するとクライアントを外し、再試行時にも同じ履歴を再投入する経路がある（`server/pipeline.py`の`_enqueue_mt`/`replay_history`）。初稿`233804`は標準値を32と誤認した試験の失敗であり、製品判定には使用していない。

STU-10は [000428](runs/20260907-000428-native-browser/browser.json) で、途中参加・停止・終了・切断の正常系と入力途絶後の失敗を同時確認した。初稿`000342`にあった終了バナーの英語期待値誤りは修正済みで、製品判定に使わない。`002952`ではサーバー内部状態と実送受信列を追加計測して同じ失敗を再現した。表示だけを直すか入力途絶で発話を安全に確定するかは設計判断が要るため、本検証では修正していない。

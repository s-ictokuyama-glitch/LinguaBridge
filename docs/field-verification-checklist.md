# 実地検証チェックリスト（イシュー #19）

学校での実地作業用。**印刷して現地で記入**し、記入後は #19 イシューに貼り付けて記録とする。
背景と設計は [plan.md](../plan.md)、運用は [README.md](../README.md) を参照。

- 実施日: **____年__月__日** / 実施者: **____________**
- 対象PC（型番・CPU・RAM）: **________________________**
- 対象端末: Chromebook **__台**（型番 __________）/ iPhone **__台**

> このチェックは「音声→字幕」の機能が実装済みであることを前提に、**実機・実ネットワーク・
> 実マイク環境**で使えるかを確認する。ソフト側の変更は原則不要（不可時は運用経路の切替のみ）。

---

## 事前準備（授業日の前・インターネット接続下で）

当日をネット不要にするため、モデル取得と性能計測は前日までに済ませる。

- [ ] **コード配置**: 学校PCに `localServer2` ブランチを配置（`git clone` → `git checkout localServer2`、またはコピー）
- [ ] **開始source snapshot**: 性能runと当日試験に使う学校PCの配置folderで、他の作業より前に `python scripts\field_story_evidence.py snapshot-sources --field-date <実施日> --phase start` を1回だけ実行（`python` commandが無い場合は `py -3.12` を使う）。`<実施日>` は実行時の学校PCローカル日付と一致必須で、別日の旧証拠を再ラベルしない。`docs\field\evidence\<実施日>\school-source-start.json` を上書きせず保持する
- [ ] **OPS-02 実取得中断**（許可された新規／隔離Windowsまたは専用clean snapshotのみ）:
  1. `start.bat` を起動し、1モデル以上が `manifest.json` に記録された後、次の大容量モデル取得中に `Ctrl-C`
  2. `Test-Path ".venv\.setup-complete"` が `False`、先生ページと授業サーバーが未起動であることを確認
  3. 完了済みモデル配下の全payloadを再帰列挙し、相対path・byte・mtime・各ファイルSHA-256を保存してから `start.bat` を再実行
  4. 再実行後のpayload一覧が前後一致して再利用され、最終的に完了マーカーと先生ページが生成されることを確認
  - OPS-02環境／snapshot ID: ________________________________
  - 中断前の完了モデル／payload一覧: ________________________________
  - 再実行後の同モデルpayload一覧: ________________________________
  - 中断中のモデル／ファイル: ________________________________
  - マーカー不在 / サーバー未起動: 可 / 否
  - 完了済みモデル保持 / 最終完了: 可 / 否
  - 中断中ファイルの挙動: 先頭から / 再開 / 不明
  - 前後manifest・ログ・Get-Item・Get-FileHash・画面記録: ________________________________
  - 注: 完成ファイルは再利用されるが、取得途中の単一ファイルは先頭から取り直す場合がある
- [ ] **OPS-01 初回セットアップ**: OPS-02とは別の新規／隔離Windows、またはOPS-02前のclean snapshotへ復元して `start.bat` を実行（初回のみモデルDL 約4GB）→ 依存・全モデル・証明書・完了マーカー → 先生ページが自動で開くところまで確認
  - OPS-01環境／snapshot ID: __________________ / 未セットアップ開始: 可・否
  - 初回ログ・完了マーカー・証明書・先生画面のrepo内証拠: __________________
  - 証拠保存後に授業サーバーを停止し、次のベンチとポート／CPU／RAMが競合しない: 可・否
- [ ] **実機ベンチ**（#9 再計測）: `.venv\Scripts\python scripts\bench.py`
  - 既定ASR: ______ / 既定翻訳: ______ / 遅延見積り 中央値 ____s・最大 ____s → 予算内 可 / 否
  - レポート: `docs\bench\____-bench.md`
- [ ] **学校PCの自動性能計測**（#17）: 物理ブラウザは接続せず、実授業WAVとPython擬似生徒10名で通常キャッシュ／キャッシュ無効を別々に45分実行
  - `--students 10` 自体が擬似生徒10名を作るため、物理ブラウザを同時接続すると20名負荷になる
  - 実行ごとに存在しない一意の `--out-dir docs\field\evidence\<実施日>\<時刻>-<hostname SHA-256先頭16桁>-school-cache-on` / `...-cache-off` を必ず指定（既存runと2本間の共有は禁止）。hostnameはtrimした `$env:COMPUTERNAME` のUTF-8 bytesから同じ学校PC上で算出し、validatorがrun名との一致を検査する
  - 固定した `--asr sherpa|faster-whisper` / `--engine hy-mt2|nllb`: ________________________________
  - 通常: `.venv\Scripts\python.exe scripts\replay_client.py --minutes 45 --students 10 --audio 授業録音.wav --asr <ASR> --engine <MT> --out-dir <cache-on> --no-config-update`
  - 無効: 同じコマンドに `--mt-cache-size 0 --out-dir <cache-off>` を指定し、1本目の終了後に実行
  - 両runとも切断・partial注入オプションを付けず、同じ学校Windows PC・同じcheckout・同じ`.venv`で連続実行する（1本目終了後4時間以内に2本目を開始）
  - 実授業WAV: __________ / 実時間 ____分 / SHA-256: ________________________________
  - レポート遅延中央値 ____s / 最大 ____s / ピークRSS ____MB / クラッシュ __ / errors __
  - 各run終了後15分以内に、学校PC上で `scripts\validate_field_performance.py` を実行する。validator v2はWindows APIから搭載RAM raw bytes・OS可視RAM raw bytes・build・product typeを、レジストリからCPU manufacturer/nameを別採取し、sealed reportのCPU・core/thread数・OS可視RAM表示と照合する。搭載RAMは `8×2^30`〜`16×2^30` bytes（両端を含む）、Windows 11 build 22000以上のclient、GenuineIntel Core i5だけを許可し、欠損・誤型・取得失敗はfail-closedにする
  - 併せて現行source/input/model/runtime、report systemとlive hardwareの各snapshot SHA-256、hostnameの非可逆SHA-256、run名のhostname binding、sealed manifest、完全送出、10生徒、45分の観測範囲、RSS、crash/errors、cache modeを検証する
  - 通常runレポート / source manifest / validator JSON: __________________ / __________________ / __________________
  - キャッシュ無効runレポート / source manifest / validator JSON / CPU中央値: __________________ / __________________ / __________________ / ____%
  - 注: replayレポートの遅延はサーバー送出／Python WS受信まで。物理ブラウザDOM表示はPhase Cで別に全件測定する
  - PERF-02は通常runのvalidatorが合格し、プロセスツリーRSS最大5000MB以下・増加傾向なし・クラッシュ/エラー0の場合だけ合格候補
  - キャッシュ無効runはPERF-02そのものの条件ではないが、別out-dirのvalidator合格とCPU実測までをフィールド証拠全体の完了条件にする
  - 2本のvalidation JSONは同じ音源SHA-256、ASR、MT、分数、言語、source／runtime／report system snapshot／live hardware snapshot／hostname fingerprintで、run dir／manifest／reportが別であること
  - 45分以上の実授業録音が無い場合、PERF-02は未完了のまま。Phase Cの目視値では代替しない

---

## Phase A — 校内Wi-Fi の AP分離確認（plan タスク5）

1. 学校PCと生徒端末を**校内Wi-Fiに接続**
2. `start.bat` 起動 → 画面表示の**生徒用URL**（`http://<IP>:8000/?code=____`）を生徒端末で開く
3. 参加画面が出れば疎通OK

判定と対処:

- [ ] **疎通OK** → 校内Wi-Fi直結を**主構成**に採用
- [ ] **疎通NG（AP分離あり）** → Windows「設定 → ネットワーク → モバイルホットスポット」ON、生徒端末をホットスポットへ接続、`start.bat` 再起動（新IPでQR再生成）→ 再疎通確認
  - ホットスポットは**同時8台上限**。10人学級は「2人1台のペア閲覧」等で節約する
  - これは授業運用の代替であり、OPS-12の「物理端末10台で全員参加」の合格証拠にはしない

**記録**:
- AP分離: 有 / 無
- 校内Wi-Fi直結 / ホットスポット再試行: 可・否 / 可・否・不要
- 採用した接続構成: 校内Wi-Fi直結 / モバイルホットスポット / 未確定
- 備考: ________________________________

### OPS-10 — 実IP変更前後の参加URLと物理QR

1. 状態Aで `ipconfig`、`/api/teacher-info`、先生画面のURL/4桁code/QR、物理スマホでQRを開いたURLを保存。APIはサーバーPCの `http://127.0.0.1:8000` または証明書承認済み `https://<IP>:8443` から取得する（非loopback HTTPは403）
2. 許可された時間に別SSID／ホットスポットへの一時切替またはDHCP更新でIPv4を実際に変更し、`start.bat` を再起動
3. 状態Bでも同じ項目を保存し、B内のIP・port・code・API URL・表示URL・QR復号URLの一致と物理端末からの到達を確認
4. 採用した授業ネットワークへ戻し、再起動してから次へ進む

- 状態A: ________________________________
- 状態B: ________________________________
- IPv4変更 / A全値一致 / B全値一致 / 採用構成へ復帰: 可・否 / 可・否 / 可・否 / 可・否
- 前後のipconfig・JSON・表示・物理QR証拠: ________________________________

---

## Phase B — 管理Chromebookでの証明書・マイク可否（判断ゲート②）

1. 管理Chromebookで先生ページ `https://<IP>:8443/teacher` を開く
2. 証明書警告で「詳細設定 → サイトへ進む」が**押せるか**
3. マイク許可ダイアログで**「許可」できるか**

判定と対処:

- [ ] **両方OK** → 先生ページを**別端末HTTPS**で運用
- [ ] **不可**（承認 or マイクがポリシー制限）→ 先生はサーバーPC上で `http://127.0.0.1:8000/teacher`（**localhost・マイク可・コード変更不要**）。生徒ページは平文HTTPなので影響なし

**記録**:
- 証明書警告の承認: 可 / 不可
- マイク許可: 可 / 不可
- 採用した先生側経路: 別端末HTTPS / サーバーPCの localhost
- 採用経路で先生ページ操作・実マイク入力: 可 / 否
- 証明書・採用経路・マイク入力の実施日dir内証拠: ________________________________
- 備考: ________________________________

---

## Phase C — 教室リハーサル

先生ページの**モニタリング**（統計・キュー深度・遅延中央値・過負荷/無音警告・文字起こしライブ表示）が主計器。
必要なら**記録トグルON**で通し授業の文字起こし＋訳文を後で見返す。

### STU-11 実端末表示（Windowsのviewport模擬では代替しない）

- [ ] Chromebook（機種・ChromeOS・Chrome版: __________________）
  - 投影QRから追加アプリなし・Chromeだけで参加: 可 / 否
  - 縦横・200%相当拡大・文字L・長文: 可 / 否
  - 設定・手動スクロール・「最新へ」をタッチだけで操作: 可 / 否
  - 字幕を3件以上進めながら30秒ロックし、解除後に再接続・履歴復元: 可 / 否
  - repoへ移した画面録画: __________________ / SHA-256: __________________
- [ ] iPhone（機種・iOS・Safari版: __________________）
  - 投影QRから追加アプリなし・Safariだけで参加: 可 / 否
  - 縦横・200%相当拡大・文字L・長文: 可 / 否
  - 設定・手動スクロール・「最新へ」をタッチだけで操作: 可 / 否
  - 字幕を3件以上進めながら30秒ロックし、解除後に再接続・履歴復元: 可 / 否
  - repoへ移した画面録画: __________________ / SHA-256: __________________

両端末で追加アプリなしを含む全項目が可、機種/OS/browserが記録済み、画面証拠が手元repoへ移送済みで入力SHA-256と実ファイルが一致する場合だけSTU-11を合格候補にする。

| # | 確認項目 | 結果 | 所見 |
|---|---------|------|------|
| 1 | Chromebook・iPhoneを含む物理ブラウザ10台が実ネットワークで接続できる | 可 / 否 | 先生表示 __台、en __ / zh __ |
| 2 | 投影QRだけで物理端末10台が **90秒以内**に参加完了 | 可 / 否（__秒） | QR __台 / 手入力 __台 |
| 3 | 教室の実マイク環境（距離・雑音・チャイム）で認識が実用レベル | 可 / 否 | 実施日dir内証拠: ______ |
| 4 | 物理ブラウザ10台で45分通し、en/zh代表各1台の**全final字幕**を照合 | 可 / 否 | __/2N行、中央値 __s、最大 __s |
| 5 | Wi-Fi瞬断・端末スリープを故意に起こし復帰を確認（字幕が差分復元される） | 可 / 否 | 実施日dir内証拠: ______ |
| 6 | 実授業30文を文別に残し、en/zh のネイティブに**訳文の理解可能性**を確認 | 可 / 否 | en __/30・平均 __ / zh __/30・平均 __ |

QUAL-01 は30文それぞれについて、領域、音響条件、正解日本語、ASR日本語、英語訳・評点、中国語訳・評点、所見を固定path `docs/field/evidence/<実施日>/<実施日>-quality-30.tsv` に記録する。No 1〜30、空白正規化後も異なる30原文、必須列、英中の有効評点数と平均、理科・数学・行事、後方・雑音・チャイム、中国語平均3.5以上を検証する。

開始source snapshotを作った後、学校PC上で固定path `<実施日>-quality-provenance.json` を作る。start snapshotのsession ID・hostname fingerprintと、取得/処理/評価時刻、TSV path+SHA-256を記録する。`sources` はPCM WAV、sealed transcript JSONL、実授業attestationを、`segments` は30行を重複しない音声区間・transcript seq・領域・音響・担当者へ、`evaluators` は匿名person ref、日英中の役割、qualification evidence、TSV hashを含むreview attestationへ、それぞれpath+SHA-256で結ぶ。全nested artifactを実施日dirと外側manifestに収録する。

全試験終了後のend snapshotと外側manifest作成後、`quality_evidence.py package` を実行する。wizardは終了status 0、LF 1行、CRなし、タブ区切り15項目（passed、従来10集計、segment数、transcript binding数、evaluator assignment数、provenance SHA-256）をfail-closedに検査し、`F_C6_MEASURED` と全集計をpackage結果から導出する。ASR誤り行数も同じpackage JSONのmetricから導出する。自由記述のsource説明、評価者説明、ASR照合回答だけでは合格にしない。

packageが機械的に保証するのはhash、時系列、30区間、sealed transcriptとの文字列一致、宣言されたrole/language/qualification kindと証拠に結ばれた90 evaluator assignments、qualification/review証拠の存在と外側manifest bindingまでである。snapshotが対象学校PC・現場時刻で採取された真正性、音源が本当に実授業由来か、sealed transcriptが監査対象runの出力か、30音声区間とTSV/transcriptの発話内容が意味的に対応するか、person refの本人性、資格証拠とreview attestationの真正性、意味評価そのものはmanual reviewで個別確認する。

PERF-01 は、実在する物理ブラウザをちょうど10台同時接続し、en/zhを各1台以上含めて代表端末を各1台選び、45分の計測window全体を同じ時計で連続録画する。全finalを `seq / lang / 発話終了時刻 / DOM表示時刻 / 差分秒` の2N行へ欠落なく注釈・照合し、両言語を合わせた中央値と最大値を出す。30件だけの抽出は診断用であり「最大8秒以下」の合格証拠にはしない。45分録画の全体性、全final分布と注釈、物理10台のidentityと同時接続、動画・発話・DOM時刻の同一clock、入力の実授業由来は人が個別確認する。先生表示____台 / en____台 / zh____台 / 代表ID `en=____; zh=____` / final N=____ / 期待2N=____ / 照合____行 / 完全照合 可・否 / 計測表: ________________________________

学校PC上で、実施日dir直下の固定path `docs\field\evidence\<実施日>\<実施日>-perf01-run.json` を作る。run schema v2には固定start snapshotと同じ `field_session_id`、`host_fingerprint_sha256`、`source_snapshot_sha256`、計測window、先生表示10台、全10端末の `device_id/lang/representative/model/os/browser`、実音声、および `measurements_tsv`、全final JSONL、録画、commit/status、config/model、runtime/browserの各 `path` と `sha256` を記録する。実授業WAVはhashした同一file handleから非圧縮PCMの全frameを読み、実時間が計測window以上でなければならない。実授業ライブはPCM検査対象外なので、45分録画と実授業由来をmanual reviewする。次を実行し、固定path `<実施日>-perf01-validation.json` が `passed: true` になった場合だけ合格候補にする。手入力したN・2N・中央値・最大値だけでは合格にしない。

`.venv\Scripts\python.exe scripts\perf01_evidence.py validate docs\field\evidence\<実施日>\<実施日>-perf01-run.json --output docs\field\evidence\<実施日>\<実施日>-perf01-validation.json --format shell`

shell出力は `passed / N / 2N / matched / 計測秒 / 10台 / en台数 / zh台数 / median / max` のタブ区切り10項目・LF 1行固定である。wizardは終了status 0、改行、項目数、型をfail-closedに検査し、これらの表示値を出力から導出する。計測秒が権威値で、表示用の分はAWK 15桁丸めに伴う最大1ナノ秒以下の秒差だけ許容する。代表ID、input kind/description/evidence path、commit/config/runtime/計測TSV pathはrun JSONと完全一致させる。runが参照する全artifactは外側manifestへ収録する。

実音声は、ライブなら実施日時・教科・距離/雑音条件を、WAVならpath・実時間・SHA-256を記録し、個人情報除去済みメタデータ証拠を実施日dirへ保存する: ________________________________。

物理runでは `git rev-parse HEAD`、`git status --porcelain`、config.yaml SHA-256、ASR/MT名、モデルmanifest、`pip freeze`、10台の機種/OS/browser版も保存する。再現情報: ________________________________。PERF-02は前日の通常キャッシュrunのsource manifest付きプロセスツリーRSSを合否根拠とし、Phase Cの目視値では代替しない。

OPS-12 はOPS-10後に採用した学校Wi-Fiへ復帰したことを確認し、10台をいったん退出させ、先生表示が0になったことを動画内で確認してから計時する。開始・最終joinはtimezone付き時刻で記録し、入力した経過秒との差を自動照合する。開始前0 可・否 / 開始時刻 ______ / 最終join ______ / 時刻整合 可・否 / 先生表示10台 可・否 / QR成功10台 可・否 / 手入力0台 可・否 / en/zh内訳 ______ / 端末ID別join表・動画: ________________________________。学校Wi-Fi直結以外、手入力fallback、8台上限のホットスポットrunは運用確認には使えるが、OPS-12合格にはしない。

**#3 の判断メモ**: 既定の whisper small は教科用語の同音異義誤り（例: 光合成→交合性）が出ることがある。
実教室音声で許容できるか、kotoba へ切替（`config.yaml` の `asr.model`）が要るかをここで判断する
（kotoba はCPUでデコードが遅く遅延基準を割るため、精度と遅延はトレードオフ）。

---

## 結果の記録と後続（#19 受け入れ基準）

- [ ] 全試験終了後、性能runと同じ学校PC・同じcheckoutで `python scripts\field_story_evidence.py snapshot-sources --field-date <実施日> --phase end` を1回だけ実行（`python` commandが無い場合は `py -3.12` を使う）。開始後にsourceまたはPCが変わっていれば失敗することを確認
- [ ] 学校PC・各端末のログ、manifest、計測表、録画、`school-source-start.json`、`school-source-end.json` を、個人情報を除いて手元repoの `docs/field/evidence/<実施日>/` へコピー
- [ ] 証拠dirは実施日と完全一致させ、symlinkを含めない。全通常ファイルを同一の `find -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum` で再計算し、非空の `docs/field/<実施日>-evidence-sha256.txt` とbyte一致・`sha256sum --strict -c` 成功を確認
- [ ] 各ストーリーの必須証拠pathは1行1 repo相対pathで実施日dir配下に置き、存在・非空・外側manifest収録を確認
- [ ] 性能validator JSON内のrun dir・source manifest・全artifact・effective config・primary report pathとSHA-256を、移送後の入力pathおよび実ファイルへ再照合し、受領側の現行checkoutのsoak必須source key集合がsource manifestと完全一致することを確認して全source bytesを再ハッシュする
- [ ] PERF-01の固定run/validation JSONを移送し、受領側で `validate_perf01_run` を再実行する。固定start snapshotとrun/validationのsession・host・canonical source snapshot SHA-256が一致し、`snapshot start < measurement start < audio end <= observation end <= snapshot end <= bundle captured_at` であること、保存validationと再計算結果の完全一致、N/2N/45分/10台/en・zh/代表/median/max/input・artifact pathのsidecar一致、およびrun参照artifact全件の外側manifest hash一致を確認する
- [ ] QUAL-01の固定TSV/provenanceと全nested artifactを移送し、受領側でsource snapshotのsession/host/start/end、sidecar captured_at、外側manifestを渡して `validate_quality_package` を再実行する。`passed: true`、manifest scope、全metrics、provenance path/SHA-256とsidecarの完全一致を確認する
- [ ] 受領側では元音声・モデル・学校PCのPython環境を要求せず、sealed input/runtime snapshotのcanonical hashとvalidation/report/manifest bindingを照合する。コピー済みartifact/effective configは再ハッシュする
- [ ] start/end snapshotのsource集合・SHA-256・session・hostname fingerprintが一致し、`end.predecessor_sha256 == SHA-256(start snapshot artifact bytes)` で直接連結され、受領checkoutとも完全一致することを確認。snapshot hostnameはcache-on/off validationと一致し、startはcache-on開始前かつOPS-12開始以前、endはcache-off終了後かつOPS-12終了以後、sidecar capture以前でなければならない
- [ ] AP分離の確認結果と採用構成を #19 に記録
- [ ] 証明書可否の確認結果と採用した先生側経路を #19 に記録
- [ ] リハーサルチェックリスト全項目の結果を #19 に記録
- [ ] wizardが生gate値を `docs/field/evidence/<実施日>/field-story-evidence.json` に保存し、そのsidecarを含む外側manifestを再生成したことを確認する。ドラフトとsidecarが参照する全artifactを目視し、PERF-01の45分録画全体・全final分布/注釈・物理10台identity/同時接続・同一clock・実授業由来と、QUAL-01のsnapshot現場真正性・実授業由来・transcriptの監査対象run由来・30区間の意味対応・person identity/資格/review attestation真正性・意味評価を個別確認してから、`story_tracker.py sync-field --run-dir docs/field/evidence/<実施日> --reviewer <実施者> --approve-field-candidates` で単一台帳へ反映する。受領側はgate・manifest・artifact hash・学校PCのstart/end source snapshot・性能2run・品質証拠を再監査し、review receiptと更新前backupを `docs/qa/runs/*-field-import/` に残す。後続のpytest/browser/soak/scale/auditは今回の補助runに含まれないfield合格もすべて再監査し、有効な合格を保持して、新しい明示的失敗または証拠失効を優先する
- [ ] #19へ投稿する前に、レポートと証拠をcommit/pushするか権限付き添付URLへ置換し、遠隔レビュアーが参照できることを確認
- [ ] 発見した問題を**個別イシューとして起票**（誤認識サンプル・遅延の詳細・端末固有の不具合 等）

**総合判定**: 授業投入 可 / 条件付き可（条件: __________________ ）/ 否（要対応: __________________ ）。「可」は外部証拠8件に加え、採用先生経路の実マイク、教室音声、瞬断・スリープ復帰のhash済み証拠がすべて揃う場合だけ。

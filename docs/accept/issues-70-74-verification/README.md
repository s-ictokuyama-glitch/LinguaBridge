# Pipelineの排出・発話中表示の検証

対象はIssue #70〜#74。基点はlocalServer2のf4d664523a9b2ceeceedd327bcc5dfaadc95dd7a。実モデル試験はc1dd8ce0e5fb2c0668985e10569a66ad09bc42f3のソースで実行した。

## 実行構成

Windows 11、AMD Ryzen 7 8840U、8コア16スレッド、RAM 16.4GB、Python 3.12.10。ASRはSherpa/ReazonSpeech K2 v2、翻訳はHy-MT2 1.8B Q4_K_M、VADはSilero、Turnはmorph、partialは無効、発話中表示は有効。生徒10接続に英語と中国語を割り当てた。音源は既存jaの合成10文を無音を挟んで繰り返した。翻訳キャッシュは既定512件で、定型文の反復によりキャッシュヒットが増える構成である。

```powershell
.venv/Scripts/python.exe scripts/replay_client.py --minutes 2 --no-config-update --engine hy-mt2 --port 18170 --out-dir docs/accept/issues-70-74-smoke
.venv/Scripts/python.exe scripts/replay_client.py --minutes 45 --no-config-update --engine hy-mt2 --port 18174 --out-dir docs/accept/issues-70-74-soak
```

合格構成は既定設定へ書き戻していない。config.yamlの実行前SHA256は69a76368c584ca261ddf19c1af07b552f262607040a249e03abbd03e278808cc。実行後のSHA256も同一だった。各試験の実効設定を同じ記録フォルダへ保存する。

## 判定

2分スモークは合格。字幕38件、遅延中央値0.64秒、最大1.94秒、ピーク常駐メモリ2587MB。クラッシュ・切断・再接続失敗・音声破棄は0件。

45分試験は1回実行して合格。音声2700秒、排出待ちを含む実行時間2720.1秒。字幕870件、遅延中央値0.09秒、p95 0.17秒、最大1.91秒。ピーク常駐メモリ2607MB、前半/後半の中央値2523→2513MB。遅延ドリフトなし、空の5分窓なし。スレッド32→32、ハンドル468→468、asyncioタスク29→29で、キューの増加傾向もない。クラッシュ・切断・再接続失敗・音声破棄は0件。

翻訳キャッシュのヒット率は95.9%。ASRは448回、翻訳モデルの推論は36回であり、継続的な翻訳推論の最大負荷試験ではない。

[45分結果](../issues-70-74-soak/2026-10-24-acceptance.md)と[実効設定](../issues-70-74-soak/effective-config.yaml)、[ソース・音源・設定のmanifest](../issues-70-74-soak/manifest.json)を保存した。

## 独立検証

実装を書いていないgpt-6.1-solが、実モデルASR/翻訳の統合テストを除く全636テストを実行して成功。最終ソースで関連50テストも再実行して成功した。gpt-6-astraの静的レビューにも修正必須の指摘はなかった。mypyは37ソースで成功した。

公開APIドライバーは、ASRを停止してdrainを開始し、ASR解放後に発生した翻訳を別のゲートで停止する。翻訳を解放するまでdrainが完了しないこと、字幕の訳文・呼出回数・キューの空状態・一時停止後の入力無視を確認した。

テスト環境ではASCIIパスの一時領域とWindows PowerShell標準のPSModulePath、既存Python環境を指すworktree内の.venvを使用した。これにより変更前にも発生した文字コード・モジュール探索・開発環境不在の失敗を解消した。製品コードによる回避は加えていない。

Webクライアントとconfig.yamlに変更はない。未追跡scripts/qa_native_browser.pyの内部カウンタ参照2か所は作業ツリーだけで削除した。

この結果は開発機での合成音源リプレイである。学校のサーバーPC・学校LAN・物理マイク・実授業録音の検証結果ではない。

変更前後へ同一PCMを入力した独立比較でも、初期・投入後・一時停止時のstats全22項目、3名へ各3字幕、3件のJSONL記録、ASR/翻訳の呼出回数、通常の発話中遷移が一致した。処理時間と作成時刻だけを正規化した。[比較結果](comparison.json)に実出力を保存した。

排出待ちドライバーはリポジトリルートで `.venv/Scripts/python.exe docs/accept/issues-70-74-verification/drain_driver.py` を実行して再確認できる。

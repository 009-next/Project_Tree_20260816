# Project_Tree

建設・土木の現場資料（議事録・メール・現場写真）を読み込み、
**時系列の台帳・5段階のストーリー・資料用イメージ図・2D/3Dモデル**を1つの画面でつなぐアプリ。
2026-08 ハッカソン提出物。

> **起動方法は [First_READ.md](First_READ.md) を読んでください。**

**公開URL（インストール不要・閲覧専用）**: デプロイ後にここへ記載します。
同梱データの閲覧・資料出力・2D/3Dモデル操作が試せます。
資料の取り込みとLLM実行は、公開先では無効にしています（下記のexeかPythonでお試しください）。

**exeで起動（Python不要）**: [Releases](https://github.com/009-next/Project_Tree/releases) から
`Project_Tree_windows.zip` をダウンロード → 展開 → `Project_Tree.exe` をダブルクリック。

**Pythonで起動**:

```bash
pip install -r requirements.txt
python run_app.py
```

---

## 設計方針

> **LLMは1回だけ呼ぶ。その後の検索・集計・整合性チェックはすべてSQLで行う。**

LLMの「揺れる・嘘をつく・高い」は、毎回生成し直していることに起因します。
生成回数を1に固定し、生成物を台帳（SQLite）に確定させることで3つとも抑え込みます。

台帳は2つのレーンに分かれます。

| レーン | 何をするか | 保証 |
|---|---|---|
| **rule レーン** | SQLだけで検出できる事実（食い違い・記録の空白）を機械的に検出 | 決定的・常にトレース可能 |
| **llm レーン** | LLMが仮説を立てる（原因究明・新発見） | 根拠 `event_id` の実在をコードで検証。引けない主張は「仮説・未検証」へ強制的に落とす |

さらに、**LLMを呼ぶ処理はすべて実行前に概算費用を提示し、承認するまでAPIを呼びません。**

---

## ファイル構成

### 起動に必要なもの

```
run_app.py            起動スクリプト（これを実行する）
server.py             FastAPI 本体。全APIエンドポイント
ledger.py             SQLite 台帳のスキーマ定義とアクセス
paths.py              実行環境ごとのパス解決（開発実行 / exe）
llm.py                モデル定数と原価計算
ledger.db             台帳データ本体（デモ用データを同梱）
requirements.txt      依存パッケージ

projecttree/          Project_Tree の機能モジュール
  ├─ autorun.py       「▶出力」の一括実行（9工程）・原価見積り
  ├─ stages.py        5段階のストーリーテリング分類（ルール主軸）
  ├─ inference.py     LLM推論・段階分類・部位対応（引用検証つき）
  ├─ illustrate.py    資料用イメージ図（SVG生成・部品単位の編集）
  ├─ slides.py        段階カード用の画像生成
  ├─ modelgen.py      2D/3Dモデル生成（trimesh + ezdxf・Blender不要）
  ├─ progress.py      部材進捗・全体進捗の算出
  ├─ exporters.py     資料出力（md / xlsx / pptx）
  ├─ docs.py          資料出力（pdf / word / 画像入りpptx）
  ├─ intake.py        資料の取り込みと重複判定
  ├─ foldersync.py    フォルダ単位の取り込み
  ├─ docthread.py     資料と案件の明示的な対応づけ
  ├─ projects.py      案件の統合（同名で割れたものをまとめる）
  ├─ visibility.py    案件の表示絞り込み（データは消さない）
  ├─ vision.py        写真から部材を推定
  ├─ blender.py       Blender連携（任意・未接続でもアプリは動く）
  ├─ masking.py       API送信前の機密情報マスキング
  ├─ models.py        モデルtier割り当て・構造化出力の後処理
  ├─ provider.py      接続先の切り替え（Anthropic直 / Orca Router）
  └─ security.py      トークン認証・PDF検証・予算管理

static/
  ├─ projecttree.html Project_Tree の画面（既定の入口）
  ├─ timeline.html    従来の台帳UI
  ├─ lib/             three.js 等（同梱・CDN不要）
  └─ models/          生成済み 2D/3Dモデル

assets/
  ├─ illust/          生成済みイメージ図（PNG）
  ├─ slides/          生成済み段階カード画像
  ├─ models/          生成済みモデル
  └─ blender/         Blender経由で書き出したモデル

uploads/models/       取り込んだ図面・モデルファイル
```

### 台帳パイプライン（コマンドラインからも実行可能）

```
extractor.py    資料 → イベント・主張の抽出（LLM 1回）
threader.py     イベント → 案件への割り当て
patterns.py     反復・連鎖の検出（LLM不使用）
gaps.py         記録の空白の検出（LLM不使用）
reconcile.py    食い違いの検出
insight.py      洞察・予測の生成
ingest.py       資料の一括読み込み
schema.json     データ構造の正本（検証制約込み）
```

### ドキュメント・提出物

```
First_READ.md                   起動方法（審査員向け）
紹介記事_Project_Tree.md          設計の狙いと開発中の実バグ
Project_Tree_プレゼン資料.md       プレゼン資料（Marp形式のソース）
Project_Tree_プレゼン資料.pptx     プレゼン資料（10枚）
scratch_out/demo_video/         デモ動画（約2分）
scratch_out/deck_shots/         資料に使用したスクリーンショット
Project_Tree.spec               PyInstaller 設定（exe化する場合）
build_exe.py                    exeのビルドスクリプト（python build_exe.py）
app_public.py                   公開URL用の起動口（閲覧専用ガードを被せる）
deploy/huggingface/             公開URL（Hugging Face Spaces）の設定一式
```

---

## 技術スタック

- **FastAPI**（Python）／ **SQLite** 単一ファイル `ledger.db`
- **three.js**（3Dビューア・ライブラリは同梱、CDN不要）
- **trimesh + ezdxf**（2D/3Dモデルのローカル生成。**Blender不要・費用0円**）
- LLMは工程別に tier を分けて割り当て（light / medium / heavy）

2D/3Dモデルは、LLMに「寸法・形状パラメータのJSON」だけを出させ、
実際の立体化は常にローカルのコードが行います。Blender連携は任意の追加経路で、
Blenderが起動していなくてもモデルの作成・表示・出力はそのまま動きます。

---

## セキュリティ

- APIキーはプロセスのメモリ上のみに保持し、ファイルに書き出さない。画面表示は末尾4文字のみ
- `127.0.0.1` 固定バインド
- 資料はAPI送信前に機密情報をマスキング（`masking.py`）
- PDFはマジックナンバー・サイズ・暗号化を検証してから取り込む
- 同一ハッシュの資料は再解析しない（原価の無駄撃ちとDoSの抑止）
- LLMの出力は `schema.json` で構造を強制し、違反した出力は**破棄する**（LLMに修正させない）

---

## 既知の制約

- 同梱の `ledger.db` はハッカソン用の合成データです（実案件の記録ではありません）
- 同梱データは、紹介する4案件（護岸・橋梁・解体・上水道管）に絞ってあります。資料27件・イベント103件・主張76件・イメージ図19枚
- pdf / word 出力は PyMuPDF・python-docx に依存します（`requirements.txt` に含まれています）

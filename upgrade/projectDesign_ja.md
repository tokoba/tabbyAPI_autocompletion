# TabbyAPI プロジェクト設計書

## 1. はじめに

このドキュメントは、TabbyAPIプロジェクトの詳細な設計について記述したものです。プロジェクトの構造、各コンポーネントの役割、そして主要な機能の実装に関する技術的な詳細を説明します。この文書は、将来の機能拡張や保守作業の際の参照資料となることを目的としています。

## 2. プロジェクト構造

プロジェクトは、機能ごとに明確に分離されたディレクトリ構造を持っています。

```
/
├── backends/         # 推論バックエンドのインターフェースと実装
├── common/           # プロジェクト全体で共有される共通モジュール
├── endpoints/        # APIエンドポイントの定義とロジック
│   ├── core/         # サーバー管理用のコアAPI
│   ├── Kobold/       # KoboldAI互換API
│   └── OAI/          # OpenAI互換API
├── templates/        # チャットプロンプト用のJinja2テンプレート
├── config_sample.yml # 設定ファイルの見本
├── main.py           # アプリケーションのエントリーポイント
└── pyproject.toml    # プロジェクトの依存関係と設定
```

### 2.1. `backends` ディレクトリ

このディレクトリには、LLM推論を行うためのバックエンドとのインターフェースが含まれます。

- **`base_model_container.py`**: すべての推論バックエンドが実装すべき抽象基底クラスを定義します。`generate`, `stream_generate`, `encode_tokens`, `decode_tokens` などの共通インターフェースを規定します。
- **`exllamav2/`**, **`exllamav3/`**: `exllamav2`および`exllamav3`バックエンドの具体的な実装です。モデルのロード、VRAM管理、キャッシュ設定、推論ロジックなどが含まれます。
- **`infinity/`**: `infinity-emb`ライブラリを使用した埋め込みモデルのバックエンド実装です。

### 2.2. `common` ディレクトリ

アプリケーション全体で共有されるユーティリティやコア機能が含まれます。

- **`tabby_config.py`**: `config.yml`、環境変数、コマンドライン引数から設定をロードし、管理するクラス `TabbyConfig` を定義します。
- **`config_models.py`**: Pydanticモデルを使用して、設定ファイルの構造を厳密に定義します。
- **`model.py`**: 現在ロードされているモデルコンテナ（`container`）と埋め込みモデルコンテナ（`embeddings_container`）のグローバルな状態を管理します。モデルのロード、アンロード、バックエンドの選択ロジックもここにあります。
- **`auth.py`**: APIキー（通常および管理者）による認証ロジックを実装します。キーは `api_tokens.yml` に保存されます。
- **`sampling.py`**: サンプリングパラメータのデフォルト値と、それを上書きするためのプリセット（`sampler_overrides/` 内のYAMLファイル）を管理します。
- **`templating.py`**: Jinja2テンプレートをロードし、チャットメッセージリストを単一のプロンプト文字列に変換する `PromptTemplate` クラスを提供します。
- **`downloader.py`**: HuggingFace HubからモデルやLoRAを非同期でダウンロードする機能を提供します。
- **`hardware.py`**: Flash Attentionなどのハードウェア機能のサポート状況を確認します。
- **`health.py`**: サービスが正常でないイベントを追跡し、ヘルスチェックエンドポイントに情報を提供します。

### 2.3. `endpoints` ディレクトリ

APIのエンドポイント定義と、各リクエストを処理するロジックが含まれます。

- **`server.py`**: FastAPIアプリケーションをセットアップし、設定に基づいてOAIやKoboldAIのルーターを動的にインクルードします。
- **`core/`**: モデルのロード/アンロード、LoRAの管理、テンプレートの切り替えなど、サーバーのコア機能を管理するためのAPIエンドポイントです。
- **`OAI/`**: OpenAI互換エンドポイントの実装。
  - **`router.py`**: `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` のエンドポイントを定義します。
  - **`utils/`**: 各エンドポイントのロジックを実装します。
    - `chat_completion.py`: チャットテンプレートの適用、ツール呼び出しの処理、ストリーミング応答の生成など。
    - `completion.py`: 標準的なプロンプト補完の処理。
    - `embeddings.py`: 埋め込み生成の処理。
- **`Kobold/`**: KoboldAI互換エンドポイントの実装。同様の構造で、KoboldAIクライアントからのリクエストを処理します。

### 2.4. `templates` ディレクトリ

チャット形式のプロンプトを生成するためのJinja2テンプレートが格納されています。

- **`chatml.jinja`**, **`alpaca.jinja`**: 一般的なチャット形式のテンプレート例。
- **`tool_calls/`**: ツール呼び出しをサポートする、より複雑なテンプレート。
- テンプレート内には、`stop_strings` や `tool_start` といったメタデータを定義でき、`templating.py` によって抽出されて推論時に利用されます。

## 3. 主要機能の実装詳細

### 3.1. モデルのロードと管理 (`common/model.py`)

1. **グローバルコンテナ**: ロードされたモデルは、グローバル変数 `model.container` に `BaseModelContainer` のサブクラスとして保持されます。
2. **バックエンドの自動検出**: モデルをロードする際、`detect_backend` 関数がモデルの `config.json` を調べて、`quant_method` (`exl2` or `exl3`) に基づいて適切なバックエンド（`exllamav2` or `exllamav3`）を選択します。
3. **設定のマージ**: モデルのロード時には、以下の優先順位で設定がマージされます。
    1. APIリクエストで渡された引数 (`ModelLoadRequest`)
    2. モデルディレクトリ内の `tabby_config.yml` (インライン設定)
    3. グローバルな `config.yml` の `model_defaults`
    4. グローバルな `config.yml` の設定
4. **非同期ロード**: モデルのロードは `load_model_gen` ジェネレータを介して行われ、進捗状況がストリーミングでクライアントに送信されます。

### 3.2. チャット補完とテンプレート (`endpoints/OAI/utils/chat_completion.py`)

1. **テンプレートの検索**: チャット補完リクエストを受け取ると、`find_prompt_template` が以下の順序で適切なテンプレートを探します。
    1. リクエストで指定されたテンプレート名
    2. モデルディレクトリ内の `tokenizer_config.json`
    3. `templates/` ディレクトリ内の一致する名前のファイル
2. **プロンプトのレンダリング**: `format_messages_with_template` が、選択されたJinja2テンプレートとメッセージリストを使って、最終的なプロンプト文字列を生成します。この際、モデルの特殊トークン（BOS/EOSなど）もテンプレートに渡されます。
3. **Visionサポート**: メッセージに画像URLが含まれている場合、`MultimodalEmbeddingWrapper` が画像をダウンロードし、Visionバックエンド（例: `exllamav2.vision`）を使って画像埋め込みを生成します。この埋め込みは、テキストプロンプトと共にモデルに渡されます。
4. **ツール呼び出し**:
    - テンプレートに `tool_start` マーカーが定義されている場合、ツール呼び出し機能が有効になります。
    - モデルが `tool_start` マーカーを生成すると、推論が一旦停止します。
    - その後、`generate_tool_calls` が、ツール定義（`TOOL_CALL_SCHEMA`）に準拠したJSONを生成するようにモデルに再度プロンプトを投げます。
    - 生成されたJSONはパースされ、`tool_calls`としてクライアントに返されます。

### 3.3. 認証 (`common/auth.py`)

- **キーの読み込み**: 起動時に `load_auth_keys` が `api_tokens.yml` を読み込みます。ファイルが存在しない場合は、新しいAPIキーと管理者キーを生成して保存します。
- **キーの検証**: `check_api_key` と `check_admin_key` はFastAPIの`Depends`として使用され、リクエストヘッダー（`X-Api-Key`, `X-Admin-Key`, `Authorization: Bearer`）からキーを取得して検証します。
- **権限**: 管理者キーは、通常キーでアクセスできるすべてのエンドポイントに加えて、モデルのロード/アンロードなどの管理者権限が必要なエンドポイントにもアクセスできます。

### 3.4. 設定管理 (`common/tabby_config.py`)

- **階層的ロード**: `TabbyConfig` クラスの `load` メソッドが、以下の優先順位で設定を読み込み、マージします。
    1. コマンドライン引数
    2. 環境変数 (例: `TABBY_NETWORK_PORT=5001`)
    3. `config.yml` ファイル
- **Pydanticによる検証**: ロードされた設定は `TabbyConfigModel` Pydanticモデルによって検証され、型や値が不正な場合はエラーが発生します。
- **設定のエクスポート**: `generate_config_file` 関数は、Pydanticモデルの定義と説明文から `config_sample.yml` を生成します。

## 4. 今後の拡張性

- **新しいバックエンドの追加**: `backends/base_model_container.py` の `BaseModelContainer` を継承した新しいクラスを作成し、`common/model.py` の `_BACKEND_REGISTRY` に登録するだけで、新しい推論バックエンドを簡単に追加できます。
- **新しいAPI互換性の追加**: `endpoints/` ディレクトリに新しいサブディレクトリを作成し、FastAPIのルーターを定義すれば、新しいAPI（例: Anthropic互換API）を追加できます。
- **カスタムロジックの追加**: 既存の `utils` モジュールを拡張するか、新しいモジュールを追加することで、特定の機能（例: より高度なキャッシング戦略）を実装できます。

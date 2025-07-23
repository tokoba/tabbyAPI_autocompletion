# リファクタリング計画書: 推論リクエストキャンセル機能の実装 (v2)

## 1. 目的

本リファクタリングの目的は、`v1/completions`エンドポイント（特にストリーミング応答の`stream_generate_completion`）において、連続して発生する推論リクエストを効率的に処理するためのキャンセル機能を導入することです。VS Codeの`continue.dev`のようなタブ補完拡張機能からの利用を想定しています。
ユーザーが文字を入力するたびに新しいリクエストが発行されるユースケースにおいて、最新のリクエストのみを処理し、それ以前の古いリクエストは自動的にキャンセルする仕組みを構築します。これにより、バックエンドの不要な負荷を削減し、ユーザーへの応答性を向上させます。

## 2. 現状の課題

現在の`endpoints/OAI/utils/completion.py`内の`stream_generate_completion`関数は、受け取ったリクエストをすべて非同期タスクとして生成し、バックエンドのキューに追加します。

- **過剰なキューイング**: ユーザーが連続して文字を入力すると、そのすべてが推論リクエストとしてキューに積まれてしまいます。
- **リソースの浪費**: ユーザーにとっては既に不要となった古いコンテキストに基づく推論が、バックエンドで実行され続けてしまい、計算リソースを無駄に消費します。
- **応答性の低下**: 多くのリクエストがキューに溜まることで、本当に必要な最新のリクエストの処理が遅れる可能性があります。

## 3. 提案する解決策

### 3.1. 設計方針の更新

当初の計画では`asyncio.Task.cancel()`を呼び出すことを想定していましたが、コードを詳細に調査した結果、`stream_generate_completion`とその呼び出し先である`_stream_collector`、さらにバックエンドの`stream_generate`に至るまで、**`abort_event`という非同期イベントを利用した、より安全なキャンセル機構が既に存在している**ことが判明しました。

この既存の仕組みを活用することで、`CancelledError`例外の複雑な伝播を考慮する必要がなくなり、より安全かつシンプルにキャンセル機能を実現できます。

**更新後の設計方針:**

1. **リクエスト管理の対象変更**: `asyncio.Task`の代わりに、各リクエストに紐づく`asyncio.Event`（`abort_event`）をセッションごとに管理します。
2. **キャンセル方法**: 新しいリクエストが来た際に、管理している古いリクエストの`abort_event.set()`を呼び出します。これにより、バックエンドの推論ループがイベントを検知し、安全に処理を中断します。
3. **フロントエンドの責務**: `stream_generate_completion`関数は、セッションごとの`abort_event`を管理し、適切なタイミングでセットする責務を持ちます。
4. **バックエンドの責務**: バックエンド(`ExllamaV3Container`)は、`abort_event`がセットされたことを検知して推論を中断する責務（これは既に実装済みと想定）と、デバッグ目的で**現在の推論キューのサイズをログに出力する**新たな責務を持ちます。

### 3.2. 問題の切り分け

- **フロントエンドのログ**: 「Session [session_id]: Cancelling previous request.」といったログを記録することで、TabbyAPI側でキャンセル処理が正しく発行されたことを確認できます。
- **バックエンドのログ**: 「Request [request_id] aborted by client event.」といったログを`abort_event`検知時に記録することで、バックエンドがキャンセル要求を受け取り、適切に処理を中断したことを確認できます。
- **キューサイズのログ**: 「Current inference queue size: [N]」というログにより、リファクタリング前後でのキューイング状況の変化を定量的に評価できます。

## 4. 更新された作業ブレークダウン (WBS)

| ID  | タスク内容                                                               | 主要な影響ファイル                                                                                             | 状態     |
|:----|:-------------------------------------------------------------------------|:---------------------------------------------------------------------------------------------------------------|:---------|
| 1   | **推論リクエスト管理クラスの実装**<br>セッションID (`str`) と`abort_event` (`asyncio.Event`) を紐付けて管理する`InferenceRequestManager`クラスを新規作成する。このクラスはスレッドセーフ（`asyncio.Lock`を使用）であること。 | `common/concurrency.py` (または新規ファイル `common/request_manager.py`)                                       | ✅完了   |
| 2   | **`stream_generate_completion`の改良**<br> - `InferenceRequestManager`を統合する。<br> - リクエストからセッションID (`request.client.host`) を特定する。<br> - 新規リクエスト処理の開始時に、同じセッションの古いリクエストを`InferenceRequestManager`経由でキャンセル (`abort_event.set()`) する。<br> - 新しい`abort_event`をマネージャーに登録する。<br> - `finally`ブロックで、完了したリクエストの情報をマネージャーからクリーンアップする。 | `endpoints/OAI/utils/completion.py`                                                                            | ✅完了   |
| 3   | **バックエンドのロギング機能強化**<br>`ExllamaV3Container`の`stream_generate`メソッド、またはそれが呼び出す内部の推論処理ループを変更し、**推論ジョブのキューイング/デキューイング時**に現在のキューサイズをログに出力する。 | `backends/exllamav3/model.py`                                                                                  | ✅完了   |
| 4   | **統合テストと検証**<br> - 擬似的な連続リクエストを非同期で送信するテストスクリプトを作成する。<br> - ログを監視し、古いリクエストが「aborted」または「cancelled」と記録されること、およびキューサイズのログが常に少数（理想的には1〜2）に保たれることを確認する。 | `tests/` (新規テストファイル `tests/test_completion_cancellation.py`)                                          | ✅完了   |

## 5. 最終的な修正内容の詳細

### 5.1. `common/concurrency.py` の変更

**変更点:**
`InferenceRequestManager`クラスを新規に実装し、`remove_request`メソッドのシグネチャを修正しました。

```python
# common/concurrency.py

class InferenceRequestManager:
    """Manages active inference requests to allow for cancellation."""

    def __init__(self):
        self._requests: Dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def add_request(self, session_id: str, abort_event: asyncio.Event):
        """Adds a new request, cancelling any existing request for the same session."""
        async with self._lock:
            if session_id in self._requests:
                logger.info(f"Session {session_id}: Setting abort event for previous request.")
                self._requests[session_id].set()
            self._requests[session_id] = abort_event

    async def remove_request(self, session_id: str, abort_event: asyncio.Event):
        """
        Removes a request from the manager, only if the event matches.
        This prevents a cancelled request from removing a new, valid request.
        """
        async with self._lock:
            if session_id in self._requests and self._requests[session_id] is abort_event:
                del self._requests[session_id]

inference_request_manager = InferenceRequestManager()
```

**変更理由:**

- **`InferenceRequestManager`クラス**: セッションIDごとに進行中のリクエスト(`abort_event`)を管理するための中央集権的なクラスです。`asyncio.Lock`により、複数のリクエストが同時にこのクラスのデータ（`_requests`辞書）を変更しようとしても、競合状態が発生しないように保護されています（スレッドセーフ/非同期セーフ）。
- **`add_request`**: 新しいリクエストが来た際に、同じセッションIDの古いリクエストがあれば、その`abort_event`をセットしてキャンセル信号を送ります。その後、新しいリクエストの`abort_event`を登録します。
- **`remove_request`の修正**: 当初の実装では、`remove_request(session_id)`は単純にそのセッションIDのエントリを削除していました。しかし、これでは「リクエストAがキャンセルされる -> リクエストBがすぐに開始・登録される -> 遅れてリクエストAのクリーンアップが走る -> リクエストBの情報が誤って削除される」という競合状態が発生する可能性がありました。修正後の`remove_request(session_id, abort_event)`は、**削除対象のイベントが、現在登録されているイベントと同一であること**を確認してから削除します。これにより、古いリクエストのクリーンアップが新しいリクエストに影響を与えることを防ぎ、堅牢性を大幅に向上させています。

### 5.2. `endpoints/OAI/utils/completion.py` の変更

**変更点:**
`stream_generate_completion`関数内で`InferenceRequestManager`を利用するように変更しました。

```python
# endpoints/OAI/utils/completion.py

# ... imports
from common.concurrency import inference_request_manager

# ...

async def stream_generate_completion(
    data: CompletionRequest, request: Request, model_path: pathlib.Path
):
    session_id = request.client.host
    abort_event = asyncio.Event()
    # ...
    
    await inference_request_manager.add_request(session_id, abort_event)
    logger.info(f"Session {session_id}: Registered new request {request.state.id}.")

    try:
        # ... (main generation loop)
        # ...
        while True:
            # ...
            if abort_event.is_set():
                logger.info(f"Session {session_id}: Aborting request {request.state.id} due to new request.")
                while not gen_queue.empty():
                    gen_queue.get_nowait()
                break
            # ...
    # ...
    finally:
        # Clean up the request from the manager
        await inference_request_manager.remove_request(session_id, abort_event)
        logger.info(f"Session {session_id}: Cleaned up request {request.state.id}.")
```

**変更理由:**

- **マネージャーへの登録**: `try`ブロックの直前で`inference_request_manager.add_request`を呼び出し、新しいリクエストの`abort_event`を登録すると同時に、同じセッションの古いリクエストをキャンセルします。
- **アボート検知**: `while`ループ内で`abort_event.is_set()`をチェックします。イベントがセットされた場合、それは新しいリクエストによってキャンセルされたことを意味するため、ループを抜けて処理を中断します。
- **安全なクリーンアップ**: `finally`ブロックで、`inference_request_manager.remove_request`を呼び出します。このとき、自身が管理していた`abort_event`を渡すことで、前述の競合状態を回避し、安全に自身のエントリのみをクリーンアップします。

### 5.3. `backends/exllamav3/model.py` の変更

**変更点:**
`stream_generate`メソッドと`generate_gen`メソッドにデバッグログを追加しました。

```python
# backends/exllamav3/model.py

# in stream_generate method
try:
    # ...
    self.active_job_ids[request_id] = None
    logger.info(f"Request {request_id} added to queue. Current queue size: {len(self.active_job_ids)}")
    # ...
finally:
    if request_id in self.active_job_ids:
        del self.active_job_ids[request_id]
    logger.info(f"Request {request_id} finished. Current queue size: {len(self.active_job_ids)}")

# in generate_gen method
try:
    async for result in job:
        if abort_event and abort_event.is_set():
            logger.info(f"Request {request_id}: Detected abort event in backend.")
            await job.cancel()
            break
        # ...
```

**変更理由:**

- **キューサイズの可視化**: `stream_generate`メソッドの最初と最後で`active_job_ids`（推論キューとして機能）のサイズをログに出力することで、キャンセル機能が正しく動作し、キューが肥大化していないことを定量的に確認できるようにしました。キャンセル機能が正常に動作している場合はキューのサイズは最大1になります。もしキューのサイズが1以上であれば、キャンセル機能に問題がある可能性があります。これは開発者や運用者がリアルタイムで異常な動作を行っているかを迅速に特定するのに役立ちます。
- **ジョブIDの追跡**: ジョブIDを使用して各推論ジョブの進行状況や終了状況を追跡できます。これにより、特定のジョブに関する情報を抽出しやすくなります。
- **バックエンドでのキャンセル検知**: `generate_gen`メソッド内で`abort_event`を検知した際にログを出力することで、フロントエンドからのキャンセル信号が、どのタイミングでバックエンドに到達し、実際に処理が中断されたかを明確に追跡できるようにしました。これは、問題発生時の原因切り分けに極めて有効です。

## 6. この設計の優位性

今回の修正で採用された設計は、いくつかの点で優れています。

1. **既存の仕組みの活用（協調的キャンセル）**: `Task.cancel()`による強制的なキャンセルではなく、`exllamav3`バックエンドが元々持っていた`abort_event`という仕組みを利用しました。これは「協調的キャンセル」と呼ばれ、バックエンドが自身の安全なタイミングで処理を中断できるため、リソースリークや状態の不整合といったリスクを最小限に抑えることができます。
2. **堅牢なリクエスト管理**: 競合状態を考慮した`InferenceRequestManager`の実装により、高速にリクエストが生成・キャンセルされる環境でも、各リクエストの状態を正確かつ安全に管理できます。特に、クリーンアップ処理が他のリクエストに影響を与えない設計は、システムの安定性にとって重要です。
3. **優れたデバッグ性**: フロントエンド（キャンセル発行）とバックエンド（キャンセル検知、キューサイズ）の両方に詳細なログを導入したことで、システムの内部状態が可視化されました。これにより、パフォーマンスのボトルネックや、キャンセル処理の遅延といった問題を迅速に特定し、解決することが可能になります。

総じて、この設計は**安全性、堅牢性、保守性**のバランスが取れており、高頻度なリクエストが想定される本番に近い環境でも安定して動作するための強固な基盤となります。

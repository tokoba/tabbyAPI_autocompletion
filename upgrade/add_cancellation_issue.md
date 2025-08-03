# Issue: Inference Request Cancellation Regression

## 1. Summary

A regression bug has been identified in the inference request cancellation mechanism. Previously, new requests for the same session would cancel any ongoing requests, keeping the inference queue size minimal (ideally 1). Currently, old requests are not being cancelled, causing the queue to grow indefinitely with each new request. This leads to unnecessary resource consumption and delays in processing the latest, most relevant request.

## 2. How to Reproduce

### Prerequisites
- Start the TabbyAPI server.
- Have a tool like `curl` available to send requests.

### Steps

1.  **Start the server in the background:**
    Open a terminal and run the server. Make sure it's running in the background or in a separate terminal window so you can execute `curl` commands.
    ```bash
    ./start_uv.sh
    ```

2.  **Send rapid, consecutive streaming requests:**
    Open another terminal and execute a loop of `curl` commands to simulate the rapid requests sent by an autocompletion client. The `&` at the end of the command sends it to the background, immediately allowing the next request to be sent without waiting for a response.

    ```bash
    for i in {1..10}; do
      curl -N -X POST http://localhost:5000/v1/completions \
      -H "Content-Type: application/json" \
      -d 
      {
        "model": "any-model",
        "prompt": "def hello_world():\n  ",
        "stream": true
      }
    \
      } & 
      sleep 0.2 # Small delay between requests
    done
    ```

3.  **Observe the server logs:**
    Monitor the logs from the TabbyAPI server.

## 3. Logs (Observed Behavior)

The server logs show the `Current queue size` incrementing with each new request, indicating that previous requests are not being cancelled.

```log
2025-08-03 08:04:57.987 INFO:     Request a7f0fbe191dd4235bfd1c5eb19586645 added to queue. Current queue size: 1
2025-08-03 08:04:57.990 INFO:     Session 127.0.0.1: Cleaned up request a7f0fbe191dd4235bfd1c5eb19586645.
2025-08-03 08:04:57.991 INFO:     Request f7733279e64d43aea57ef4ceefd97b39 added to queue. Current queue size: 2
2025-08-03 08:05:01.464 INFO:     Request f7733279e64d43aea57ef4ceefd97b39 finished. Current queue size: 1
2025-08-03 08:05:01.906 INFO:     Request 841affaadd8a486188cad790822035ac added to queue. Current queue size: 3
2025-08-03 08:05:02.305 INFO:     Request b0975458af694f58a622c77fe20ff41d added to queue. Current queue size: 4
2025-08-03 08:05:04.014 INFO:     Request 7372a9756f4e4dc1a910abc6180b5e00 added to queue. Current queue size: 5
... 
2025-08-03 08:05:10.748 INFO:     Request 298fe4f1f6804aefb85562649c0060ac added to queue. Current queue size: 10
```

The expected behavior is that the queue size should remain at 1, as each new request should trigger the cancellation of the previous one.

## 4. Investigation

The root cause appears to be a timing issue related to the lifecycle of the streaming generator function (`stream_generate_completion`) and the cleanup of the request from the `InferenceRequestManager`.

1.  **Problematic Log Entry:** The log `Session ...: Cleaned up request ...` appears very shortly after the request is added to the queue. This log is emitted from the `finally` block of the `stream_generate_completion` function in `@endpoints/OAI/utils/completion.py`.
2.  **Generator Lifecycle:** When a client sends a request and doesn't wait for the full streaming response (e.g., `curl ... &`), the connection may be closed prematurely from the server's perspective. This can cause the generator object on the server to be garbage-collected.
3.  **Premature Cleanup:** The `finally` block of a generator is executed when it's garbage-collected. This means `inference_request_manager.remove_request()` is called *before* the actual backend inference task (`_stream_collector`) has finished.
4.  **Cancellation Failure:** Because the request's `abort_event` is removed from the manager prematurely, when the next request arrives, the manager no longer has a record of the previous request's event to set for cancellation.
5.  **Result:** The old inference task continues to run in the background, and the new task is simply added to the queue, leading to the ever-increasing queue size.

## 5. Proposed Solution

To fix this, the cleanup logic (`remove_request`) must be decoupled from the generator's lifecycle and tied directly to the completion of the backend inference task.

1.  **Modify `InferenceRequestManager.remove_request`:** Change the function to return a boolean value indicating whether a request was actually removed. This will prevent duplicate logging when multiple generation tasks (`n > 1`) for a single request complete.
2.  **Use a Done Callback:** In `stream_generate_completion`, instead of using a `finally` block for cleanup, attach a callback to the background inference task (`_stream_collector` task) using `task.add_done_callback()`.
3.  **Implement the Callback:** This callback will call the modified `remove_request` function. It will only log the "Cleaned up" message if a request was actually removed, ensuring the log is printed only once per request.

This approach ensures that the request is removed from the manager only when the inference is truly finished or cancelled, correctly restoring the cancellation logic.

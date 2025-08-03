"""
Completion utilities for OAI server.

Also serves as a common module for completions and chat completions.
"""

import asyncio
import pathlib
from asyncio import CancelledError
from fastapi import HTTPException, Request
from loguru import logger
from typing import List, Optional, Union

from common import model
from common.auth import get_key_permission
from common.concurrency import inference_request_manager
from common.multimodal import MultimodalEmbeddingWrapper
from common.networking import (
    get_generator_error,
    handle_request_disconnect,
    handle_request_error,
    request_disconnect_loop,
)
from common.tabby_config import config
from common.utils import unwrap
from endpoints.OAI.types.completion import (
    CompletionRequest,
    CompletionResponse,
    CompletionRespChoice,
    CompletionLogProbs,
)
from endpoints.OAI.types.common import UsageStats


def _parse_gen_request_id(n: int, request_id: str, task_idx: int):
    if n > 1:
        return f"{request_id}-{task_idx}"
    else:
        return request_id


def _create_response(
    request_id: str, generations: Union[dict, List[dict]], model_name: str = ""
):
    """Create a completion response from the provided choices."""

    # Convert the single choice object into a list
    if not isinstance(generations, list):
        generations = [generations]

    choices: List[CompletionRespChoice] = []
    for index, generation in enumerate(generations):
        logprob_response = None

        token_probs = unwrap(generation.get("token_probs"), {})
        if token_probs:
            logprobs = unwrap(generation.get("logprobs"), [])
            offset = unwrap(generation.get("offset"), [])

            logprob_response = CompletionLogProbs(
                text_offset=offset if isinstance(offset, list) else [offset],
                token_logprobs=token_probs.values(),
                tokens=token_probs.keys(),
                top_logprobs=logprobs if isinstance(logprobs, list) else [logprobs],
            )

        # The index can be located in the generation itself
        choice = CompletionRespChoice(
            index=unwrap(generation.get("index"), index),
            finish_reason=generation.get("finish_reason"),
            text=unwrap(generation.get("text"), ""),
            logprobs=logprob_response,
        )

        choices.append(choice)

    final_generation = generations[-1]
    prompt_tokens = unwrap(final_generation.get("prompt_tokens"), 0)
    completion_tokens = unwrap(final_generation.get("gen_tokens"), 0)

    response = CompletionResponse(
        id=f"cmpl-{request_id}",
        choices=choices,
        model=model_name,
        usage=UsageStats(
            prompt_tokens=prompt_tokens,
            prompt_time=final_generation.get("prompt_time"),
            prompt_tokens_per_sec=final_generation.get("prompt_tokens_per_sec"),
            completion_tokens=completion_tokens,
            completion_time=final_generation.get("gen_time"),
            completion_tokens_per_sec=final_generation.get("gen_tokens_per_sec"),
            total_tokens=prompt_tokens + completion_tokens,
            total_time=final_generation.get("total_time"),
        ),
    )

    return response


async def _stream_collector(
    task_idx: int,
    gen_queue: asyncio.Queue,
    request_id: str,
    prompt: str,
    params: CompletionRequest,
    abort_event: asyncio.Event,
    mm_embeddings: Optional[MultimodalEmbeddingWrapper] = None,
):
    """Collects a stream and places results in a common queue"""

    try:
        new_generation = model.container.stream_generate(
            request_id,
            prompt,
            params,
            abort_event,
            mm_embeddings,
        )
        async for generation in new_generation:
            if abort_event.is_set():
                logger.info(f"Request {request_id} aborted by client event.")
                break

            generation["index"] = task_idx
            await gen_queue.put(generation)

            if "finish_reason" in generation:
                break
    except Exception as e:
        await gen_queue.put(e)


async def load_inline_model(model_name: str, request: Request):
    """Load a model from the data.model parameter"""

    # Return if the model container already exists and the model is fully loaded
    if (
        model.container
        and model.container.model_dir.name == model_name
        and model.container.loaded
    ):
        return

    # Return if inline loading is disabled
    # Also warn if an admin key is used
    if not config.model.inline_model_loading:
        if get_key_permission(request) == "admin":
            logger.warning(
                f"Unable to switch model to {model_name} because "
                '"inline_model_loading" is not True in config.yml.'
            )

        return

    is_dummy_model = (
        config.model.use_dummy_models and model_name in config.model.dummy_model_names
    )

    # Error if an invalid key is passed
    # If a dummy model is provided, don't error
    if get_key_permission(request) != "admin":
        if not is_dummy_model:
            error_message = handle_request_error(
                f"Unable to switch model to {model_name} because "
                + "an admin key isn't provided",
                exc_info=False,
            ).error.message

            raise HTTPException(401, error_message)
        else:
            return

    # Start inline loading
    # Past here, user is assumed to be admin

    # Skip if the model is a dummy
    if is_dummy_model:
        logger.warning(f"Dummy model {model_name} provided. Skipping inline load.")

        return

    model_path = pathlib.Path(config.model.model_dir)
    model_path = model_path / model_name

    # Model path doesn't exist
    if not model_path.exists():
        logger.warning(
            f"Could not find model path {str(model_path)}. Skipping inline model load."
        )

        return

    # Load the model and also add draft dir
    await model.load_model(
        model_path,
        draft_model=config.draft_model.model_dump(include={"draft_model_dir"}),
    )


async def stream_generate_completion(
    data: CompletionRequest, request: Request, model_path: pathlib.Path
):
    """Streaming generation for completions."""
    session_id = request.client.host
    abort_event = asyncio.Event()
    gen_queue = asyncio.Queue()
    gen_tasks: List[asyncio.Task] = []
    disconnect_task = asyncio.create_task(request_disconnect_loop(request))

    await inference_request_manager.add_request(session_id, abort_event)
    logger.info(f"Session {session_id}: Registered new request {request.state.id}.")

    def make_cleanup_callback(sid, event, req_id):
        def callback(task):
            async def cleanup():
                was_removed = await inference_request_manager.remove_request(sid, event)
                if was_removed:
                    logger.info(f"Session {sid}: Cleaned up request {req_id}.")

            asyncio.create_task(cleanup())

        return callback

    try:
        logger.info(f"Received streaming completion request {request.state.id}")

        for idx in range(0, data.n):
            task_gen_params = data.model_copy(deep=True)
            request_id = _parse_gen_request_id(data.n, request.state.id, idx)

            gen_task = asyncio.create_task(
                _stream_collector(
                    idx,
                    gen_queue,
                    request_id,
                    data.prompt,
                    task_gen_params,
                    abort_event,
                )
            )

            cleanup_cb = make_cleanup_callback(
                session_id, abort_event, request.state.id
            )
            gen_task.add_done_callback(cleanup_cb)
            gen_tasks.append(gen_task)

        # Consumer loop
        while True:
            if disconnect_task.done():
                abort_event.set()
                handle_request_disconnect(
                    f"Completion generation {request.state.id} cancelled by user."
                )

            if abort_event.is_set():
                logger.info(
                    f"Session {session_id}: Aborting request {request.state.id} due to new request."
                )
                # Drain the queue of any remaining items from the aborted task
                while not gen_queue.empty():
                    gen_queue.get_nowait()
                break

            generation = await gen_queue.get()

            # Stream collector will push an exception to the queue if it fails
            if isinstance(generation, Exception):
                raise generation

            response = _create_response(request.state.id, generation, model_path.name)
            yield response.model_dump_json()

            # Check if all tasks are completed
            if all(task.done() for task in gen_tasks) and gen_queue.empty():
                yield "[DONE]"
                logger.info(f"Finished streaming completion request {request.state.id}")
                break
    except CancelledError:
        # Get out if the request gets disconnected
        if not disconnect_task.done():
            abort_event.set()
            handle_request_disconnect(
                f"Completion generation {request.state.id} cancelled by user."
            )
    except Exception:
        yield get_generator_error(
            f"Completion {request.state.id} aborted. Please check the server console."
        )
    finally:
        # The cleanup is now handled by the done callback.
        # This finally block is kept to ensure disconnect is handled,
        # but no direct cleanup action is needed here.
        if disconnect_task.done() and not abort_event.is_set():
            abort_event.set()


async def generate_completion(
    data: CompletionRequest, request: Request, model_path: pathlib.Path
):
    """Non-streaming generate for completions"""

    gen_tasks: List[asyncio.Task] = []

    try:
        logger.info(f"Received completion request {request.state.id}")

        for idx in range(0, data.n):
            task_gen_params = data.model_copy(deep=True)
            request_id = _parse_gen_request_id(data.n, request.state.id, idx)

            gen_tasks.append(
                asyncio.create_task(
                    model.container.generate(
                        request_id,
                        data.prompt,
                        task_gen_params,
                    )
                )
            )

        generations = await asyncio.gather(*gen_tasks)
        response = _create_response(request.state.id, generations, model_path.name)

        logger.info(f"Finished completion request {request.state.id}")

        return response
    except Exception as exc:
        error_message = handle_request_error(
            f"Completion {request.state.id} aborted. Maybe the model was unloaded? "
            "Please check the server console."
        ).error.message

        # Server error if there's a generation exception
        raise HTTPException(503, error_message) from exc

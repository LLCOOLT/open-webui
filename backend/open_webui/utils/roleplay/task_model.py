"""One-shot prompts to the configured task model (the model title generation uses)."""

from __future__ import annotations


async def run_task_prompt(request, user, model_id: str, prompt: str, task: str, direct: bool = False) -> str:
    """Send ``prompt`` as a single user message to the task model chosen for ``model_id`` (or, with
    ``direct``, to ``model_id`` itself, for jobs that need a stronger model than the task model).

    Returns the reply text. Raises ``ValueError`` when the response has no message.
    """
    from open_webui.routers.tasks import apply_task_model_params, get_task_model_generation_config
    from open_webui.utils.chat import generate_chat_completion

    models = request.app.state.MODELS
    if direct:
        task_model_id, task_model_params = model_id, {}
    else:
        task_model_id, task_model_params = await get_task_model_generation_config(model_id, models)
    payload = {
        'model': task_model_id,
        'messages': [{'role': 'user', 'content': prompt}],
        'stream': False,
        'metadata': {'task': task},
    }
    payload = apply_task_model_params(payload, models, task_model_id, task_model_params)
    response = await generate_chat_completion(request, form_data=payload, user=user)
    try:
        return response['choices'][0]['message']['content'] or ''
    except (TypeError, KeyError, IndexError) as error:
        raise ValueError('The model returned no reply.') from error

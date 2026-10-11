"""User personas: who the user is in a roleplay (``{{user}}`` and ``{{persona}}``).

Personas live in the user's UI settings as ``settings.ui.personas``:
``[{id, name, description, avatar_url, is_default}]``.
"""

from __future__ import annotations

MAX_DESCRIPTION_CHARS = 4_000


def user_personas(user) -> list[dict]:
    settings = getattr(user, 'settings', None)
    ui = getattr(settings, 'ui', None) if settings is not None else None
    personas = (ui or {}).get('personas') if isinstance(ui, dict) else None
    if not isinstance(personas, list):
        return []
    return [
        persona
        for persona in personas
        if isinstance(persona, dict) and isinstance(persona.get('id'), str) and isinstance(persona.get('name'), str)
    ]


def resolve_persona(user, *persona_ids: str | None) -> dict | None:
    """The first of ``persona_ids`` that names one of the user's personas, else their default persona.

    An explicit empty string means "no persona" and stops the search.
    """
    personas = user_personas(user)
    for persona_id in persona_ids:
        if persona_id == '':
            return None
        if persona_id:
            match = next((persona for persona in personas if persona['id'] == persona_id), None)
            if match:
                return match
    return next((persona for persona in personas if persona.get('is_default')), None)


def persona_description(persona: dict | None) -> str:
    description = (persona or {}).get('description')
    return description[:MAX_DESCRIPTION_CHARS] if isinstance(description, str) else ''

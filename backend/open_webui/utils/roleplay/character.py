"""The ``meta.character`` object stored on a workspace model that acts as a character."""

from __future__ import annotations

from typing import Literal

from open_webui.utils.roleplay.cards import CharacterData
from open_webui.utils.roleplay.cleanup import ReplyCleanup
from pydantic import BaseModel, ConfigDict, Field, field_validator

OverrideMode = Literal['replace', 'supplement']
PromptRole = Literal['system', 'user', 'assistant']


class PromptOverrides(BaseModel):
    """How the card's own prompts combine with the defaults (``{{original}}``)."""

    system_prompt: OverrideMode = 'replace'
    post_history: OverrideMode = 'replace'


class DepthPrompt(BaseModel):
    """Text injected into the history ``depth`` messages from the end (SillyTavern's character note)."""

    text: str = ''
    depth: int = Field(default=4, ge=0, le=1000)
    role: PromptRole = 'system'


class CharacterMeta(CharacterData):
    """Character card fields plus the Open WebUI settings that control prompting.

    Stored leniently: a missing name falls back to the model name and unknown keys are kept, so a
    hand-edited or older record never makes the model unreadable.
    """

    model_config = ConfigDict(extra='allow')

    name: str = ''
    prompt_overrides: PromptOverrides = Field(default_factory=PromptOverrides)
    depth_prompt: DepthPrompt = Field(default_factory=DepthPrompt)
    example_mode: Literal['system', 'messages'] = 'system'
    # Full-resolution avatar kept for export; ``meta.profile_image_url`` holds the thumbnail.
    avatar_file_id: str | None = None
    # Emotion images: expression name -> file id (see ``utils.roleplay.expressions``).
    expressions: dict[str, str] = Field(default_factory=dict)
    # What is cut or replaced in finished replies (see ``utils.roleplay.cleanup``).
    reply_cleanup: ReplyCleanup = Field(default_factory=ReplyCleanup)

    @field_validator('expressions', mode='before')
    @classmethod
    def clean_expressions(cls, value):
        from open_webui.utils.roleplay.expressions import MAX_EXPRESSIONS, clean_expression_name

        if not isinstance(value, dict):
            return {}
        cleaned: dict[str, str] = {}
        for name, file_id in value.items():
            name = clean_expression_name(name) if isinstance(name, str) else ''
            if name and isinstance(file_id, str) and file_id and len(cleaned) < MAX_EXPRESSIONS:
                cleaned.setdefault(name, file_id)
        return cleaned

    @field_validator('prompt_overrides', 'depth_prompt', 'reply_cleanup', mode='before')
    @classmethod
    def default_when_null(cls, value, info):
        if value is None:
            return {'prompt_overrides': PromptOverrides, 'depth_prompt': DepthPrompt, 'reply_cleanup': ReplyCleanup}[
                info.field_name
            ]()
        return value

    @classmethod
    def from_card(cls, card: CharacterData) -> CharacterMeta:
        """Build the stored object from an imported card, lifting SillyTavern's depth prompt."""
        meta = cls(**card.model_dump())
        depth_prompt = card.extensions.get('depth_prompt')
        if isinstance(depth_prompt, dict) and isinstance(depth_prompt.get('prompt'), str):
            role = depth_prompt.get('role')
            depth = depth_prompt.get('depth')
            meta.depth_prompt = DepthPrompt(
                text=depth_prompt['prompt'],
                depth=depth if isinstance(depth, int) and 0 <= depth <= 1000 else 4,
                role=role if role in ('system', 'user', 'assistant') else 'system',
            )
        return meta

    def display_name(self, fallback: str = '') -> str:
        """The value of ``{{char}}``."""
        return self.nickname or self.name or fallback

    def to_card(self, fallback_name: str = '') -> CharacterData:
        """Card data for export, with the depth prompt written back into the extensions."""
        data = self.model_dump(include=set(CharacterData.model_fields))
        data['name'] = self.name or fallback_name
        extensions = dict(self.extensions)
        if self.depth_prompt.text:
            extensions['depth_prompt'] = {
                'prompt': self.depth_prompt.text,
                'depth': self.depth_prompt.depth,
                'role': self.depth_prompt.role,
            }
        else:
            extensions.pop('depth_prompt', None)
        data['extensions'] = extensions
        return CharacterData(**data)


SUMMARY_FIELDS = (
    'name',
    'nickname',
    'creator',
    'creator_notes',
    'creator_notes_multilingual',
    'character_version',
    'spec_version',
    'tags',
)


def character_summary(character: dict) -> dict:
    """The display-only subset of ``meta.character`` sent with model lists.

    Prompt text stays server side (it is not shown to read-only users, like ``params.system``);
    the editor loads the full object through the single-model endpoint.
    """
    greetings = [character.get('first_mes'), *(character.get('alternate_greetings') or [])]
    expressions = character.get('expressions')
    return {
        **{key: character[key] for key in SUMMARY_FIELDS if key in character},
        'greeting_count': sum(1 for text in greetings if isinstance(text, str) and text.strip()),
        # Names only: images are served per model by /models/model/character/expression.
        'expressions': sorted(expressions) if isinstance(expressions, dict) else [],
    }

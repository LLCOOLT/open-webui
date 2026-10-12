"""Emotion-aware speech: turn the expression picked for a reply into a hint for the voice engine.

Expression names are the character's own (``joy``, ``smirk``, ``crying``…) or, for characters without
expression images, the defaults below. Each maps to one of a few canonical emotions, which in turn map
to what each engine can take: a spoken-style instruction (OpenAI ``instructions``, OpenAI Realtime),
voice settings and an audio tag (ElevenLabs), or an ``express-as`` style (Azure).
"""

from __future__ import annotations

import re

# Offered to the task model for characters that have no expression images of their own.
DEFAULT_EMOTIONS = [
    'neutral',
    'joy',
    'sadness',
    'anger',
    'fear',
    'surprise',
    'disgust',
    'love',
    'embarrassment',
    'curiosity',
    'amusement',
    'excitement',
]

# How each canonical emotion sounds, for engines that take a prose instruction.
INSTRUCTIONS = {
    'neutral': 'calm and even',
    'joy': 'happy and warm, with a lively pace and a smile in the voice',
    'sadness': 'sad and subdued, slower and softer',
    'anger': 'angry and tense, sharper and more forceful',
    'fear': 'frightened and uneasy, hushed and uneven',
    'surprise': 'surprised, with a quick, raised delivery',
    'disgust': 'disgusted and cold, with distaste in the voice',
    'love': 'tender and affectionate, soft and close',
    'embarrassment': 'flustered and hesitant, a little awkward',
    'curiosity': 'curious and engaged, with rising interest',
    'amusement': 'amused and playful, on the edge of laughing',
    'excitement': 'excited and energetic, fast and bright',
}

# Azure AI Speech ``mstts:express-as`` styles. Voices that lack a style fall back to neutral speech.
AZURE_STYLES = {
    'joy': 'cheerful',
    'sadness': 'sad',
    'anger': 'angry',
    'fear': 'terrified',
    'surprise': 'excited',
    'disgust': 'disgruntled',
    'love': 'affectionate',
    'embarrassment': 'embarrassed',
    'curiosity': 'friendly',
    'amusement': 'cheerful',
    'excitement': 'excited',
}

# ElevenLabs voice settings: expressive emotions trade stability for style.
_ELEVENLABS_NEUTRAL = {'stability': 0.5, 'similarity_boost': 0.5}
_ELEVENLABS_SOFT = {'stability': 0.45, 'similarity_boost': 0.5, 'style': 0.3}
_ELEVENLABS_STRONG = {'stability': 0.3, 'similarity_boost': 0.5, 'style': 0.6}
ELEVENLABS_SETTINGS = {
    'neutral': _ELEVENLABS_NEUTRAL,
    'joy': _ELEVENLABS_STRONG,
    'sadness': _ELEVENLABS_SOFT,
    'anger': _ELEVENLABS_STRONG,
    'fear': _ELEVENLABS_STRONG,
    'surprise': _ELEVENLABS_STRONG,
    'disgust': _ELEVENLABS_SOFT,
    'love': _ELEVENLABS_SOFT,
    'embarrassment': _ELEVENLABS_SOFT,
    'curiosity': _ELEVENLABS_SOFT,
    'amusement': _ELEVENLABS_STRONG,
    'excitement': _ELEVENLABS_STRONG,
}

# Audio tags the ElevenLabs v3 model reads at the start of the text.
ELEVENLABS_TAGS = {
    'joy': 'happy',
    'sadness': 'sad',
    'anger': 'angry',
    'fear': 'nervous',
    'surprise': 'surprised',
    'disgust': 'disgusted',
    'love': 'warmly',
    'embarrassment': 'embarrassed',
    'curiosity': 'curious',
    'amusement': 'amused',
    'excitement': 'excited',
}

# Expression names (as cards and the SillyTavern classifier use them) by canonical emotion.
_ALIASES = {
    'neutral': ['neutral', 'calm', 'default', 'normal', 'idle', 'thinking', 'serious', 'bored', 'tired', 'sleepy'],
    'joy': [
        'joy',
        'joyful',
        'happy',
        'happiness',
        'smile',
        'smiling',
        'grin',
        'grinning',
        'laugh',
        'laughing',
        'delight',
        'delighted',
        'cheerful',
        'pleased',
        'content',
        'glad',
        'approval',
        'gratitude',
        'optimism',
        'pride',
        'proud',
        'relief',
        'relieved',
        'hopeful',
    ],
    'sadness': [
        'sadness',
        'sad',
        'cry',
        'crying',
        'tears',
        'tearful',
        'grief',
        'grieving',
        'sorrow',
        'disappointment',
        'disappointed',
        'remorse',
        'guilty',
        'hurt',
        'lonely',
        'depressed',
        'gloomy',
        'melancholy',
        'upset',
    ],
    'anger': [
        'anger',
        'angry',
        'rage',
        'furious',
        'mad',
        'annoyance',
        'annoyed',
        'irritated',
        'frustrated',
        'frustration',
        'disapproval',
        'hostile',
        'glare',
        'glaring',
        'pout',
        'pouting',
    ],
    'fear': [
        'fear',
        'afraid',
        'scared',
        'frightened',
        'terrified',
        'nervousness',
        'nervous',
        'anxious',
        'anxiety',
        'worried',
        'worry',
        'panic',
        'dread',
        'horror',
    ],
    'surprise': [
        'surprise',
        'surprised',
        'shock',
        'shocked',
        'startled',
        'astonished',
        'realization',
        'confusion',
        'confused',
        'puzzled',
    ],
    'disgust': ['disgust', 'disgusted', 'contempt', 'revulsion', 'grossed out'],
    'love': [
        'love',
        'loving',
        'affection',
        'affectionate',
        'caring',
        'desire',
        'admiration',
        'adoring',
        'tender',
        'longing',
        'lust',
        'seductive',
        'flirty',
        'flirting',
    ],
    'embarrassment': [
        'embarrassment',
        'embarrassed',
        'blush',
        'blushing',
        'shy',
        'flustered',
        'bashful',
        'awkward',
        'ashamed',
    ],
    'curiosity': ['curiosity', 'curious', 'interested', 'intrigued', 'wondering'],
    'amusement': [
        'amusement',
        'amused',
        'smirk',
        'smirking',
        'playful',
        'teasing',
        'mischievous',
        'giggle',
        'giggling',
        'sly',
    ],
    'excitement': ['excitement', 'excited', 'thrilled', 'eager', 'enthusiastic', 'energetic'],
}
EMOTION_BY_ALIAS = {alias: emotion for emotion, aliases in _ALIASES.items() for alias in aliases}

MAX_NAME_LENGTH = 40


def clean_emotion_name(name) -> str:
    """Lowercase, single-spaced, letters/digits/space/_/- only, at most 40 characters."""
    if not isinstance(name, str):
        return ''
    name = re.sub(r'[^\w\s-]', '', name.strip().lower())
    return re.sub(r'\s+', ' ', name).strip()[:MAX_NAME_LENGTH].strip()


def canonical_emotion(name: str) -> str | None:
    """The canonical emotion an expression name means, or ``None`` for one not recognized.

    ``joy_2`` and ``very happy`` count: a trailing number is dropped, then any word is tried.
    """
    name = clean_emotion_name(name)
    if not name:
        return None
    base = re.sub(r'[\s_-]*\d+$', '', name).strip()
    for candidate in (name, base, base.replace('_', ' '), base.replace('-', ' ')):
        if candidate in EMOTION_BY_ALIAS:
            return EMOTION_BY_ALIAS[candidate]
    for word in re.split(r'[\s_-]+', base):
        if word in EMOTION_BY_ALIAS:
            return EMOTION_BY_ALIAS[word]
    return None


def speech_instruction(name: str) -> str:
    """A sentence for engines that take prose instructions, empty when the name is unusable or neutral."""
    emotion = canonical_emotion(name)
    if emotion is None:
        clean = clean_emotion_name(name)
        return f'The speaker feels "{clean}"; let that show in the voice.' if clean else ''
    if emotion == 'neutral':
        return ''
    return f'The speaker is {INSTRUCTIONS[emotion]}.'


def azure_style(name: str) -> str | None:
    return AZURE_STYLES.get(canonical_emotion(name) or '')


def elevenlabs_settings(name: str) -> dict:
    return dict(ELEVENLABS_SETTINGS.get(canonical_emotion(name) or 'neutral', _ELEVENLABS_NEUTRAL))


def elevenlabs_tag(name: str) -> str | None:
    return ELEVENLABS_TAGS.get(canonical_emotion(name) or '')

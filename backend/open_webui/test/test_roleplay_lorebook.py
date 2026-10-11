import pytest

from open_webui.utils.roleplay.lorebook import DepthEntry, activate, estimate_tokens


def entry(content, keys=(), **fields):
    return {'keys': list(keys), 'content': content, **fields}


def book(*entries, **fields):
    return {'entries': list(entries), **fields}


def chat(*texts):
    return [{'role': 'user' if index % 2 == 0 else 'assistant', 'content': text} for index, text in enumerate(texts)]


def test_keyword_activation_is_case_insensitive_by_default():
    result = activate([book(entry('Castle lore.', ['castle']))], chat('We reach the CASTLE gates.'))
    assert result.before_char == ['Castle lore.']
    assert activate([book(entry('Castle lore.', ['castle']))], chat('A quiet forest.')).empty


def test_case_sensitive_keys():
    lore = book(entry('Exact.', ['Castle'], case_sensitive=True))
    assert activate([lore], chat('the castle')).empty
    assert activate([lore], chat('the Castle')).before_char == ['Exact.']


def test_regex_keys_and_invalid_patterns():
    lore = book(entry('Dragon.', [r'\bdrag(on|ons)\b'], use_regex=True), entry('Bad.', ['(unclosed'], use_regex=True))
    assert activate([lore], chat('Two dragons fly.')).before_char == ['Dragon.']
    assert activate([lore], chat('dragonfly')).empty


def test_catastrophic_regex_times_out_instead_of_hanging():
    lore = book(entry('Never.', [r'(a+)+$'], use_regex=True))
    assert activate([lore], chat('a' * 5000 + 'b')).empty


def test_selective_needs_a_secondary_key():
    lore = book(entry('Royal guard.', ['guard'], selective=True, secondary_keys=['king', 'queen']))
    assert activate([lore], chat('The guard waits.')).empty
    assert activate([lore], chat('The guard serves the queen.')).before_char == ['Royal guard.']


def test_constant_and_disabled_entries():
    lore = book(entry('Always.', constant=True), entry('Off.', ['x'], enabled=False))
    assert activate([lore], chat('x')).before_char == ['Always.']


def test_scan_depth_limits_the_messages_searched():
    lore = book(entry('Old topic.', ['sword']), scan_depth=2)
    messages = chat('I found a sword.', 'Nice.', 'Let us go.')
    assert activate([lore], messages).empty
    assert activate([book(entry('Old topic.', ['sword']), scan_depth=3)], messages).before_char == ['Old topic.']
    # The @@scan_depth decorator overrides the book.
    override = book(entry('@@scan_depth 5\nOld topic.', ['sword']), scan_depth=1)
    assert activate([override], messages).before_char == ['Old topic.']


def test_system_messages_are_not_scanned():
    messages = [{'role': 'system', 'content': 'castle'}, *chat('hello')]
    assert activate([book(entry('Castle.', ['castle']))], messages).empty


def test_recursive_scanning_runs_one_more_pass():
    entries = [entry('The keep has a dungeon.', ['keep']), entry('The dungeon is cold.', ['dungeon'])]
    assert activate([book(*entries)], chat('Enter the keep.')).before_char == ['The keep has a dungeon.']
    recursive = activate([book(*entries, recursive_scanning=True)], chat('Enter the keep.'))
    assert recursive.before_char == ['The keep has a dungeon.', 'The dungeon is cold.']


def test_token_budget_keeps_highest_priority_and_output_follows_insertion_order():
    long = 'x' * 400  # 100 tokens
    lore = book(
        entry('A ' + long, ['go'], priority=1, insertion_order=1),
        entry('B ' + long, ['go'], priority=50, insertion_order=3),
        entry('C ' + long, ['go'], priority=20, insertion_order=2),
        token_budget=210,
    )
    result = activate([lore], chat('go'))
    assert [text[0] for text in result.before_char] == ['C', 'B']
    assert estimate_tokens('x' * 8) == 2


def test_positions_and_depth_decorators():
    lore = book(
        entry('Before.', ['go']),
        entry('After.', ['go'], position='after_char'),
        entry('@@position after_char\nMoved.', ['go']),
        entry('@@depth 2\n@@role user\nDeep.', ['go']),
        entry('@@depth 0\n@@role narrator\nBad role.', ['go']),
    )
    result = activate([lore], chat('go'))
    assert result.before_char == ['Before.']
    assert result.after_char == ['After.', 'Moved.']
    assert result.depth == [DepthEntry('Deep.', 2, 'user'), DepthEntry('Bad role.', 0, 'system')]


@pytest.mark.parametrize(
    ('content', 'text', 'active'),
    [
        ('@@activate\nForced.', 'nothing', True),
        ('@@dont_activate\nNever.', 'go', False),
        ('@@additional_keys sail, boat\nSea.', 'a boat', True),
        ('@@exclude_keys calm\nStorm.', 'go, calm seas', False),
        ('@@unknown thing\n@@@activate\nFallback.', 'nothing', True),
    ],
)
def test_decorators(content, text, active):
    result = activate([book(entry(content, ['go']))], chat(text))
    assert (not result.empty) is active


def test_multiple_books_each_use_their_own_settings():
    first = book(entry('First.', ['a']), scan_depth=1)
    second = book(entry('Second.', ['a'], insertion_order=1))
    result = activate([first, second], chat('a', 'b'))
    assert result.before_char == ['Second.']


def test_unnormalized_entries_are_accepted():
    lore = {'entries': {'0': {'key': 'castle, keep', 'content': 'From SillyTavern.'}}}
    from open_webui.utils.roleplay.lorebook import normalize_lorebook

    assert activate([normalize_lorebook(lore)], chat('the keep')).before_char == ['From SillyTavern.']
    assert activate([{'entries': [{'keys': 'castle', 'content': 'Raw.'}]}], chat('castle')).before_char == ['Raw.']

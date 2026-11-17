"""Tests for schema inference and docstring parsing.

A few pinned examples document the exact output shape; the property tests do
the heavy lifting by generating many random annotations / docstrings.

Generation is seeded, so every run checks the same cases (no flakes). Set
FRAPPE_MCP_PROPERTY_SEED to explore new cases locally; failures print the
offending input so it can be pinned as an example.
"""

import os
import random
import string
from typing import Any, Optional, Union

import pytest
from jsonschema import Draft202012Validator

from frappe_mcp.server.tools.tool_schema import get_descriptions, get_input_schema

SEED = int(os.environ.get('FRAPPE_MCP_PROPERTY_SEED', '0'))
CASES = 500


# --- Pinned examples ----------------------------------------------------------


def _schema_of(annotation):
    def fn(a): ...

    fn.__annotations__ = {'a': annotation}
    return get_input_schema(fn)['properties']['a']


@pytest.mark.parametrize(
    ('annotation', 'expected'),
    [
        (int, {'type': 'integer'}),
        (Any, {}),
        (list, {'type': 'array'}),
        (
            dict[str, int],
            {'type': 'object', 'additionalProperties': {'type': 'integer'}},
        ),
        (int | None, {'type': ['integer', 'null']}),
        (Optional[str], {'type': ['string', 'null']}),  # noqa: UP045
        (Union[int, str], {'anyOf': [{'type': 'integer'}, {'type': 'string'}]}),  # noqa: UP007
        (
            list[int | str] | None,
            {
                'type': ['array', 'null'],
                'items': {'anyOf': [{'type': 'integer'}, {'type': 'string'}]},
            },
        ),
        ('int | None', {'type': ['integer', 'null']}),  # forward reference
    ],
    ids=repr,
)
def test_schema_examples(annotation, expected):
    assert _schema_of(annotation) == expected


def test_required_follows_defaults():
    def fn(a: int, b: str = 'x', *args, c: bool, d: float = 1.0, **kw): ...

    schema = get_input_schema(fn)
    assert list(schema['properties']) == ['a', 'b', 'c', 'd']
    assert schema['required'] == ['a', 'c']


@pytest.mark.parametrize(
    ('docstring', 'description', 'args'),
    [
        ('', '', {}),
        ('Just a summary.', 'Just a summary.', {}),
        (
            'Summary.\n\nArgs:\n    a (str): First.\n    b (int, optional): Second.',
            'Summary.',
            {'a': 'First.', 'b': 'Second.'},
        ),
        ('Args:\n    a: Only args.', '', {'a': 'Only args.'}),
        # Regression: trailing sections used to be parsed as an argument.
        ('S.\n\nArgs:\n    a: x.\n\nReturns:\n    y.', 'S.', {'a': 'x.'}),
        # Regression: a wrapped line containing "(...)" used to truncate the arg.
        (
            'S.\n\nArgs:\n    a: one\n        two (three)\n    b (str): four',
            'S.',
            {'a': 'one two (three)', 'b': 'four'},
        ),
    ],
    ids=['empty', 'no-args', 'typed-args', 'args-only', 'returns', 'wrapped-parens'],
)
def test_docstring_examples(docstring, description, args):
    assert get_descriptions(docstring) == (description, args)


# --- Property: generated schema accepts every value of the annotated type ------

LEAVES = [int, str, float, bool, Any, list, dict]


def _random_annotation(rng: random.Random, depth: int = 0):
    if depth >= 3 or rng.random() < 0.4:
        return rng.choice(LEAVES)
    kind = rng.choice(['list', 'dict', 'union', 'optional'])
    inner = _random_annotation(rng, depth + 1)
    if kind == 'list':
        return list[inner]
    if kind == 'dict':
        return dict[str, inner]
    if kind == 'optional':
        return Optional[inner]  # noqa: UP045
    return Union[inner, _random_annotation(rng, depth + 1)]  # noqa: UP007


def _random_value(rng: random.Random, annotation, depth: int = 0):
    if annotation is Any:
        annotation = rng.choice([int, str, list, dict])
    if annotation is int:
        return rng.randint(-(2**40), 2**40)
    if annotation is float:
        return rng.choice([rng.uniform(-1e9, 1e9), 0.0, float(rng.randint(-5, 5))])
    if annotation is bool:
        return rng.random() < 0.5
    if annotation is str:
        return ''.join(rng.choices(string.printable, k=rng.randint(0, 8)))
    if annotation is type(None):
        return None
    if annotation is list:
        annotation = list[Any]
    if annotation is dict:
        annotation = dict[str, Any]
    n = 0 if depth > 3 else rng.randint(0, 3)
    origin, args = getattr(annotation, '__origin__', None), annotation.__args__
    if origin is list:
        return [_random_value(rng, args[0], depth + 1) for _ in range(n)]
    if origin is dict:
        return {f'k{i}': _random_value(rng, args[1], depth + 1) for i in range(n)}
    return _random_value(rng, rng.choice(args), depth)  # Union


def test_schema_accepts_values_of_annotated_type():
    rng = random.Random(SEED)
    for _ in range(CASES):
        annotation = _random_annotation(rng)
        validator = Draft202012Validator(_schema_of(annotation))
        Draft202012Validator.check_schema(validator.schema)
        for _ in range(5):
            value = _random_value(rng, annotation)
            errors = list(validator.iter_errors(value))
            assert not errors, f'{annotation!r} rejected {value!r}: {errors[0].message}'


def _admits(annotation, leaf) -> bool:
    if annotation is Any:
        return True
    if getattr(annotation, '__origin__', None) is Union:
        return any(_admits(a, leaf) for a in annotation.__args__)
    return annotation is leaf


def test_schema_rejects_values_of_other_types():
    rng = random.Random(SEED)
    for _ in range(CASES):
        annotation = _random_annotation(rng)
        validator = Draft202012Validator(_schema_of(annotation))
        if not _admits(annotation, str):
            assert not validator.is_valid('x'), f'{annotation!r} accepted a str'
        if not _admits(annotation, type(None)):
            assert not validator.is_valid(None), f'{annotation!r} accepted None'


# --- Property: docstring Args round-trip -------------------------------------

WORDS = ['the', 'value', 'Customer', 'ID.', 'a,b', '42', 'e.g.', '(optional)', 'list']


def _random_docstring(rng: random.Random):
    summary = ' '.join(rng.choices(WORDS, k=rng.randint(1, 6))).strip()
    names = rng.sample(
        ['a', 'b', 'customer_id', 'limit', 'x1', 'filters'], rng.randint(1, 4)
    )
    args = {n: ' '.join(rng.choices(WORDS, k=rng.randint(1, 10))) for n in names}

    lines = [summary, '', 'Args:']
    for name, desc in args.items():
        type_hint = rng.choice(['', ' (str)', ' (int, optional)'])
        words = desc.split()
        cut = rng.randint(1, len(words))  # wrap long descriptions onto a new line
        lines.append(f'    {name}{type_hint}: {" ".join(words[:cut])}')
        if words[cut:]:
            lines.append(f'        {" ".join(words[cut:])}')
    if rng.random() < 0.5:
        lines += ['', 'Returns:', '    Something.']
    return '\n'.join(lines), summary, args


def test_docstring_round_trip():
    rng = random.Random(SEED)
    for _ in range(CASES):
        docstring, summary, args = _random_docstring(rng)
        assert get_descriptions(docstring) == (summary, args), docstring

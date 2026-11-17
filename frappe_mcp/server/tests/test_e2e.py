"""End-to-end tests: drive an MCP server over HTTP the way a real client does.

Every test here goes through a WSGI app wrapping `MCP.handle`, so request
parsing, routing, handler logic and response serialisation are all exercised
together. Prefer adding tests here over unit-testing internals.
"""

import pytest
from werkzeug.test import Client
from werkzeug.wrappers import Request, Response

from frappe_mcp import MCP, PromptMessage, TextContent
from frappe_mcp.server import types


class MCPClient:
    def __init__(self, mcp: MCP):
        self.http = Client(
            lambda env, sr: mcp.handle(Request(env), Response())(env, sr)
        )
        self._next_id = 0

    def call(self, method, params=None):
        """Send a request; return the parsed JSON-RPC response envelope."""
        self._next_id += 1
        body = {'jsonrpc': '2.0', 'id': self._next_id, 'method': method}
        if params is not None:
            body['params'] = params
        resp = self.http.post(json=body)
        data = resp.get_json()
        assert data['jsonrpc'] == '2.0'
        assert data['id'] == self._next_id
        return data

    def result(self, method, params=None):
        data = self.call(method, params)
        assert 'error' not in data, data
        return data['result']

    def error_code(self, method, params=None):
        data = self.call(method, params)
        assert 'result' not in data, data
        return data['error']['code']


@pytest.fixture
def mcp():
    return MCP(name='test-server')


@pytest.fixture
def client(mcp):
    return MCPClient(mcp)


# --- Transport / JSON-RPC envelope -------------------------------------------


def test_initialize_handshake(client):
    result = client.result('initialize', {'clientInfo': {'name': 'c'}})
    assert result['serverInfo']['name'] == 'test-server'
    assert result['protocolVersion']
    assert {'tools', 'prompts'} <= result['capabilities'].keys()

    resp = client.http.post(
        json={'jsonrpc': '2.0', 'method': 'notifications/initialized'}
    )
    assert resp.status_code == 202
    assert not resp.data


def test_ping(client):
    assert client.result('ping') == {}


def test_get_is_rejected(client):
    assert client.http.get().status_code == 405


@pytest.mark.parametrize(
    'body',
    [b'{not json', b'', b'\xff\xfe'],
    ids=['malformed', 'empty', 'not-utf8'],
)
def test_unparseable_body_is_parse_error(client, body):
    resp = client.http.post(data=body, content_type='application/json')
    assert resp.status_code == 400
    assert resp.get_json()['error']['code'] == types.PARSE_ERROR


@pytest.mark.parametrize(
    'payload',
    [
        [{'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}],  # batch
        'ping',
        {'jsonrpc': '2.0', 'method': 'ping'},  # missing id, not a notification
    ],
    ids=['batch', 'string', 'missing-id'],
)
def test_invalid_request(client, payload):
    resp = client.http.post(json=payload)
    assert resp.status_code == 400
    assert resp.get_json()['error']['code'] == types.INVALID_REQUEST


def test_unknown_method(client):
    assert client.error_code('foo/bar') == types.METHOD_NOT_FOUND


def test_unimplemented_method(client):
    assert client.error_code('resources/list') == types.METHOD_NOT_FOUND


# --- Tools --------------------------------------------------------------------


def test_tools_list_exposes_schema_from_signature_and_docstring(mcp, client):
    @mcp.tool()
    def lookup(customer: str, limit: int = 10):
        """Look up a customer.

        Args:
            customer: Customer name.
            limit: Max rows.
        """

    (tool,) = client.result('tools/list')['tools']
    assert tool['name'] == 'lookup'
    assert tool['description'] == 'Look up a customer.'
    assert tool['inputSchema'] == {
        'type': 'object',
        'properties': {
            'customer': {'type': 'string', 'description': 'Customer name.'},
            'limit': {'type': 'integer', 'description': 'Max rows.'},
        },
        'required': ['customer'],
    }
    assert 'annotations' not in tool


def test_tools_list_honours_decorator_options(mcp, client):
    schema = {'type': 'object', 'properties': {'x': {'type': 'boolean'}}}

    @mcp.tool(
        name='renamed',
        description='Custom.',
        input_schema=schema,
        annotations={'readOnlyHint': True},
    )
    def original(y: int):
        """Ignored."""

    @mcp.tool(use_entire_docstring=True)
    def documented(a: int):
        """Summary.

        Args:
            a: The a.
        """

    renamed, documented_tool = client.result('tools/list')['tools']
    assert renamed['name'] == 'renamed'
    assert renamed['description'] == 'Custom.'
    assert renamed['inputSchema'] == schema
    assert renamed['annotations'] == {'readOnlyHint': True}
    assert documented_tool['description'] == 'Summary.\n\nArgs:\n    a: The a.'


def test_tools_list_skips_malformed_tool(mcp, client):
    mcp.add_tool({'name': None, 'description': 'x', 'input_schema': {}, 'fn': print})

    @mcp.tool()
    def ok():
        """Fine."""

    assert [t['name'] for t in client.result('tools/list')['tools']] == ['ok']


def test_tools_call_dict_result_is_structured(mcp, client):
    @mcp.tool()
    def add(a: int, b: int):
        return {'sum': a + b}

    result = client.result(
        'tools/call', {'name': 'add', 'arguments': {'a': 5, 'b': 10}}
    )
    assert result['isError'] is False
    assert result['structuredContent'] == {'sum': 15}
    assert result['content'] == [{'type': 'text', 'text': '{"sum": 15}'}]


@pytest.mark.parametrize(
    ('returned', 'text'),
    [('hi', 'hi'), (3, '3'), ([1, 2], '[1, 2]'), ({1}, '{1}')],
    ids=['str', 'int', 'list', 'unserialisable'],
)
def test_tools_call_non_dict_result_is_text(mcp, client, returned, text):
    @mcp.tool()
    def t():
        return returned

    result = client.result('tools/call', {'name': 't'})
    assert result == {'content': [{'type': 'text', 'text': text}], 'isError': False}


def test_tools_call_errors_are_reported_as_tool_errors(mcp, client):
    @mcp.tool()
    def boom(a: int):
        raise RuntimeError('kaboom')

    for name, args, needle in [
        ('boom', {'a': 1}, 'kaboom'),
        ('boom', {}, "missing 1 required positional argument: 'a'"),
        ('missing', {}, "Tool 'missing' not found"),
    ]:
        result = client.result('tools/call', {'name': name, 'arguments': args})
        assert result['isError'] is True
        assert needle in result['content'][0]['text']


def test_tools_call_without_name_is_invalid_params(client):
    assert client.error_code('tools/call', {'arguments': {}}) == types.INVALID_PARAMS


# --- Prompts ------------------------------------------------------------------


def test_prompts_list(mcp, client):
    assert client.result('prompts/list') == {'prompts': []}

    @mcp.prompt()
    def summarize(topic: str, language: str = 'english'):
        """Summarize a topic."""

    @mcp.prompt(name='hello', description='Custom.')
    def bare():
        pass

    assert client.result('prompts/list')['prompts'] == [
        {
            'name': 'summarize',
            'description': 'Summarize a topic.',
            'arguments': [
                {'name': 'topic', 'required': True},
                {'name': 'language', 'required': False},
            ],
        },
        {'name': 'hello', 'description': 'Custom.'},
    ]


def test_prompts_get(mcp, client):
    @mcp.prompt()
    def summarize(topic: str, language: str = 'english'):
        """Summarize a topic."""
        return [
            PromptMessage(role='user', content=TextContent(text=f'{topic}/{language}'))
        ]

    result = client.result(
        'prompts/get', {'name': 'summarize', 'arguments': {'topic': 'Go'}}
    )
    assert result == {
        'description': 'Summarize a topic.',
        'messages': [
            {'role': 'user', 'content': {'type': 'text', 'text': 'Go/english'}}
        ],
    }

    result = client.result(
        'prompts/get',
        {'name': 'summarize', 'arguments': {'topic': 'Go', 'language': 'fr'}},
    )
    assert result['messages'][0]['content']['text'] == 'Go/fr'


def test_prompts_get_accepts_full_result(mcp, client):
    @mcp.prompt()
    def custom():
        return types.GetPromptResult(
            description='own',
            messages=[PromptMessage(role='assistant', content=TextContent(text='Hi'))],
        )

    result = client.result('prompts/get', {'name': 'custom'})
    assert result['description'] == 'own'
    assert result['messages'][0]['role'] == 'assistant'


def test_prompts_get_errors(mcp, client):
    @mcp.prompt()
    def bad():
        return 'not a message list'

    assert client.error_code('prompts/get', {'name': 'missing'}) == types.INVALID_PARAMS
    assert client.error_code('prompts/get', {'name': 'bad'}) == types.INVALID_PARAMS

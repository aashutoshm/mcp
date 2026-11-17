---
name: verify
description: Verify a change to frappe-mcp before committing. Runs lint, the end-to-end and property test suites, and a live HTTP smoke test against a real server.
---

Run these in order and stop at the first failure. Fix the cause; do not skip,
retry or add sleeps to get a test to pass. If you still can't fix it after
three attempts, stop and report to the user.

1. `uv run ruff check . && uv run ruff format --check frappe_mcp/server/tests`
2. `uv run pytest -q` (takes under 2 seconds)
3. Live smoke test against a real server:

   ```bash
   uv run python - <<'PY'
   import json, threading, urllib.request
   from werkzeug.serving import make_server
   from werkzeug.wrappers import Request, Response
   from frappe_mcp import MCP

   mcp = MCP(name='smoke')

   @mcp.tool()
   def add(a: int, b: int):
       """Add numbers."""
       return {'sum': a + b}

   app = Request.application(lambda req: mcp.handle(req, Response()))
   srv = make_server('127.0.0.1', 0, app)
   threading.Thread(target=srv.serve_forever, daemon=True).start()

   def rpc(method, params=None):
       body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})
       req = urllib.request.Request(f'http://127.0.0.1:{srv.port}', body.encode(),
                                    {'Content-Type': 'application/json'})
       return json.load(urllib.request.urlopen(req))['result']

   assert rpc('initialize')['serverInfo']['name'] == 'smoke'
   assert [t['name'] for t in rpc('tools/list')['tools']] == ['add']
   assert rpc('tools/call', {'name': 'add', 'arguments': {'a': 2, 'b': 3}})['structuredContent'] == {'sum': 5}
   srv.shutdown()
   print('smoke OK')
   PY
   ```

When you touch request handling, tools or prompts, add or extend a test in
`frappe_mcp/server/tests/test_e2e.py`. When you touch schema inference or docstring
parsing, check that the property tests in `test_tool_schema.py` still cover it.
When a property test fails, pin the failing input as a named example.

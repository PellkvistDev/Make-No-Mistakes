"""A minimal stdio MCP server for tests: newline-delimited JSON-RPC 2.0.
Exposes one tool, `echo`, that returns its `text` argument uppercased."""

import json
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method", "")
        mid = msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": msg["params"].get("protocolVersion", ""),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "0.1"},
            }})
        elif method == "notifications/initialized":
            pass  # notification, no reply
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{
                "name": "echo",
                "description": "Echo the given text back, uppercased.",
                "inputSchema": {"type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"]},
            }]}})
        elif method == "tools/call":
            params = msg.get("params", {})
            if params.get("name") == "echo":
                text = str(params.get("arguments", {}).get("text", ""))
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": text.upper()}],
                    "isError": False,
                }})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "no such tool"}],
                    "isError": True,
                }})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": "unknown method"}})


if __name__ == "__main__":
    main()

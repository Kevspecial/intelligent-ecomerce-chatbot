"""
Gateway tool test log — calls each Gateway-backed tool directly over MCP and
prints the raw request/response, so the submission shows every tool returning
a well-formed (non-error, non-empty) result.

  uv run gateway_tool_log.py | tee test_outputs/gateway_tool_log.txt
"""
import json
import logging
import sys
from datetime import datetime, timezone

import boto3
from strands.tools.mcp.mcp_client import MCPClient

from main import GATEWAY_URL, REGION, GatewaySigV4Auth

# (bare tool name, arguments) — order_tracker is the API Gateway target,
# refund_processor is the Lambda target.
CALLS = [
    ("get_order",            {"order_id": "ORD-001"}),
    ("get_customer",         {"customer_id": "CUST-123"}),
    ("get_customer_orders",  {"customer_id": "CUST-123"}),
    ("initiate_refund",      {"order_id": "ORD-002", "reason": "Customer return", "amount": 139.99}),
    ("check_refund_status",  {"refund_id": "REF-TEST0001"}),
    ("get_return_label",     {"order_id": "ORD-002"}),
]


def main():
    # Fail fast with a clear message: expired lab credentials otherwise show up
    # as a long MCP "Authentication error - Invalid credentials" traceback.
    try:
        boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as e:
        sys.exit(f"AWS credentials are missing or expired ({e}). Refresh them and re-run.")

    print(f"# Gateway tool test log — {datetime.now(timezone.utc).isoformat()}")
    print(f"# Gateway: {GATEWAY_URL}\n")
    client = MCPClient(url=GATEWAY_URL, auth_provider=GatewaySigV4Auth(REGION))
    logging.getLogger("strands.tools.mcp.mcp_client").setLevel(logging.CRITICAL)
    try:
        client.start()
    except Exception as e:
        sys.exit(f"Could not connect to the Gateway: {e}")
    try:
        tools = {t.tool_name.split("___")[-1]: t.tool_name for t in client.list_tools_sync()}
        print(f"Loaded tools: {sorted(tools.values())}\n")

        passed = 0
        for i, (name, args) in enumerate(CALLS, 1):
            full_name = tools.get(name)
            if not full_name:
                print(f"[{i}] {name}: NOT FOUND on Gateway\n")
                continue
            result = client.call_tool_sync(f"log-{i}", full_name, args)
            text = "\n".join(c.get("text", "") for c in result["content"])
            ok = result["status"] == "success" and text.strip() and '"error"' not in text
            passed += bool(ok)
            print(f"[{i}] {full_name}  args={json.dumps(args)}")
            print(f"    status: {result['status']}  -> {'OK' if ok else 'FAILED'}")
            print(f"    response: {text}\n")

        print(f"Summary: {passed}/{len(CALLS)} Gateway tool calls returned well-formed responses.")
    finally:
        client.stop(None, None, None)


if __name__ == "__main__":
    main()

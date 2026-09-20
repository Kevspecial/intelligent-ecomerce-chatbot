"""
Customer Support AI Agent — Starter Code


Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
# These imports are provided. Do not remove them.
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser

# Added for AWS IAM (SigV4) authentication to the AgentCore Gateway.
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")


# TODO: Create the BedrockAgentCoreApp instance
app = BedrockAgentCoreApp()


# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── TODO 2 — Configuration ────────────────────────────────────────────────────


GATEWAY_URL = "https://customersupportgateway-r1i7itmvaf.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
KB_ID       = "SJWI4N8IL6"
REGION      = "us-east-1"
MEMORY_ID   = "CustomerSupportMemory-3Z7WT597cu"


# ── TODO 3 — Model and Clients ────────────────────────────────────────────────

model_id = "global.amazon.nova-2-lite-v1:0"

# TODO: Create the BedrockModel instance
model = BedrockModel(model_id=model_id)

# TODO: Create the MemoryClient instance
memory_client = MemoryClient(region_name=REGION)

# TODO: Create the boto3 bedrock-agent-runtime client
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# ── TODO 4 — Namespace Helper ─────────────────────────────────────────────────
# Implement get_namespaces() to return a dict mapping strategy type to
# namespace template string.

def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    strategies = mem_client.get_memory_strategies(memory_id)
    result = {}
    for s in strategies:
        result[s["type"]] = s["namespaces"][0]
    return result



# ── TODO 5 — Memory Hook ──────────────────────────────────────────────────────
# Implement MemoryHook, a HookProvider subclass that adds long-term memory.
#


class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.namespaces = get_namespaces(memory_client, memory_id)
        # Original (un-augmented) user query of the current turn, so we save
        # the customer's real words rather than the "Customer Context:" prefix.
        self._last_query = None

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        messages = event.agent.messages
        if not messages:
            return
        last = messages[-1]
        # Only plain-text user messages — tool results are also "user" role
        # but carry a toolResult block instead of text.
        if last.get("role") != "user" or "text" not in last["content"][0]:
            return
        query = last["content"][0]["text"]
        self._last_query = query

        found = []
        for strategy_type, ns in self.namespaces.items():
            try:
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=ns.format(actorId=self.actor_id),
                    query=query,
                    top_k=5,
                )
            except Exception as e:
                logger.warning(f"Memory retrieval failed for {strategy_type}: {e}")
                continue
            for m in memories:
                text = m.get("content", {}).get("text", "").strip()
                if text:
                    found.append(f"[{strategy_type}] {text}")

        if found:
            last["content"][0]["text"] = (
                "Customer Context:\n" + "\n".join(found) + "\n\n" + query
            )
            logger.info(f"Injected {len(found)} memories for {self.actor_id}")

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        messages = event.agent.messages
        customer_query = self._last_query
        agent_response = None

        # Walk backwards: the assistant's final text reply comes first, then
        # (if we didn't capture it in the retrieve hook) the user's question.
        for msg in reversed(messages):
            content = msg.get("content", [])
            if not content or "text" not in content[0]:
                continue
            if msg["role"] == "assistant" and agent_response is None:
                agent_response = content[0]["text"]
            elif msg["role"] == "user" and customer_query is None:
                customer_query = content[0]["text"]
            if customer_query and agent_response:
                break

        if not (customer_query and agent_response):
            return
        try:
            self.memory_client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")],
            )
            logger.info(f"Saved interaction for {self.actor_id}/{self.session_id}")
        except Exception as e:
            logger.warning(f"Failed to save interaction to memory: {e}")

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── TODO 6 — Knowledge Base Tool ─────────────────────────────────────────────
# Implement search_knowledge_base(query) using the @tool decorator.
#
# Steps:
#   1. Guard: if KB_ID is empty return "Knowledge base not configured."
#   2. Call _bedrock_runtime.retrieve(
#          knowledgeBaseId=KB_ID,
#          retrievalQuery={"text": query}
#      )
#   3. Extract resp["retrievalResults"]; return a message if empty
#   4. Join the text chunks with "\n---\n" and return the result
#
# The docstring is the tool description — the model uses it to decide when
# to call this tool, so keep it clear and accurate.

@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    # TODO: Implement the Knowledge Base search
    if not KB_ID:
        return "Knowledge base not configured."

    try:
        resp = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
        )
        results = resp.get("retrievalResults", [])
        if not results:
            return "No relevant information found in the knowledge base."

        chunks = [r["content"]["text"] for r in results]
        return "\n---\n".join(chunks)

    except Exception as e:
        logger.error(f"Knowledge base retrieval failed: {e}")
        return f"Knowledge base error: {e}"



# ── TODO 7 — Loyalty Discount Tool (Code Interpreter) ────────────────────────
# Implement calculate_loyalty_discount() using the @tool decorator.
#
# The tool must:
#   1. Build a self-contained Python code string that:
#        • Defines earn_rates: {"standard": 1, "device": 2, "fresh": 5}
#        • Defines tier_rates: {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
#        • Calculates points_redeemed (floor to nearest 500, cap at 50% of order)
#        • Calculates tier_discount (applied to subtotal after points)
#        • Calculates final_total, total_savings, points_earned, remaining_points
#        • Prints a JSON result dict
#   2. Execute the code with code_session(REGION).invoke("executeCode", {...})
#      using language="python" and clearContext=True
#   3. Return the first result event as a JSON string
#   4. Include a fallback that computes only the tier discount if the
#      Code Interpreter is unavailable

@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    # Self-contained program executed in the sandbox. Braces that belong to
    # the generated Python (dict literals) are doubled because this is an
    # f-string; single braces inject the tool arguments as literals.
    # Points policy: 500 points = $5.00 (1 point = $0.01), redeemed in blocks
    # of 500, never covering more than 50% of the order.
    code = f"""
import json, math

loyalty_points   = {int(loyalty_points)}
tier             = "{tier.strip().title()}"
order_total      = {float(order_total)}
product_category = "{product_category.strip().lower()}"

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}
POINT_VALUE = 0.01   # dollars per point

# 1. Points: floor to nearest 500, then cap so value <= 50% of the order.
points_redeemed = (loyalty_points // 500) * 500
max_points_by_cap = int((order_total * 0.5) / POINT_VALUE) // 500 * 500
points_redeemed = min(points_redeemed, max_points_by_cap)
points_value = round(points_redeemed * POINT_VALUE, 2)

# 2. Tier discount applies to the subtotal after points.
subtotal      = round(order_total - points_value, 2)
tier_rate     = tier_rates.get(tier, 0.0)
tier_discount = round(subtotal * tier_rate, 2)

final_total   = round(subtotal - tier_discount, 2)
total_savings = round(points_value + tier_discount, 2)
points_earned = math.floor(final_total * earn_rates.get(product_category, 1))
remaining_points = loyalty_points - points_redeemed + points_earned

result = {{
    "order_total": order_total,
    "tier": tier,
    "tier_rate": tier_rate,
    "points_redeemed": points_redeemed,
    "points_value": points_value,
    "subtotal_after_points": subtotal,
    "tier_discount": tier_discount,
    "final_total": final_total,
    "total_savings": total_savings,
    "points_earned": points_earned,
    "remaining_points": remaining_points,
}}
print(json.dumps(result))
"""

    try:
        with code_session(REGION) as cs:
            resp = cs.invoke(
                "executeCode",
                {"code": code, "language": "python", "clearContext": True},
            )
            for event in resp["stream"]:
                return json.dumps(event["result"])
        raise RuntimeError("Code Interpreter returned no result")

    except Exception as e:
        # Fallback: tier discount only, computed locally, clearly flagged so
        # the agent can tell the customer points were not applied.
        logger.warning(f"Code Interpreter unavailable, using fallback: {e}")
        tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        rate = tier_rates.get(tier.strip().title(), 0.0)
        discount = round(order_total * rate, 2)
        return json.dumps({
            "order_total": order_total,
            "tier": tier,
            "tier_discount": discount,
            "final_total": round(order_total - discount, 2),
            "points_redeemed": 0,
            "note": "Code Interpreter unavailable; loyalty points were not applied.",
        })


# ── TODO 8 — Agent Entrypoint ─────────────────────────────────────────────────
# Implement the invoke() function decorated with @app.entrypoint.
#
# Steps:
#   1. Extract user_input, actor_id, and session_id from the payload
#      (generate a UUID if session_id is missing)
#   2. Instantiate MemoryHook for this actor/session
#   3. Instantiate AgentCoreBrowser(region=REGION)
#   4. Build the tools list: [search_knowledge_base, calculate_loyalty_discount,
#                              agent_core_browser.browser]
#   5. Connect to the Gateway via MCPClient, load gateway_tools, extend tools list
#   6. Create and invoke the Agent with all tools, hooks, and system_prompt
#   7. Return the text from the first content block of the response
#   8. Handle exceptions gracefully

class GatewaySigV4Auth(httpx.Auth):
    """
    httpx auth hook that SigV4-signs every request to the Gateway.

    The Gateway was created with AWS IAM inbound authorization, so no Cognito
    tokens or secrets are needed: locally this uses the developer's AWS
    credentials, and inside AgentCore Runtime it uses the execution role.
    """
    requires_request_body = True

    def __init__(self, region: str, service: str = "bedrock-agentcore"):
        self.region = region
        self.service = service
        self.credentials = boto3.Session().get_credentials()

    def auth_flow(self, request: httpx.Request):
        aws_req = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers={"content-type": request.headers.get("content-type", "")},
        )
        SigV4Auth(
            self.credentials.get_frozen_credentials(), self.service, self.region
        ).add_auth(aws_req)
        request.headers.update(dict(aws_req.headers))
        yield request


SYSTEM_PROMPT = """You are a friendly, professional customer support agent for an Amazon store.

You have these tools:
- search_knowledge_base: product specs, return/warranty policies, loyalty program tiers, order status definitions.
- get_order / get_customer / get_customer_orders (via the Gateway): live order tracking and customer profiles.
- initiate_refund / check_refund_status / get_return_label (via the Gateway): refund handling.
- calculate_loyalty_discount: exact discount maths — always use it instead of doing arithmetic yourself.
- browser: visit a web page when the customer asks for live information from a website.

Guidelines:
- Use tools to get facts; never invent order details, prices, tracking numbers or policies.
- When tracking an order, report status, tracking number, carrier and estimated delivery.
- Before initiating a refund, look up the order with get_order and pass its total as the
  refund amount; then confirm the refund ID, amount, status and the expected timeline.
- If a message starts with "Customer Context:", use those remembered facts and preferences naturally.
- Be concise and clear. Ask for missing details (e.g. order ID) rather than guessing.
"""


@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    user_input = payload.get("prompt", "")
    actor_id = payload.get("customer_id", "anonymous")
    session_id = payload.get("session_id") or str(uuid.uuid4())

    if not user_input:
        return "Please provide a message in the 'prompt' field."

    try:
        memory_hook = MemoryHook(actor_id, session_id, memory_client, MEMORY_ID)
        agent_core_browser = AgentCoreBrowser(region=REGION)

        tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
            agent_core_browser.browser,
        ]

        # The MCP connection must stay open for the whole agent run, because
        # gateway tools are executed over it — hence Agent lives inside `with`.
        mcp_client = MCPClient(
            url=GATEWAY_URL,
            auth_provider=GatewaySigV4Auth(REGION),
        )
        with mcp_client:
            gateway_tools = mcp_client.list_tools_sync()
            tools.extend(gateway_tools)
            logger.info(f"Loaded {len(gateway_tools)} gateway tools")

            agent = Agent(
                model=model,
                tools=tools,
                hooks=[memory_hook],
                system_prompt=SYSTEM_PROMPT,
                callback_handler=None,
            )
            response = agent(user_input)

        return response.message["content"][0]["text"]

    except Exception as e:
        logger.exception("Agent invocation failed")
        return f"I'm sorry, something went wrong while handling your request: {e}"


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()

# Intelligent E-commerce Support Agent on Amazon Bedrock AgentCore

A production-style AI customer-support agent for an online store, built and deployed
end-to-end on **Amazon Bedrock AgentCore** with the **Strands Agents** framework.
It answers product and policy questions with RAG, tracks orders and processes refunds
through Lambda tools exposed over MCP, remembers customers across sessions, does exact
discount maths in a sandboxed code interpreter, and can browse the live web.

Built by **Kelvin Nwokike** as the capstone for Udacity's AWS AI Engineering Nanodegree
(Course 2). The original assignment brief is kept in [INSTRUCTIONS.md](INSTRUCTIONS.md);
my write-up of design decisions and challenges is in
[solution/REFLECTION.md](solution/REFLECTION.md).

---

## What it can do

| Capability | How it works |
|---|---|
| **Product & policy Q&A (RAG)** | `search_knowledge_base` tool → Bedrock Knowledge Base (Titan Embeddings v2, managed vector store) over the product catalogue |
| **Order tracking** | `get_order`, `get_customer`, `get_customer_orders` — a Lambda behind API Gateway, exposed as MCP tools by the AgentCore Gateway |
| **Refunds & return labels** | `initiate_refund`, `check_refund_status`, `get_return_label` — a second Lambda invoked directly by the Gateway |
| **Long-term memory** | Strands hooks + AgentCore Memory: facts and preferences are extracted per customer and injected into every new session |
| **Exact loyalty discounts** | `calculate_loyalty_discount` generates Python and runs it in the AgentCore Code Interpreter sandbox, with a local fallback |
| **Live web access** | AgentCore Browser tool (e.g. "go to amazon.com and tell me the page title") |
| **Monitoring** | CloudWatch metric filter on `ERROR` logs + alarm (> 5 errors / 5 min) |

## Architecture

```
                    ┌──────────────────────────────────────────────────────┐
  agentcore invoke  │            AgentCore Runtime  (main.py)              │
  ─────────────────►│  Strands Agent · Amazon Nova 2 Lite · MemoryHook     │
                    └───┬──────────┬──────────────┬─────────────┬──────────┘
                        │          │              │             │
              SigV4/MCP │          │ Retrieve     │ executeCode │ CDP
                        ▼          ▼              ▼             ▼
              ┌──────────────┐ ┌─────────────┐ ┌────────────┐ ┌──────────┐
              │  AgentCore   │ │  Bedrock    │ │ AgentCore  │ │AgentCore │
              │  Gateway     │ │  Knowledge  │ │ Code       │ │ Browser  │
              │  (AWS IAM)   │ │  Base       │ │ Interpreter│ │          │
              └──┬───────┬───┘ └─────────────┘ └────────────┘ └──────────┘
                 │       │
       API GW    │       │ direct
                 ▼       ▼
        ┌─────────────┐ ┌──────────────────┐        ┌───────────────────┐
        │order-tracker│ │ refund-processor │        │ AgentCore Memory  │
        │  (Lambda)   │ │    (Lambda)      │        │ facts+preferences │
        └─────────────┘ └──────────────────┘        └───────────────────┘
```

## Repository layout

```
solution/
├── main.py                 # the agent: tools, memory hook, Gateway auth, entrypoint
├── lambda/
│   ├── order_tracker.py    # order / customer lookup (API Gateway proxy)
│   ├── refund_processor.py # refund tools (direct Lambda target)
│   └── lambda_schema       # MCP tool schema for the refund target
├── product_catalog.txt     # Knowledge Base source document
├── test_outputs/           # screenshots of the 6 functional tests + CloudWatch alarm
├── REFLECTION.md           # design decision, challenges, production roadmap
├── pyproject.toml / uv.lock
└── .bedrock_agentcore.yaml # deployment config written by the AgentCore toolkit
```

## Implementation highlights

- **IAM-authenticated MCP.** The Gateway uses AWS IAM inbound auth, so there are no
  Cognito secrets. `GatewaySigV4Auth` (an `httpx.Auth` subclass) SigV4-signs each MCP
  request with botocore — developer credentials locally, the execution role in Runtime.
- **Memory hook that doesn't pollute itself.** `MemoryHook` listens on
  `MessageAddedEvent` to prepend retrieved memories as `Customer Context:` and on
  `AfterInvocationEvent` to persist the turn. It filters out tool-result messages and
  saves the customer's *original* words, so injected memories are never re-stored as
  new facts.
- **Deterministic maths.** The discount tool builds a self-contained Python program
  (points rounded to 500-blocks, capped at 50 % of the order, tier discount on the
  remainder) and executes it in the Code Interpreter; if the sandbox is unavailable it
  returns a clearly flagged tier-only estimate.
- **Prompt engineering from tests.** A test run produced a `$0` refund because the
  model skipped the optional `amount` argument; one system-prompt rule ("look up the
  order and pass its total") fixed it.

## Running it

Prerequisites: Python 3.14 via [uv](https://docs.astral.sh/uv/), AWS CLI v2 with
credentials for an account where the resources below exist (us-east-1).

```bash
cd solution
uv sync

# Configure & deploy (first time)
export AGENTCORE_SUPPRESS_RECOMMENDATION=1
uv run agentcore configure --entrypoint main.py --name csai_support_agent \
    --region us-east-1 --disable-memory --non-interactive
uv run agentcore deploy

# Talk to it
uv run agentcore invoke '{"prompt": "Can you track order ORD-001?", "customer_id": "CUST-123", "session_id": "t1"}'
```

Set `GATEWAY_URL`, `KB_ID`, `REGION`, `MEMORY_ID` at the top of `main.py` to your own
resources. The runtime execution role needs `bedrock-agentcore:InvokeGateway`, Memory
read/write, Browser session, and `bedrock:Retrieve` on the Knowledge Base.

## Verified scenarios

All six scenarios pass against the deployed agent (screenshots in
[solution/test_outputs](solution/test_outputs)):

1. **Order tracking** — status, tracking number `TRK987654321`, carrier UPS, ETA
2. **Refund** — refund ID, `APPROVED`, correct $139.99 amount, 3–5 business days
3. **RAG** — Platinum tier benefits: same-day shipping, 15 % discount, priority support
4. **Memory** — "I am Jane, I prefer concise responses" recalled in a brand-new session
5. **Discount** — Gold, 4 250 pts, $150 order → 4 000 pts redeemed, 10 % tier, **$99.00**
6. **Browser** — live page title fetched from amazon.com

## Stack

Amazon Bedrock AgentCore (Runtime, Gateway, Memory, Code Interpreter, Browser) ·
Strands Agents · Amazon Nova 2 Lite · Bedrock Knowledge Bases · AWS Lambda ·
API Gateway · CloudWatch · Python 3.14 · uv · MCP

## License

See [LICENSE.txt](LICENSE.txt).

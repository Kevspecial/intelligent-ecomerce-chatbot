# Project Reflection — Customer Support Agent on Amazon Bedrock AgentCore

## Design decision: IAM (SigV4) authentication for the Gateway

When creating the AgentCore Gateway I had to choose between JWT/Cognito and AWS IAM
inbound authorization. I chose **AWS IAM**. With Cognito I would have needed a user
pool, an app client, a client secret stored somewhere, and token-refresh code in the
agent. With IAM there are no secrets at all: locally the agent signs Gateway requests
with my developer credentials, and inside AgentCore Runtime it signs them with the
execution role. Because neither Strands nor the MCP SDK ships a SigV4 signer, I wrote
a small `httpx.Auth` subclass (`GatewaySigV4Auth`) that signs each request with
botocore's `SigV4Auth` and passed it to `MCPClient(url=..., auth_provider=...)`.
Related: the memory hook saves the customer's *original* question (`self._last_query`),
not the "Customer Context:"-augmented one, so retrieved memories are never re-stored as
new facts.

## Challenges and how I solved them

1. **Knowledge Base creation was blocked.** The lab IAM role denied
   `aoss:CreateSecurityPolicy` and every other vector store (S3 Vectors, Neptune, RDS).
   I worked around it by creating the Knowledge Base with Bedrock's fully *managed*
   vector store, which needs no OpenSearch permissions. `retrieve` worked immediately.
2. **The API Gateway target failed to import.** The Gateway exports an OpenAPI spec from
   the REST API, and my CLI-created methods had no method responses, so it reported
   `responses is missing`. Adding a `200` method response to each route and redeploying
   the stage fixed it. Target names also cannot contain underscores.
3. **Runtime permissions.** The first deployed invocation returned
   `AccessDeniedException: GetMemory`. The auto-created execution role covered model
   invocation and the Code Interpreter but not Memory, Gateway, Browser or the KB, so I
   attached an inline policy scoped to exactly those four resource ARNs.
4. **Refunds of $0.** In testing, the model called `initiate_refund` without the
   optional `amount`, which the Lambda defaults to 0. One line in the system prompt —
   "look up the order with `get_order` and pass its total" — fixed it.

## Extending the agent for production

- **Configuration and secrets:** move `GATEWAY_URL`, `KB_ID` and `MEMORY_ID` into
  environment variables or SSM Parameter Store, so one artifact deploys to dev/staging/prod.
- **Real data and idempotency:** back the Lambdas with DynamoDB and make
  `initiate_refund` idempotent (one refund per order) with an audit trail.
- **Identity and authorization:** derive `customer_id` from an authenticated identity
  (Cognito/JWT on the Runtime) instead of trusting the payload, so a caller cannot read
  another customer's orders or memories.
- **Safety and quality:** add Bedrock Guardrails for PII and off-topic content,
  streaming responses for lower perceived latency, retries with backoff around tool
  calls, and an evaluation set of the six test scenarios run in CI before each deploy.
- **Observability:** keep the `ERROR` alarm, add latency/token metrics and X-Ray
  traces, and route alarms to SNS for on-call notification.

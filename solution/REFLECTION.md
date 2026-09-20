# Project Reflection — Customer Support Agent on Amazon Bedrock AgentCore

## Design decision: IAM (SigV4) authentication for the Gateway

When I set up the AgentCore Gateway, I had to pick how the agent would log in. I went with AWS IAM.

The nice thing about IAM is that there are no passwords or secret keys to manage. When I run the agent on my own machine, it uses my developer credentials to sign each request. When the agent runs inside AgentCore Runtime, it uses the execution role instead.

The tricky part: neither Strands nor the MCP SDK comes with a built-in way to sign requests with SigV4. So I wrote a small helper class (GatewaySigV4Auth) that uses botocore's SigV4Auth to sign every request, then handed it to MCPClient. That way the Gateway accepts the requests.

One more small thing: the memory hook saves the customer's original question (I stored it in self._last_query), not the version that gets padded with "Customer Context:". That way, when the agent retrieves old memories, it doesn't accidentally save them again as brand-new facts.
new facts.

## Challenges and how I solved them

I couldn't create the Knowledge Base. The lab IAM role blocked aoss:CreateSecurityPolicy and every other vector store I tried (S3 Vectors, Neptune, RDS). I got around it by using Bedrock's fully managed vector store — that one doesn't need OpenSearch permissions at all. After that, retrieve worked right away.

The API Gateway target wouldn't import. The Gateway pulls an OpenAPI spec from my REST API, but the methods I created with the CLI had no method responses, so it complained responses is missing. I added a 200 method response to each route, redeployed the stage, and it worked. Also learned the hard way: target names can't have underscores.

Runtime permissions blew up. The first time I deployed and ran it, I got AccessDeniedException: GetMemory. The auto-created execution role covered model invocation and the Code Interpreter, but not Memory, Gateway, Browser, or the Knowledge Base. I fixed it by attaching an inline policy scoped to exactly those four resource ARNs.

Refunds were coming out as $0. During testing, the model called initiate_refund without the optional amount, and the Lambda defaults that to 0. One line in the system prompt — "look up the order with get_order and pass its total" — fixed it.

## Extending the agent for production

Config and secrets: move GATEWAY_URL, KB_ID, and MEMORY_ID into environment variables or SSM Parameter Store, so one build can deploy to dev, staging, and prod.

Real data and idempotency: back the Lambdas with DynamoDB, and make initiate_refund idempotent (one refund per order max) with an audit trail.

Identity and authorization: get customer_id from an authenticated identity (Cognito/JWT on the Runtime) instead of trusting whatever the payload says — otherwise someone could read another customer's orders or memories.

Safety and quality: add Bedrock Guardrails for PII and off-topic stuff, stream responses so it feels faster, add retries with backoff around tool calls, and run the six test scenarios in CI before every deploy.

Observability: keep the ERROR alarm, add latency and token metrics plus X-Ray traces, and send alarms to SNS so someone gets paged.

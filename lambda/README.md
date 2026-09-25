# nanobanana-mcp on AWS Lambda

Runs the same [nanobanana MCP server](https://github.com/ConechoAI/Nano-Banana-MCP)
behind a Lambda Function URL so Claude Code (or any HTTP MCP client) can use it
without a local Docker install.

## Architecture

```
Claude Code  ──HTTPS──▶  Lambda Function URL  ──▶  Lambda container
  type: http                  (AuthType: NONE)         nanobanana_mcp_server
  Authorization: Bearer <token>                        (FastMCP http, stateless)
                                                       IMAGE_OUTPUT_DIR=/tmp
                                                             │
                                                             ▼
                                                       Google Gemini API
```

Key differences from the Docker/stdio image:

| Concern | Docker image | Lambda |
|---|---|---|
| Transport | stdio (spawned by Claude) | Streamable HTTP (stateless, JSON mode) |
| Output files | `/output` bind-mounted to host | `/tmp/nanobanana` on Lambda, also uploaded to S3 with presigned `download_url` |
| Auth | none (local spawn) | bearer token (`MCP_AUTH_TOKEN`) |
| Startup | `docker run -i ...` | cold start ~2-5s, then warm |

Images are returned inline as MCP content blocks **and** uploaded to an S3
bucket created by the SAM stack. The wrapper injects an `instructions` block
telling the calling LLM to `curl` each image's `download_url` into the user's
working directory. The S3 bucket has a lifecycle rule that **auto-deletes
generated images after `ImageRetentionDays` days** (default 7) — clients should
download promptly. Presigned URLs themselves expire after
`PresignTtlSeconds` seconds (default 3600).

Why S3? Lambda's `/tmp` is per-instance; the file written by `generate_image`
on instance A is not visible to a follow-up request that lands on instance B.
S3 is the only reliable cross-invocation store.

## Prerequisites

- AWS account + credentials (`aws configure` or env vars)
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-sam-cli-install.html)
- Docker (used by `sam build` to produce the image)
- `GEMINI_API_KEY`

## WSL2 + Docker Desktop note

On WSL2 with Docker Desktop, `~/.docker/config.json` often carries
`"credsStore": "desktop.exe"` which breaks `sam build`/`sam deploy` (they
invoke the Python Docker SDK from inside WSL, which can't launch the Windows
helper). Work around it by pointing SAM at an isolated, empty Docker config
directory — do this once per shell:

```bash
mkdir -p ~/.docker-sam
echo '{}' > ~/.docker-sam/config.json
export DOCKER_CONFIG=~/.docker-sam   # or prefix every sam command
```

The snippets below assume `DOCKER_CONFIG` is exported. Drop the line on
Linux/macOS without Docker Desktop.

## Deploy

```bash
cd infra

# one-time ECR repo + bucket bootstrap is handled by --resolve-image-repos
sam build

sam deploy --guided \
  --parameter-overrides \
    GeminiApiKey=YOUR_GEMINI_KEY \
    McpAuthToken=$(openssl rand -hex 32) \
    PresignTtlSeconds=3600 \
    ImageRetentionDays=7
```

On success the stack prints `FunctionUrl`, e.g.
`https://abcd1234.lambda-url.ap-northeast-1.on.aws/`.

Re-deploying after code changes:

```bash
cd infra
sam build && sam deploy
```

One-time post-deploy: cap CloudWatch log retention (the Lambda-managed log
group defaults to never expire). Adjust days to taste.

```bash
aws logs put-retention-policy \
  --log-group-name "/aws/lambda/$(aws cloudformation describe-stacks \
    --stack-name nanobanana-mcp \
    --query 'Stacks[0].Outputs[?OutputKey==`FunctionName`].OutputValue' \
    --output text)" \
  --retention-in-days 14
```

## Client config (Claude Code)

`.mcp.json`:

```json
{
  "mcpServers": {
    "nanobanana": {
      "type": "http",
      "url": "https://abcd1234.lambda-url.ap-northeast-1.on.aws/mcp",
      "headers": {
        "Authorization": "Bearer PASTE_MCP_AUTH_TOKEN_HERE"
      }
    }
  }
}
```

Claude Code's `settings.json` `env` section is **not** used for the remote
variant — there is no `GEMINI_API_KEY` or `HOST_OUTPUT_DIR` to set on the
client. Everything lives in the Lambda env.

## Smoke test

```bash
URL="https://abcd1234.lambda-url.ap-northeast-1.on.aws/mcp"
TOKEN="..."

curl -sS -X POST "$URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Expect a JSON list of tools (generate_image, show_output_stats, maintenance,
request_image_upload). `upload_file` is hidden on Lambda (see below).

## Reference images (local files as input)

The Lambda cannot see the client's filesystem, so on this deployment
`generate_image.input_image_path_1/2/3` take an **S3 key** instead of a path:

```
1. request_image_upload(filename="ref.png")
     -> { upload_url: <presigned PUT, 900 s>, s3_key: "uploads/<32hex>/ref.png" }
2. curl -g -fsS -X PUT --upload-file './ref.png' '<upload_url>'
3. generate_image(prompt=..., input_image_path_1="uploads/<32hex>/ref.png")
```

- The server downloads the objects into `/tmp` just for that call and deletes
  them afterwards. The key itself stays valid for `ImageRetentionDays`, so reuse
  it across calls instead of uploading again. Results show your keys, not the
  server's `/tmp` paths.
- Every generated image in `images[]` also carries an `s3_key`
  (`images/<32hex>-<name>`); pass it the same way to edit or reuse a result.
- Only keys minted by this server are accepted. Any other value — a local path,
  `s3://...`, a URL — is rejected with a message pointing at
  `request_image_upload`, and never reaches upstream (on Lambda a path could only
  name the server's own files, e.g. `/proc/self/environ`).
- Upload URLs are issued only for image names (`.png .jpg .jpeg .webp .heic
  .heif`) and expire after `UPLOAD_URL_TTL_SECONDS` (default 900). The curl
  command is single-quoted with `-g` so a filename containing `$(...)`, `{a,b}`
  or `[1-2]` is uploaded literally instead of being expanded; the model is told
  to ask the user if the path contains `'` or a newline.
- Format is detected from the bytes, not the filename: PNG, JPEG, WEBP, HEIC,
  HEIF (what Gemini accepts). The references of one call may total
  `MAX_REFERENCE_MB` (default 13): upstream sends them inline as base64, and
  Gemini caps a request at 20 MB
  ([docs](https://ai.google.dev/gemini-api/docs/image-understanding)).
  Larger images must be downscaled or re-encoded (e.g. JPEG) first.
- Upstream's other server-side path arguments are closed: `output_path` is
  refused (images arrive via `download_url` / `safe_filename`), and `upload_file`
  is hidden from `tools/list` and refused — upstream only accepts relative paths
  and Lambda's working directory is read-only, so it could never work here.
- The flow is also described in the server's `instructions` (kept under Claude
  Code's 2048-character cut-off, asserted by a test) and in the schema
  descriptions of the affected arguments.

## Local end-to-end test

`sam local start-api` can run the container locally against Docker:

```bash
cd infra
sam local start-api \
  --parameter-overrides \
    GeminiApiKey=YOUR_KEY \
    McpAuthToken=local-dev-token
```

Then point a client at `http://127.0.0.1:3000/mcp` with the same bearer.

## Tests

```bash
DOCKER_CONFIG=~/.docker-sam ./lambda/run-tests.sh      # add pytest args after, e.g. -k upload
```

Builds the production image, layers pytest, a moto S3 server
and a fake Gemini endpoint on top (`Dockerfile.test`) and runs `lambda/tests/`
inside it with `--network none`. The real upstream `generate_image` runs end to
end against the fake Gemini, so the tests check the reference bytes and MIME
types that would be sent. The upload test runs the exact `curl` command the
model is told to use, with a hostile filename. Nothing is mounted from the host.

Runtime dependency versions are pinned by `lambda/constraints.txt` (taken from
the image deployed on 2026-04-22). `requirements.txt` alone would pull
fastmcp 4 / mcp 2 as of 2026-09-25; bump the pins deliberately and rerun the
tests.

## Costs / limits

- **Memory**: default 2048 MB — tune via `MemorySize` param.
- **Timeout**: default 180 s — Gemini Pro 4K gen can approach 90 s; leave headroom.
- **Cold start**: container images start slower than zip; first request after
  idle is ~2-5 s before the actual tool call runs.
- **Concurrency**: no reserved concurrency is set. If you want to cap spend add
  `ReservedConcurrentExecutions` in the template.
- **S3**: each generated image and uploaded reference is stored for
  `ImageRetentionDays` (default 7);
  S3 standard storage cost is negligible at this volume but the bucket name is
  exported as `ImageBucketName` in the stack outputs if you need to inspect it.

## Teardown

```bash
cd infra
sam delete
```

# Current task
Let the Lambda-hosted nanobanana MCP accept reference images from the
client machine (input_image_path_* / upload_file.path) — keep Lambda.

# Goal
Client flow: `request_image_upload(filename)` -> presigned PUT URL + s3_key
-> `curl -X PUT --upload-file` -> `generate_image(input_image_path_1=<s3_key>)`.
Generated images also expose their own `s3_key` so they can be fed back as
references without re-uploading. Mirrors ~/whisper-mcp-docker's
request_audio_upload flow.

# Pass criteria (written BEFORE implementation, 2026-09-25 15:40 JST)
1. Unit/integration tests (fastmcp 3.2.4 in-process client + moto S3) pass:
   - s3_key args are staged to /tmp and the tool sees a readable local file
     with the correct suffix (png/jpeg/webp/heic) and bytes identical to S3
   - local client paths (/home/..., /proc/self/environ, relative) are
     rejected with an error that names request_image_upload; the tool never runs
   - oversize objects, non-images, missing keys, malformed keys -> error
   - staged files are deleted after the call (success AND failure)
   - request_image_upload returns a PUT URL for `uploads/<32hex>/<safe>`
   - generated-image augment adds `s3_key`, and that key is accepted as input
2. Live deploy: a real reference PNG uploaded via curl to the presigned URL
   is used by generate_image on the deployed Lambda and yields an image whose
   metadata shows used_input_images=true; a local path yields the guided error.
3. Existing behaviour preserved: tools/list still lists the 4 upstream tools;
   generation without references still returns download_url/safe_filename.

# Done
- Root cause: upstream reads input_image_path_* / upload_file.path from the
  server FS; Lambda cannot see the client FS.
- Deployed image versions captured in logs/deployed-freeze.txt
  (fastmcp 3.2.4, mcp 1.27.0, nanobanana-mcp-server 0.4.4). Unpinned rebuild
  would jump to fastmcp 4.0.9 / mcp 2.2.0 -> pin via constraints file.
- Found: python:3.12 Lambda image has no .webp mimetype -> upstream sends
  webp input as image/png. Register it.
- Branch feat/lambda-reference-image-upload stacked on PR #2
  (feat/aws-lambda-deployment).

# Next
- [done] v1 impl + 56 tests; reproduced bug on OLD deploy
- [done] reviews (adversarial-critic + security-auditor). Fixed:
  upload_file broken vs real upstream (abs path refused, cwd read-only) ->
  hidden + refused on Lambda; output_path refused; curl now
  `curl -g -fsS -X PUT --upload-file '<LOCAL_PATH>' '<upload_url>'`;
  upload URLs only for image extensions, TTL 900 s; combined cap
  MAX_REFERENCE_MB=13 (Gemini 20 MB inline after base64); streamed staging
  with partial-file cleanup; /tmp paths in results replaced by s3_keys;
  AccessDenied msg; SigV4 presign; auth bytes compare (401 not 500);
  fail closed on Lambda without token; McpAuthToken MinLength 32 (current
  client token is 64 hex -> OK); _safe_basename fullmatch + short suffix;
  sniff handles 64-bit/0 ftyp sizes and AVIF brands beyond 64 bytes.
- [done] tests: 89 passed with --network none (logs/tests-4.log), incl. real
  upstream generate_image against a fake Gemini (GEMINI_BASE_URL)
- [done] mutation check of the revised suite: 11/11 mutants killed
- [running] adversarial-critic re-review (round 2) of the uncommitted diff
- [blocked 17:06 JST] host dockerd stopped answering (/_ping times out;
  other projects' `docker ps` hang too). `sam build` failed on it. Did NOT
  restart dockerd: it would kill other projects' running containers
  (e.g. a 2h pso.py run) -> user decision if it doesn't recover.
- then: commit, push, PR (base feat/aws-lambda-deployment)
- then: sam build + deploy, live E2E (scratchpad e2e.py; e2e `gen` must be
  updated: model_tier nb2 / resolution 1k; `uploadfile` step now expected to
  be refused). Verify SigV4 GET+PUT URLs work against real S3.
- Not fixed on purpose (user decisions): dependency bumps for known advisories
  in the pinned versions (mcp 1.27->1.28.1, starlette 1.0->1.3.1+, pillow
  12.2->12.3; all judged unreachable here); ReservedConcurrentExecutions;
  presigned POST size limit at upload time.

# Waiting
none (secret rotation below is the user's call, not blocking this task)

# Risks
- Previous session's secret-rotation item (GEMINI_API_KEY / MCP_AUTH_TOKEN
  leaked into an old transcript) is still the user's call; this task does not
  touch secrets. Redeploy reuses samconfig parameter values.

# Resume instruction
Continue from `# Next`. Build/test only inside Docker. Deploy with
`cd infra && DOCKER_CONFIG=~/.docker-sam sam build && DOCKER_CONFIG=~/.docker-sam sam deploy`.

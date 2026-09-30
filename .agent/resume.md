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
   AMENDED 2026-09-25 17:10 after review: upload_file is deliberately hidden
   on Lambda (it could never work there: upstream refuses absolute paths and
   /var/task is read-only), so tools/list = generate_image, maintenance,
   show_output_stats, request_image_upload. Recorded, not silently dropped.

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
- [done] deployed 2026-09-26 11:43 JST (commit f318661); live E2E passed all
  pass criteria (details in PR #3 test plan). Criterion 3 amended (upload_file
  hidden on purpose).
- Remaining = user decisions only (fleet TODOs filed):
  - merge PR #3 -> feat/aws-lambda-deployment and PR #2 -> main. The repo is
    PUBLIC, so the agent must not self-merge (CLAUDE.md: self-merge only for
    private personal repos).
  - rotate MCP_AUTH_TOKEN / GEMINI_API_KEY (high)
  - bump pinned deps with known advisories within majors (normal)
  - ReservedConcurrentExecutions / billing alarm (low)
  - upstream stores JPEG bytes as *.png -> S3 Content-Type image/png (low)
- To pick up the new tool, the user's Claude Code sessions must reconnect
  the nanobanana MCP server (tools/list + instructions are read at connect).
- 2026-09-29 18:30 JST resume check: live server still serves the new
  tools/list (e2e.py list OK). This long-running session's own tool schema is
  stale (old list) even after the user's /mcp. A fresh headless
  `claude -p` (2.1.284) run from inside this session's Bash could not verify
  the new tools: its new "version negotiation probe" timed out and then
  CONNECT_TIMEOUT for ALL remote HTTP MCPs at once (nanobanana, whisper,
  deck-forge, figure-mcp; no Lambda invocation logged), so it is a
  client/environment issue, not this deploy.
- 2026-09-29 19:12 JST after the user restarted the session: nanobanana
  connected in 227 ms, request_image_upload visible, upload_file gone; full
  flow via MCP tools OK. whisper / deck-forge connected but tools/list timed
  out in the 2.1.284 client (raw HTTP answers in 0.1 s) -> client-side, not
  this repo.

# Waiting
User decisions (agent work is finished; nothing else to do until one of these):
(1. DONE 2026-09-29: user asked to merge. #3 -> e09fc17, #2 -> main bb40f7d; main lambda/ infra/ == deployed f318661.)
2. Rotate MCP_AUTH_TOKEN (agent can do it on request) and GEMINI_API_KEY (user reissues in AI Studio).
5. PR #5 (fleet TODO, low): generated images delivered as their real format (JPEG -> .jpg,
   image/jpeg). Tested (100 passed, 2/2 mutants), NOT deployed: changes live filenames.
   On merge: sam build && sam deploy, then check safe_filename .jpg + curl -I Content-Type.
(3. DONE 2026-09-29: dep bump PR #4 deployed 21:53 JST, verified live, merged -> main 1c44966.)
(4. DONE 2026-09-29 19:12 JST: full flow verified through Claude Code's own MCP tools after a session restart.)

# Risks
- Previous session's secret-rotation item (GEMINI_API_KEY / MCP_AUTH_TOKEN
  leaked into an old transcript) is still the user's call; this task does not
  touch secrets. Redeploy reuses samconfig parameter values.

# Resume instruction
Continue from `# Next`. Build/test only inside Docker. Deploy with
`cd infra && DOCKER_CONFIG=~/.docker-sam sam build && DOCKER_CONFIG=~/.docker-sam sam deploy`.

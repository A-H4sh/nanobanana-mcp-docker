#!/usr/bin/env bash
# Build the production Lambda image, layer the tests on top, run them.
# Nothing is mounted from the host, so the source tree stays clean.
set -euo pipefail
cd "$(dirname "$0")"
docker build -q -t nanobanana-lambda:test-base .
docker build -q -t nanobanana-lambda:test -f Dockerfile.test .
# --network none: moto and the fake Gemini run on loopback inside the container.
docker run --rm --network none nanobanana-lambda:test "$@"

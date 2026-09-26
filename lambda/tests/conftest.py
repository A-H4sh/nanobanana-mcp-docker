"""Test harness, all on loopback (run-tests.sh uses --network none):

- a moto S3 server, so presigned PUT URLs can be exercised over the wire;
- a fake Gemini endpoint (upstream honours GEMINI_BASE_URL), so the real
  upstream generate_image runs end to end and we can inspect what it sends.

Env is set before `app` is imported: app builds the whole ASGI stack at import
time, like it does on Lambda.
"""

import base64
import io
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
import pytest
from moto.server import ThreadedMotoServer
from PIL import Image


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = _free_port()
GEMINI_PORT = _free_port()
BUCKET = "test-bucket"
TOKEN = "test-token-0123456789abcdef0123456789abcdef"

_buf = io.BytesIO()
Image.new("RGB", (16, 16), (10, 200, 30)).save(_buf, format="PNG")
FAKE_OUTPUT_PNG = _buf.getvalue()

# Parsed JSON bodies of generateContent calls the fake Gemini received.
GEMINI_GENERATE_BODIES: list[dict] = []


class _FakeGemini(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if ":generateContent" in self.path:
            GEMINI_GENERATE_BODIES.append(json.loads(body))
            self._reply(200, {"candidates": [{
                "content": {"role": "model", "parts": [{"inlineData": {
                    "mimeType": "image/png",
                    "data": base64.b64encode(FAKE_OUTPUT_PNG).decode()}}]},
                "finishReason": "STOP", "index": 0}]})
        else:  # Files API etc.: upstream treats failures there as non-fatal
            self._reply(400, {"error": {"code": 400, "message": "fake", "status": "INVALID_ARGUMENT"}})

    def do_GET(self):
        self._reply(404, {"error": {"code": 404, "message": "fake", "status": "NOT_FOUND"}})


os.environ.update(
    {
        "AWS_ENDPOINT_URL_S3": f"http://127.0.0.1:{PORT}",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "ap-northeast-1",
        "GEMINI_API_KEY": "dummy",
        "GEMINI_BASE_URL": f"http://127.0.0.1:{GEMINI_PORT}",
        "IMAGE_S3_BUCKET": BUCKET,
        "MAX_REFERENCE_MB": "1",
        "MCP_AUTH_TOKEN": TOKEN,
    }
)
os.environ.pop("AWS_SESSION_TOKEN", None)
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)

_server = ThreadedMotoServer(ip_address="127.0.0.1", port=PORT, verbose=False)
_server.start()
_gemini = ThreadingHTTPServer(("127.0.0.1", GEMINI_PORT), _FakeGemini)
threading.Thread(target=_gemini.serve_forever, daemon=True).start()


@pytest.fixture(scope="session", autouse=True)
def _bucket():
    client = boto3.client("s3", region_name="ap-northeast-1")
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"},
    )
    yield
    _server.stop()
    _gemini.shutdown()


@pytest.fixture
def s3():
    return boto3.client("s3", region_name="ap-northeast-1")

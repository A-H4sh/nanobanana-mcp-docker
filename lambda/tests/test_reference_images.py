"""Reference-image upload flow for the Lambda variant.

Each test states a claim that is false if the feature is broken: a staged
file that is missing or has different bytes, a local path that reaches the
tool, a staged file left behind, a schema/instructions change that never
reaches the client, bytes that never reach (fake) Gemini.
"""

import asyncio
import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from botocore.stub import Stubber
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from PIL import Image
from starlette.testclient import TestClient

import app
from conftest import BUCKET, GEMINI_GENERATE_BODIES, TOKEN

MAX = app.MAX_REFERENCE_BYTES  # 1 MiB under MAX_REFERENCE_MB=1


def _img(fmt: str, size=(8, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 90)).save(buf, format=fmt)
    return buf.getvalue()


def _ftyp(major: bytes, compatible: tuple[bytes, ...] = (), size_field: int | None = None) -> bytes:
    body = major + b"\x00\x00\x00\x00" + b"".join(compatible)
    size = 8 + len(body) if size_field is None else size_field
    return size.to_bytes(4, "big") + b"ftyp" + body + b"\x00" * 32


def _ftyp64(major: bytes, compatible: tuple[bytes, ...] = ()) -> bytes:
    body = major + b"\x00\x00\x00\x00" + b"".join(compatible)
    return (1).to_bytes(4, "big") + b"ftyp" + (16 + len(body)).to_bytes(8, "big") + body


PNG, JPEG, WEBP = _img("PNG"), _img("JPEG"), _img("WEBP")
HEIC = _ftyp(b"heic", (b"mif1", b"heic"))


def _upload_key(name="ref.png") -> str:
    return f"{app.UPLOAD_PREFIX}{uuid.uuid4().hex}/{name}"


def _put(s3, data: bytes, key: str | None = None) -> str:
    key = key or _upload_key()
    s3.put_object(Bucket=BUCKET, Key=key, Body=data)
    return key


def _staged_files() -> set[Path]:
    d = app.INPUT_STAGING_DIR
    return set(d.iterdir()) if d.exists() else set()


# --------------------------------------------------------------------------
# format sniffing


@pytest.mark.parametrize(
    "data,suffix",
    [
        (PNG, ".png"),
        (JPEG, ".jpg"),
        (WEBP, ".webp"),
        (HEIC, ".heic"),
        (_ftyp(b"mif1", (b"mif1", b"heic")), ".heif"),
        (_ftyp64(b"heic", (b"mif1", b"heic")), ".heic"),  # 64-bit box size
        (_ftyp(b"avif", (b"mif1", b"avif")), None),
        (_ftyp(b"mif1", (b"mif1", b"avif")), None),  # AVIF behind a mif1 major
        (_ftyp(b"mif1", tuple([b"mif1"] * 12) + (b"avif",)), None),  # 13th brand
        (_ftyp(b"mif1", (b"mif1", b"avif"), size_field=0), None),  # size 0 = to EOF
        (_ftyp(b"heic", (b"mif1",), size_field=4096), None),  # box beyond the head
        (_ftyp(b"heic", tuple([b"mif1"] * 14) + (b"heic",)), ".heic"),  # 76-byte box
        (_img("GIF"), None),
        (b"#!/bin/sh\necho hi\n", None),
        (b"", None),
    ],
)
def test_sniff_image_suffix(data, suffix):
    assert app._sniff_image_suffix(data[: app._SNIFF_BYTES]) == suffix


@pytest.mark.parametrize(
    "suffix,mime",
    [(".png", "image/png"), (".jpg", "image/jpeg"), (".webp", "image/webp"),
     (".heic", "image/heic"), (".heif", "image/heif")],
)
def test_staged_suffix_maps_to_the_mime_upstream_sends(suffix, mime):
    # upstream: mimetypes.guess_type(path), falling back to image/png
    assert mimetypes.guess_type("x" + suffix)[0] == mime


@pytest.mark.parametrize(
    "name,suffix,kept",
    [("ref-01.png", ".png", True), ("ref.png\n", ".png", False),
     ("my photo.png", ".png", False), ("a" * 129, "", False)],
)
def test_safe_basename(name, suffix, kept):
    out = app._safe_basename(name, suffix)
    assert (out == name) is kept
    assert app._SAFE_BASENAME_RE.fullmatch(out)


def test_safe_basename_fallback_never_exceeds_key_limit():
    out = app._safe_basename("my photo." + "a" * 120, "." + "a" * 120)
    assert out.endswith(".bin") and len(out) <= 128


# --------------------------------------------------------------------------
# middleware, against an in-process server whose tools mirror upstream's
# argument names and report what they actually received


def _fake_server():
    srv = FastMCP("fake")
    calls = []

    def _describe(path):
        p = Path(path)
        return {
            "path": path,
            "is_file": p.is_file(),
            "sha": hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None,
            "suffix": p.suffix,
        }

    @srv.tool()
    def generate_image(
        prompt: str,
        input_image_path_1: str | None = None,
        input_image_path_2: str | None = None,
        input_image_path_3: str | None = None,
        output_path: str | None = None,
        explode: bool = False,
    ) -> dict:
        got = {
            k: _describe(v)
            for k, v in (("1", input_image_path_1), ("2", input_image_path_2),
                         ("3", input_image_path_3))
            if v
        }
        calls.append(got)
        if explode:
            raise RuntimeError(f"Failed to load input image {input_image_path_1}: boom")
        return got

    @srv.tool()
    def upload_file(path: str) -> dict:
        calls.append(path)
        return {}

    @srv.tool()
    def other_tool(input_image_path_1: str) -> str:
        calls.append(input_image_path_1)
        return input_image_path_1

    srv.add_middleware(app.S3InputImageMiddleware())
    return srv, calls


def _call(srv, name, args):
    async def go():
        async with Client(srv) as c:
            return await c.call_tool(name, args, raise_on_error=False)

    return asyncio.run(go())


def test_three_references_are_staged_with_identical_bytes_and_cleaned_up(s3):
    srv, calls = _fake_server()
    keys = [_put(s3, PNG), _put(s3, JPEG, _upload_key("photo.jpeg")), _put(s3, WEBP)]
    before = _staged_files()
    r = _call(srv, "generate_image", {
        "prompt": "p",
        "input_image_path_1": keys[0],
        "input_image_path_2": keys[1],
        "input_image_path_3": keys[2],
    })
    assert not r.is_error, r.content
    got = calls[-1]
    for slot, data, suffix in (("1", PNG, ".png"), ("2", JPEG, ".jpg"), ("3", WEBP, ".webp")):
        assert got[slot]["is_file"]
        assert got[slot]["sha"] == hashlib.sha256(data).hexdigest()
        assert got[slot]["suffix"] == suffix
        assert got[slot]["path"].startswith(str(app.INPUT_STAGING_DIR) + "/")
    assert _staged_files() == before  # all three removed after the call


def test_result_shows_the_callers_keys_not_staged_paths(s3):
    srv, calls = _fake_server()
    key = _put(s3, PNG)
    r = _call(srv, "generate_image", {"prompt": "p", "input_image_path_1": key})
    assert not r.is_error
    assert r.structured_content["1"]["path"] == key
    assert str(app.INPUT_STAGING_DIR) not in json.dumps(r.structured_content)
    assert str(app.INPUT_STAGING_DIR) not in r.content[0].text


def test_suffix_comes_from_content_not_from_client_filename(s3):
    srv, calls = _fake_server()
    key = _put(s3, JPEG, _upload_key("lies.png"))
    r = _call(srv, "generate_image", {"prompt": "p", "input_image_path_1": key})
    assert not r.is_error
    assert calls[-1]["1"]["suffix"] == ".jpg"


@pytest.mark.parametrize(
    "value",
    [
        "/home/user/ref.png",
        "/proc/self/environ",
        "ref.png",
        "./ref.png",
        "~/ref.png",
        "C:\\Users\\u\\ref.png",
        f"s3://{BUCKET}/uploads/{'a' * 32}/ref.png",
        f"uploads/{'a' * 32}/../images/x.png",
        f"uploads/{'a' * 31}/ref.png",
        f"uploads/{'A' * 32}/ref.png",
        f"uploads/{'a' * 32}/.hidden",
        f"uploads/{'a' * 32}/sub/ref.png",
        f"/uploads/{'a' * 32}/ref.png",
        f"uploads/{'a' * 32}/ref.png\n",
        f"images/{'a' * 32}/ref.png",
        f"other/{'a' * 32}/ref.png",
        "https://example.com/ref.png",
    ],
)
def test_non_key_values_are_rejected_before_the_tool_runs(value):
    srv, calls = _fake_server()
    r = _call(srv, "generate_image", {"prompt": "p", "input_image_path_1": value})
    assert r.is_error
    text = r.content[0].text
    assert "request_image_upload" in text and "cannot read files on your machine" in text
    assert app._CURL_UPLOAD in text
    assert calls == []


@pytest.mark.parametrize(
    "value",
    [1, True, ["/proc/self/environ"], {"path": "/proc/self/environ"}],
)
def test_non_string_values_are_rejected(value):
    srv, calls = _fake_server()
    r = _call(srv, "generate_image", {"prompt": "p", "input_image_path_1": value})
    assert r.is_error and calls == []


def test_a_bad_later_argument_rejects_before_anything_is_downloaded(s3):
    srv, calls = _fake_server()
    before = _staged_files()
    r = _call(srv, "generate_image", {
        "prompt": "p",
        "input_image_path_1": _put(s3, PNG),
        "input_image_path_2": "/home/user/second.png",
    })
    assert r.is_error and "input_image_path_2" in r.content[0].text
    assert calls == [] and _staged_files() == before


@pytest.mark.parametrize("value", ["/tmp/nanobanana-inputs", "out.png", "/tmp/nanobanana/README.md", " "])
def test_output_path_is_refused(value):
    srv, calls = _fake_server()
    r = _call(srv, "generate_image", {"prompt": "p", "output_path": value})
    assert r.is_error and "output_path is not supported" in r.content[0].text
    assert calls == []


def test_upload_file_is_refused_whatever_the_path(s3):
    srv, calls = _fake_server()
    for value in ("/proc/self/environ", "app.py", _put(s3, PNG)):
        r = _call(srv, "upload_file", {"path": value})
        assert r.is_error and "not available on this remote server" in r.content[0].text
    assert calls == []


def test_tools_without_path_args_are_untouched():
    srv, calls = _fake_server()
    r = _call(srv, "other_tool", {"input_image_path_1": "/anything"})
    assert not r.is_error and calls == ["/anything"]


@pytest.mark.parametrize("output_path", [None, ""])
def test_no_reference_args_passes_through(output_path):
    srv, calls = _fake_server()
    r = _call(srv, "generate_image", {"prompt": "p", "input_image_path_1": None,
                                      "input_image_path_2": "", "output_path": output_path})
    assert not r.is_error and calls == [{}]


@pytest.mark.parametrize(
    "setup,expect",
    [
        (lambda s3: _upload_key(), "No readable image"),
        (lambda s3: _put(s3, b""), "empty"),
        (lambda s3: _put(s3, b"not an image at all" * 10), "is not a PNG, JPEG, WEBP, HEIC or HEIF"),
        (lambda s3: _put(s3, _img("GIF")), "is not a PNG"),
        (lambda s3: _put(s3, PNG + b"\x00" * (MAX + 1 - len(PNG))), "exceed 1 MB combined"),
    ],
)
def test_bad_objects_are_reported(s3, setup, expect):
    srv, calls = _fake_server()
    before = _staged_files()
    r = _call(srv, "generate_image", {"prompt": "p", "input_image_path_1": setup(s3)})
    assert r.is_error
    assert expect in r.content[0].text, r.content[0].text
    assert calls == []
    assert _staged_files() == before


def test_object_of_exactly_the_limit_is_accepted(s3):
    srv, calls = _fake_server()
    data = PNG + b"\x00" * (MAX - len(PNG))
    assert len(data) == MAX
    r = _call(srv, "generate_image", {"prompt": "p", "input_image_path_1": _put(s3, data)})
    assert not r.is_error, r.content
    assert calls[-1]["1"]["sha"] == hashlib.sha256(data).hexdigest()


def test_limit_is_on_the_combined_size_of_all_references(s3):
    srv, calls = _fake_server()
    half = PNG + b"\x00" * (MAX // 2 + 1 - len(PNG))  # two of these exceed MAX
    before = _staged_files()
    r = _call(srv, "generate_image", {
        "prompt": "p",
        "input_image_path_1": _put(s3, half),
        "input_image_path_2": _put(s3, half),
    })
    assert r.is_error and "exceed 1 MB combined" in r.content[0].text
    assert calls == [] and _staged_files() == before


def test_staged_files_are_removed_when_the_tool_itself_fails(s3):
    srv, calls = _fake_server()
    before = _staged_files()
    key = _put(s3, PNG)
    r = _call(srv, "generate_image", {
        "prompt": "p", "input_image_path_1": key, "explode": True,
    })
    assert r.is_error and calls[-1]["1"]["is_file"]
    assert _staged_files() == before
    # the error quotes the caller's key, not the server's /tmp path
    assert key in r.content[0].text
    assert str(app.INPUT_STAGING_DIR) not in r.content[0].text


def test_stale_staged_files_from_killed_calls_are_swept(s3):
    app.INPUT_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    stale = app.INPUT_STAGING_DIR / "deadbeef.part"
    fresh = app.INPUT_STAGING_DIR / "cafebabe.png"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"x")
    old = time.time() - app._STALE_STAGING_SECONDS - 60
    os.utime(stale, (old, old))
    try:
        srv, calls = _fake_server()
        r = _call(srv, "generate_image", {"prompt": "p", "input_image_path_1": _put(s3, PNG)})
        assert not r.is_error
        assert not stale.exists() and fresh.exists()
    finally:
        stale.unlink(missing_ok=True)
        fresh.unlink(missing_ok=True)


def test_partial_file_is_removed_when_the_disk_write_fails(s3, monkeypatch):
    real_open = open

    class _Full:
        def __init__(self, f):
            self.f = f

        def write(self, b):
            self.f.write(b[:10])
            raise OSError(28, "No space left on device")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

    monkeypatch.setattr(app, "open", lambda p, m: _Full(real_open(p, m)), raising=False)
    srv, calls = _fake_server()
    before = _staged_files()
    r = _call(srv, "generate_image", {"prompt": "p", "input_image_path_1": _put(s3, PNG)})
    assert r.is_error and "No space left" in r.content[0].text
    assert calls == [] and _staged_files() == before


def test_same_key_can_be_reused_across_calls(s3):
    srv, calls = _fake_server()
    key = _put(s3, PNG)
    for _ in range(2):
        r = _call(srv, "generate_image", {"prompt": "p", "input_image_path_1": key})
        assert not r.is_error
    assert calls[0]["1"]["sha"] == calls[1]["1"]["sha"]


def test_real_aws_urls_use_the_regional_virtual_host(monkeypatch):
    # No custom endpoint = production. Presigning needs no network.
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3")
    client = app._make_s3_client()
    for method in ("put_object", "get_object"):
        url = client.generate_presigned_url(
            method, Params={"Bucket": "my-bucket", "Key": "uploads/k/a.png"}, ExpiresIn=60)
        parts = urlsplit(url)
        assert parts.scheme == "https"
        assert parts.netloc == "my-bucket.s3.ap-northeast-1.amazonaws.com", url
        assert parse_qs(parts.query)["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]


def test_access_denied_reads_as_missing_upload():
    # Production has no s3:ListBucket, so S3 answers a missing key with 403;
    # moto answers 404, hence the stub.
    client = app._s3()
    with Stubber(client) as stub:
        stub.add_client_error("get_object", service_error_code="AccessDenied",
                              http_status_code=403)
        with pytest.raises(ToolError, match=r"No readable image .*AccessDenied"):
            app._stage_s3_image(_upload_key(), MAX, [])


# --------------------------------------------------------------------------
# generated images expose an s3_key that round-trips as a reference


def test_generated_image_s3_key_is_accepted_as_a_reference():
    app.IMAGE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = app.IMAGE_OUTPUT_DIR / f"gen-{uuid.uuid4().hex[:8]}.png"
    out.write_bytes(PNG)
    images = [{"full_path": str(out)}]
    assert app.S3AugmentMiddleware._augment_images(images)
    key = images[0]["s3_key"]
    q = parse_qs(urlsplit(images[0]["download_url"]).query)
    assert q["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert q["X-Amz-Expires"] == [str(app.PRESIGN_TTL)]
    assert key.startswith(app.S3_PREFIX)
    assert app._INPUT_KEY_RE.fullmatch(key)
    staged = []
    local, size = app._stage_s3_image(key, MAX, staged)
    try:
        assert local.read_bytes() == PNG and size == len(PNG)
    finally:
        app._discard(staged)


# --------------------------------------------------------------------------
# the real server, over the real HTTP stack (auth + S3 augment + upstream)


def _rpc(client, method, params=None, token=TOKEN):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token is not None:
        headers["Authorization"] = token if isinstance(token, bytes) else f"Bearer {token}"
    return client.post(
        "/mcp",
        headers=headers,
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                            "params": params or {}}),
    )


def _tool(http, name, args):
    return _rpc(http, "tools/call", {"name": name, "arguments": args}).json()["result"]


@pytest.fixture(scope="module")
def http():
    with TestClient(app._asgi_app, raise_server_exceptions=False) as c:
        yield c


def test_auth_still_required(http):
    assert _rpc(http, "tools/list", token=None).status_code == 401
    assert _rpc(http, "tools/list", token="wrong").status_code == 401


def test_non_ascii_bearer_is_401_not_500(http):
    assert _rpc(http, "tools/list", token=b"Bearer \xe9\xe9").status_code == 401


def test_lambda_refuses_to_start_without_a_token():
    env = {k: v for k, v in os.environ.items() if k != "MCP_AUTH_TOKEN"}
    env["AWS_LAMBDA_FUNCTION_NAME"] = "nanobanana-test"
    proc = subprocess.run([sys.executable, "-c", "import app"], env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    assert "MCP_AUTH_TOKEN must be set" in proc.stderr


def test_info_logs_reach_a_preinstalled_root_handler():
    # Lambda installs a root handler (at WARNING) before importing the handler
    # module; INFO lines must still come out when LOG_LEVEL=INFO.
    env = dict(os.environ, LOG_LEVEL="INFO")
    code = ("import logging, sys; "
            "logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format='%(message)s'); "
            "import app; app.log.info('probe-info-line')")
    proc = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "probe-info-line" in proc.stderr
    assert "config: bucket=test-bucket" in proc.stderr and "auth=bearer" in proc.stderr


def test_tools_list_hides_upload_file_and_rewrites_descriptions(http):
    tools = {t["name"]: t for t in _rpc(http, "tools/list").json()["result"]["tools"]}
    assert set(tools) == {"generate_image", "maintenance", "show_output_stats",
                          "request_image_upload"}
    props = tools["generate_image"]["inputSchema"]["properties"]
    for arg in ("input_image_path_1", "input_image_path_2", "input_image_path_3"):
        assert "s3_key from request_image_upload" in props[arg]["description"]
    assert "Not supported on this remote server" in props["output_path"]["description"]
    assert "On this remote server: input_image_path_1/2/3 take an s3_key" in (
        tools["generate_image"]["description"])
    assert "s3_key" not in props["prompt"]["description"]  # untouched


def test_instructions_fit_claude_code_limit_and_carry_both_flows(http):
    result = _rpc(http, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    }).json()["result"]
    text = result["instructions"]
    # 16 chars of headroom for longer TTL / retention / size values than the defaults
    assert len(text) <= 2048 - 16, len(text)
    assert "## Image download" in text and "## Reference images" in text
    assert app._CURL_UPLOAD in text and "output_path" in text


def test_request_image_upload_then_documented_curl_then_reference(http, s3, tmp_path):
    out = _tool(http, "request_image_upload", {"filename": "/home/u/My Ref (1).webp"})
    assert not out.get("isError"), out
    out = out["structuredContent"]
    key = out["s3_key"]
    assert re.fullmatch(r"uploads/[0-9a-f]{32}/image-[0-9a-f]{12}\.webp", key), key
    assert app._INPUT_KEY_RE.fullmatch(key)
    assert out["expires_in_seconds"] == app.UPLOAD_URL_TTL == 900
    q = parse_qs(urlsplit(out["upload_url"]).query)
    assert q["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert q["X-Amz-Expires"] == ["900"]
    assert out["max_reference_mb_combined"] == 1
    assert out["how_to_upload"] == app._CURL_UPLOAD

    # A hostile but legal filename: with the documented single quotes and -g,
    # neither the command substitution nor curl's {a,b} globbing fires.
    src_dir = tmp_path / "pics"
    src_dir.mkdir()
    src = src_dir / "My Ref $(touch PWNED) {a,b}[1-2].webp"
    src.write_bytes(WEBP)
    cmd = out["how_to_upload"].replace("<LOCAL_PATH>", str(src)).replace(
        "<upload_url>", out["upload_url"])
    subprocess.run(["bash", "-c", cmd], cwd=tmp_path, check=True, timeout=30)
    assert not (tmp_path / "PWNED").exists()
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == WEBP


@pytest.mark.parametrize(
    "filename,suffix",
    [("C:\\pics\\ref-01.jpg", "/ref-01.jpg"), ("shot.HEIC", "/shot.HEIC"),
     ("ref.png\n", ".png"), ("x." + "p" * 3 + ".jpeg", ".jpeg")],
)
def test_upload_keys_are_always_accepted_back(http, filename, suffix):
    out = _tool(http, "request_image_upload", {"filename": filename})
    key = out["structuredContent"]["s3_key"]
    assert key.endswith(suffix), key
    assert app._INPUT_KEY_RE.fullmatch(key)


@pytest.mark.parametrize("filename", ["id_rsa", ".env", "credentials", "notes.txt",
                                      "archive.png.zip", "photo.gif"])
def test_request_image_upload_refuses_non_images(http, filename):
    out = _tool(http, "request_image_upload", {"filename": filename})
    assert out["isError"] is True and "is not an image file" in out["content"][0]["text"]


@pytest.mark.parametrize(
    "tool,args,expect",
    [
        ("generate_image", {"prompt": "p", "input_image_path_1": "/home/u/ref.png"}, "request_image_upload"),
        ("generate_image", {"prompt": "p", "output_path": "/tmp/nanobanana-inputs"}, "output_path is not supported"),
        ("upload_file", {"path": "/proc/self/environ"}, "not available on this remote server"),
    ],
)
def test_real_upstream_tools_refuse_server_paths(http, tool, args, expect):
    before = len(GEMINI_GENERATE_BODIES)
    body = _tool(http, tool, args)
    assert body["isError"] is True and expect in body["content"][0]["text"]
    assert len(GEMINI_GENERATE_BODIES) == before


def test_real_upstream_generate_image_missing_key(http):
    body = _tool(http, "generate_image", {"prompt": "p", "input_image_path_1": _upload_key()})
    assert body["isError"] is True and "No readable image" in body["content"][0]["text"]


def test_real_upstream_generate_image_sends_reference_bytes_to_gemini(http, s3):
    png_key = _put(s3, PNG)
    heic_key = _put(s3, HEIC, _upload_key("phone.heic"))
    before = len(GEMINI_GENERATE_BODIES)
    staged_before = _staged_files()
    body = _tool(http, "generate_image", {
        "prompt": "combine these", "model_tier": "nb2",
        "input_image_path_1": png_key, "input_image_path_2": heic_key,
    })
    assert not body.get("isError"), json.dumps(body)[:800]

    sent = GEMINI_GENERATE_BODIES[before:]
    assert len(sent) == 1
    inline = [p["inlineData"] for c in sent[0]["contents"] for p in c["parts"] if "inlineData" in p]
    # google-genai sends url-safe base64 without padding
    decode = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    assert {(d["mimeType"], decode(d["data"])) for d in inline} == {
        ("image/png", PNG), ("image/heic", HEIC)}

    sc = body["structuredContent"]
    assert sc["input_image_paths"] == [png_key, heic_key]
    assert str(app.INPUT_STAGING_DIR) not in json.dumps(body)
    [img] = sc["images"]
    assert img["download_url"] and app._INPUT_KEY_RE.fullmatch(img["s3_key"])
    assert _staged_files() == staged_before

    # the generated image feeds back in as a reference (edit mode, 1 input)
    before = len(GEMINI_GENERATE_BODIES)
    body = _tool(http, "generate_image", {"prompt": "make it blue", "model_tier": "nb2",
                                          "input_image_path_1": img["s3_key"]})
    assert not body.get("isError"), json.dumps(body)[:800]
    sent = GEMINI_GENERATE_BODIES[before:]
    assert len(sent) == 1 and "inlineData" in json.dumps(sent[0])

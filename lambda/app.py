"""AWS Lambda entrypoint for nanobanana MCP server.

Exposes the upstream FastMCP server via Streamable HTTP (stateless JSON mode)
wrapped with Mangum so it can be invoked behind a Lambda Function URL or API
Gateway.

Generated images are uploaded to an S3 bucket and a presigned download URL is
injected into the JSON metadata of the tool response, so the calling LLM can
fetch the file (Lambda /tmp is per-instance and not reachable across calls).

Reference images travel the other way: the server cannot read the client's
filesystem, so `request_image_upload` hands out a presigned PUT URL, the client
uploads with curl, and the returned `s3_key` is passed as
`generate_image.input_image_path_1/2/3`. `S3InputImageMiddleware` downloads the
objects into /tmp for the duration of the call and swaps in the local paths.
Generated images carry their own `s3_key` so a result can be fed back as a
reference without a round trip. Upstream's other server-side path arguments are
closed on Lambda: `generate_image.output_path` is refused and `upload_file` is
hidden (upstream only accepts relative paths, and Lambda's cwd is read-only).

Env vars consumed here (Lambda / SAM template sets these):
    GEMINI_API_KEY              - required, passed straight through
    MCP_AUTH_TOKEN              - required on Lambda, `Authorization: Bearer <token>`
    IMAGE_OUTPUT_DIR            - optional, defaults to /tmp/nanobanana
    IMAGE_S3_BUCKET             - optional, enables S3 upload + download_url injection
    IMAGE_S3_PREFIX             - optional, defaults to "images/"
    IMAGE_PRESIGN_TTL_SECONDS   - optional, download URL TTL, defaults to 3600
    UPLOAD_URL_TTL_SECONDS      - optional, upload URL TTL, defaults to 900
    IMAGE_RETENTION_DAYS        - optional, advisory only (used in instructions text)
    MAX_REFERENCE_MB            - optional, combined reference size per call, defaults to 13
    LOG_LEVEL                   - optional, forwarded to upstream
"""

from __future__ import annotations

import copy
import hmac
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

os.environ.setdefault("FASTMCP_TRANSPORT", "http")
os.environ.setdefault("IMAGE_OUTPUT_DIR", "/tmp/nanobanana")

import anyio
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult
from mangum import Mangum
from mcp.types import TextContent
from pydantic import Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from nanobanana_mcp_server.server import create_wrapper_app


log = logging.getLogger(__name__)

# The Lambda runtime installs a root handler before this module is imported,
# so upstream's setup_logging() (it only runs when the root has no handlers)
# is skipped and the root stays at WARNING: LOG_LEVEL was silently ignored and
# no INFO line (ours or upstream's) ever reached CloudWatch.
logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") and not os.environ.get("MCP_AUTH_TOKEN"):
    # BearerAuthMiddleware passes everything through without a token; on a
    # public Function URL that would hand Gemini quota and upload URLs to anyone.
    raise RuntimeError("MCP_AUTH_TOKEN must be set when running on Lambda")

S3_BUCKET = os.environ.get("IMAGE_S3_BUCKET")
S3_PREFIX = os.environ.get("IMAGE_S3_PREFIX", "images/")
PRESIGN_TTL = int(os.environ.get("IMAGE_PRESIGN_TTL_SECONDS", "3600"))
# Upload URLs are used seconds after they are issued; keep them short-lived.
UPLOAD_URL_TTL = int(os.environ.get("UPLOAD_URL_TTL_SECONDS", "900"))
IMAGE_RETENTION_DAYS = int(os.environ.get("IMAGE_RETENTION_DAYS", "7"))
IMAGE_OUTPUT_DIR = Path(os.environ["IMAGE_OUTPUT_DIR"]).resolve()
# Mirrored by the SAM template's lifecycle rule and IAM resource ARNs — change
# them together (infra/template.yaml).
UPLOAD_PREFIX = "uploads/"
# Upstream sends references inline (base64). Gemini caps a request at 20 MB of
# prompt + inline bytes, and base64 inflates by 4/3, so ~13 MiB of raw image
# is what actually fits. The same cap bounds /tmp and memory per call.
MAX_REFERENCE_MB = int(os.environ.get("MAX_REFERENCE_MB", "13"))
MAX_REFERENCE_BYTES = MAX_REFERENCE_MB * 1024 * 1024
# Outside IMAGE_OUTPUT_DIR on purpose: S3AugmentMiddleware only uploads files
# under IMAGE_OUTPUT_DIR, so staged references are never re-published.
INPUT_STAGING_DIR = Path("/tmp/nanobanana-inputs")

# Upstream picks the MIME type it sends to Gemini from the file suffix, and
# the Lambda python:3.12 base image has no /etc/mime.types, so `.webp` is
# unknown and WEBP references would be labelled image/png.
mimetypes.add_type("image/webp", ".webp")

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
# What request_image_upload issues URLs for: the formats Gemini accepts.
_UPLOAD_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif")

_SAFE_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_SAFE_BASENAME_RE = re.compile(_SAFE_NAME_PATTERN)
_SAFE_SUFFIX_RE = re.compile(r"\.[A-Za-z0-9]{1,10}")
# Only keys this server minted are accepted as references: uploads from
# request_image_upload and images it generated itself.
_INPUT_KEY_RE = re.compile(
    rf"(?:{re.escape(UPLOAD_PREFIX)}[0-9a-f]{{32}}/"
    rf"|{re.escape(S3_PREFIX)}[0-9a-f]{{32}}-){_SAFE_NAME_PATTERN}"
)

_REFERENCE_ARGS = ("input_image_path_1", "input_image_path_2", "input_image_path_3")
# Upstream tools with server-side path arguments that cannot mean anything
# useful to a remote caller.
_HIDDEN_TOOLS = {"upload_file"}

# Older than any call can run (TimeoutSeconds <= 900): leftovers of a call the
# Lambda runtime killed (timeout / OOM) before `finally` could clean up.
_STALE_STAGING_SECONDS = 1800

_HEIC_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs"}
_HEIF_BRANDS = {b"mif1", b"msf1", b"heif"}
_SNIFF_BYTES = 512

_s3_client = None


def _make_s3_client():
    """SigV4 on the regional virtual-hosted endpoint
    (`<bucket>.s3.<region>.amazonaws.com`). botocore's defaults presign
    against the global endpoint, which S3 answers with a 307 for a freshly
    created bucket outside us-east-1; a SigV4 signature covers the host, so
    that redirect cannot be followed. Custom endpoints (moto in the tests)
    have no per-bucket DNS and are path-addressed."""
    style = "path" if os.environ.get("AWS_ENDPOINT_URL_S3") else "virtual"
    return boto3.client(
        "s3",
        config=Config(signature_version="s3v4", s3={"addressing_style": style}),
    )


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = _make_s3_client()
    return _s3_client


def _safe_basename(original: str, suffix: str) -> str:
    """Return a basename guaranteed safe to interpolate into a shell command.

    If `original` already matches a strict allow-list (alnum + `._-`, leading
    alnum, length <= 128) we keep it; otherwise we replace it with a uuid so
    the LLM never substitutes attacker-controlled bytes into `-o <name>`.
    """
    if _SAFE_BASENAME_RE.fullmatch(original):
        return original
    safe_suffix = suffix if _SAFE_SUFFIX_RE.fullmatch(suffix) else ".bin"
    return f"image-{uuid.uuid4().hex[:12]}{safe_suffix}"


def _upload_and_presign(local_path: str) -> tuple[str, str, str] | None:
    """Upload a generated image and return `(presigned_url, safe_filename, key)`.

    Path is anchored under `IMAGE_OUTPUT_DIR` and symlinks are rejected so a
    compromised upstream tool cannot trick us into uploading e.g. /etc/passwd.
    """
    if not S3_BUCKET:
        return None
    try:
        p = Path(local_path)
        if p.is_symlink():
            log.warning("s3 upload rejected (symlink): %s", local_path)
            return None
        resolved = p.resolve(strict=True)
    except (OSError, RuntimeError):
        log.warning("s3 upload skipped: cannot resolve %s", local_path)
        return None
    if not resolved.is_file():
        log.warning("s3 upload skipped: %s not a regular file", local_path)
        return None
    try:
        resolved.relative_to(IMAGE_OUTPUT_DIR)
    except ValueError:
        log.warning(
            "s3 upload rejected (outside IMAGE_OUTPUT_DIR=%s): %s",
            IMAGE_OUTPUT_DIR,
            resolved,
        )
        return None

    safe_name = _safe_basename(resolved.name, resolved.suffix.lower())
    key = f"{S3_PREFIX}{uuid.uuid4().hex}-{safe_name}"
    content_type = _MIME_BY_SUFFIX.get(
        resolved.suffix.lower(), "application/octet-stream"
    )
    try:
        _s3().upload_file(
            str(resolved),
            S3_BUCKET,
            key,
            ExtraArgs={
                "ContentType": content_type,
                "ContentDisposition": f'attachment; filename="{safe_name}"',
            },
        )
        url = _s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=PRESIGN_TTL,
        )
        return url, safe_name, key
    except Exception:
        log.exception("s3 upload failed for %s", resolved)
        return None


def _sniff_image_suffix(head: bytes) -> str | None:
    """Map magic bytes to one of the formats Gemini accepts as image input
    (PNG, JPEG, WEBP, HEIC, HEIF). The suffix decides the MIME type upstream
    sends, so it must come from the content, not from the client's filename.
    This labels the bytes; it does not validate the whole image."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":
        box_size = int.from_bytes(head[:4], "big")
        brands_at = 8
        if box_size == 1:  # 64-bit size follows the box type
            box_size = int.from_bytes(head[8:16], "big")
            brands_at = 16
        elif box_size == 0:  # box runs to the end of the file
            box_size = len(head)
        if box_size > len(head) or box_size < brands_at + 8:
            return None  # cannot see every compatible brand; refuse to guess
        major = head[brands_at : brands_at + 4]
        compatible = {
            head[i : i + 4] for i in range(brands_at + 8, box_size - 3, 4)
        }
        # AVIF shares the mif1 container brand with HEIF but Gemini rejects it.
        if {b"avif", b"avis"} & ({major} | compatible):
            return None
        if major in _HEIC_BRANDS:
            return ".heic"
        if major in _HEIF_BRANDS:
            return ".heif"
    return None


_CURL_UPLOAD = "curl -g -fsS -X PUT --upload-file '<LOCAL_PATH>' '<upload_url>'"
_CURL_UPLOAD_NOTE = (
    "Keep both values in single quotes; if the local path contains a single "
    "quote or a newline, ask the user instead of running the command."
)


def _not_a_key_error(arg: str, value: str) -> ToolError:
    return ToolError(
        f"{arg}={value!r} is not an s3_key minted by this server. This MCP "
        "server runs remotely on AWS Lambda and cannot read files on your "
        "machine. For an image file the user chose as a reference, call "
        "request_image_upload(filename=...), upload it with "
        f"`{_CURL_UPLOAD}` ({_CURL_UPLOAD_NOTE}), then pass the returned "
        f"s3_key as {arg}. To reuse a generated image, pass the `s3_key` from "
        "its images[] entry."
    )


def _stage_s3_image(key: str, budget: int, staged: list[Path]) -> tuple[Path, int]:
    """Stream one reference image from S3 into INPUT_STAGING_DIR.

    Reads at most `budget` + 1 bytes. A single ranged GET is an atomic snapshot
    of the object, so a concurrent re-upload through the still-valid presigned
    PUT URL cannot swap bytes between the size check and the read. The file is
    registered in `staged` before the first byte is written, so the caller's
    cleanup covers partial writes too.
    """
    try:
        INPUT_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.exception("cannot create %s", INPUT_STAGING_DIR)
        raise ToolError(f"Could not stage {key!r} on the server: {_exc_text(exc)}") from exc
    try:
        resp = _s3().get_object(
            Bucket=S3_BUCKET, Key=key, Range=f"bytes=0-{budget}"
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        log.warning("reference image fetch failed: key=%s code=%s", key, code)
        if code == "InvalidRange":
            raise ToolError(f"Uploaded object {key!r} is empty (0 bytes).") from exc
        if code in ("NoSuchKey", "404", "AccessDenied", "403"):
            # The role has no s3:ListBucket, so a missing key reads as 403.
            raise ToolError(
                f"No readable image at s3_key={key!r} (S3: {code}). Did the "
                "`curl --upload-file` step succeed? Uploads expire after "
                f"{IMAGE_RETENTION_DAYS} days."
            ) from exc
        raise ToolError(f"Could not read {key!r} from S3 ({code}).") from exc
    except BotoCoreError as exc:
        log.exception("reference image fetch failed: key=%s", key)
        raise ToolError(
            f"Could not read {key!r} from S3 ({type(exc).__name__})."
        ) from exc

    part = INPUT_STAGING_DIR / f"{uuid.uuid4().hex}.part"
    staged.append(part)
    body = resp["Body"]
    size = 0
    head = b""
    try:
        with open(part, "wb") as out:
            while size <= budget:
                piece = body.read(1024 * 1024)
                if not piece:
                    break
                if len(head) < _SNIFF_BYTES:
                    head += piece[: _SNIFF_BYTES - len(head)]
                out.write(piece)
                size += len(piece)
    except (OSError, BotoCoreError) as exc:
        log.exception("staging reference failed: key=%s", key)
        raise ToolError(f"Could not stage {key!r} on the server: {_exc_text(exc)}") from exc
    finally:
        body.close()

    if size > budget:
        raise ToolError(
            f"Reference images exceed {MAX_REFERENCE_MB} MB combined (at "
            f"{key!r}). Gemini caps a request at 20 MB after base64 encoding; "
            "downscale or re-encode as JPEG and upload again."
        )
    if size == 0:
        raise ToolError(f"Uploaded object {key!r} is empty (0 bytes).")
    suffix = _sniff_image_suffix(head)
    if suffix is None:
        raise ToolError(
            f"Object {key!r} is not a PNG, JPEG, WEBP, HEIC or HEIF image."
        )
    local = part.with_suffix(suffix)
    staged.append(local)
    try:
        part.rename(local)
    except OSError as exc:
        log.exception("staging reference failed: key=%s", key)
        raise ToolError(f"Could not stage {key!r} on the server: {_exc_text(exc)}") from exc
    log.info(
        "staged reference image: key=%s bytes=%d format=%s local=%s budget_left=%d",
        key, size, suffix, local, budget - size,
    )
    return local, size


def _exc_text(exc: BaseException) -> str:
    # strerror keeps "No space left on device" but not the server-side paths
    # that str(OSError) appends.
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return type(exc).__name__


def _sweep_stale_staging() -> None:
    """Remove staged files a killed call left behind (/tmp can outlive a
    Lambda timeout or OOM kill; `finally` does not run on those)."""
    cutoff = time.time() - _STALE_STAGING_SECONDS
    try:
        entries = list(INPUT_STAGING_DIR.iterdir())
    except FileNotFoundError:
        return
    for path in entries:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                log.info("swept stale staged reference %s", path)
        except OSError:
            log.warning("could not sweep %s", path)


def _discard(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("failed to remove staged reference %s", path)


def _sub_paths(text: str, mapping: dict[str, str]) -> str:
    for local, key in mapping.items():
        text = text.replace(local, key)
    return text


def _with_args(context, args: dict):
    return context.copy(
        message=context.message.model_copy(update={"arguments": args})
    )


def _replace_paths(result: Any, mapping: dict[str, str]) -> Any:
    """Show the caller its own s3_keys instead of the /tmp paths upstream echoes
    (summary text, `input_image_paths`, `source_path`); a /tmp path would only
    be rejected if the model tried to reuse it."""
    if not mapping or not isinstance(result, ToolResult):
        return result

    def sub(text: str) -> str:
        return _sub_paths(text, mapping)

    content = [
        block.model_copy(update={"text": sub(block.text)})
        if isinstance(block, TextContent)
        else block
        for block in result.content
    ]
    structured = result.structured_content
    if structured is not None:
        structured = json.loads(sub(json.dumps(structured)))
    return result.model_copy(
        update={"content": content, "structured_content": structured}
    )


class S3InputImageMiddleware(Middleware):
    """Close upstream's server-side path arguments on Lambda.

    - generate_image.input_image_path_1/2/3: only server-minted s3_keys are
      accepted; each is staged into /tmp for this call and swapped in.
    - generate_image.output_path: refused (it can only name server paths, and
      it would choose the client-side download filename).
    - upload_file: refused and hidden from tools/list.

    Any other path value is refused: on Lambda it can only name the server's
    own files, which is never what the caller means and would let a caller
    point upstream at e.g. /proc/self/environ.
    """

    async def on_call_tool(self, context, call_next):
        name = context.message.name
        args = dict(context.message.arguments or {})
        if name in _HIDDEN_TOOLS:
            log.warning("rejected call to hidden tool %s", name)
            raise ToolError(
                f"{name} is not available on this remote server: it reads a "
                "path on the server. Pass the s3_key from request_image_upload "
                "directly as input_image_path_1/2/3 of generate_image instead; "
                "keys can be reused across calls."
            )
        if name != "generate_image":
            return await call_next(context)
        if args.get("output_path") == "":
            del args["output_path"]  # empty means "not given", as for the input slots
        if args.get("output_path") is not None:
            log.warning("rejected generate_image.output_path=%r", args.get("output_path"))
            raise ToolError(
                "output_path is not supported on this remote server: images are "
                "saved on the server and delivered via each images[] entry's "
                "download_url and safe_filename. Omit output_path and rename "
                "the file after downloading if needed."
            )

        refs = [(arg, args.get(arg)) for arg in _REFERENCE_ARGS if args.get(arg)]
        if not refs:
            # upstream treats empty as "not given"
            return await call_next(_with_args(context, args))
        for arg, value in refs:  # validate all before downloading any
            if not isinstance(value, str) or not _INPUT_KEY_RE.fullmatch(value):
                log.warning("rejected generate_image.%s: not a server-minted s3_key: %r", arg, value)
                raise _not_a_key_error(arg, str(value))

        _sweep_stale_staging()
        staged: list[Path] = []
        try:
            new_args = dict(args)
            mapping: dict[str, str] = {}
            budget = MAX_REFERENCE_BYTES
            for arg, key in refs:
                local, size = await anyio.to_thread.run_sync(
                    _stage_s3_image, key, budget, staged
                )
                budget -= size
                new_args[arg] = str(local)
                mapping[str(local)] = key
            try:
                result = await call_next(_with_args(context, new_args))
            except ToolError as exc:
                # upstream errors quote the path, e.g. "Failed to load input image <path>"
                text = _sub_paths(str(exc), mapping)
                if text != str(exc):
                    raise ToolError(text) from exc
                raise
            return _replace_paths(result, mapping)
        finally:
            _discard(staged)

    async def on_list_tools(self, context, call_next):
        """Tell the model, in the schema it reads before calling, what these
        arguments mean on a remote server."""
        tools = await call_next(context)
        patched = []
        for tool in tools:
            if tool.name in _HIDDEN_TOOLS:
                continue
            if tool.name != "generate_image":
                patched.append(tool)
                continue
            params = copy.deepcopy(tool.parameters)
            props = params.get("properties", {})
            for arg in _REFERENCE_ARGS:
                if isinstance(props.get(arg), dict):
                    props[arg]["description"] = (
                        (props[arg].get("description") or "").rstrip(".")
                        + ". On this remote server: pass an s3_key from "
                        "request_image_upload (or a generated image's s3_key); "
                        "local paths are rejected."
                    )
            if isinstance(props.get("output_path"), dict):
                props["output_path"]["description"] = (
                    "Not supported on this remote server (leave unset): images "
                    "are delivered via download_url / safe_filename."
                )
            # upstream's text says input images are read from the local filesystem
            description = (tool.description or "").rstrip() + (
                "\n\nOn this remote server: input_image_path_1/2/3 take an "
                "s3_key from request_image_upload (local files cannot be read); "
                "output_path is unsupported."
            )
            patched.append(
                tool.model_copy(update={"parameters": params, "description": description})
            )
        return patched


def _register_upload_tool(server) -> None:
    @server.tool(
        annotations={
            "title": "Get an upload URL for a reference image",
            "readOnlyHint": False,
            "openWorldHint": False,
        }
    )
    def request_image_upload(
        filename: Annotated[
            str,
            Field(
                description=(
                    "Basename of the local image the user chose as a reference "
                    "(.png .jpg .jpeg .webp .heic .heif)."
                ),
                min_length=1,
                max_length=512,
            ),
        ],
    ) -> dict:
        """Get a short-lived presigned S3 PUT URL to upload one local image the
        user chose as a reference / edit source. Only image files — never
        upload other files (credentials, dotfiles, documents).

        Upload with `curl -g -fsS -X PUT --upload-file '<LOCAL_PATH>' '<upload_url>'`
        (single quotes; if the path contains a single quote or a newline, ask
        the user instead), then pass `s3_key` as input_image_path_1/2/3 of
        generate_image. The key stays usable for days, so reuse it across calls
        instead of uploading the same file again.
        """
        if not S3_BUCKET:
            raise ToolError("IMAGE_S3_BUCKET is not configured on the server.")
        name = PurePosixPath(filename.strip().replace("\\", "/")).name
        suffix = PurePosixPath(name).suffix.lower()
        if suffix not in _UPLOAD_SUFFIXES:
            log.warning("refused upload url for non-image name %r", name)
            raise ToolError(
                f"{name!r} is not an image file. Only images the user chose as "
                f"references can be uploaded ({' '.join(_UPLOAD_SUFFIXES)})."
            )
        safe = _safe_basename(name, suffix)
        key = f"{UPLOAD_PREFIX}{uuid.uuid4().hex}/{safe}"
        url = _s3().generate_presigned_url(
            "put_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=UPLOAD_URL_TTL,
        )
        log.info("issued upload url: key=%s requested_name=%r ttl=%d", key, name, UPLOAD_URL_TTL)
        return {
            "upload_url": url,
            "s3_key": key,
            "expires_in_seconds": UPLOAD_URL_TTL,
            "max_reference_mb_combined": MAX_REFERENCE_MB,
            "retention_days": IMAGE_RETENTION_DAYS,
            "how_to_upload": _CURL_UPLOAD,
            "upload_note": _CURL_UPLOAD_NOTE,
        }


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without a matching `Authorization: Bearer <token>` header.

    Disabled (pass-through) when MCP_AUTH_TOKEN is unset, which only happens
    off Lambda (e.g. running the ASGI app directly); on Lambda — including
    `sam local`, which sets AWS_LAMBDA_FUNCTION_NAME — import fails without it.
    """

    def __init__(self, app, expected_token: str | None) -> None:
        super().__init__(app)
        self._expected = expected_token

    async def dispatch(self, request, call_next):
        if self._expected:
            header = request.headers.get("authorization", "")
            prefix = "Bearer "
            # Compare bytes: str compare_digest raises TypeError (-> 500) on
            # non-ASCII input. Starlette decodes headers as latin-1.
            if not header.startswith(prefix) or not hmac.compare_digest(
                header[len(prefix) :].encode("latin-1"),
                self._expected.encode("utf-8"),
            ):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


class S3AugmentMiddleware(BaseHTTPMiddleware):
    """Upload generated image files to S3 and inject `download_url` into the
    JSON metadata of `tools/call` responses. No-op when IMAGE_S3_BUCKET is
    unset, when the response is not JSON, or when no image paths are found."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if not S3_BUCKET:
            return response
        if "application/json" not in response.headers.get("content-type", ""):
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return self._passthrough(body, response)

        try:
            mutated = self._augment(payload)
        except Exception:
            log.exception("s3 augment failed; returning original body")
            return self._passthrough(body, response)

        if not mutated:
            return self._passthrough(body, response)

        new_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = dict(response.headers)
        headers["content-length"] = str(len(new_body))
        return Response(
            content=new_body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )

    @staticmethod
    def _passthrough(body: bytes, response) -> Response:
        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

    def _augment(self, payload: Any) -> bool:
        items = payload if isinstance(payload, list) else [payload]
        mutated_any = False
        for item in items:
            if not isinstance(item, dict):
                continue
            result = item.get("result")
            if not isinstance(result, dict):
                continue

            structured = result.get("structuredContent")
            structured_images = (
                structured.get("images") if isinstance(structured, dict) else None
            )
            if isinstance(structured_images, list) and structured_images:
                if self._augment_images(structured_images):
                    mutated_any = True
                    self._sync_structured_into_text_blocks(result, structured)

            for block in result.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = block.get("text")
                if not isinstance(text, str):
                    continue
                try:
                    inner = json.loads(text)
                except Exception:
                    continue
                if not isinstance(inner, dict):
                    continue
                images = inner.get("images")
                if not isinstance(images, list) or not images:
                    continue
                if self._augment_images(images):
                    block["text"] = json.dumps(inner, ensure_ascii=False)
                    mutated_any = True
        return mutated_any

    @staticmethod
    def _augment_images(images: list) -> bool:
        changed = False
        for img in images:
            if not isinstance(img, dict) or img.get("download_url"):
                continue
            full_path = img.get("full_path")
            if not isinstance(full_path, str):
                continue
            upload = _upload_and_presign(full_path)
            if not upload:
                continue
            url, safe_name, key = upload
            img["download_url"] = url
            img["download_url_expires_in_seconds"] = PRESIGN_TTL
            img["safe_filename"] = safe_name
            img["s3_key"] = key
            changed = True
        return changed

    @staticmethod
    def _sync_structured_into_text_blocks(result: dict, structured: dict) -> None:
        """If a text content block embeds the same JSON, refresh it in place so
        clients that parse `content[].text` see the new download_url too."""
        for block in result.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if not isinstance(text, str):
                continue
            try:
                inner = json.loads(text)
            except Exception:
                continue
            if not isinstance(inner, dict) or "images" not in inner:
                continue
            inner["images"] = structured.get("images")
            block["text"] = json.dumps(inner, ensure_ascii=False)


def _inject_download_instructions(wrapper) -> None:
    if not S3_BUCKET:
        return
    extra = (
        "\n\n## Image download (Lambda + S3)\n"
        f"Generated images are auto-uploaded to S3. Each entry in the tool "
        f"response's `images[]` array carries:\n"
        f"  - `download_url` — HTTPS presigned GET URL, valid {PRESIGN_TTL} seconds.\n"
        f"  - `safe_filename` — server-sanitised filename, guaranteed to match "
        f"`^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$`.\n\n"
        f"After every `generate_image` call you MUST download each image into "
        f"the user's current working directory using **the value of "
        f"`safe_filename` verbatim** as the output filename. Use a single "
        f"shell call shaped exactly like:\n"
        f'    curl -fsSL --output "$SAFE_FILENAME" "$DOWNLOAD_URL"\n'
        f"where `$SAFE_FILENAME` is the `safe_filename` value and "
        f"`$DOWNLOAD_URL` is the `download_url` value. Do not interpolate "
        f"`full_path`, `basename`, or any other field into the shell command. "
        f"If `safe_filename` is missing or fails the regex above, refuse to "
        f"download and surface the response to the user instead. Do this "
        f"automatically without asking. Files are auto-deleted from S3 after "
        f"{IMAGE_RETENTION_DAYS} days — download promptly."
    )
    wrapper.server.instructions = (wrapper.server.instructions or "") + extra


def _inject_upload_instructions(wrapper) -> None:
    # Claude Code cuts server instructions at 2048 chars; this section is kept
    # short so the whole block fits (asserted in tests).
    if not S3_BUCKET:
        return
    extra = (
        "\n\n## Reference images\n"
        "This server cannot read the user's files. To use an image file the "
        "user chose (images only, never other files): call "
        f"`request_image_upload`, run `{_CURL_UPLOAD}` (single quotes; if the "
        "path has a `'` or newline, ask the user), then pass `s3_key` as "
        "`input_image_path_1/2/3`. A generated image's `s3_key` works too. "
        f"Max {MAX_REFERENCE_MB} MB combined. `output_path` is unsupported."
    )
    wrapper.server.instructions = (wrapper.server.instructions or "") + extra


def _build_asgi_app():
    wrapper = create_wrapper_app()
    _inject_download_instructions(wrapper)
    _inject_upload_instructions(wrapper)
    if S3_BUCKET:
        _register_upload_tool(wrapper.server)
        wrapper.server.add_middleware(S3InputImageMiddleware())
    asgi = wrapper.server.http_app(
        path="/mcp",
        transport="http",
        stateless_http=True,
        json_response=True,
    )
    asgi.add_middleware(S3AugmentMiddleware)
    asgi.add_middleware(
        BearerAuthMiddleware, expected_token=os.environ.get("MCP_AUTH_TOKEN")
    )
    return asgi


_asgi_app = _build_asgi_app()
log.info(
    "config: bucket=%s images_prefix=%s uploads_prefix=%s max_reference_mb=%d "
    "upload_url_ttl=%ds download_url_ttl=%ds retention_days=%d log_level=%s "
    "auth=%s",
    S3_BUCKET, S3_PREFIX, UPLOAD_PREFIX, MAX_REFERENCE_MB, UPLOAD_URL_TTL,
    PRESIGN_TTL, IMAGE_RETENTION_DAYS, logging.getLevelName(logging.getLogger().level),
    "bearer" if os.environ.get("MCP_AUTH_TOKEN") else "NONE",
)

handler = Mangum(_asgi_app, lifespan="on", api_gateway_base_path="/")

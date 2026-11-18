import hmac
import html
import io
import ipaddress
import mimetypes
import os
import re
import secrets
import socket
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import quote, urlparse

import boto3
import markdown as md_lib
import weasyprint
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
)

load_dotenv()

ACCESS_KEY = os.getenv("DO_SPACES_KEY")
SECRET_KEY = os.getenv("DO_SPACES_SECRET")
REGION = os.getenv("DO_SPACES_REGION")
ENDPOINT_URL = os.getenv("DO_SPACES_ENDPOINT")
BUCKET_NAME = os.getenv("DO_SPACES_BUCKET", "igorosio")

APP_USERNAME = os.getenv("APP_USERNAME", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

PAGE_SIZE = 200
TEXT_PREVIEW_MAX_BYTES = 512 * 1024  # 512 KB
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", 100))
MAX_FOLDER_ZIP_BYTES = int(os.getenv("MAX_FOLDER_ZIP_BYTES", 500 * 1024 * 1024))
BUCKET_QUOTA_GIB = float(os.getenv("BUCKET_QUOTA_GIB", 250))
BUCKET_STATS_CACHE_TTL = 300  # 5 minuti
_bucket_stats_cache = {"data": None, "ts": 0.0}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"}
UNSAFE_INLINE_EXTENSIONS = {
    ".html", ".htm", ".svg", ".xml", ".xhtml", ".js", ".mjs", ".swf",
}
PDF_EXTENSIONS = {".pdf"}
ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a"}
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".html", ".htm", ".css", ".json", ".yaml", ".yml",
    ".xml", ".sh", ".bash", ".sql", ".toml", ".tf", ".dockerfile",
}
TEXT_EXTENSIONS = {
    ".txt", ".log", ".json", ".yaml", ".yml", ".xml", ".csv", ".tsv",
    ".md", ".py", ".js", ".ts", ".html", ".htm", ".css", ".sh", ".bash",
    ".conf", ".ini", ".cfg", ".env", ".sql", ".toml", ".tf", ".dockerfile",
}

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


def get_csrf_token() -> str:
    """Return or generate a per-session CSRF token."""
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_globals():
    return {
        "csrf_token": get_csrf_token(),
        "current_year": datetime.now(timezone.utc).year,
    }


@app.before_request
def verify_csrf():
    if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
        expected = session.get("_csrf_token")
        provided = (
            request.headers.get("X-CSRF-Token")
            or request.form.get("csrf_token")
            or ""
        )
        if not expected or not provided or not hmac.compare_digest(provided, expected):
            return jsonify({"error": "Token CSRF mancante o non valido."}), 403


@app.after_request
def set_security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://uicdn.toast.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://uicdn.toast.com; "
        "font-src 'self' https://cdnjs.cloudflare.com data:; "
        "img-src 'self' data: blob: https:; "
        "frame-src 'self' https:; "
        "connect-src 'self';",
    )
    return response


@app.errorhandler(413)
def handle_request_entity_too_large(_err):
    msg = f"Il file supera la dimensione massima consentita ({MAX_UPLOAD_MB} MB)."
    if request.path.startswith("/api/"):
        return jsonify({"error": msg}), 413
    return Response(msg, 413)


def safe_error_message(exc: Exception, fallback: str = "Operazione non riuscita sullo storage.") -> str:
    """Log technical exception details server-side and return a safe message."""
    app.logger.warning("Storage/App error: %s", exc)
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"NoSuchKey", "404"}:
            return "Elemento non trovato nel bucket."
        if code in {"AccessDenied", "403"}:
            return "Accesso negato sul bucket."
    return fallback


def check_auth(username: str, password: str) -> bool:
    """Verify HTTP Basic Auth credentials when configured."""
    return hmac.compare_digest(username, APP_USERNAME) and hmac.compare_digest(
        password, APP_PASSWORD
    )


def requires_auth(f):
    """Optionally enforce HTTP Basic Auth if APP_USERNAME and APP_PASSWORD are set."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if APP_USERNAME and APP_PASSWORD:
            auth = request.authorization
            if not auth or not check_auth(auth.username or "", auth.password or ""):
                return Response(
                    "Autenticazione richiesta.",
                    401,
                    {"WWW-Authenticate": 'Basic realm="Spaces Browser"'},
                )
        return f(*args, **kwargs)

    return decorated


def get_s3_client():
    """Create and return a configured boto3 S3 client for DigitalOcean Spaces."""
    if not all([ACCESS_KEY, SECRET_KEY, ENDPOINT_URL, BUCKET_NAME]):
        raise ValueError("Mancano alcuni parametri obbligatori nel file .env")

    session = boto3.session.Session()
    return session.client(
        "s3",
        region_name=REGION,
        endpoint_url=ENDPOINT_URL,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
    )


def normalize_prefix(raw_prefix: str) -> str:
    """Normalize an S3 prefix, stripping '.' / '..' segments and control chars."""
    raw = (raw_prefix or "").replace("\\", "/").strip()
    raw = re.sub(r"[\x00-\x1f\x7f]", "", raw)
    parts = [p for p in raw.split("/") if p and p not in {".", ".."}]
    if not parts:
        return ""
    return "/".join(parts) + "/"


def sanitize_s3_key(raw_key: str) -> str:
    """Validate and normalize an S3 object key, rejecting '..' traversal."""
    raw = (raw_key or "").replace("\\", "/").strip().lstrip("/")
    if not raw or raw.endswith("/") or re.search(r"[\x00-\x1f\x7f]", raw):
        return ""
    parts = [p for p in raw.split("/") if p]
    if not parts or any(p in {".", ".."} for p in parts):
        return ""
    return "/".join(parts)


def format_size(num_bytes: int) -> str:
    """Format byte size into a human-readable string."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"


def detect_preview_type(filename: str) -> str | None:
    """Determine if a file supports inline preview ('image', 'pdf', 'text', or None)."""
    lower = filename.lower()
    _, ext = os.path.splitext(lower)
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in PDF_EXTENSIONS:
        return "pdf"
    if ext in TEXT_EXTENSIONS or lower in {"readme", "license", "dockerfile", "makefile"}:
        return "text"
    return None


def detect_file_icon(filename: str) -> str:
    """Return neutral gray Font Awesome icon classes suited for the file extension."""
    lower = filename.lower()
    _, ext = os.path.splitext(lower)
    if ext in IMAGE_EXTENSIONS:
        return "fa-regular fa-file-image text-secondary"
    if ext in PDF_EXTENSIONS:
        return "fa-regular fa-file-pdf text-secondary"
    if ext in ARCHIVE_EXTENSIONS:
        return "fa-regular fa-file-zipper text-secondary"
    if ext in VIDEO_EXTENSIONS:
        return "fa-regular fa-file-video text-secondary"
    if ext in AUDIO_EXTENSIONS:
        return "fa-regular fa-file-audio text-secondary"
    if ext in CODE_EXTENSIONS or lower in {"dockerfile", "makefile"}:
        return "fa-regular fa-file-code text-secondary"
    if ext in TEXT_EXTENSIONS or lower in {"readme", "license"}:
        return "fa-regular fa-file-lines text-secondary"
    return "fa-regular fa-file text-secondary"


def build_breadcrumbs(prefix: str) -> list[dict]:
    """Build breadcrumb navigation items from an S3 prefix."""
    crumbs = [{"name": BUCKET_NAME, "prefix": ""}]
    if not prefix:
        return crumbs

    acc = ""
    for part in prefix.strip("/").split("/"):
        acc += f"{part}/"
        crumbs.append({"name": part, "prefix": acc})
    return crumbs


def list_prefix_contents(
    client,
    prefix: str,
    continuation_token: str | None = None,
    query: str = "",
) -> dict:
    """List folders and files under a given S3 prefix.

    When `query` is empty, fetches a single page of up to PAGE_SIZE items using
    ContinuationToken. When `query` is provided, paginates across the folder to
    return all matching entries at that level.
    """
    folders = []
    files = []
    next_token = None

    if query:
        q_lower = query.lower()
        paginator = client.get_paginator("list_objects_v2")
        pages = paginator.paginate(
            Bucket=BUCKET_NAME,
            Prefix=prefix,
            Delimiter="/",
        )
        for page in pages:
            _collect_page(page, prefix, folders, files, q_lower)
    else:
        kwargs = {
            "Bucket": BUCKET_NAME,
            "Prefix": prefix,
            "Delimiter": "/",
            "MaxKeys": PAGE_SIZE,
        }
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        page = client.list_objects_v2(**kwargs)
        _collect_page(page, prefix, folders, files, "")
        if page.get("IsTruncated"):
            next_token = page.get("NextContinuationToken")

    return {
        "folders": folders,
        "files": files,
        "next_token": next_token,
    }


def _collect_page(
    page: dict,
    prefix: str,
    folders: list,
    files: list,
    q_lower: str,
) -> None:
    """Extract folders and files from a single list_objects_v2 response page."""
    for cp in page.get("CommonPrefixes", []):
        folder_prefix = cp["Prefix"]
        name = folder_prefix[len(prefix) :].rstrip("/")
        if not name:
            continue
        if q_lower and q_lower not in name.lower():
            continue
        folders.append({"name": name, "prefix": folder_prefix})

    for obj in page.get("Contents", []):
        key = obj["Key"]
        if key == prefix or key.endswith("/"):
            continue
        name = key[len(prefix) :]
        if q_lower and q_lower not in name.lower():
            continue
        last_mod = obj["LastModified"].astimezone(timezone.utc)
        files.append(
            {
                "key": key,
                "name": name,
                "size": obj["Size"],
                "human_size": format_size(obj["Size"]),
                "last_modified": last_mod.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "last_modified_ts": last_mod.timestamp(),
                "preview_type": detect_preview_type(name),
                "icon": detect_file_icon(name),
                "is_markdown": name.lower().endswith(".md"),
            }
        )


def sort_entries(folders: list, files: list, sort_by: str, order: str) -> None:
    """Sort folders and files in place according to sort_by and order."""
    reverse = order == "desc"
    folders.sort(key=lambda item: item["name"].lower(), reverse=reverse)

    if sort_by == "size":
        files.sort(key=lambda item: item["size"], reverse=reverse)
    elif sort_by == "modified":
        files.sort(key=lambda item: item["last_modified_ts"], reverse=reverse)
    else:
        files.sort(key=lambda item: item["name"].lower(), reverse=reverse)


def generate_presigned_url(client, key: str, inline: bool = False) -> str:
    """Generate a short-lived presigned URL for downloading or viewing an object."""
    filename = os.path.basename(key) or "file"
    _, ext = os.path.splitext(filename.lower())
    if ext in UNSAFE_INLINE_EXTENSIONS:
        inline = False

    disposition = "inline" if inline else f'attachment; filename="{quote(filename)}"'
    params = {
        "Bucket": BUCKET_NAME,
        "Key": key,
        "ResponseContentDisposition": disposition,
    }
    if inline:
        guessed_type, _ = mimetypes.guess_type(filename)
        if guessed_type:
            params["ResponseContentType"] = guessed_type

    return client.generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=300,
    )


@app.route("/")
@requires_auth
def index():
    prefix = normalize_prefix(request.args.get("prefix", ""))
    token = request.args.get("token") or None
    query = request.args.get("q", "").strip()
    sort_by = request.args.get("sort", "name")
    if sort_by not in {"name", "size", "modified"}:
        sort_by = "name"
    order = request.args.get("order", "asc")
    if order not in {"asc", "desc"}:
        order = "asc"

    error = None
    folders = []
    files = []
    next_token = None

    try:
        client = get_s3_client()
        result = list_prefix_contents(
            client,
            prefix=prefix,
            continuation_token=token,
            query=query,
        )
        folders = result["folders"]
        files = result["files"]
        next_token = result["next_token"]
        sort_entries(folders, files, sort_by, order)
    except (ClientError, ValueError) as exc:
        error = safe_error_message(exc)

    parent_prefix = None
    if prefix:
        parts = prefix.strip("/").split("/")
        parent_prefix = "/".join(parts[:-1]) + "/" if len(parts) > 1 else ""

    total_size = sum(f["size"] for f in files)

    return render_template(
        "index.html",
        bucket_name=BUCKET_NAME,
        prefix=prefix,
        parent_prefix=parent_prefix,
        breadcrumbs=build_breadcrumbs(prefix),
        folders=folders,
        files=files,
        next_token=next_token,
        query=query,
        sort_by=sort_by,
        order=order,
        total_size_human=format_size(total_size),
        error=error,
    )


@app.route("/download")
@requires_auth
def download():
    key = sanitize_s3_key(request.args.get("key", ""))
    if not key:
        abort(400, "Chiave file non valida.")

    try:
        client = get_s3_client()
        url = generate_presigned_url(client, key, inline=False)
        return redirect(url, code=302)
    except (ClientError, ValueError) as exc:
        abort(500, safe_error_message(exc))


@app.route("/open")
@requires_auth
def open_inline():
    key = sanitize_s3_key(request.args.get("key", ""))
    if not key:
        abort(400, "Chiave file non valida.")

    try:
        client = get_s3_client()
        url = generate_presigned_url(client, key, inline=True)
        return redirect(url, code=302)
    except (ClientError, ValueError) as exc:
        abort(500, safe_error_message(exc))


@app.route("/api/preview")
@requires_auth
def api_preview():
    key = sanitize_s3_key(request.args.get("key", ""))
    if not key:
        return jsonify({"error": "Chiave file non valida."}), 400

    filename = os.path.basename(key)
    preview_type = detect_preview_type(filename)
    if not preview_type:
        return jsonify({"error": "Anteprima non supportata per questo tipo di file."}), 400

    try:
        client = get_s3_client()
        if preview_type in {"image", "pdf"}:
            url = generate_presigned_url(client, key, inline=True)
            return jsonify({"type": preview_type, "name": filename, "url": url})

        head = client.head_object(Bucket=BUCKET_NAME, Key=key)
        total_size = head.get("ContentLength", 0)
        truncated = total_size > TEXT_PREVIEW_MAX_BYTES
        end_byte = min(total_size, TEXT_PREVIEW_MAX_BYTES) - 1

        if total_size == 0:
            content = ""
        else:
            obj = client.get_object(
                Bucket=BUCKET_NAME,
                Key=key,
                Range=f"bytes=0-{end_byte}",
            )
            raw = obj["Body"].read()
            content = raw.decode("utf-8", errors="replace")

        return jsonify(
            {
                "type": "text",
                "name": filename,
                "content": content,
                "truncated": truncated,
                "size": format_size(total_size),
            }
        )
    except ClientError as exc:
        code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 500)
        return jsonify({"error": safe_error_message(exc)}), code
    except ValueError as exc:
        return jsonify({"error": safe_error_message(exc)}), 500


def sanitize_entry_name(raw_name: str) -> str:
    """Extract a safe single filename or folder name without path traversal or control chars."""
    raw = (raw_name or "").replace("\\", "/")
    raw = re.sub(r"[\x00-\x1f\x7f]", "", raw)
    cleaned = os.path.basename(raw).strip()
    if not cleaned or cleaned in {".", ".."}:
        return ""
    return cleaned


def object_exists(client, key: str) -> bool:
    """Return True if an object exists in the bucket."""
    try:
        client.head_object(Bucket=BUCKET_NAME, Key=key)
        return True
    except ClientError:
        return False


_DANGEROUS_HTML_TAG_RE = re.compile(
    r"</?\s*(?:script|iframe|object|embed|link|meta|base|form|input|button|style|applet|svg|math)\b[^>]*>",
    re.IGNORECASE,
)


def sanitize_rendered_html(raw_html: str) -> str:
    """Strip dangerous HTML elements from rendered Markdown before PDF conversion."""
    cleaned = _DANGEROUS_HTML_TAG_RE.sub("", raw_html or "")
    cleaned = re.sub(r"\son[a-z]+\s*=", " data-blocked=", cleaned, flags=re.IGNORECASE)
    return cleaned


def safe_weasyprint_url_fetcher(url: str):
    """Allow only data: URIs and public HTTPS URLs (blocking file:// and private/local IPs)."""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme == "data":
        return weasyprint.default_url_fetcher(url)
    if scheme != "https" or not parsed.hostname:
        raise ValueError(f"Schema o URL non consentito nel PDF: {scheme}")

    try:
        addr_infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise ValueError("Impossibile risolvere l'host della risorsa esterna.") from exc

    for info in addr_infos:
        ip_str = info[4][0]
        ip_obj = ipaddress.ip_address(ip_str)
        if not ip_obj.is_global:
            raise ValueError("Accesso a indirizzi IP privati o locali bloccato.")

    return weasyprint.default_url_fetcher(url)


def markdown_to_pdf_bytes(md_text: str, title: str = "Documento") -> bytes:
    """Convert Markdown text into a styled PDF byte payload using WeasyPrint."""
    raw_body_html = md_lib.markdown(
        md_text or "",
        extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
    )
    body_html = sanitize_rendered_html(raw_body_html)
    safe_title = html.escape(title)
    full_html = f"""<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <title>{safe_title}</title>
  <style>
    @page {{
      size: A4;
      margin: 20mm 18mm;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      font-size: 10.5pt;
      line-height: 1.55;
      color: #212529;
    }}
    h1, h2, h3, h4 {{
      color: #111827;
      margin-top: 1.2em;
      margin-bottom: 0.45em;
      page-break-after: avoid;
    }}
    h1 {{ font-size: 18pt; border-bottom: 1px solid #dee2e6; padding-bottom: 4pt; }}
    h2 {{ font-size: 14pt; }}
    h3 {{ font-size: 12pt; }}
    p {{ margin: 0 0 0.7em; }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 9pt;
      background: #f1f3f5;
      padding: 1pt 3pt;
      border-radius: 3pt;
    }}
    pre {{
      background: #f8f9fa;
      border: 1px solid #dee2e6;
      border-radius: 4pt;
      padding: 8pt 10pt;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 8.5pt;
    }}
    pre code {{ background: transparent; padding: 0; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin: 0.8em 0;
      font-size: 9.5pt;
    }}
    th, td {{
      border: 1px solid #ced4da;
      padding: 5pt 7pt;
      text-align: left;
    }}
    th {{ background: #f1f3f5; font-weight: 600; }}
    blockquote {{
      margin: 0.8em 0;
      padding: 4pt 10pt;
      border-left: 3pt solid #adb5bd;
      color: #495057;
      background: #f8f9fa;
    }}
    img {{ max-width: 100%; height: auto; }}
  </style>
</head>
<body>
  {body_html}
</body>
</html>"""
    return weasyprint.HTML(
        string=full_html,
        url_fetcher=safe_weasyprint_url_fetcher,
    ).write_pdf()


@app.route("/api/upload", methods=["POST"])
@requires_auth
def api_upload():
    prefix = normalize_prefix(request.form.get("prefix", ""))
    overwrite = request.form.get("overwrite", "false").lower() == "true"
    uploaded_files = request.files.getlist("files")

    valid_files = []
    for f in uploaded_files:
        name = sanitize_entry_name(f.filename)
        if name:
            valid_files.append((name, f))

    if not valid_files:
        return jsonify({"error": "Nessun file valido selezionato."}), 400

    try:
        client = get_s3_client()
        if not overwrite:
            conflicts = [
                name for name, _ in valid_files if object_exists(client, f"{prefix}{name}")
            ]
            if conflicts:
                return (
                    jsonify(
                        {
                            "conflicts": conflicts,
                            "error": "Alcuni file esistono già nella cartella corrente.",
                        }
                    ),
                    409,
                )

        uploaded_keys = []
        for name, file_storage in valid_files:
            key = f"{prefix}{name}"
            content_type, _ = mimetypes.guess_type(name)
            extra_args = {"ContentType": content_type} if content_type else {}
            file_storage.stream.seek(0)
            client.upload_fileobj(
                file_storage.stream,
                BUCKET_NAME,
                key,
                ExtraArgs=extra_args or None,
            )
            uploaded_keys.append(key)

        return jsonify({"uploaded": uploaded_keys, "count": len(uploaded_keys)})
    except (ClientError, ValueError) as exc:
        return jsonify({"error": safe_error_message(exc)}), 500


@app.route("/api/mkdir", methods=["POST"])
@requires_auth
def api_mkdir():
    data = request.get_json(silent=True) or {}
    prefix = normalize_prefix(data.get("prefix", ""))
    name = sanitize_entry_name(data.get("name", ""))
    if not name:
        return jsonify({"error": "Nome cartella non valido."}), 400

    folder_key = f"{prefix}{name}/"
    try:
        client = get_s3_client()
        client.put_object(Bucket=BUCKET_NAME, Key=folder_key, Body=b"")
        return jsonify({"prefix": folder_key, "name": name})
    except (ClientError, ValueError) as exc:
        return jsonify({"error": safe_error_message(exc)}), 500


@app.route("/download-folder")
@requires_auth
def download_folder():
    prefix = normalize_prefix(request.args.get("prefix", ""))
    folder_label = prefix.strip("/").split("/")[-1] if prefix else BUCKET_NAME
    zip_filename = f"{folder_label}.zip"

    try:
        client = get_s3_client()
        paginator = client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=BUCKET_NAME, Prefix=prefix)

        objects = []
        total_bytes = 0
        for page in pages:
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                total_bytes += obj.get("Size", 0)
                if total_bytes > MAX_FOLDER_ZIP_BYTES:
                    limit_mb = MAX_FOLDER_ZIP_BYTES // (1024 * 1024)
                    abort(
                        413,
                        f"La cartella supera il limite massimo consentito ({limit_mb} MB).",
                    )
                objects.append(key)

        if not objects:
            abort(404, "La cartella è vuota o non contiene file scaricabili.")

        tmp = tempfile.SpooledTemporaryFile(max_size=20 * 1024 * 1024)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for key in objects:
                raw_arc = key[len(prefix) :] if prefix else key
                safe_parts = [p for p in raw_arc.replace("\\", "/").split("/") if p and p not in {".", ".."}]
                arcname = "/".join(safe_parts)
                if not arcname:
                    continue
                obj = client.get_object(Bucket=BUCKET_NAME, Key=key)
                with zf.open(arcname, "w") as dest:
                    for chunk in obj["Body"].iter_chunks(chunk_size=1024 * 256):
                        dest.write(chunk)

        tmp.seek(0)
        return send_file(
            tmp,
            as_attachment=True,
            download_name=zip_filename,
            mimetype="application/zip",
        )
    except (ClientError, ValueError) as exc:
        abort(500, safe_error_message(exc))


@app.route("/editor")
@requires_auth
def editor():
    raw_key = request.args.get("key", "")
    key = sanitize_s3_key(raw_key) if raw_key else ""
    prefix = normalize_prefix(request.args.get("prefix", ""))
    filename = "nuovo-documento.md"
    content = ""
    error = None

    if raw_key and not key:
        abort(400, "Chiave file non valida.")

    if key:
        filename = os.path.basename(key)
        parent = os.path.dirname(key)
        prefix = normalize_prefix(parent)
        try:
            client = get_s3_client()
            obj = client.get_object(Bucket=BUCKET_NAME, Key=key)
            content = obj["Body"].read().decode("utf-8", errors="replace")
        except (ClientError, ValueError) as exc:
            error = safe_error_message(exc)

    return render_template(
        "editor.html",
        bucket_name=BUCKET_NAME,
        prefix=prefix,
        key=key,
        filename=filename,
        content=content,
        error=error,
    )


@app.route("/api/markdown/save", methods=["POST"])
@requires_auth
def api_markdown_save():
    data = request.get_json(silent=True) or {}
    prefix = normalize_prefix(data.get("prefix", ""))
    raw_name = sanitize_entry_name(data.get("filename", ""))
    content = data.get("content", "")

    if not raw_name:
        return jsonify({"error": "Nome file non valido."}), 400
    if not raw_name.lower().endswith(".md"):
        raw_name += ".md"

    key = f"{prefix}{raw_name}"
    try:
        client = get_s3_client()
        client.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        return jsonify({"key": key, "filename": raw_name})
    except (ClientError, ValueError) as exc:
        return jsonify({"error": safe_error_message(exc)}), 500


@app.route("/api/markdown/pdf", methods=["POST"])
@requires_auth
def api_markdown_pdf():
    data = request.get_json(silent=True) or {}
    prefix = normalize_prefix(data.get("prefix", ""))
    raw_name = sanitize_entry_name(data.get("filename", "")) or "documento.md"
    content = data.get("content", "")
    save_to_bucket = bool(data.get("save_to_bucket", False))

    base_name = raw_name[:-3] if raw_name.lower().endswith(".md") else raw_name
    pdf_filename = f"{base_name}.pdf"

    try:
        pdf_bytes = markdown_to_pdf_bytes(content, title=base_name)
        if save_to_bucket:
            client = get_s3_client()
            pdf_key = f"{prefix}{pdf_filename}"
            client.put_object(
                Bucket=BUCKET_NAME,
                Key=pdf_key,
                Body=pdf_bytes,
                ContentType="application/pdf",
            )
            return jsonify({"key": pdf_key, "filename": pdf_filename})

        return send_file(
            io.BytesIO(pdf_bytes),
            as_attachment=True,
            download_name=pdf_filename,
            mimetype="application/pdf",
        )
    except (ClientError, ValueError) as exc:
        return jsonify({"error": safe_error_message(exc)}), 500


@app.route("/api/bucket-stats")
@requires_auth
def api_bucket_stats():
    """Calculate total bucket size and usage percentage against BUCKET_QUOTA_GIB."""
    force_refresh = request.args.get("refresh") == "1"
    now = time.time()
    if (
        not force_refresh
        and _bucket_stats_cache["data"] is not None
        and (now - _bucket_stats_cache["ts"]) < BUCKET_STATS_CACHE_TTL
    ):
        cached_payload = dict(_bucket_stats_cache["data"])
        cached_payload["cached"] = True
        return jsonify(cached_payload)

    try:
        client = get_s3_client()
        paginator = client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=BUCKET_NAME)

        total_bytes = 0
        total_files = 0
        for page in pages:
            for obj in page.get("Contents", []):
                if obj["Key"].endswith("/"):
                    continue
                total_bytes += obj.get("Size", 0)
                total_files += 1

        quota_bytes = BUCKET_QUOTA_GIB * (1024 ** 3)
        percent = round((total_bytes / quota_bytes) * 100.0, 2) if quota_bytes > 0 else 0.0

        payload = {
            "total_bytes": total_bytes,
            "total_human": format_size(total_bytes),
            "total_files": total_files,
            "quota_gib": int(BUCKET_QUOTA_GIB) if BUCKET_QUOTA_GIB.is_integer() else BUCKET_QUOTA_GIB,
            "percent": percent,
            "cached": False,
        }
        _bucket_stats_cache["data"] = payload
        _bucket_stats_cache["ts"] = now
        return jsonify(payload)
    except (ClientError, ValueError) as exc:
        return jsonify({"error": safe_error_message(exc)}), 500


if __name__ == "__main__":
    port = int(os.getenv("FLASK_PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="127.0.0.1", port=port, debug=debug)

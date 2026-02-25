#!/usr/bin/env python3
"""
ExtensionLens — Local Chrome Extension Source Viewer

Browse, search, and read source of any installed Chrome extension.
Stdlib only. Bind to 127.0.0.1:8080.

Usage:
    python3 server.py
"""
from __future__ import annotations

import atexit
import hashlib
import json
import mimetypes
import os
import re
import shutil
import signal
import struct
import sys
import tempfile
import zipfile
from functools import lru_cache
from http import HTTPStatus
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"
PORT = 8080

STATIC_DIR = Path(__file__).resolve().parent / "static"

EXTENSION_DIRS: list[tuple[Path, str]] = [
    (Path.home() / ".config/google-chrome/Default/Extensions", "chrome"),
    (Path.home() / ".config/chromium/Default/Extensions", "chromium"),
]

# Managed temp directories for uploaded CRX/ZIP files
_temp_dirs: list[Path] = []

# Uploaded extensions tracked in memory: id -> metadata dict
_uploaded_extensions: dict[str, dict] = {}

# Maximum search results returned
MAX_SEARCH_RESULTS = 500

# Maximum file size for search scanning (skip huge minified bundles)
MAX_SEARCH_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

# ---------------------------------------------------------------------------
# Extension Discovery
# ---------------------------------------------------------------------------


def _extract_icon_path(manifest: dict, ext_root: Path) -> str | None:
    """Get the absolute path to the largest icon declared in manifest.json."""
    icons = manifest.get("icons")
    if not icons or not isinstance(icons, dict):
        return None
    # icons is e.g. {"16": "icon16.png", "48": "icon48.png", "128": "icon128.png"}
    try:
        largest_key = max(icons.keys(), key=lambda k: int(k))
    except (ValueError, TypeError):
        return None
    icon_rel = icons[largest_key]
    icon_path = ext_root / icon_rel
    if icon_path.exists():
        return str(icon_path)
    return None


def discover_extensions() -> list[dict]:
    """Find all installed Chrome/Chromium extensions."""
    results: list[dict] = []
    for base_dir, profile in EXTENSION_DIRS:
        if not base_dir.exists():
            continue
        try:
            children = sorted(base_dir.iterdir())
        except OSError:
            continue
        for ext_dir in children:
            if not ext_dir.is_dir() or ext_dir.name == "Temp":
                continue
            # Pick the latest version sub-directory
            try:
                versions = sorted(ext_dir.iterdir())
            except OSError:
                continue
            if not versions:
                continue
            latest = versions[-1]
            manifest_path = latest / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
                name = data.get("name", "?")
                if name.startswith("__MSG_"):
                    name = f"({ext_dir.name[:12]}...)"
                icon_path = _extract_icon_path(data, latest)
                results.append({
                    "id": ext_dir.name,
                    "name": name,
                    "version": data.get("version", "?"),
                    "path": str(latest),
                    "profile": profile,
                    "description": (data.get("description") or "")[:120],
                    "icon_path": icon_path,
                })
            except (json.JSONDecodeError, OSError):
                continue
    return results


def _find_extension(ext_id: str, profile: str) -> dict | None:
    """Locate a single extension by ID and profile (includes uploads)."""
    if ext_id in _uploaded_extensions:
        return _uploaded_extensions[ext_id]
    for ext in discover_extensions():
        if ext["id"] == ext_id and ext["profile"] == profile:
            return ext
    return None


# ---------------------------------------------------------------------------
# Allowed-path validation (path traversal protection)
# ---------------------------------------------------------------------------


def _allowed_base_dirs() -> list[Path]:
    """Return all base directories we allow file access within."""
    dirs = [base for base, _ in EXTENSION_DIRS if base.exists()]
    for td in _temp_dirs:
        dirs.append(td)
    return dirs


def _is_path_allowed(requested: str) -> bool:
    """Return True if the absolute requested path is inside an allowed dir."""
    try:
        rp = Path(requested).resolve()
    except (OSError, ValueError):
        return False
    for base in _allowed_base_dirs():
        try:
            rp.relative_to(base.resolve())
            return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# File tree builder
# ---------------------------------------------------------------------------


def _build_tree(root: Path) -> dict:
    """Build a recursive file-tree dict rooted at *root*."""
    node: dict = {"name": root.name or "root", "type": "directory", "children": []}
    try:
        entries = list(root.iterdir())
    except OSError:
        return node

    dirs: list[dict] = []
    files: list[dict] = []
    manifest_entry: dict | None = None

    for entry in entries:
        if entry.is_dir():
            child = _build_tree(entry)
            dirs.append(child)
        elif entry.is_file():
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            fnode = {
                "name": entry.name,
                "path": str(entry),
                "size": size,
                "type": "file",
            }
            if entry.name == "manifest.json":
                manifest_entry = fnode
            else:
                files.append(fnode)

    # Sort: manifest.json first, then dirs alphabetically, then files alphabetically
    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["name"].lower())

    if manifest_entry:
        node["children"].append(manifest_entry)
    node["children"].extend(dirs)
    node["children"].extend(files)
    return node


# ---------------------------------------------------------------------------
# Search engine
# ---------------------------------------------------------------------------


def _is_binary(filepath: Path) -> bool:
    """Heuristic: file is binary if first 512 bytes contain a null byte."""
    try:
        chunk = filepath.read_bytes()[:512]
        return b"\x00" in chunk
    except OSError:
        return True


def _search_extension(ext_root: Path, query: str, use_regex: bool, case_sensitive: bool) -> list[dict]:
    """Grep across all text files in ext_root. Returns list of match dicts."""
    results: list[dict] = []
    flags = 0 if case_sensitive else re.IGNORECASE

    if use_regex:
        try:
            pattern = re.compile(query, flags)
        except re.error:
            return []
    else:
        if case_sensitive:
            pattern = re.compile(re.escape(query), flags)
        else:
            pattern = re.compile(re.escape(query), flags)

    root = Path(ext_root)
    for fpath in sorted(root.rglob("*")):
        if len(results) >= MAX_SEARCH_RESULTS:
            break
        if not fpath.is_file():
            continue
        try:
            if fpath.stat().st_size > MAX_SEARCH_FILE_SIZE:
                continue
        except OSError:
            continue
        if _is_binary(fpath):
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if len(results) >= MAX_SEARCH_RESULTS:
                break
            m = pattern.search(line)
            if m:
                results.append({
                    "file": str(fpath.relative_to(root)),
                    "line": lineno,
                    "text": line[:500],
                    "col": m.start(),
                })
    return results


# ---------------------------------------------------------------------------
# CRX / ZIP upload handling
# ---------------------------------------------------------------------------


def _strip_crx_header(data: bytes) -> bytes:
    """Strip the CRX3 header and return the inner ZIP bytes.

    CRX3 format:
        4 bytes  magic   "Cr24"
        4 bytes  version (3)
        4 bytes  header_length
        <header_length bytes of protobuf header>
        <remaining bytes = ZIP archive>
    """
    if len(data) < 12:
        raise ValueError("File too small to be a CRX")
    magic = data[:4]
    if magic != b"Cr24":
        raise ValueError(f"Not a CRX file (magic: {magic!r})")
    version = struct.unpack("<I", data[4:8])[0]
    if version != 3:
        # CRX2 fallback: version=2, pubkey_len(4), sig_len(4), pubkey, sig, zip
        if version == 2:
            pk_len = struct.unpack("<I", data[8:12])[0]
            sig_len = struct.unpack("<I", data[12:16])[0]
            zip_start = 16 + pk_len + sig_len
            return data[zip_start:]
        raise ValueError(f"Unsupported CRX version: {version}")
    header_len = struct.unpack("<I", data[8:12])[0]
    zip_start = 12 + header_len
    return data[zip_start:]


def _handle_upload(file_data: bytes, filename: str) -> dict:
    """Extract a CRX or ZIP upload, return extension metadata."""
    lower = filename.lower()
    if lower.endswith(".crx"):
        zip_bytes = _strip_crx_header(file_data)
    elif lower.endswith(".zip"):
        zip_bytes = file_data
    else:
        raise ValueError("Only .crx and .zip files are accepted")

    # Create temp directory
    tmpdir = Path(tempfile.mkdtemp(prefix="ext-viewer-"))
    _temp_dirs.append(tmpdir)

    # Extract
    import io
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(tmpdir)

    # Read manifest if present
    manifest_path = tmpdir / "manifest.json"
    name = filename
    version = "?"
    description = ""
    icon_path = None
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
            name = data.get("name", filename)
            version = data.get("version", "?")
            description = (data.get("description") or "")[:120]
            icon_path = _extract_icon_path(data, tmpdir)
        except (json.JSONDecodeError, OSError):
            pass

    # Deterministic ID from content hash
    content_hash = hashlib.sha256(file_data[:4096]).hexdigest()[:12]
    ext_id = f"temp-{content_hash}"

    result = {
        "id": ext_id,
        "name": name,
        "version": version,
        "path": str(tmpdir),
        "profile": "upload",
        "description": description,
        "icon_path": icon_path,
    }
    _uploaded_extensions[ext_id] = result
    return result


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def _cleanup_temp_dirs():
    """Remove all temporary directories created for uploads."""
    for td in _temp_dirs:
        try:
            shutil.rmtree(td, ignore_errors=True)
        except Exception:
            pass
    _temp_dirs.clear()
    _uploaded_extensions.clear()


atexit.register(_cleanup_temp_dirs)


# ---------------------------------------------------------------------------
# HTTP Request Handler
# ---------------------------------------------------------------------------


class ExtensionViewerHandler(BaseHTTPRequestHandler):
    """Handle all HTTP requests for the extension viewer."""

    server_version = "ExtensionLens/1.0"

    # Suppress default stderr logging for each request — we log our own way
    def log_message(self, format: str, *args) -> None:
        sys.stderr.write(f"[viewer] {args[0]} {args[1]} {args[2]}\n")

    # ----- routing ---------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        # API routes
        if path == "/api/extensions":
            self._api_extensions()
        elif path == "/api/tree":
            self._api_tree(params)
        elif path == "/api/file":
            self._api_file(params)
        elif path == "/api/icon":
            self._api_icon(params)
        elif path == "/api/search":
            self._api_search(params)
        # Static files
        elif path == "/" or path == "/index.html":
            self._serve_static("index.html")
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
            self._serve_static(rel)
        else:
            self._send_error(404, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/upload":
            self._api_upload()
        else:
            self._send_error(404, "Not found")

    # ----- API: list extensions --------------------------------------------

    def _api_extensions(self):
        exts = discover_extensions()
        # Include uploaded extensions
        for ext in _uploaded_extensions.values():
            exts.append(ext)
        self._send_json(exts)

    # ----- API: file tree --------------------------------------------------

    def _api_tree(self, params: dict):
        ext_id = self._param(params, "ext")
        profile = self._param(params, "profile", "chrome")
        if not ext_id:
            self._send_error(400, "Missing 'ext' parameter")
            return
        ext = _find_extension(ext_id, profile)
        if not ext:
            self._send_error(404, f"Extension not found: {ext_id}")
            return
        root = Path(ext["path"])
        if not root.is_dir():
            self._send_error(404, "Extension directory not found")
            return
        tree = _build_tree(root)
        tree["name"] = "root"
        self._send_json(tree)

    # ----- API: raw file ---------------------------------------------------

    def _api_file(self, params: dict):
        filepath = self._param(params, "path")
        if not filepath:
            self._send_error(400, "Missing 'path' parameter")
            return
        if not _is_path_allowed(filepath):
            self._send_error(403, "Access denied: path outside allowed directories")
            return
        p = Path(filepath)
        if not p.is_file():
            self._send_error(404, "File not found")
            return
        try:
            data = p.read_bytes()
        except OSError as e:
            self._send_error(500, f"Read error: {e}")
            return

        content_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        # For text files without explicit charset, assume UTF-8
        if content_type.startswith("text/") and "charset" not in content_type:
            content_type += "; charset=utf-8"
        # Treat .js, .mjs, .cjs as text
        if p.suffix in (".js", ".mjs", ".cjs"):
            content_type = "text/javascript; charset=utf-8"
        elif p.suffix == ".json":
            content_type = "application/json; charset=utf-8"
        elif p.suffix == ".wasm":
            content_type = "application/wasm"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-File-Size", str(len(data)))
        self.send_header("Access-Control-Expose-Headers", "X-File-Size")
        self.end_headers()
        self.wfile.write(data)

    # ----- API: icon -------------------------------------------------------

    def _api_icon(self, params: dict):
        filepath = self._param(params, "path")
        if not filepath:
            self._send_error(400, "Missing 'path' parameter")
            return
        if not _is_path_allowed(filepath):
            self._send_error(403, "Access denied: path outside allowed directories")
            return
        p = Path(filepath)
        if not p.is_file():
            self._send_error(404, "Icon not found")
            return
        try:
            data = p.read_bytes()
        except OSError as e:
            self._send_error(500, f"Read error: {e}")
            return

        content_type = mimetypes.guess_type(p.name)[0] or "image/png"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    # ----- API: search -----------------------------------------------------

    def _api_search(self, params: dict):
        ext_id = self._param(params, "ext")
        profile = self._param(params, "profile", "chrome")
        query = self._param(params, "q")
        use_regex = self._param(params, "regex", "0") == "1"
        case_sensitive = self._param(params, "case", "0") == "1"

        if not ext_id:
            self._send_error(400, "Missing 'ext' parameter")
            return
        if not query:
            self._send_error(400, "Missing 'q' parameter")
            return

        ext = _find_extension(ext_id, profile)
        if not ext:
            self._send_error(404, f"Extension not found: {ext_id}")
            return

        root = Path(ext["path"])
        if not root.is_dir():
            self._send_error(404, "Extension directory not found")
            return

        results = _search_extension(root, query, use_regex, case_sensitive)
        self._send_json(results)

    # ----- API: upload CRX/ZIP --------------------------------------------

    def _api_upload(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_error(400, "Expected multipart/form-data")
            return

        try:
            # Manual multipart parsing (no cgi module needed)
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)

            # Extract boundary from Content-Type
            boundary = None
            for part in content_type.split(";"):
                part = part.strip()
                if part.startswith("boundary="):
                    boundary = part[len("boundary="):].strip('"')
                    break
            if not boundary:
                self._send_error(400, "No boundary in Content-Type")
                return

            # Split on boundary, skip first (empty) and last (closing)
            sep = f"--{boundary}".encode()
            parts = body.split(sep)
            file_data = None
            filename = None

            for part in parts:
                part = part.strip(b"\r\n")
                if not part or part == b"--":
                    continue
                # Split headers from body at double CRLF
                if b"\r\n\r\n" in part:
                    header_bytes, payload = part.split(b"\r\n\r\n", 1)
                else:
                    continue
                header_str = header_bytes.decode("utf-8", errors="replace")
                if 'name="file"' in header_str:
                    # Extract filename
                    for token in header_str.split(";"):
                        token = token.strip()
                        if token.startswith("filename="):
                            filename = token[len("filename="):].strip('"')
                    # Strip trailing boundary marker
                    if payload.endswith(b"\r\n"):
                        payload = payload[:-2]
                    file_data = payload
                    break

            if file_data is None or not filename:
                self._send_error(400, "No file found in upload")
                return
        except Exception as e:
            self._send_error(400, f"Upload parse error: {e}")
            return

        try:
            result = _handle_upload(file_data, filename)
        except ValueError as e:
            self._send_error(400, str(e))
            return
        except Exception as e:
            self._send_error(500, f"Extraction failed: {e}")
            return

        self._send_json(result)

    # ----- static file serving ---------------------------------------------

    def _serve_static(self, rel_path: str):
        # Prevent path traversal in static serving
        safe = Path(rel_path).parts
        if any(part in ("..", "~") for part in safe):
            self._send_error(403, "Invalid path")
            return
        filepath = STATIC_DIR / rel_path
        filepath = filepath.resolve()
        # Verify it's still under STATIC_DIR
        try:
            filepath.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send_error(403, "Access denied")
            return
        if not filepath.is_file():
            self._send_error(404, f"Static file not found: {rel_path}")
            return
        try:
            data = filepath.read_bytes()
        except OSError as e:
            self._send_error(500, f"Read error: {e}")
            return

        content_type = mimetypes.guess_type(filepath.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") and "charset" not in content_type:
            content_type += "; charset=utf-8"
        if filepath.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        elif filepath.suffix == ".css":
            content_type = "text/css; charset=utf-8"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ----- helpers ---------------------------------------------------------

    @staticmethod
    def _param(params: dict, key: str, default: str | None = None) -> str | None:
        """Extract a single query parameter value."""
        vals = params.get(key)
        if vals:
            return vals[0]
        return default

    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str):
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------


def main():
    server = HTTPServer((HOST, PORT), ExtensionViewerHandler)
    print(f"ExtensionLens running at http://{HOST}:{PORT}")
    print(f"Static dir: {STATIC_DIR}")
    print("Press Ctrl+C to stop.\n")

    def _shutdown(signum, frame):
        print("\nShutting down...")
        _cleanup_temp_dirs()
        server.shutdown()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _cleanup_temp_dirs()
        server.server_close()
        print("Server stopped.")


if __name__ == "__main__":
    main()

"""Validation of student file uploads (answers to FILE questions).

Nothing the browser says about a file is trusted. The type is decided from the file's own bytes
(magic numbers), must be on a short allow-list, and must agree with the extension. The stored
`content_type` and the served headers come from this module's table, never from the request.

Allowed: PNG, JPEG, GIF, WebP images, PDF, and Word .docx. Not allowed (on purpose): SVG and HTML
(script in the file), macro-enabled Office files, archives, executables.

NOT done: virus or malware scanning, and no check that a PDF or Word file is free of embedded
links or active content beyond what is listed above. A lecturer opening a student's file is
opening a file from an untrusted source; the app only makes sure it is served as a download with
a fixed type.
"""

import io
import re
import unicodedata
import zipfile

from .models import MAX_UPLOAD_BYTES

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# kind -> (content type, allowed extensions)
TYPES = {
    "png": ("image/png", {".png"}),
    "jpeg": ("image/jpeg", {".jpg", ".jpeg"}),
    "gif": ("image/gif", {".gif"}),
    "webp": ("image/webp", {".webp"}),
    "pdf": ("application/pdf", {".pdf"}),
    "docx": (DOCX_MIME, {".docx"}),
}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
ALLOWED_DESCRIPTION = "PNG, JPEG, GIF or WebP images, PDF, or Word (.docx)"

_MAX_ZIP_ENTRIES = 2000
_MAX_ZIP_UNCOMPRESSED = 100 * 1024 * 1024  # declared sizes are only summed, never extracted
_SAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9 ._()\-]")


class UploadRejected(Exception):
    """The upload is not acceptable; the message is shown to the student."""


def _sniff(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04") and _looks_like_docx(data):
        return "docx"
    return None


def _looks_like_docx(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > _MAX_ZIP_UNCOMPRESSED:
                return False
            names = {i.filename for i in infos}
    except (zipfile.BadZipFile, RuntimeError, ValueError):
        return False
    if any(n.lower().endswith("vbaproject.bin") for n in names):  # macro-enabled document
        return False
    return "[Content_Types].xml" in names and "word/document.xml" in names


def clean_filename(name, extension):
    """A safe display/download name: no path, no odd characters, at most 100 characters."""
    name = unicodedata.normalize("NFKC", name or "")
    name = re.split(r"[\\/]", name)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    stem = _SAFE_NAME_CHARS.sub("_", stem).strip(" ._")
    stem = re.sub(r"\s+", " ", stem)[: 100 - len(extension)]
    return (stem or "upload") + extension


def inspect_upload(uploaded):
    """Return (filename, content_type, data) for an acceptable upload, else raise UploadRejected."""
    if uploaded is None:
        raise UploadRejected("Choose a file to upload.")
    if uploaded.size == 0:
        raise UploadRejected("The file is empty.")
    if uploaded.size > MAX_UPLOAD_BYTES:
        raise UploadRejected(f"The file is too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    chunks = []
    total = 0
    for chunk in uploaded.chunks():
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:  # the declared size could be wrong; count what actually arrived
            raise UploadRejected(f"The file is too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
        chunks.append(chunk)
    data = b"".join(chunks)

    kind = _sniff(data)
    if kind is None:
        raise UploadRejected(f"This file type is not accepted. Use {ALLOWED_DESCRIPTION}.")
    content_type, extensions = TYPES[kind]
    original = unicodedata.normalize("NFKC", uploaded.name or "")
    extension = ("." + original.rsplit(".", 1)[-1].lower()) if "." in original else ""
    if extension not in extensions:
        raise UploadRejected("The file's extension does not match its contents.")
    return clean_filename(original, extension), content_type, data

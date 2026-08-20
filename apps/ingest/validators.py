"""
Upload validation.

The governing rule: **nothing the client declared is treated as a fact.** A
Content-Type header is a hint, a filename is decoration, and a stated size is a
claim. Every one of them is re-derived from the bytes actually stored in B2
before a MediaItem row exists.

The checks, and what each one actually stops:

  magic bytes        an .exe uploaded as image/png
  Pillow verify      a decompression bomb (a 200 MB bitmap from a 2 KB file)
  MAX_IMAGE_PIXELS   the same, before Pillow allocates anything
  zip entry scan     path traversal (`../../etc/passwd`) and zip bombs
  size/mime match    a presign for a 2 MB PNG redeemed for something else

Only small byte ranges are ever pulled into the process: 4 KB for a signature,
the central directory for a zip, and the whole file only for images under
INLINE_PROCESS_MAX_BYTES.
"""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass, field

from django.conf import settings
from PIL import Image, ImageFile, UnidentifiedImageError

from apps.catalog.models import Feature, MediaType

from . import storage

logger = logging.getLogger("ingest.upload")

# Refuse to reconstruct images from damaged streams: a truncated file should be
# an error, not a half-decoded surprise.
ImageFile.LOAD_TRUNCATED_IMAGES = False

SNIFF_BYTES = 4096

# Zip's End Of Central Directory record sits at the tail. 64 KiB covers it plus
# a comment, which is all that is needed to enumerate entries.
ZIP_TAIL_BYTES = 96 * 1024


class ValidationFailure(Exception):
    """A rejected upload. The message is safe to return to a staff client."""

    def __init__(self, message: str, code: str = "invalid_file") -> None:
        self.message = message
        self.code = code
        super().__init__(message)


# --------------------------------------------------------------------------- #
# Magic bytes
# --------------------------------------------------------------------------- #

# (offset, signature, mime). Deliberately hand-rolled rather than python-magic:
# libmagic needs a system library that free-tier images do not ship, and this
# only has to recognise the handful of types the API accepts.
_SIGNATURES: list[tuple[int, bytes, str]] = [
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"GIF87a", "image/gif"),
    (0, b"GIF89a", "image/gif"),
    (0, b"PK\x03\x04", "application/zip"),
    (0, b"PK\x05\x06", "application/zip"),  # empty archive
    (0, b"PK\x07\x08", "application/zip"),  # spanned archive
]

# Families where the signature alone is ambiguous and a second marker is needed.
_RIFF_WEBP = (b"RIFF", b"WEBP")
_MP4_BRAND_OFFSET = 4
_MP4_BRAND = b"ftyp"
_MATROSKA = b"\x1a\x45\xdf\xa3"


def sniff_mime(head: bytes) -> str:
    """Identify a MIME type from a file's leading bytes, or "" if unrecognised."""
    if not head:
        return ""

    for offset, signature, mime in _SIGNATURES:
        if head[offset : offset + len(signature)] == signature:
            return mime

    # WebP is a RIFF container with a WEBP fourcc at offset 8.
    if head[:4] == _RIFF_WEBP[0] and head[8:12] == _RIFF_WEBP[1]:
        return "image/webp"

    # ISO-BMFF (mp4/mov): a 'ftyp' box at offset 4.
    if head[_MP4_BRAND_OFFSET : _MP4_BRAND_OFFSET + 4] == _MP4_BRAND:
        return "video/mp4"

    if head[:4] == _MATROSKA:
        return "video/webm"

    return ""


# Sniffed type -> the declared types it is allowed to satisfy. Zip is listed
# under both spellings because browsers disagree about which to send.
_MIME_EQUIVALENTS: dict[str, set[str]] = {
    "image/jpeg": {"image/jpeg", "image/jpg"},
    "image/png": {"image/png"},
    "image/gif": {"image/gif"},
    "image/webp": {"image/webp"},
    "video/mp4": {"video/mp4"},
    "video/webm": {"video/webm"},
    "application/zip": {"application/zip", "application/x-zip-compressed"},
}


def mime_matches(sniffed: str, declared: str) -> bool:
    return declared.lower() in _MIME_EQUIVALENTS.get(sniffed, {sniffed})


def media_type_for_mime(mime: str) -> str:
    lowered = mime.lower()
    if lowered == "image/gif":
        return MediaType.GIF
    if lowered.startswith("video/"):
        return MediaType.VIDEO
    if lowered in {"application/zip", "application/x-zip-compressed"}:
        return MediaType.ZIP
    if lowered.startswith("image/"):
        return MediaType.IMAGE
    raise ValidationFailure(f"Unsupported media type: {mime}", code="unsupported_mime")


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass
class ImageProbe:
    width: int | None = None
    height: int | None = None
    dominant_color: str = ""
    is_animated: bool = False
    frames: int = 1


@dataclass
class ZipProbe:
    entries: int = 0
    has_conf: bool = False
    image_entries: int = 0
    uncompressed_bytes: int = 0
    names: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Image validation
# --------------------------------------------------------------------------- #


def probe_image(data: bytes, feature: Feature) -> ImageProbe:
    """
    Validate image bytes and pull out dimensions plus a placeholder colour.

    `verify()` runs first because it checks structural integrity without
    decoding pixel data. It also leaves the file object unusable, which is why
    the stream is reopened afterwards — a genuinely surprising Pillow API that
    silently returns garbage if you skip the reopen.
    """
    # Cap decoding before Pillow allocates. Pillow's own guard raises a warning
    # at half this value and an error at the limit.
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max(int(feature.max_pixels), 1)

    try:
        try:
            with Image.open(io.BytesIO(data)) as probe:
                probe.verify()
        except UnidentifiedImageError as exc:
            raise ValidationFailure(
                "File is not a readable image.", code="unreadable_image"
            ) from exc
        except Image.DecompressionBombError as exc:
            raise ValidationFailure(
                "Image exceeds the allowed pixel budget for this type.",
                code="decompression_bomb",
            ) from exc
        except Exception as exc:
            raise ValidationFailure(
                f"Image failed verification: {exc}", code="corrupt_image"
            ) from exc

        # verify() consumed the handle; reopen to read anything from it.
        try:
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                if width * height > feature.max_pixels:
                    raise ValidationFailure(
                        f"Image is {width}x{height}, above the "
                        f"{feature.max_pixels} pixel limit for this type.",
                        code="too_many_pixels",
                    )
                frames = getattr(image, "n_frames", 1)
                probe_result = ImageProbe(
                    width=width,
                    height=height,
                    is_animated=bool(getattr(image, "is_animated", False)),
                    frames=frames,
                    dominant_color=_dominant_color(image),
                )
        except ValidationFailure:
            raise
        except Exception as exc:
            raise ValidationFailure(
                f"Could not read image metadata: {exc}", code="corrupt_image"
            ) from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit

    return probe_result


def _dominant_color(image: Image.Image) -> str:
    """
    Average colour, for the app to show behind a loading thumbnail.

    Resizing to a single pixel makes Pillow do the averaging in C, so this costs
    almost nothing compared to sampling in Python.
    """
    try:
        frame = image.convert("RGB").resize((1, 1), Image.Resampling.BILINEAR)
        r, g, b = frame.getpixel((0, 0))
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:  # pragma: no cover - never fail an upload over a nicety
        return ""


def make_preview(data: bytes, max_edge: int, quality: int) -> tuple[bytes, str]:
    """
    Downscale to a JPEG preview, preserving aspect ratio.

    `thumbnail()` never upscales, so a small source is left alone rather than
    being blown up into a larger file than the original.
    """
    with Image.open(io.BytesIO(data)) as image:
        # Flatten transparency onto white: JPEG has no alpha channel, and
        # converting straight to RGB turns transparent pixels black.
        if image.mode in {"RGBA", "LA", "P"}:
            image = image.convert("RGBA")
            backdrop = Image.new("RGBA", image.size, (255, 255, 255, 255))
            image = Image.alpha_composite(backdrop, image).convert("RGB")
        elif image.mode != "RGB":
            image = image.convert("RGB")

        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
        return buffer.getvalue(), "image/jpeg"


# --------------------------------------------------------------------------- #
# Zip validation (Shimeji packs)
# --------------------------------------------------------------------------- #

_IMAGE_SUFFIXES = (".png", ".gif", ".jpg", ".jpeg", ".webp")


def probe_zip(data: bytes, feature: Feature) -> ZipProbe:
    """
    Inspect a zip archive held in memory, without extracting a single byte.

    Reads the central directory only, so entry names and declared sizes are
    checked before anything is decompressed. That ordering is the point: a zip
    bomb is rejected on the strength of its own metadata, never by attempting
    the extraction and running out of memory.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        infos = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise ValidationFailure("File is not a valid zip archive.", code="bad_zip") from exc
    except Exception as exc:
        raise ValidationFailure("Could not read the zip directory.", code="bad_zip") from exc

    return _probe_zip_infos(infos, feature)


def _probe_zip_infos(infos: list[zipfile.ZipInfo], feature: Feature) -> ZipProbe:
    """Apply every archive check to an already-read central directory."""
    max_entries = settings.ZIP_MAX_ENTRIES
    if len(infos) > max_entries:
        raise ValidationFailure(
            f"Archive has {len(infos)} entries, above the {max_entries} limit.",
            code="zip_too_many_entries",
        )
    if not infos:
        raise ValidationFailure("Archive is empty.", code="zip_empty")

    total_uncompressed = 0
    total_compressed = 0
    has_conf = False
    image_entries = 0
    names: list[str] = []

    for info in infos:
        name = info.filename

        # Path traversal. Zip stores forward slashes, but archives built on
        # Windows sometimes carry backslashes, so normalise before checking.
        normalised = name.replace("\\", "/")
        if normalised.startswith("/") or normalised.startswith("~"):
            raise ValidationFailure(
                f"Archive entry uses an absolute path: {name}", code="zip_absolute_path"
            )
        if ".." in normalised.split("/"):
            raise ValidationFailure(
                f"Archive entry escapes its directory: {name}", code="zip_traversal"
            )
        if "\x00" in name:
            raise ValidationFailure(
                "Archive entry name contains a null byte.", code="zip_bad_name"
            )

        total_uncompressed += info.file_size
        total_compressed += info.compress_size

        lowered = normalised.lower()
        if lowered.startswith("conf/") or "/conf/" in lowered:
            has_conf = True
        if lowered.endswith(_IMAGE_SUFFIXES):
            image_entries += 1
        if len(names) < 50:
            names.append(normalised)

    if total_uncompressed > settings.ZIP_MAX_UNCOMPRESSED_BYTES:
        raise ValidationFailure(
            f"Archive expands to {total_uncompressed} bytes, above the "
            f"{settings.ZIP_MAX_UNCOMPRESSED_BYTES} byte limit.",
            code="zip_too_large",
        )

    # Ratio check catches the classic bomb: a few KB that expands enormously.
    # Guarded on a minimum compressed size so a tiny, highly compressible but
    # entirely harmless archive is not rejected.
    if total_compressed > 1024:
        ratio = total_uncompressed / max(total_compressed, 1)
        if ratio > settings.ZIP_MAX_COMPRESSION_RATIO:
            raise ValidationFailure(
                f"Archive compression ratio is {ratio:.0f}:1, above the "
                f"{settings.ZIP_MAX_COMPRESSION_RATIO}:1 limit.",
                code="zip_bomb",
            )

    if feature.strict_zip_structure and not has_conf:
        raise ValidationFailure(
            "Shimeji pack is missing a conf/ directory.", code="zip_missing_conf"
        )
    if feature.strict_zip_structure and image_entries == 0:
        raise ValidationFailure(
            "Shimeji pack contains no image entries.", code="zip_missing_images"
        )

    return ZipProbe(
        entries=len(infos),
        has_conf=has_conf,
        image_entries=image_entries,
        uncompressed_bytes=total_uncompressed,
        names=names,
    )


class _TailBackedFile(io.RawIOBase):
    """
    Seekable view over an object whose tail alone has been downloaded.

    ZipFile locates the End Of Central Directory at the end of the file, then
    seeks by absolute offset to read the directory. It therefore needs a file of
    the real length — but only the tail's *contents*. Materialising the head as
    actual zero bytes would allocate the whole file size in RAM, which is exactly
    what this module exists to avoid, so the head is synthesised per read
    instead. Allocation stays bounded by ZipFile's own read sizes.
    """

    def __init__(self, size: int, tail_offset: int, tail: bytes) -> None:
        super().__init__()
        self._size = size
        self._tail_offset = tail_offset
        self._tail = tail
        self._pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._pos + offset
        elif whence == io.SEEK_END:
            target = self._size + offset
        else:  # pragma: no cover
            raise ValueError(f"Invalid whence: {whence}")
        self._pos = max(0, min(target, self._size))
        return self._pos

    def read(self, size: int = -1) -> bytes:
        start = self._pos
        end = self._size if size is None or size < 0 else min(start + size, self._size)
        length = max(0, end - start)
        if length == 0:
            return b""
        self._pos = end

        tail_start = self._tail_offset
        if end <= tail_start:
            return b"\x00" * length  # wholly inside the synthesised head
        if start >= tail_start:
            offset = start - tail_start
            return self._tail[offset : offset + length]
        # Straddles the boundary.
        return b"\x00" * (tail_start - start) + self._tail[: end - tail_start]

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


# Progressively larger tails. A zip whose central directory sits further from
# the end than the first window needs a second, larger fetch.
_ZIP_TAIL_ATTEMPTS = (ZIP_TAIL_BYTES, 1024 * 1024, 8 * 1024 * 1024)


def probe_zip_from_storage(key: str, size: int, feature: Feature) -> ZipProbe:
    """
    Inspect a stored zip, downloading only what the directory needs.

    Entry *contents* are never fetched and are not needed: every check in
    probe_zip() works off the central directory alone.
    """
    if size <= _ZIP_TAIL_ATTEMPTS[0]:
        return probe_zip(storage.get_range(key, 0, size), feature)

    last_failure: ValidationFailure | None = None
    for tail_size in _ZIP_TAIL_ATTEMPTS:
        if tail_size >= size:
            return probe_zip(storage.get_range(key, 0, size), feature)

        tail_offset = size - tail_size
        tail = storage.get_range(key, tail_offset, tail_size)
        backing = _TailBackedFile(size, tail_offset, tail)
        try:
            archive = zipfile.ZipFile(backing)
            infos = archive.infolist()
        except Exception as exc:
            # Could be a genuinely bad zip, or a directory that starts before
            # our window. Widen the window and try again before giving up.
            last_failure = ValidationFailure(
                f"Could not read the zip directory: {exc}", code="bad_zip"
            )
            continue
        return _probe_zip_infos(infos, feature)

    raise last_failure or ValidationFailure("Could not read the zip directory.", code="bad_zip")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def validate_declared_upload(
    *, feature: Feature, filename: str, content_type: str, size: int
) -> None:
    """
    Presign-time checks. Cheap, and run before any bytes move.

    Catching a disallowed type here means a rejected bulk upload costs one round
    trip rather than fifty wasted uploads followed by fifty rejections.
    """
    if size <= 0:
        raise ValidationFailure("File size must be greater than zero.", code="empty_file")
    if size > feature.max_file_bytes:
        raise ValidationFailure(
            f"File is {size} bytes, above the {feature.max_file_bytes} byte "
            f"limit for '{feature.slug}'.",
            code="file_too_large",
        )
    if not feature.allows_mime(content_type):
        allowed = ", ".join(sorted(feature.allowed_mimes or [])) or "(none configured)"
        raise ValidationFailure(
            f"Content type '{content_type}' is not accepted for '{feature.slug}'. "
            f"Allowed: {allowed}.",
            code="mime_not_allowed",
        )

    # The extension is not trusted for storage — keys are minted server-side —
    # but a mismatch signals a confused uploader, so it is worth reporting.
    expected_ext = storage.extension_for_mime(content_type)
    if filename and "." in filename:
        actual_ext = filename.rsplit(".", 1)[-1].lower()
        equivalents = {expected_ext}
        if expected_ext == "jpg":
            equivalents.add("jpeg")
        if actual_ext not in equivalents:
            raise ValidationFailure(
                f"Filename extension '.{actual_ext}' does not match content type "
                f"'{content_type}' (expected '.{expected_ext}').",
                code="extension_mismatch",
            )


def verify_stored_object(
    *, key: str, expected_mime: str, expected_size: int
) -> tuple[storage.ObjectMeta, str]:
    """
    Confirm what actually landed in B2 and identify it from its bytes.

    Returns (metadata, sniffed mime). Raises ValidationFailure if the object is
    missing, the wrong size, or not the type it claims to be.
    """
    try:
        meta = storage.head_object(key)
    except storage.ObjectNotFound as exc:
        raise ValidationFailure(
            "No uploaded object found for this ticket. Complete the PUT first.",
            code="object_missing",
        ) from exc

    if meta.size != expected_size:
        raise ValidationFailure(
            f"Uploaded object is {meta.size} bytes but the ticket declared {expected_size}.",
            code="size_mismatch",
        )

    head = storage.get_range(key, 0, SNIFF_BYTES)
    sniffed = sniff_mime(head)
    if not sniffed:
        raise ValidationFailure(
            "Could not identify the file type from its contents.",
            code="unrecognised_content",
        )
    if not mime_matches(sniffed, expected_mime):
        raise ValidationFailure(
            f"File contents are '{sniffed}' but '{expected_mime}' was declared.",
            code="content_mismatch",
        )

    return meta, sniffed

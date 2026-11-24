"""Photos sent to the bot (e.g. the box whiteboard), stored under DATA_DIR.

Files go to ``DATA_DIR/photos/YYYY/MM/<date>_s<session>_<file_unique_id><ext>``. The
database keeps the path relative to DATA_DIR (POSIX separators), so the folder can be
moved or mounted in a container (D-010). Nothing is interpreted or sent anywhere yet.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from telegram import Document, Message, PhotoSize

logger = logging.getLogger(__name__)

PHOTOS_DIR = "photos"
# The Bot API can only download files up to 20 MB.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")


class PhotoTooLargeError(ValueError):
    """The file is larger than the Bot API lets bots download."""


def relative_photo_path(
    day: dt.date, session_id: int, file_unique_id: str, extension: str = ".jpg"
) -> PurePosixPath:
    safe_id = _SAFE_ID.sub("_", file_unique_id)
    name = f"{day.isoformat()}_s{session_id}_{safe_id}{extension}"
    return PurePosixPath(PHOTOS_DIR, f"{day:%Y}", f"{day:%m}", name)


def image_extension(document: Document) -> str:
    """Extension for an image sent as a file: from the MIME type, else the file name."""
    if document.mime_type in _IMAGE_EXTENSIONS:
        return _IMAGE_EXTENSIONS[document.mime_type]
    suffix = PurePosixPath(document.file_name or "").suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix) else ".jpg"


def best_image(message: Message) -> tuple[PhotoSize | Document, str] | None:
    """The largest version of the image in ``message`` and its extension.

    Telegram sends each photo in several sizes; the last one is the largest. An image
    sent as a file (uncompressed) is a document with an ``image/*`` MIME type.
    """
    if message.photo:
        return message.photo[-1], ".jpg"
    document = message.document
    if document and (document.mime_type or "").startswith("image/"):
        return document, image_extension(document)
    return None


async def download_image(
    image: PhotoSize | Document, data_dir: Path, relative: PurePosixPath
) -> Path:
    """Download ``image`` to ``data_dir / relative`` and return the absolute path."""
    if image.file_size and image.file_size > MAX_DOWNLOAD_BYTES:
        raise PhotoTooLargeError(f"{image.file_size} bytes")
    target = data_dir / Path(*relative.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    telegram_file = await image.get_file()
    await telegram_file.download_to_drive(target)
    return target


def delete_photos(data_dir: Path, relative_paths: Iterable[str]) -> None:
    """Delete stored photo files; a missing file is not an error."""
    for relative in relative_paths:
        path = data_dir / Path(*PurePosixPath(relative).parts)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not delete photo %s: %s", relative, exc)

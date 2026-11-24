import datetime as dt
from pathlib import PurePosixPath

import pytest

from kide.bot.photos import (
    MAX_DOWNLOAD_BYTES,
    PhotoTooLargeError,
    best_image,
    delete_photos,
    download_image,
    relative_photo_path,
)
from tests.bot_helpers import bind, fake_downloads, make_bot
from tests.telegram_factories import document_update, message, photo_update

DAY = dt.date(2026, 11, 23)


def test_relative_path_is_by_month_and_safe():
    path = relative_photo_path(DAY, 7, "AgAD/../x")
    assert path == PurePosixPath("photos/2026/11/2026-11-23_s7_AgAD____x.jpg")


def test_the_largest_photo_size_is_chosen():
    image, extension = best_image(photo_update("BIG").message)
    assert image.file_unique_id == "BIG"
    assert extension == ".jpg"


def test_images_sent_as_files_keep_their_format():
    image, extension = best_image(document_update(mime_type="image/png").message)
    assert image.file_unique_id == "DOC1"
    assert extension == ".png"


def test_non_images_are_ignored():
    assert best_image(document_update(mime_type="application/pdf").message) is None
    assert best_image(message(text="hi")) is None


@pytest.mark.anyio
async def test_download_writes_under_data_dir(tmp_path):
    bot = make_bot()
    fake_downloads(bot)
    update = bind(photo_update("PH1"), bot)
    image, _ = best_image(update.message)
    relative = relative_photo_path(DAY, 1, "PH1")
    target = await download_image(image, tmp_path, relative)
    assert target == tmp_path / "photos" / "2026" / "11" / "2026-11-23_s1_PH1.jpg"
    assert target.read_bytes() == b"fake image"
    bot.get_file.assert_awaited_once()
    assert bot.get_file.await_args.kwargs["file_id"] == "PH1-l"  # the large one


@pytest.mark.anyio
async def test_files_over_20_mb_are_refused_before_downloading(tmp_path):
    bot = make_bot()
    update = bind(document_update(file_size=MAX_DOWNLOAD_BYTES + 1), bot)
    image, _ = best_image(update.message)
    with pytest.raises(PhotoTooLargeError):
        await download_image(image, tmp_path, relative_photo_path(DAY, 1, "DOC1"))
    bot.get_file.assert_not_awaited()


def test_delete_photos_ignores_missing_files(tmp_path):
    kept = tmp_path / "photos" / "a.jpg"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"x")
    delete_photos(tmp_path, ["photos/a.jpg", "photos/missing.jpg"])
    assert not kept.exists()

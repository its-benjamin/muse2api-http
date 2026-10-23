from __future__ import annotations

import asyncio
import base64

import pytest

from muse2api.server import ApiError, first_video_reference, reference_image_item

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG).decode("ascii")
DATA_URI = f"data:image/png;base64,{PNG_B64}"


def test_data_uri_is_decoded_to_attachment_item():
    item = asyncio.run(reference_image_item(DATA_URI))
    assert item["type"] == "image"
    assert item["mime_type"] == "image/png"
    assert base64.b64decode(item["data_base64"]) == PNG
    assert item["filename"].startswith("reference")


def test_dict_base64_and_plain_base64_are_accepted():
    from_dict = asyncio.run(reference_image_item({"b64_json": PNG_B64, "mime_type": "image/png"}))
    from_plain = asyncio.run(reference_image_item(PNG_B64))
    assert from_dict["data_base64"] == from_plain["data_base64"] == PNG_B64


def test_invalid_data_is_rejected():
    with pytest.raises(ApiError):
        asyncio.run(reference_image_item("data:image/png;base64,@@@"))
    with pytest.raises(ApiError):
        asyncio.run(reference_image_item(f"data:text/plain;base64,{PNG_B64}"))
    with pytest.raises(ApiError):
        asyncio.run(reference_image_item("data:image/png,notbase64"))


def test_first_reference_prefers_documented_order():
    body = {
        "reference_image": DATA_URI,
        "image_url": f"data:image/png;base64,{PNG_B64}",
        "image": f"data:image/png;base64,{PNG_B64}",
    }
    item = asyncio.run(first_video_reference(body))
    assert base64.b64decode(item["data_base64"]) == PNG
    assert asyncio.run(first_video_reference({"prompt": "x"})) is None

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from muse2api.filesystem import (
    UPLOAD_CHUNK_SIZE,
    MuseFilesystem,
    normalize_gateway_path,
)
from muse2api.gateway import RpcResponse


def test_normalize_gateway_path():
    assert normalize_gateway_path("file:///Users/alice/workspace/code/main.py") == "workspace/code/main.py"
    assert normalize_gateway_path("file://home/bob/project/doc.md?raw=true#title") == "project/doc.md"
    assert normalize_gateway_path("workspace/sub/file.txt") == "workspace/sub/file.txt"
    assert normalize_gateway_path("Users/charlie/repo/foo.py") == "repo/foo.py"
    assert normalize_gateway_path("file:///C:/Users/alice/workspace/bar.txt") == "workspace/bar.txt"
    assert normalize_gateway_path("") == ""


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn.request = AsyncMock()
    return conn


def test_stat(mock_conn):
    async def _test():
        fs = MuseFilesystem(mock_conn)
        mock_conn.request.return_value = RpcResponse(200, [], b'{"kind":"file","size":123}')
        res = await fs.stat("workspace/data.txt")
        assert res == {"kind": "file", "size": 123}
        mock_conn.request.assert_awaited_once_with("POST", "/fs/stat", {"path": "workspace/data.txt"})

    asyncio.run(_test())


def test_list(mock_conn):
    async def _test():
        fs = MuseFilesystem(mock_conn)
        mock_conn.request.return_value = RpcResponse(200, [], b'{"entries":[{"name":"foo.py"}]}')
        res = await fs.list("workspace")
        assert res == {"entries": [{"name": "foo.py"}]}
        mock_conn.request.assert_awaited_once_with("POST", "/fs/list", {"path": "workspace"})

    asyncio.run(_test())


def test_write_chunk(mock_conn):
    async def _test():
        fs = MuseFilesystem(mock_conn)
        mock_conn.request.return_value = RpcResponse(200, [], b'{"ok":true}')
        chunk = b"hello binary data"
        res = await fs.write_chunk("workspace/file.bin", chunk, overwrite=True, append=False, create_parent=True)
        assert res == {"ok": True}
        mock_conn.request.assert_awaited_once_with(
            "POST",
            "/fs/write",
            {
                "path": "workspace/file.bin",
                "data_base64": base64.b64encode(chunk).decode("ascii"),
                "overwrite": True,
                "append": False,
                "create_parent": True,
            },
            timeout=120,
        )

    asyncio.run(_test())


def test_rename(mock_conn):
    async def _test():
        fs = MuseFilesystem(mock_conn)
        mock_conn.request.return_value = RpcResponse(200, [], b'{"renamed":true}')
        res = await fs.rename("src.txt", "dst.txt")
        assert res == {"renamed": True}
        mock_conn.request.assert_awaited_once_with(
            "POST",
            "/fs/rename",
            {
                "source": "src.txt",
                "destination": "dst.txt",
                "create_parent": True,
                "overwrite": True,
            },
        )

    asyncio.run(_test())


def test_delete(mock_conn):
    async def _test():
        fs = MuseFilesystem(mock_conn)
        mock_conn.request.return_value = RpcResponse(200, [], b'{"deleted":true}')
        res = await fs.delete("trash.txt", recursive=True)
        assert res == {"deleted": True}
        mock_conn.request.assert_awaited_once_with(
            "POST",
            "/fs/delete",
            {"path": "trash.txt", "recursive": True},
        )

    asyncio.run(_test())


def test_upload_file_staged(mock_conn, tmp_path):
    async def _test():
        fs = MuseFilesystem(mock_conn)
        test_file = tmp_path / "sample.txt"
        content = b"x" * (UPLOAD_CHUNK_SIZE + 100)
        test_file.write_bytes(content)

        mock_conn.request.side_effect = [
            RpcResponse(200, [], b'{"ok":true}'),
            RpcResponse(200, [], b'{"ok":true}'),
            RpcResponse(200, [], b'{"kind":"file","size":' + str(len(content)).encode() + b'}'),
            RpcResponse(200, [], b'{"ok":true}'),
        ]

        dest = await fs.upload_file(str(test_file), "remote/path.txt", stage_before_publish=True)
        assert dest == "remote/path.txt"
        assert mock_conn.request.await_count == 4

    asyncio.run(_test())


def test_upload_file_size_mismatch_cleans_up(mock_conn, tmp_path):
    async def _test():
        fs = MuseFilesystem(mock_conn)
        test_file = tmp_path / "sample.txt"
        test_file.write_bytes(b"12345")

        mock_conn.request.side_effect = [
            RpcResponse(200, [], b'{"ok":true}'),
            RpcResponse(200, [], b'{"kind":"file","size":999}'),
            RpcResponse(200, [], b'{"ok":true}'),
        ]

        with pytest.raises(RuntimeError, match="size validation"):
            await fs.upload_file(str(test_file), "remote/bad.txt", stage_before_publish=True)

        last_call = mock_conn.request.call_args_list[-1]
        assert last_call[0][1] == "/fs/delete"

    asyncio.run(_test())


def test_raw_and_download(mock_conn, tmp_path):
    async def _test():
        fs = MuseFilesystem(mock_conn)
        mock_conn.request.return_value = RpcResponse(200, [], b"downloaded bytes")

        data = await fs.raw("remote/file.txt", byte_range=(0, 15))
        assert data == b"downloaded bytes"
        mock_conn.request.assert_awaited_with(
            "GET",
            "/fs/raw/remote/file.txt",
            {},
            extra_headers=[("Range", "bytes=0-15")],
            timeout=300,
        )

        out_file = tmp_path / "out" / "file.txt"
        path = await fs.download("remote/file.txt", str(out_file))
        assert path.is_file()
        assert path.read_bytes() == b"downloaded bytes"

    asyncio.run(_test())

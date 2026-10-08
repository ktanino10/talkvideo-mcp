import os

import pytest

from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.storage import LocalStore


def test_read_only_store_does_not_create_output(tmp_path):
    store = LocalStore(tmp_path / "output")
    assert not store.exists("a/b")
    assert not store.root.exists()


def test_atomic_no_overwrite(tmp_path):
    store = LocalStore(tmp_path / "output")
    store.write_bytes("project/revision/one.wav", b"first")
    with pytest.raises(TalkVideoError, match="output_exists"):
        store.write_bytes("project/revision/one.wav", b"second")
    assert store.read_bytes("project/revision/one.wav") == b"first"
    assert not list(store.root.rglob(".tmp-*"))


@pytest.mark.parametrize("path", ["../outside", "a/../../b", "/tmp/out", "a\\b", "a//b", "./b"])
def test_traversal(tmp_path, path):
    with pytest.raises(TalkVideoError, match="unsafe_path"):
        LocalStore(tmp_path / "output").write_bytes(path, b"no")


def test_symlink_directory_file_and_root_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    store = LocalStore(tmp_path / "output")
    store.root.mkdir()
    (store.root / "escape").symlink_to(outside)
    with pytest.raises(TalkVideoError, match="unsafe_path"):
        store.write_bytes("escape/no", b"no")
    (outside / "secret").write_bytes(b"do not read")
    (store.root / "link").symlink_to(outside / "secret")
    with pytest.raises(TalkVideoError, match="unsafe_path"):
        store.read_bytes("link")
    with pytest.raises(TalkVideoError, match="unsafe_path"):
        store.write_bytes("link", b"no", replace=True)
    alias = tmp_path / "alias"
    alias.symlink_to(store.root)
    with pytest.raises(TalkVideoError, match="unsafe_path"):
        LocalStore(alias).read_bytes("link")
    assert (outside / "secret").read_bytes() == b"do not read"


def test_fifo_is_not_read_and_reads_are_bounded(tmp_path):
    store = LocalStore(tmp_path / "output")
    store.root.mkdir()
    os.mkfifo(store.root / "fifo")
    with pytest.raises(TalkVideoError, match="unsafe_path"):
        store.read_bytes("fifo")
    store.write_bytes("large", b"12345")
    with pytest.raises(TalkVideoError, match="file_too_large"):
        store.read_bytes("large", limit=4)


def test_single_writer(tmp_path):
    first = LocalStore(tmp_path / "output")
    second = LocalStore(first.root)
    first.acquire_writer()
    try:
        with pytest.raises(TalkVideoError, match="root_busy"):
            second.acquire_writer()
    finally:
        first.close()
    second.acquire_writer()
    second.close()

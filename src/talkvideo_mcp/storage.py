from __future__ import annotations

import errno
import fcntl
import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.models import MAX_FILE_BYTES, MAX_JSON_BYTES, canonical_bytes

T = TypeVar("T", bound=BaseModel)


def path_error() -> TalkVideoError:
    return TalkVideoError(
        "unsafe_path",
        "A managed path is a symlink, special file, or unsafe relative path.",
        "Use a private local output directory without symlinks; do not alter managed files.",
        needs_user_action=True,
    )


class LocalStore:
    """Descriptor-relative files under a configured, trusted POSIX output root."""

    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self._lock: int | None = None

    def _check_root(self) -> None:
        if ".." in self.root.parts:
            raise path_error()
        for part in (*reversed(self.root.parents), self.root):
            if part.is_symlink():
                raise path_error()

    @staticmethod
    def _parts(relative: str) -> tuple[str, ...]:
        path = PurePosixPath(relative)
        if (
            not relative
            or path.is_absolute()
            or "\\" in relative
            or "\x00" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise path_error()
        return path.parts

    @contextmanager
    def directory(self, relative: str = "", *, create: bool = False) -> Iterator[int]:
        parts = self._parts(relative) if relative else ()
        self._check_root()
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        handles: list[int] = []
        try:
            current = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            handles.append(current)
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                current = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
                )
                handles.append(current)
            yield current
        except FileNotFoundError as exc:
            raise TalkVideoError(
                "not_found",
                "The requested managed file or revision does not exist.",
                "Use the exact reference returned by save_revision or start_job.",
            ) from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise path_error() from exc
            raise
        finally:
            for handle in reversed(handles):
                os.close(handle)

    @contextmanager
    def _parent(self, relative: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
        parts = self._parts(relative)
        with self.directory("/".join(parts[:-1]), create=create) as directory:
            yield directory, parts[-1]

    def exists(self, relative: str) -> bool:
        try:
            with self._parent(relative) as (directory, leaf):
                info = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise path_error()
                return True
        except TalkVideoError as exc:
            if exc.problem.code == "not_found":
                return False
            raise

    def read_bytes(self, relative: str, *, limit: int = MAX_FILE_BYTES) -> bytes:
        with self._parent(relative) as (directory, leaf):
            handle = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(handle, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise path_error()
                if info.st_size > limit:
                    raise TalkVideoError(
                        "file_too_large",
                        "A managed file exceeds the bounded read limit.",
                        "Inspect storage and create a new revision; do not truncate the file.",
                        needs_user_action=True,
                    )
                data = stream.read(limit + 1)
                if len(data) > limit:
                    raise TalkVideoError(
                        "file_too_large",
                        "A managed file grew beyond its read limit.",
                        "Stop external modifications to the output directory.",
                        needs_user_action=True,
                    )
                return data

    def read_model(self, relative: str, model: type[T]) -> T:
        data = self.read_bytes(relative, limit=MAX_JSON_BYTES)
        try:
            return model.model_validate_json(data)
        except ValidationError as exc:
            raise TalkVideoError(
                "invalid_manifest",
                "A managed manifest is invalid or incompatible.",
                "Preserve files for inspection; create a new revision instead of editing state.",
                needs_user_action=True,
            ) from exc

    def write_bytes(self, relative: str, data: bytes, *, replace: bool = False) -> None:
        if len(data) > MAX_FILE_BYTES:
            raise TalkVideoError(
                "file_too_large",
                "Output exceeds the 64 MiB per-file limit.",
                "Use a shorter approved script.",
            )
        with self._parent(relative, create=True) as (directory, leaf):
            temporary = f".tmp-{uuid.uuid4().hex}"
            handle = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if replace:
                    try:
                        existing = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        existing = None
                    if existing is not None and not stat.S_ISREG(existing.st_mode):
                        raise path_error()
                    os.replace(temporary, leaf, src_dir_fd=directory, dst_dir_fd=directory)
                else:
                    try:
                        os.link(
                            temporary,
                            leaf,
                            src_dir_fd=directory,
                            dst_dir_fd=directory,
                            follow_symlinks=False,
                        )
                    except FileExistsError as exc:
                        raise TalkVideoError(
                            "output_exists",
                            "An immutable output already exists and will not be overwritten.",
                            "Inspect/resume the existing job or create a new revision.",
                            needs_user_action=True,
                        ) from exc
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                    os.fsync(directory)
                except FileNotFoundError:
                    pass

    def write_model(self, relative: str, model: BaseModel, *, replace: bool = False) -> None:
        data = canonical_bytes(model)
        if len(data) > MAX_JSON_BYTES:
            raise TalkVideoError(
                "manifest_too_large",
                "The manifest exceeds the 1 MiB limit.",
                "Use fewer cues or a shorter script.",
            )
        self.write_bytes(relative, data, replace=replace)

    def list_names(self, relative: str, *, limit: int) -> list[str]:
        try:
            with self.directory(relative) as directory, os.scandir(directory) as entries:
                result: list[str] = []
                for entry in entries:
                    if entry.name.startswith(".tmp-"):
                        continue
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        raise path_error()
                    result.append(entry.name)
                    if len(result) > limit:
                        raise TalkVideoError(
                            "storage_limit",
                            "The managed job count exceeds the configured limit.",
                            "Use a separate output root or manually archive completed projects.",
                            needs_user_action=True,
                        )
                return sorted(result)
        except TalkVideoError as exc:
            if exc.problem.code == "not_found":
                return []
            raise

    def acquire_writer(self) -> None:
        if self._lock is not None:
            return
        with self.directory(".state", create=True) as directory:
            handle = os.open(
                "writer.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
        try:
            if not stat.S_ISREG(os.fstat(handle).st_mode):
                raise path_error()
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(handle)
            raise TalkVideoError(
                "root_busy",
                "Another process owns this output root.",
                "Use that server session or wait for it to exit; do not delete its lock.",
            ) from exc
        except BaseException:
            os.close(handle)
            raise
        self._lock = handle

    @property
    def writer_held(self) -> bool:
        return self._lock is not None

    def close(self) -> None:
        if self._lock is not None:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            os.close(self._lock)
            self._lock = None

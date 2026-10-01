from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PreparedWrite:
    path: Path
    temporary: Path
    sha256: str
    size: int
    device: int
    inode: int

    def validate(self, path: Path, content: bytes) -> None:
        stat = self.temporary.stat()
        with self.temporary.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if (
            path != self.path or self.temporary.is_symlink()
            or (stat.st_dev, stat.st_ino, stat.st_size) != (self.device, self.inode, self.size)
            or digest != self.sha256 or hashlib.sha256(content).hexdigest() != digest
        ):
            raise ValueError("prepared output changed; publication refused")


@contextmanager
def prepare_write_bytes(
    path: Path, content: bytes, *, mode: int = 0o600,
) -> Iterator[PreparedWrite]:
    """Prepare a complete file whose identity can be recorded before any publication."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        stat = temporary.stat()
        yield PreparedWrite(path, temporary, hashlib.sha256(content).hexdigest(), len(content),
                            stat.st_dev, stat.st_ino)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write_bytes(
    path: Path, content: bytes, *, mode: int = 0o600, overwrite: bool = True,
    before_publish: Callable[[], None] | None = None,
    after_publish: Callable[[], None] | None = None,
    prepared: PreparedWrite | None = None,
) -> None:
    """Publish a complete file, optionally refusing any existing destination."""
    if prepared is None:
        with prepare_write_bytes(path, content, mode=mode) as ready:
            atomic_write_bytes(path, content, overwrite=overwrite, before_publish=before_publish,
                               after_publish=after_publish, prepared=ready)
    else:
        prepared.validate(path, content)
        if before_publish is not None:
            before_publish()
        if overwrite:
            os.replace(prepared.temporary, path)
        else:
            # Linking in the same directory fails atomically if a destination appears.
            # Unlike an existence check followed by replace, it cannot clobber a racer.
            os.link(prepared.temporary, path)
        if after_publish is not None:
            after_publish()


def atomic_write_text(path: Path, content: str, *, overwrite: bool = True) -> None:
    atomic_write_bytes(path, content.encode("utf-8"), overwrite=overwrite)

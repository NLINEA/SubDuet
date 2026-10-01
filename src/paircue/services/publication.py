from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from paircue.models import ProcessResult
from paircue.services.atomic import PreparedWrite
from paircue.services.readability import ReadabilityIssue, ReadabilityMetric
from paircue.services.review import ReviewDetails


class ProofModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class IssueProof(ProofModel):
    output_cue: int = Field(ge=1)
    metric: ReadabilityMetric
    measured: int = Field(ge=0)
    limit: int = Field(ge=1)


class ReviewProof(ProofModel):
    source_only: tuple[int, ...] = ()
    target_only: tuple[int, ...] = ()
    readability: tuple[IssueProof, ...] = ()

    def details(self) -> ReviewDetails:
        return ReviewDetails(self.source_only, self.target_only, tuple(
            ReadabilityIssue(issue.output_cue, issue.metric, issue.measured, issue.limit)
            for issue in self.readability
        ))


class OutputProof(ProofModel):
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    device: int | None = None
    inode: int | None = None
    overwrite: bool = False

    @classmethod
    def planned(cls, path: Path, content: bytes, overwrite: bool) -> OutputProof:
        return cls(path=path, sha256=hashlib.sha256(content).hexdigest(), size=len(content),
                   overwrite=overwrite)

    def prepared(self, ready: PreparedWrite) -> OutputProof:
        if not ready.inode or not ready.device:
            raise ValueError("stable prepared-file identity unavailable; publication refused")
        if (ready.path, ready.sha256, ready.size) != (self.path, self.sha256, self.size):
            raise ValueError("prepared output differs from publication intent")
        return self.model_copy(update={"device": ready.device, "inode": ready.inode})

    def matches(self) -> bool:
        # Hash alone cannot distinguish our link from a racer's identical copy.
        if self.device is None or self.inode is None:
            return False
        nonblocking = getattr(os, "O_NONBLOCK", None)
        if nonblocking is None:
            return False  # Never risk a blocking open on a filesystem object substitution.
        identity = self.device, self.inode, self.size
        try:
            initial = self.path.lstat()
            if (not stat.S_ISREG(initial.st_mode)
                    or (initial.st_dev, initial.st_ino, initial.st_size) != identity):
                return False
            flags = os.O_RDONLY | nonblocking | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags)
            try:
                before = os.fstat(descriptor)
                if (not stat.S_ISREG(before.st_mode)
                        or (before.st_dev, before.st_ino, before.st_size) != identity):
                    return False
                # The descriptor is now verified regular; it cannot turn into a FIFO.
                os.set_blocking(descriptor, True)
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                    after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            current = self.path.lstat()
        except OSError:
            return False
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        return (
            digest == self.sha256 and stat.S_ISREG(current.st_mode)
            and all(getattr(before, key) == getattr(after, key) == getattr(current, key)
                    for key in fields)
        )


class PublicationProof(ProofModel):
    schema_version: Literal[1] = 1
    media_path: Path
    job_id: str
    media_hash: str
    settings_hash: str
    recipe_version: str
    validation_version: str
    original_tracks: dict[str, str]
    expected_tracks: dict[str, str]
    outputs: tuple[OutputProof, ...]
    final_path: Path | None = None
    result_outputs: tuple[Path, ...]
    message: str
    review: ReviewProof | None = None

    @classmethod
    def review_from(cls, result: ProcessResult) -> ReviewProof | None:
        return (ReviewProof.model_validate(asdict(result.review_details))
                if result.review_details is not None else None)

    def valid_location(self, path: Path) -> bool:
        if path != self.media_path:
            return False
        paths = {path.with_name(path.stem + label) for label in self.expected_tracks}
        for output in self.outputs:
            if (output.path.parent != path.parent or output.path.suffix != ".srt"
                    or not output.path.name.startswith(path.stem + ".")):
                return False
            paths.add(output.path)
        return set(self.result_outputs) <= paths and (
            self.final_path is None or self.final_path in {output.path for output in self.outputs}
        )

    def result(self) -> ProcessResult:
        return ProcessResult("completed", self.message, self.result_outputs,
                             self.review.details() if self.review is not None else None)

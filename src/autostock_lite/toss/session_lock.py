"""프로세스마다 하나의 토스 세션만 사용하도록 보장하는 OS 잠금."""

from __future__ import annotations

from datetime import datetime, timezone
import errno
import json
import logging
import math
import os
from pathlib import Path
import threading
import time
from typing import BinaryIO

from .. import paths


logger = logging.getLogger(__name__)


if os.name == "nt":
    import msvcrt
elif os.name == "posix":
    import fcntl


class TossSessionLockError(RuntimeError):
    """토스 세션 잠금 오류."""


class TossSessionBusy(TossSessionLockError):
    """제한 시간 안에 토스 세션 잠금을 얻지 못한 경우."""


class TossSessionReentryError(TossSessionLockError):
    """같은 프로세스에서 같은 경로의 잠금을 다시 획득한 경우."""


_PROCESS_LOCKS: set[tuple[int, str]] = set()
_PROCESS_LOCKS_GUARD = threading.Lock()
_POLL_INTERVAL_SECONDS = 0.05


def _default_lock_path() -> Path:
    return paths.toss_session_lock_path()


class TossSessionLock:
    """토스 접근 프로세스를 하나로 제한하는 배타 잠금.

    같은 프로세스에서 같은 객체나 같은 경로의 다른 객체로 재진입하면
    :class:`TossSessionReentryError`를 즉시 발생시킨다. 운영체제별 재진입
    의미 차이와 조용한 교착을 피하기 위해 재진입은 허용하지 않는다.

    잠금 파일의 기존 내용이나 pid로 stale 여부를 판단하지 않는다. 프로세스가
    끝나면 운영체제가 잠금을 해제하며, 파일에는 마지막 획득자의 pid와 UTC
    시작 시각만 남는다.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        timeout: float = 0.0,
    ) -> None:
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout은 0 이상의 유한한 초 단위 값이어야 합니다")
        raw_path = Path(path) if path is not None else _default_lock_path()
        self.path = raw_path.expanduser().resolve(strict=False)
        self.timeout = timeout
        self._file: BinaryIO | None = None
        self._process_key: tuple[int, str] | None = None

    def acquire(self) -> TossSessionLock:
        """제한 시간 안에 OS 잠금을 획득하고 자신을 반환한다."""

        process_key = (os.getpid(), os.path.normcase(str(self.path)))
        with _PROCESS_LOCKS_GUARD:
            if process_key in _PROCESS_LOCKS:
                raise TossSessionReentryError(
                    "같은 프로세스에서 같은 토스 세션 잠금을 재진입할 수 없습니다"
                )
            _PROCESS_LOCKS.add(process_key)

        handle: BinaryIO | None = None
        native_lock_held = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                handle = self.path.open("x+b")
            except FileExistsError:
                handle = self.path.open("r+b")

            deadline = time.monotonic() + self.timeout
            while True:
                if self._try_native_lock(handle):
                    native_lock_held = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TossSessionBusy(
                        "다른 프로세스가 토스 세션 잠금을 사용 중입니다"
                    )
                time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))

            self._write_metadata(handle)
            self._file = handle
            self._process_key = process_key
            return self
        except BaseException:
            try:
                if native_lock_held and handle is not None:
                    self._unlock_native(handle)
            finally:
                try:
                    if handle is not None:
                        try:
                            handle.close()
                        except OSError:
                            logger.warning(
                                "세션 잠금 파일 close 실패",
                                exc_info=True,
                            )
                finally:
                    with _PROCESS_LOCKS_GUARD:
                        _PROCESS_LOCKS.discard(process_key)
            raise

    def release(self) -> None:
        """현재 객체가 보유한 OS 잠금을 해제한다."""

        handle = self._file
        process_key = self._process_key
        if handle is None or process_key is None:
            return
        self._file = None
        self._process_key = None
        try:
            self._unlock_native(handle)
        finally:
            try:
                handle.close()
            finally:
                with _PROCESS_LOCKS_GUARD:
                    _PROCESS_LOCKS.discard(process_key)

    def __enter__(self) -> TossSessionLock:
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            self.release()
        except OSError:
            if exc_type is None:
                raise
            logger.warning("세션 잠금 해제 실패", exc_info=True)

    @staticmethod
    def _try_native_lock(handle: BinaryIO) -> bool:
        handle.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            elif os.name == "posix":
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover - 지원 대상은 Windows와 POSIX뿐이다.
                raise TossSessionLockError("지원하지 않는 운영체제입니다")
        except OSError as exc:
            busy_errnos = {errno.EACCES, errno.EAGAIN}
            if exc.errno in busy_errnos:
                return False
            raise
        return True

    @staticmethod
    def _unlock_native(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        elif os.name == "posix":
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _write_metadata(handle: BinaryIO) -> None:
        metadata = {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        encoded = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
        payload = encoded + b"\n"
        handle.seek(0)
        handle.write(payload)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())

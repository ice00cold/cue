"""LockedYamlStore — the persistence skeleton the per-profile YAML state stores share.

``GoalsStore`` (``<home>/goals.yaml``) and ``DecisionsStore`` (``<home>/decisions.yaml``)
are read-modify-write YAML state mutated from agent turns, slash commands and cron ticks
alike, so they share one skeleton: atomic replace under an exclusive flock on a sidecar
``.lock`` file (an agent turn and a cron review never interleave writes), a corrupt-file
backup + fresh start, and the ``_mutate`` read-modify-write cycle. Subclasses own the file
name, the state shape (``_empty_state`` / ``_normalize_state``) and the record logic.
"""

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from utils import atomic_write_text

logger = logging.getLogger("tools.yaml_state_store")

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt  # noqa: F401
except ImportError:
    msvcrt = None


class LockedYamlStore:
    """One locked YAML state file under a profile home (``get_hermes_home()`` by default)."""

    #: File name inside the home, e.g. ``"goals.yaml"`` — subclasses must set it.
    file_name: str = ""

    def __init__(self, home: Optional[Path] = None):
        if home is None:
            from hermes_constants import get_hermes_home

            home = get_hermes_home()
        self._home = Path(home)

    # ---- paths & locking ------------------------------------------------

    @property
    def path(self) -> Path:
        return self._home / self.file_name

    @contextmanager
    def _file_lock(self):
        """Exclusive lock on a separate .lock file (the store itself is atomically replaced)."""
        from hermes_constants import mkdir_under_hermes_home

        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        if fcntl is None and msvcrt is None:
            yield
            return
        mkdir_under_hermes_home(lock_path.parent)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        raw_fd = os.open(lock_path, flags, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(raw_fd, 0o600)
            fd = os.fdopen(raw_fd, "r+", encoding="utf-8")
        except Exception:
            os.close(raw_fd)
            raise
        with fd:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                try:
                    if fcntl:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    else:
                        fd.seek(0)
                        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass

    # ---- raw state ------------------------------------------------------

    @staticmethod
    def _empty_state() -> Dict[str, Any]:
        """State for a missing/unreadable file; subclasses override."""
        return {}

    @staticmethod
    def _normalize_state(state: Dict[str, Any]) -> Dict[str, Any]:
        """Repair a just-loaded state dict (setdefaults, key checks); subclasses override."""
        return state

    def load(self) -> Dict[str, Any]:
        """Whole store state; missing file → empty, unreadable file → backed up + empty."""
        from hermes_yaml import YAMLError, safe_load

        try:
            raw = self.path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return self._empty_state()
        except OSError:
            logger.warning("%s unreadable (%s); treating as empty", self.file_name, self.path, exc_info=True)
            return self._empty_state()
        if not raw.strip():
            return self._empty_state()
        try:
            state = safe_load(raw)
        except YAMLError:
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            try:
                os.replace(self.path, backup)
                logger.warning(
                    "%s was not valid YAML; moved to %s and started fresh", self.file_name, backup,
                    exc_info=True)
            except OSError:
                logger.warning("%s was not valid YAML and could not be backed up", self.file_name, exc_info=True)
            return self._empty_state()
        if not isinstance(state, dict):
            return self._empty_state()
        return self._normalize_state(state)

    def save(self, state: Dict[str, Any]) -> None:
        from hermes_constants import mkdir_under_hermes_home
        from hermes_yaml import safe_dump

        mkdir_under_hermes_home(self.path.parent)
        atomic_write_text(self.path, safe_dump(state, sort_keys=False), create_mode=0o600)

    def _mutate(self, mutate):
        """Run ``mutate(state) -> result`` under the lock on a fresh read."""
        with self._file_lock():
            state = self.load()
            result = mutate(state)
            self.save(state)
            return result

"""Read-only inventory of detached recorder processes."""

from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence, Tuple

import psutil

from utils.username import normalize_tiktok_username


@dataclass(frozen=True)
class RecorderProcess:
    """A recorder process identified from its exact command-line arguments."""

    pid: int
    username: str
    output_file: Optional[str]
    create_time: float
    cmdline: Tuple[str, ...]

    @property
    def fingerprint(self) -> Tuple[int, float, Tuple[str, ...]]:
        """Stable identity used to avoid signaling a reused PID."""
        return self.pid, self.create_time, self.cmdline


def _argument_value(args: Sequence[str], flag: str) -> Optional[str]:
    try:
        index = args.index(flag)
    except ValueError:
        return None

    if index + 1 >= len(args):
        return None
    return args[index + 1]


def parse_recorder_process(
    pid: int,
    cmdline: Sequence[str],
    create_time: float = 0.0,
) -> Optional[RecorderProcess]:
    """Parse one process, returning ``None`` when it is not our recorder."""
    args = tuple(str(argument) for argument in (cmdline or ()))
    if "recorder.main" not in args or "--persistent-mode" not in args:
        return None

    username = _argument_value(args, "-user")
    if normalize_tiktok_username(username, strip_at=False) != username:
        return None

    return RecorderProcess(
        pid=int(pid),
        username=username,
        output_file=_argument_value(args, "--output-file"),
        create_time=float(create_time or 0.0),
        cmdline=args,
    )


def list_recorder_processes(
    process_iter: Optional[Callable[..., Iterable]] = None,
) -> list[RecorderProcess]:
    """List running persistent recorders without consulting SQLite."""
    iterator = process_iter or psutil.process_iter
    recorders: list[RecorderProcess] = []

    try:
        processes = iterator(["pid", "cmdline", "create_time"])
    except TypeError:
        processes = iterator()

    for process in processes:
        try:
            info = process.info
            recorder = parse_recorder_process(
                info["pid"],
                info.get("cmdline") or (),
                info.get("create_time") or 0.0,
            )
            if recorder is not None:
                recorders.append(recorder)
        except (KeyError, psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue

    return sorted(recorders, key=lambda recorder: recorder.pid)

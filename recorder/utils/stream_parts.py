from pathlib import Path
from typing import Iterator


def stream_part_path(output: str, part_number: int) -> str:
    """Return the deterministic output path for one reconnect segment."""
    if part_number < 1:
        raise ValueError("part_number must be at least 1")

    path = Path(output)
    return str(path.with_name(
        f"{path.stem}_part{part_number:03d}{path.suffix}"
    ))


def existing_stream_parts(output: str) -> Iterator[str]:
    """Yield contiguous reconnect parts without scanning the directory."""
    base = Path(output)
    first_part = Path(stream_part_path(output, 1))

    # New layout: the unsuffixed path is reserved for the compressor output.
    if first_part.exists():
        part_number = 1
        while True:
            part = Path(stream_part_path(output, part_number))
            if not part.exists():
                return
            yield str(part)
            part_number += 1

    # Compatibility with recorders started before part001 was introduced.
    if base.exists():
        yield str(base)
    part_number = 2
    while True:
        part = Path(stream_part_path(output, part_number))
        if not part.exists():
            return
        yield str(part)
        part_number += 1

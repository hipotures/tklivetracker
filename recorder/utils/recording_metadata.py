import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .stream_parts import existing_stream_parts


def validate_metadata_path(metadata_path: str) -> Path:
    """Create the metadata directory and verify that this user can write to it."""
    destination = Path(metadata_path).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    probe = destination / f".ttracker-write-test.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with probe.open("x", encoding="utf-8") as probe_file:
            probe_file.write("{}\n")
            probe_file.flush()
            os.fsync(probe_file.fileno())
    finally:
        probe.unlink(missing_ok=True)
    return destination


def write_recording_metadata(
    output: str,
    username: str,
    metadata_path: str,
    compressed_output_path: str,
) -> Optional[Path]:
    """Atomically publish one metadata document for closed stream parts."""
    logical_output = Path(output).resolve()
    inputs = [Path(path).resolve() for path in existing_stream_parts(str(logical_output))]
    input_stats = [path.stat() for path in inputs]
    if not input_stats or not any(file_stat.st_size > 0 for file_stat in input_stats):
        return None

    recording_id = logical_output.stem
    request_id = f"ttracker-{recording_id}"
    compressed_output = (
        Path(compressed_output_path).expanduser().resolve()
        / logical_output.parent.name
        / logical_output.name
    )
    document = {
        "schema_version": 1,
        "request_id": request_id,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "producer": {
            "app": "ttracker",
            "username": username,
            "recording_id": recording_id,
            "source_size_bytes": sum(file_stat.st_size for file_stat in input_stats),
            "source_latest_mtime_ns": max(file_stat.st_mtime_ns for file_stat in input_stats),
        },
        "operation": "concat_transcode",
        "inputs": [str(path) for path in inputs],
        "output_path": str(compressed_output),
        "source_policy": "keep",
        "compression_profile": "tiktok",
        "error_policy": {
            "missing_input": "fail",
        },
    }

    destination = Path(metadata_path).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    final_path = destination / f"{request_id}.json"
    temporary_path = destination / f".{request_id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"

    try:
        with temporary_path.open("x", encoding="utf-8") as metadata_file:
            json.dump(document, metadata_file, ensure_ascii=False, indent=2)
            metadata_file.write("\n")
            metadata_file.flush()
            os.fsync(metadata_file.fileno())
        os.replace(temporary_path, final_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    return final_path

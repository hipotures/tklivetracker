import json

import pytest

import recorder.utils.recording_metadata as recording_metadata_module
from persistent_live_manager.live_process_manager import LiveProcessManager
from recorder.core.tiktok_recorder import TikTokRecorder
from recorder.utils.recording_metadata import write_recording_metadata


def test_writes_atomic_metadata_for_ordered_parts(tmp_path):
    recordings = tmp_path / "recordings" / "alice"
    recordings.mkdir(parents=True)
    output = recordings / "alice_20260718_175715.mp4"
    part_one = recordings / "alice_20260718_175715_part001.mp4"
    part_two = recordings / "alice_20260718_175715_part002.mp4"
    part_one.write_bytes(b"first")
    part_two.write_bytes(b"second")
    metadata_path = tmp_path / "metadata"
    compressed_output_path = tmp_path / "compressed"

    manifest_path = write_recording_metadata(
        str(output),
        "alice",
        str(metadata_path),
        str(compressed_output_path),
    )

    assert manifest_path == metadata_path / "ttracker-alice_20260718_175715.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert document["producer"]["username"] == "alice"
    assert document["producer"]["recording_id"] == "alice_20260718_175715"
    assert document["producer"]["source_size_bytes"] == len(b"firstsecond")
    assert document["producer"]["source_latest_mtime_ns"] == max(
        part_one.stat().st_mtime_ns,
        part_two.stat().st_mtime_ns,
    )
    assert document["inputs"] == [str(part_one), str(part_two)]
    assert document["output_path"] == str(
        compressed_output_path / "alice" / "alice_20260718_175715.mp4"
    )
    assert document["source_policy"] == "keep"
    assert document["error_policy"] == {"missing_input": "fail"}
    assert list(metadata_path.glob("*.tmp")) == []


@pytest.mark.parametrize("create_empty_part", [False, True])
def test_does_not_publish_without_nonempty_input(tmp_path, create_empty_part):
    output = tmp_path / "recording.mp4"
    if create_empty_part:
        (tmp_path / "recording_part001.mp4").touch()

    result = write_recording_metadata(
        str(output),
        "alice",
        str(tmp_path / "metadata"),
        str(tmp_path / "compressed"),
    )

    assert result is None
    assert not (tmp_path / "metadata").exists()


def test_process_command_enables_metadata_only_when_configured(tmp_path):
    metadata_path = tmp_path / "metadata"
    manager = LiveProcessManager(object(), {
        "recordings_path": str(tmp_path / "recordings"),
        "segment_on_reconnect": True,
        "metadata_enabled": True,
        "metadata_path": str(metadata_path),
        "compressed_output_path": str(tmp_path / "compressed"),
        "config_path": str(tmp_path / "deployment" / "config.yaml"),
    })

    command = manager._build_recording_command(
        "alice",
        "room",
        tmp_path / "recordings" / "alice",
        tmp_path / "recordings" / "alice" / "alice_20260718_175715.mp4",
    )

    option_index = command.index("--metadata-path")
    assert command[option_index + 1] == str(metadata_path)
    output_option_index = command.index("--compressed-output-path")
    assert command[output_option_index + 1] == str(tmp_path / "compressed")
    config_option_index = command.index("--config")
    assert command[config_option_index + 1] == str(
        tmp_path / "deployment" / "config.yaml"
    )


def test_process_command_does_not_infer_remux_mode_from_username(tmp_path):
    manager = LiveProcessManager(
        object(),
        {"recordings_path": str(tmp_path / "recordings")},
    )

    command = manager._build_recording_command(
        "iphone_streamer",
        "room",
        tmp_path / "recordings" / "iphone_streamer",
        tmp_path / "recordings" / "iphone_streamer" / "recording.mp4",
    )

    assert "--ffmpeg-remux" not in command


def test_ffmpeg_remux_compatibility_flag_uses_raw_stream_writer(monkeypatch):
    calls = []

    def raw_writer(self, live_url, output, buffer_size, buffer, stop, **kwargs):
        calls.append((live_url, output, kwargs["live_url_candidates"]))
        return 12.5

    monkeypatch.setattr(TikTokRecorder, "_record_raw_stream", raw_writer)
    recorder = object.__new__(TikTokRecorder)

    result = recorder._record_with_ffmpeg_remux(
        "https://media.example/stream", "recording.mp4", ["backup"]
    )

    assert result == 12.5
    assert calls == [(
        "https://media.example/stream", "recording.mp4", ["backup"]
    )]


def test_metadata_requires_segmented_recordings(tmp_path):
    with pytest.raises(ValueError, match="requires segment_on_reconnect"):
        LiveProcessManager(object(), {
            "recordings_path": str(tmp_path),
            "segment_on_reconnect": False,
            "metadata_enabled": True,
            "metadata_path": str(tmp_path / "metadata"),
            "compressed_output_path": str(tmp_path / "compressed"),
        })


def test_metadata_path_must_be_writable_when_enabled(tmp_path):
    file_instead_of_directory = tmp_path / "not-a-directory"
    file_instead_of_directory.write_text("occupied", encoding="utf-8")

    with pytest.raises(ValueError, match="metadata_path is not writable"):
        LiveProcessManager(object(), {
            "recordings_path": str(tmp_path),
            "segment_on_reconnect": True,
            "metadata_enabled": True,
            "metadata_path": str(file_instead_of_directory / "metadata"),
            "compressed_output_path": str(tmp_path / "compressed"),
        })


def test_metadata_requires_compressed_output_path(tmp_path):
    with pytest.raises(ValueError, match="compressed_output_path is required"):
        LiveProcessManager(object(), {
            "recordings_path": str(tmp_path),
            "segment_on_reconnect": True,
            "metadata_enabled": True,
            "metadata_path": str(tmp_path / "metadata"),
        })


def test_publish_error_removes_temporary_document(monkeypatch, tmp_path):
    output = tmp_path / "recording.mp4"
    (tmp_path / "recording_part001.mp4").write_bytes(b"video")
    metadata_path = tmp_path / "metadata"

    def fail_replace(_source, _destination):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(recording_metadata_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="No space left on device"):
        write_recording_metadata(
            str(output),
            "alice",
            str(metadata_path),
            str(tmp_path / "compressed"),
        )

    assert list(metadata_path.iterdir()) == []

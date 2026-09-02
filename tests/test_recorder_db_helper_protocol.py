import pytest

from recorder.main import cleanup_own_zero_byte_output, extract_db_helper_live_id


def test_extract_db_helper_live_id_ignores_surrounding_output():
    output = "diagnostic before\n{\"live_id\": 218700}\ndiagnostic after\n"

    assert extract_db_helper_live_id(output) == 218700


def test_extract_db_helper_live_id_rejects_missing_result():
    with pytest.raises(ValueError, match="does not contain a live_id"):
        extract_db_helper_live_id("diagnostic only")


def test_recorder_removes_only_its_exact_empty_mp4(tmp_path):
    exact_output = tmp_path / "exact.mp4"
    unrelated_output = tmp_path / "unrelated.mp4"
    exact_output.touch()
    unrelated_output.touch()

    assert cleanup_own_zero_byte_output(str(exact_output)) is True
    assert not exact_output.exists()
    assert unrelated_output.exists()


def test_recorder_keeps_nonempty_output(tmp_path):
    output = tmp_path / "recording.mp4"
    output.write_bytes(b"video")

    assert cleanup_own_zero_byte_output(str(output)) is False
    assert output.read_bytes() == b"video"

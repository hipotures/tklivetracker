import logging
from pathlib import Path

from persistent_live_manager.recording_health_monitor import RecordingHealthMonitor
from recorder.core.tiktok_recorder import TikTokRecorder
from recorder.utils.stream_parts import stream_part_path


class ImmediateStopEvent:
    def is_set(self):
        return False

    def wait(self, timeout):
        assert timeout == 5
        return False


class ReconnectingTikTok:
    def __init__(self, reconnect_succeeds=True):
        self.reconnect_succeeds = reconnect_succeeds
        self.download_calls = 0
        self.live_checks = iter((True, True, False))

    def is_room_alive(self, _room_id):
        return next(self.live_checks)

    def download_live_stream(self, _live_url, _stop_event):
        self.download_calls += 1
        if self.download_calls == 1:
            yield b"first-connection"
            raise ConnectionError("disconnected")
        if not self.reconnect_succeeds:
            raise ConnectionError("still disconnected")
        yield b"second-connection"


class NoDataTikTok:
    def __init__(self):
        self.live_checks = iter((True, True, False))

    def is_room_alive(self, _room_id):
        return next(self.live_checks)

    def download_live_stream(self, _live_url, _stop_event):
        raise ConnectionError("no stream data")
        yield


class NoInitialLiveUrlTikTok:
    def get_live_url_candidates(self, _room_id):
        return []


class PersistentlyFailingTikTok:
    def __init__(self):
        self.download_calls = 0
        self.refresh_calls = 0

    def is_room_alive(self, _room_id):
        return True

    def download_live_stream(self, _live_url, _stop_event):
        self.download_calls += 1
        raise ConnectionError("no stream data")
        yield

    def get_live_url(self, _room_id):
        self.refresh_calls += 1
        return f"https://stream-{self.refresh_calls}"


class RefreshingTikTok:
    def __init__(self):
        self.live_checks = iter((True, True, True, True, False))
        self.download_urls = []
        self.refresh_calls = 0

    def is_room_alive(self, _room_id):
        return next(self.live_checks)

    def download_live_stream(self, live_url, _stop_event):
        self.download_urls.append(live_url)
        if len(self.download_urls) <= 3:
            raise ConnectionError("expired stream URL")
        yield b"recovered-stream"

    def get_live_url(self, _room_id):
        self.refresh_calls += 1
        return "https://new-stream"


class EofRefreshingTikTok:
    def __init__(self):
        self.live_checks = iter((True, True, True, False))
        self.download_urls = []
        self.refresh_calls = 0

    def is_room_alive(self, _room_id):
        return next(self.live_checks)

    def download_live_stream(self, live_url, _stop_event):
        self.download_urls.append(live_url)
        yield live_url.encode()

    def get_live_url(self, _room_id):
        self.refresh_calls += 1
        return "https://new-stream"


class AlternateCdnTikTok:
    def __init__(self):
        self.live_checks = iter((True, True, False))
        self.download_urls = []

    def is_room_alive(self, _room_id):
        return next(self.live_checks)

    def download_live_stream(self, live_url, _stop_event):
        self.download_urls.append(live_url)
        if live_url == "https://bad-cdn/stream.flv":
            raise ConnectionError("Live stream endpoint returned HTTP 404")
        yield b"alternate-cdn-stream"


class RefreshedCdnTikTok:
    def __init__(self):
        self.live_checks = iter((True, True, True, False))
        self.download_urls = []
        self.refresh_calls = 0

    def is_room_alive(self, _room_id):
        return next(self.live_checks)

    def download_live_stream(self, live_url, _stop_event):
        self.download_urls.append(live_url)
        if live_url != "https://refreshed-cdn/stream.flv":
            raise ConnectionError("Live stream endpoint returned HTTP 404")
        yield b"refreshed-cdn-stream"

    def get_live_url_candidates(self, _room_id):
        self.refresh_calls += 1
        return ["https://refreshed-cdn/stream.flv"]


def make_recorder(tiktok, segment_on_reconnect=True):
    recorder = TikTokRecorder.__new__(TikTokRecorder)
    recorder.tiktok = tiktok
    recorder.room_id = "room"
    recorder.duration = None
    recorder.segment_on_reconnect = segment_on_reconnect
    recorder._stop_requested = ImmediateStopEvent()
    recorder.use_telegram = False
    recorder.no_stream_data = False
    return recorder


def test_missing_initial_stream_url_sets_no_stream_data_warning(caplog):
    recorder = make_recorder(NoInitialLiveUrlTikTok())

    with caplog.at_level(logging.WARNING, logger="logger"):
        duration = recorder.start_recording()

    assert duration == 0
    assert recorder.no_stream_data is True
    matching = [
        record
        for record in caplog.records
        if "[NO_STREAM_DATA]" in record.getMessage()
    ]
    assert [record.levelname for record in matching] == ["WARNING"]


def test_successful_reconnect_writes_and_logs_a_new_part(tmp_path, caplog):
    output = tmp_path / "recording.mp4"
    recorder = make_recorder(ReconnectingTikTok())

    with caplog.at_level(logging.INFO, logger="logger"):
        recorder._record_raw_stream(
            "https://stream",
            str(output),
            buffer_size=1024,
            buffer=bytearray(),
            stop_recording=False,
        )

    part_one = stream_part_path(str(output), 1)
    part_two = stream_part_path(str(output), 2)
    assert not output.exists()
    assert Path(part_one).read_bytes() == b"first-connection"
    assert Path(part_two).read_bytes() == b"second-connection"
    assert "[STREAM_PART] opened part=1" in caplog.text
    assert "[STREAM_PART] opened part=2" in caplog.text
    assert "[STREAM_PART] closed part=2" in caplog.text


def test_failed_reconnect_does_not_create_an_empty_part(tmp_path):
    output = tmp_path / "recording.mp4"
    recorder = make_recorder(ReconnectingTikTok(reconnect_succeeds=False))

    recorder._record_raw_stream(
        "https://stream",
        str(output),
        buffer_size=1024,
        buffer=bytearray(),
        stop_recording=False,
    )

    assert not output.exists()
    assert Path(stream_part_path(str(output), 1)).read_bytes() == b"first-connection"
    assert not (tmp_path / "recording_part002.mp4").exists()


def test_failures_before_any_data_do_not_create_the_base_file(tmp_path):
    output = tmp_path / "recording.mp4"
    recorder = make_recorder(NoDataTikTok())

    recorder._record_raw_stream(
        "https://stream",
        str(output),
        buffer_size=1024,
        buffer=bytearray(),
        stop_recording=False,
    )

    assert not output.exists()
    assert not Path(stream_part_path(str(output), 1)).exists()


def test_recorder_stops_after_bounded_retries_without_stream_data(
    tmp_path,
    caplog,
):
    output = tmp_path / "recording.mp4"
    tiktok = PersistentlyFailingTikTok()
    recorder = make_recorder(tiktok)

    with caplog.at_level(logging.WARNING, logger="logger"):
        recorder._record_raw_stream(
            "https://stream",
            str(output),
            buffer_size=1024,
            buffer=bytearray(),
            stop_recording=False,
        )

    assert tiktok.download_calls == 9
    assert tiktok.refresh_calls == 3
    assert recorder.no_stream_data is True
    assert not Path(stream_part_path(str(output), 1)).exists()
    assert "[NO_STREAM_DATA] Recorder produced no stream data" in caplog.text


def test_three_consecutive_failures_refresh_the_stream_url(tmp_path, caplog):
    output = tmp_path / "recording.mp4"
    tiktok = RefreshingTikTok()
    recorder = make_recorder(tiktok)

    with caplog.at_level(logging.INFO, logger="logger"):
        recorder._record_raw_stream(
            "https://old-stream",
            str(output),
            buffer_size=1024,
            buffer=bytearray(),
            stop_recording=False,
        )

    assert tiktok.refresh_calls == 1
    assert tiktok.download_urls == [
        "https://old-stream",
        "https://old-stream",
        "https://old-stream",
        "https://new-stream",
    ]
    assert not output.exists()
    assert Path(stream_part_path(str(output), 1)).read_bytes() == b"recovered-stream"
    assert "[STREAM_URL] refreshed after 3 consecutive failures changed=True" in caplog.text


def test_initial_failure_switches_to_alternate_cdn_without_empty_part(
    tmp_path,
    caplog,
):
    output = tmp_path / "recording.mp4"
    tiktok = AlternateCdnTikTok()
    recorder = make_recorder(tiktok)
    candidates = [
        "https://bad-cdn/stream.flv",
        "https://working-cdn/stream.flv",
    ]

    with caplog.at_level(logging.INFO, logger="logger"):
        recorder._record_raw_stream(
            candidates[0],
            str(output),
            buffer_size=1024,
            buffer=bytearray(),
            stop_recording=False,
            live_url_candidates=candidates,
        )

    assert tiktok.download_urls == candidates
    assert not output.exists()
    assert (
        Path(stream_part_path(str(output), 1)).read_bytes()
        == b"alternate-cdn-stream"
    )
    assert not (tmp_path / "recording_part002.mp4").exists()
    assert "[STREAM_URL] switched to candidate 2/2" in caplog.text


def test_exhausted_candidates_refresh_payload_before_no_data_failure(tmp_path):
    output = tmp_path / "recording.mp4"
    tiktok = RefreshedCdnTikTok()
    recorder = make_recorder(tiktok)
    candidates = [
        "https://bad-cdn-1/stream.flv",
        "https://bad-cdn-2/stream.flv",
    ]

    recorder._record_raw_stream(
        candidates[0],
        str(output),
        buffer_size=1024,
        buffer=bytearray(),
        stop_recording=False,
        live_url_candidates=candidates,
    )

    assert tiktok.download_urls == [
        *candidates,
        "https://refreshed-cdn/stream.flv",
    ]
    assert tiktok.refresh_calls == 1
    assert recorder.no_stream_data is False
    assert (
        Path(stream_part_path(str(output), 1)).read_bytes()
        == b"refreshed-cdn-stream"
    )


def test_successful_stream_eof_refreshes_url_before_reconnect(tmp_path, caplog):
    output = tmp_path / "recording.mp4"
    tiktok = EofRefreshingTikTok()
    recorder = make_recorder(tiktok)

    with caplog.at_level(logging.INFO, logger="logger"):
        recorder._record_raw_stream(
            "https://old-stream",
            str(output),
            buffer_size=1024,
            buffer=bytearray(),
            stop_recording=False,
        )

    assert tiktok.refresh_calls == 1
    assert tiktok.download_urls == [
        "https://old-stream",
        "https://new-stream",
    ]
    assert (
        Path(stream_part_path(str(output), 1)).read_bytes()
        == b"https://old-stream"
    )
    assert (
        Path(stream_part_path(str(output), 2)).read_bytes()
        == b"https://new-stream"
    )
    assert "[STREAM_URL] refreshed after stream EOF changed=True" in caplog.text


def test_disabled_option_preserves_single_file_behavior(tmp_path):
    output = tmp_path / "recording.mp4"
    recorder = make_recorder(
        ReconnectingTikTok(),
        segment_on_reconnect=False,
    )

    recorder._record_raw_stream(
        "https://stream",
        str(output),
        buffer_size=1024,
        buffer=bytearray(),
        stop_recording=False,
    )

    assert output.read_bytes() == b"first-connectionsecond-connection"
    assert not (tmp_path / "recording_part002.mp4").exists()


def test_health_progress_combines_base_file_and_contiguous_parts(tmp_path):
    output = tmp_path / "recording.mp4"
    (tmp_path / "recording_part001.mp4").write_bytes(b"first")
    (tmp_path / "recording_part002.mp4").write_bytes(b"second")
    monitor = RecordingHealthMonitor(
        metadata_store=object(),
        config={"segment_on_reconnect": True},
    )

    size, _mtime = monitor._recording_file_progress(str(output))

    assert size == len(b"firstsecond")


def test_health_progress_supports_legacy_base_and_parts(tmp_path):
    output = tmp_path / "recording.mp4"
    output.write_bytes(b"legacy-first")
    (tmp_path / "recording_part002.mp4").write_bytes(b"legacy-second")
    monitor = RecordingHealthMonitor(
        metadata_store=object(),
        config={"segment_on_reconnect": True},
    )

    size, _mtime = monitor._recording_file_progress(str(output))

    assert size == len(b"legacy-firstlegacy-second")


def test_part_suffix_is_added_after_a_username_containing_part_text():
    output = "/recordings/user_part003/user_part003_20260718_164214.mp4"

    assert stream_part_path(output, 1).endswith(
        "/user_part003_20260718_164214_part001.mp4"
    )

from recorder.utils.flv_video import FLVVideoMonitor, flv_header_has_video


def flv_tag(tag_type, payload=b"x"):
    header = bytes([tag_type]) + len(payload).to_bytes(3, "big") + b"\0" * 7
    return header + payload + (11 + len(payload)).to_bytes(4, "big")


def flv_stream(flags, *tag_types):
    return (
        b"FLV\x01"
        + bytes([flags])
        + (9).to_bytes(4, "big")
        + b"\0" * 4
        + b"".join(flv_tag(tag_type) for tag_type in tag_types)
    )


def test_header_reports_audio_only_and_audio_video():
    assert flv_header_has_video(flv_stream(0x04)) is False
    assert flv_header_has_video(flv_stream(0x05)) is True
    assert flv_header_has_video(b"not-flv") is None


def test_monitor_parses_split_header_and_video_tag():
    payload = flv_stream(0x05, 8, 9)
    monitor = FLVVideoMonitor(10, 30)

    assert monitor.feed(payload[:2], now=0) is None
    assert monitor.feed(payload[2:15], now=1) is None
    assert monitor.feed(payload[15:], now=2) is None
    assert monitor.video_seen is True
    assert monitor.validated is True


def test_monitor_rejects_audio_only_header_immediately():
    monitor = FLVVideoMonitor(10, 30)

    assert monitor.feed(flv_stream(0x04, 8), now=0) == "audio_only"
    assert monitor.validated is False


def test_monitor_detects_missing_initial_video_after_timeout():
    monitor = FLVVideoMonitor(10, 30)

    assert monitor.feed(flv_stream(0x05, 8), now=0) is None
    assert monitor.feed(flv_tag(8), now=11) == "video_start_timeout"


def test_monitor_detects_video_stall_while_audio_continues():
    monitor = FLVVideoMonitor(10, 30)

    assert monitor.feed(flv_stream(0x05, 9), now=0) is None
    assert monitor.video_seen is True
    assert monitor.feed(flv_tag(8), now=31) == "video_stalled"


def test_non_flv_media_is_left_to_the_existing_download_validation():
    monitor = FLVVideoMonitor(10, 30)

    assert monitor.feed(b"not-an-flv-stream", now=0) is None
    assert monitor.validated is True

"""Incremental FLV video-track validation used by recorder health checks."""

from __future__ import annotations

import time
from typing import Optional


FLV_AUDIO_FLAG = 0x04
FLV_VIDEO_FLAG = 0x01
FLV_VIDEO_TAG = 0x09


def flv_header_has_video(header: bytes) -> Optional[bool]:
    """Return the FLV video flag, or ``None`` for short/non-FLV input."""
    if len(header) < 9 or header[:3] != b"FLV":
        return None
    return bool(header[4] & FLV_VIDEO_FLAG)


def flv_header_has_audio(header: bytes) -> Optional[bool]:
    """Return the FLV audio flag, or ``None`` for short/non-FLV input."""
    if len(header) < 9 or header[:3] != b"FLV":
        return None
    return bool(header[4] & FLV_AUDIO_FLAG)


class FLVVideoMonitor:
    """Track FLV video tags without buffering media payloads."""

    def __init__(
        self,
        start_timeout: float,
        stall_timeout: float,
        *,
        clock=time.monotonic,
    ) -> None:
        self.start_timeout = float(start_timeout)
        self.stall_timeout = float(stall_timeout)
        self._clock = clock
        self._first_byte_at: Optional[float] = None
        self._last_video_at: Optional[float] = None
        self._header = bytearray()
        self._tag_header = bytearray()
        self._skip_bytes = 0
        self._state = "header"
        self.is_flv: Optional[bool] = None
        self.has_audio: Optional[bool] = None
        self.has_video_flag: Optional[bool] = None
        self.video_seen = False

    @property
    def validated(self) -> bool:
        """Whether this connection is non-FLV or has produced a video tag."""
        return self.is_flv is False or self.video_seen

    def feed(self, data: bytes, *, now: Optional[float] = None) -> Optional[str]:
        """Consume bytes and return a video issue code when one is detected."""
        if not data:
            return self.issue_at_eof(now=now)

        current_time = self._clock() if now is None else now
        if self._first_byte_at is None:
            self._first_byte_at = current_time

        if self.is_flv is False:
            return None

        offset = 0
        data_length = len(data)
        while offset < data_length:
            if self._state == "header":
                needed = 9 - len(self._header)
                take = min(needed, data_length - offset)
                self._header.extend(data[offset:offset + take])
                offset += take

                if len(self._header) >= 3 and self._header[:3] != b"FLV":
                    self.is_flv = False
                    self._state = "done"
                    return None
                if len(self._header) < 9:
                    break

                self.is_flv = True
                self.has_audio = bool(self._header[4] & FLV_AUDIO_FLAG)
                self.has_video_flag = bool(self._header[4] & FLV_VIDEO_FLAG)
                if not self.has_video_flag:
                    return "audio_only" if self.has_audio else "video_missing"

                data_offset = int.from_bytes(self._header[5:9], "big")
                if data_offset < 9:
                    return "invalid_flv"
                self._skip_bytes = data_offset - 9 + 4
                self._state = "skip"
                continue

            if self._state == "skip":
                take = min(self._skip_bytes, data_length - offset)
                self._skip_bytes -= take
                offset += take
                if self._skip_bytes == 0:
                    self._state = "tag_header"
                continue

            if self._state == "tag_header":
                needed = 11 - len(self._tag_header)
                take = min(needed, data_length - offset)
                self._tag_header.extend(data[offset:offset + take])
                offset += take
                if len(self._tag_header) < 11:
                    break

                tag_type = self._tag_header[0]
                data_size = int.from_bytes(self._tag_header[1:4], "big")
                self._tag_header.clear()
                if tag_type == FLV_VIDEO_TAG:
                    self.video_seen = True
                    self._last_video_at = current_time
                self._skip_bytes = data_size + 4
                self._state = "skip"
                continue

            break

        return self._timeout_issue(current_time)

    def issue_at_eof(self, *, now: Optional[float] = None) -> Optional[str]:
        """Return an issue when an FLV connection ends without any video tag."""
        current_time = self._clock() if now is None else now
        issue = self._timeout_issue(current_time)
        if issue is not None:
            return issue
        if self.is_flv and not self.video_seen:
            return "video_missing"
        return None

    def _timeout_issue(self, now: float) -> Optional[str]:
        if not self.is_flv or self._first_byte_at is None:
            return None
        if not self.video_seen:
            if now - self._first_byte_at >= self.start_timeout:
                return "video_start_timeout"
            return None
        if (
            self._last_video_at is not None
            and now - self._last_video_at >= self.stall_timeout
        ):
            return "video_stalled"
        return None

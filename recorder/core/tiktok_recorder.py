import os
import time
import threading
from http.client import HTTPException

from requests import RequestException

from .tiktok_api import TikTokAPI # Relative import (sibling)
from ..utils.logger_manager import logger # Relative import (up one level)
from ..utils.custom_exceptions import UserLiveException, \
    TikTokException # Relative import (up one level)
from ..utils.enums import Mode, TikTokError # Relative import (up one level)
from ..utils.stream_parts import stream_part_path


class TikTokRecorder:
    # Use __slots__ to reduce memory usage
    __slots__ = ('tiktok', 'url', 'user', 'room_id', 'mode', 'duration',
                'output', 'use_telegram', 'is_exact_file_path', 'ffmpeg_remux',
                'segment_on_reconnect', 'tiktok_stream_id', 'tiktok_started_at',
                'tiktok_owner_user_id', '_stop_requested', 'no_stream_data',
                'config_path')

    # Constants for dynamic sleep in automatic mode
    INITIAL_SLEEP_DURATION = 1
    MAX_SLEEP_DURATION = 128
    SLEEP_INCREMENT_FACTOR = 2
    MIN_RECORDING_DURATION_FOR_RESET = 60 # Min duration (seconds) to reset sleep timer
    CONNECTION_ERROR_SLEEP = 60 # Sleep duration after connection errors
    STREAM_FAILURES_BEFORE_URL_REFRESH = 3
    MAX_INITIAL_STREAM_URL_REFRESHES = 3

    def _upload_to_telegram(self, output: str) -> None:
        from ..upload.telegram import Telegram

        Telegram(self.config_path).upload(output)

    def __init__(
        self,
        url,
        user,
        room_id,
        mode,
        cookies,
        proxy,
        output,
        duration,
        use_telegram,
        is_exact_file_path=False,
        ffmpeg_remux=False,
        segment_on_reconnect=False,
        config_path="config.yaml",
    ):
        # Setup TikTok API client
        # Use the imported class directly
        self.tiktok = TikTokAPI(proxy=proxy, cookies=cookies)

        # TikTok Data
        self.url = url
        self.user = user
        self.room_id = room_id
        self.tiktok_stream_id = None
        self.tiktok_started_at = None
        self.tiktok_owner_user_id = None

        # Tool Settings
        self.mode = mode
        self.duration = duration
        self.output = output
        self.is_exact_file_path = is_exact_file_path
        self.ffmpeg_remux = ffmpeg_remux
        self.segment_on_reconnect = segment_on_reconnect

        # Upload Settings
        self.use_telegram = use_telegram
        self.config_path = config_path

        # Signal handling for graceful shutdown
        self._stop_requested = threading.Event()
        self.no_stream_data = False

        # Check if the user's country is blacklisted
        self.check_country_blacklisted()

        # Get live information based on the provided user data
        if self.url:
            self.user, self.room_id = \
                self.tiktok.get_room_and_user_from_url(self.url)

        if not self.user:
            self.user = self.tiktok.get_user_from_room_id(self.room_id)

        if not self.room_id:
            self.room_id = self.tiktok.get_room_id_from_user(self.user) # Corrected method call

        live_identity = self.tiktok.live_identity or {}
        self.tiktok_stream_id = live_identity.get('stream_id')
        self.tiktok_started_at = live_identity.get('start_time')
        self.tiktok_owner_user_id = live_identity.get('owner_user_id')
        live_status = live_identity.get('status')
        if live_status is not None and live_status not in (2, '2'):
            logger.debug(
                f"[LIVE_IDENTITY] Refusing recorder startup for explicit "
                f"non-live status={live_status!r}"
            )
            raise UserLiveException(TikTokError.USER_NOT_CURRENTLY_LIVE)
        if not self.tiktok_owner_user_id and self.room_id:
            try:
                self.tiktok_owner_user_id = (
                    self.tiktok.get_room_owner_user_id(self.room_id)
                )
            except Exception as owner_error:
                logger.debug(
                    f"[LIVE_IDENTITY] Could not resolve owner_user_id for "
                    f"room_id={self.room_id}: {owner_error}"
                )

        # Log USERNAME and ROOM_ID after they have been determined
        logger.info(f"USERNAME: {self.user}")
        # Log ROOM_ID without checking live status here to avoid early exceptions
        logger.info(f"ROOM_ID:  {self.room_id}\n")

        # Check if room_id was successfully determined during initialization
        if not self.room_id:
             # Raise an exception if ROOM_ID is still empty after attempts
             raise UserLiveException(TikTokError.USER_NEVER_BEEN_LIVE) # Or TikTokError.ROOM_ID_ERROR depending on desired message # Use imported enum

        # If proxy is provided, set up the HTTP client without the proxy
        if proxy:
            # Use the imported class directly
            self.tiktok = TikTokAPI(proxy=None, cookies=cookies)

    def run(self):
        """
        runs the program in the selected mode.

        If the mode is MANUAL, it checks if the user is currently live and
        if so, starts recording.

        If the mode is AUTOMATIC, it continuously checks if the user is live
        and if not, waits for the specified timeout before rechecking.
        If the user is live, it starts recording.
        """
        if self.mode == Mode.MANUAL: # Use imported enum
            self.manual_mode()


    def manual_mode(self):
        if not self.tiktok.is_room_alive(self.room_id):
            raise UserLiveException(
                f"@{self.user}: {TikTokError.USER_NOT_CURRENTLY_LIVE}" # Use imported enum
            )

        self.start_recording() # Returns duration, but ignored in manual mode call

    def request_stop(self):
        """
        Request graceful shutdown of the recording process.
        This method is called by signal handlers.
        """
        logger.info(f"🛑 Stop requested for user {self.user}")
        self._stop_requested.set()

    def start_recording(self) -> float:
        """
        Start recording live and return the actual recording duration in seconds.
        """
        actual_duration = 0.0
        live_url_candidates = self.tiktok.get_live_url_candidates(self.room_id)
        if not live_url_candidates:
            self.no_stream_data = True
            logger.warning(
                f"[NO_STREAM_DATA] {TikTokError.RETRIEVE_LIVE_URL}"
            )
            return actual_duration # Return 0 duration
        live_url = live_url_candidates[0]
        logger.info(
            "Selected live media candidate 1/%d", len(live_url_candidates)
        )

        # Handle output path based on whether it's an exact file path or directory
        if self.is_exact_file_path and self.output:
            # Use the exact file path provided by supervisor
            output = self.output
        else:
            # Use legacy directory-based logic with generated filename
            current_date = time.strftime("%Y.%m.%d_%H-%M-%S", time.localtime())

            if isinstance(self.output, str) and self.output != '':
                if not (self.output.endswith('/') or self.output.endswith('\\')):
                    if os.name == 'nt':
                        self.output = self.output + "\\"
                    else:
                        self.output = self.output + "/"

            # Preserve the historical filename selected by the compatibility flag.
            if self.ffmpeg_remux:
                output = f"{self.output if self.output else ''}TK_{self.user}_{current_date}.mp4"
            else:
                output = f"{self.output if self.output else ''}TK_{self.user}_{current_date}_flv.mp4"

        if self.duration:
            logger.info(f"Started recording for {self.duration} seconds ")
        else:
            logger.info("Started recording...")

        buffer_size = 512 * 1024 # Restored original 512 KB buffer size
        buffer = bytearray()

        logger.info("[PRESS CTRL + C ONCE TO STOP]")
        recording_start_time = 0
        actual_duration = 0.0
        stop_recording = False

        try:
            if self.ffmpeg_remux:
                # Compatibility path currently writes the raw stream.
                actual_duration = self._record_with_ffmpeg_remux(
                    live_url,
                    output,
                    live_url_candidates,
                )
            else:
                # Original raw recording mode
                actual_duration = self._record_raw_stream(
                    live_url,
                    output,
                    buffer_size,
                    buffer,
                    stop_recording,
                    live_url_candidates=live_url_candidates,
                )
        except Exception as e:
            logger.error(f"Recording error: {e}")
            actual_duration = time.time() - recording_start_time if 'recording_start_time' in locals() else 0.0

        logger.info(f"FINISH: {output} (Recorded Duration: {actual_duration:.2f}s)\n")

        if self.use_telegram:
            # Upload the recorded file
            self._upload_to_telegram(output)

        return actual_duration

    def _record_with_ffmpeg_remux(
        self,
        live_url: str,
        output: str,
        live_url_candidates: list[str] | None = None,
    ) -> float:
        """Honor the compatibility flag while retaining raw stream output."""
        logger.warning(
            "⚠️  --ffmpeg-remux is a compatibility flag; FFmpeg remux is not "
            "implemented, so the recorder is writing the raw stream"
        )

        buffer_size = 512 * 1024
        buffer = bytearray()
        stop_recording = False

        return self._record_raw_stream(
            live_url,
            output,
            buffer_size,
            buffer,
            stop_recording,
            live_url_candidates=live_url_candidates,
        )

    def _record_raw_stream(
        self,
        live_url: str,
        output: str,
        buffer_size: int,
        buffer: bytearray,
        stop_recording: bool,
        live_url_candidates: list[str] | None = None,
    ) -> float:
        """Original raw stream recording method"""
        recording_start_time = 0
        actual_duration = 0.0
        out_file = None
        part_number = 1
        current_output = (
            stream_part_path(output, part_number)
            if self.segment_on_reconnect
            else output
        )
        part_completed = False
        consecutive_connection_failures = 0
        initial_stream_url_refreshes = 0
        candidate_urls = []
        for candidate in live_url_candidates or [live_url]:
            if candidate and candidate not in candidate_urls:
                candidate_urls.append(candidate)
        if live_url not in candidate_urls:
            candidate_urls.insert(0, live_url)
        candidate_index = candidate_urls.index(live_url)

        def fetch_live_url_candidates() -> list[str]:
            get_candidates = getattr(
                self.tiktok,
                'get_live_url_candidates',
                None,
            )
            if callable(get_candidates):
                refreshed_candidates = get_candidates(self.room_id)
            else:
                refreshed_url = self.tiktok.get_live_url(self.room_id)
                refreshed_candidates = [refreshed_url] if refreshed_url else []

            unique_candidates = []
            for candidate in refreshed_candidates:
                if candidate and candidate not in unique_candidates:
                    unique_candidates.append(candidate)
            return unique_candidates

        def refresh_stream_url(reason: str) -> str:
            nonlocal live_url, candidate_urls, candidate_index
            old_live_url = live_url

            if candidate_index + 1 < len(candidate_urls):
                candidate_index += 1
                live_url = candidate_urls[candidate_index]
                logger.info(
                    f"[STREAM_URL] switched to candidate "
                    f"{candidate_index + 1}/{len(candidate_urls)} {reason}"
                )
                return 'candidate'

            try:
                refreshed_candidates = fetch_live_url_candidates()
                if not refreshed_candidates:
                    logger.warning(
                        f"[STREAM_URL] refresh returned no URL {reason}"
                    )
                    return 'failed'
                candidate_urls = refreshed_candidates
                candidate_index = 0
                live_url = candidate_urls[0]
                logger.info(
                    f"[STREAM_URL] refreshed {reason} "
                    f"changed={live_url != old_live_url} "
                    f"candidates={len(candidate_urls)}"
                )
                return 'refetched'
            except Exception as refresh_error:
                logger.error(
                    f"[STREAM_URL] refresh failed {reason}: {refresh_error}"
                )
                return 'failed'

        def initial_stream_retries_exhausted() -> bool:
            nonlocal initial_stream_url_refreshes
            if recording_start_time != 0:
                return False
            initial_stream_url_refreshes += 1
            if initial_stream_url_refreshes < self.MAX_INITIAL_STREAM_URL_REFRESHES:
                return False
            self.no_stream_data = True
            logger.warning(
                "[NO_STREAM_DATA] Recorder produced no stream data after "
                f"{initial_stream_url_refreshes} URL refreshes; stopping"
            )
            return True

        def ensure_output_open():
            nonlocal out_file
            if out_file is not None:
                return
            mode = "xb" if part_number > 1 else "wb"
            out_file = open(current_output, mode)
            if self.segment_on_reconnect:
                logger.info(
                    f"[STREAM_PART] opened part={part_number} file={current_output}"
                )

        def flush_buffer(sync: bool = False):
            if not buffer:
                return
            ensure_output_open()
            out_file.write(buffer)
            out_file.flush()
            if sync:
                os.fsync(out_file.fileno())
            buffer.clear()

        def close_current_part():
            nonlocal out_file, part_completed
            if out_file is None and buffer:
                ensure_output_open()
            if out_file is None:
                return
            flush_buffer(sync=True)
            out_file.close()
            out_file = None
            if self.segment_on_reconnect:
                try:
                    part_size = os.path.getsize(current_output)
                except OSError:
                    part_size = -1
                logger.info(
                    f"[STREAM_PART] closed part={part_number} "
                    f"bytes={part_size} file={current_output}"
                )
                part_completed = True

        try:
            while not stop_recording and not self._stop_requested.is_set():
                connection_had_data = False
                try:
                    # Check if user is still live at the beginning of each inner loop iteration
                    if not self.tiktok.is_room_alive(self.room_id):
                        logger.info("User is no longer live. Stopping recording.")
                        stop_recording = True
                        break # Exit while loop

                    # Download and write chunks
                    stream_reached_eof = False
                    for chunk in self.tiktok.download_live_stream(live_url, self._stop_requested):
                        # Check for stop signal during chunk processing
                        if self._stop_requested.is_set():
                            logger.info("📡 Stop signal received during download. Stopping recording gracefully.")
                            stop_recording = True
                            break

                        if not chunk: # Handle potential empty chunks if the stream ends abruptly
                            logger.warning("Received empty chunk, stream might have ended.")
                            # Check again if user is live before deciding to stop
                            if not self.tiktok.is_room_alive(self.room_id):
                                 logger.info("User confirmed offline after empty chunk. Stopping.")
                                 stop_recording = True
                            break # Exit for loop, re-check in while

                        first_chunk = not connection_had_data
                        if self.segment_on_reconnect and part_completed and first_chunk:
                            part_number += 1
                            current_output = stream_part_path(output, part_number)
                            part_completed = False

                        connection_had_data = True
                        consecutive_connection_failures = 0
                        if recording_start_time == 0:
                            recording_start_time = time.time()
                        buffer.extend(chunk)
                        # Open the output only after real stream bytes arrive. Writing
                        # the first chunk immediately also prevents a visible 0-byte
                        # placeholder while the buffer is still below buffer_size.
                        if first_chunk or len(buffer) >= buffer_size:
                            flush_buffer()

                        # Check duration limit inside the inner loop
                        elapsed_time = time.time() - recording_start_time
                        if self.duration and elapsed_time >= self.duration:
                            logger.info(f"Reached specified duration limit ({self.duration}s). Stopping recording.")
                            stop_recording = True
                            break # Exit for loop
                    else:
                        stream_reached_eof = True

                    # If the for loop finished without break (stream ended?), check live status again
                    if not stop_recording and not self.tiktok.is_room_alive(self.room_id):
                         logger.info("Stream ended or user went offline. Stopping recording.")
                         stop_recording = True
                    elif (
                        not stop_recording
                        and not self._stop_requested.is_set()
                        and stream_reached_eof
                    ):
                        refresh_result = refresh_stream_url("after stream EOF")
                        if not connection_had_data:
                            if (
                                refresh_result != 'candidate'
                                and initial_stream_retries_exhausted()
                            ):
                                stop_recording = True
                            elif refresh_result == 'candidate':
                                continue
                            elif self._stop_requested.wait(timeout=5):
                                stop_recording = True

                except (ConnectionError, RequestException, HTTPException) as e:
                    consecutive_connection_failures += 1
                    using_startup_candidate_pool = (
                        recording_start_time == 0
                        and len(candidate_urls) > 1
                    )
                    failure_limit = (
                        1
                        if using_startup_candidate_pool
                        else self.STREAM_FAILURES_BEFORE_URL_REFRESH
                    )
                    logger.error(
                        "Connection error during recording: "
                        f"{e}. Retrying... "
                        f"({consecutive_connection_failures}/"
                        f"{failure_limit})"
                    )

                    refresh_result = None
                    if consecutive_connection_failures >= failure_limit:
                        refresh_result = refresh_stream_url(
                            "after "
                            f"{consecutive_connection_failures} consecutive failures"
                        )
                        # Even a failed refresh is rate-limited to one attempt per
                        # group of failures rather than once every five seconds.
                        consecutive_connection_failures = 0
                        if (
                            refresh_result != 'candidate'
                            and initial_stream_retries_exhausted()
                        ):
                            stop_recording = True
                            break
                        if (
                            refresh_result == 'candidate'
                            and recording_start_time == 0
                        ):
                            continue

                    # Interruptible sleep - check for stop signal every second
                    if self._stop_requested.wait(timeout=5):
                        logger.info("📡 Stop signal received during connection error recovery.")
                        stop_recording = True
                        break
                    continue

                except Exception as e: # Catch other unexpected errors within the loop
                    logger.error(f"Unexpected error within recording loop: {e}")
                    logger.exception("Traceback:")
                    stop_recording = True # Stop recording on unexpected errors
                finally:
                    if self.segment_on_reconnect and connection_had_data:
                        close_current_part()

        except KeyboardInterrupt:
            logger.info("Recording stopped by user (KeyboardInterrupt).")
            stop_recording = True # Ensure loop terminates if Ctrl+C is pressed outside the inner try

        except Exception as e: # Catch errors opening file or other setup issues
             logger.error(f"Unexpected error setting up recording: {e}")
             logger.exception("Traceback:")
             stop_recording = True # Prevent loop start

        finally:
            # Log graceful shutdown if signal was received
            if self._stop_requested.is_set():
                logger.info("🔚 Recording stopped gracefully due to stop signal")

            # Calculate duration after loop finishes or stops
            if recording_start_time > 0:
                 actual_duration = time.time() - recording_start_time

            # Ensure any remaining buffer is written with proper flushing
            if buffer:
                remaining_bytes = len(buffer)
                try:
                    flush_buffer(sync=True)
                    logger.info(f"💾 Flushed remaining {remaining_bytes} bytes to file")
                except Exception as write_err:
                     logger.error(f"Error writing remaining buffer: {write_err}")
                buffer.clear()

            close_current_part()


        logger.info(f"FINISH: {output} (Recorded Duration: {actual_duration:.2f}s)\n")

        if self.use_telegram:
            # Upload the original .flv file
            self._upload_to_telegram(output)

        return actual_duration # Return the calculated duration

    def check_country_blacklisted(self):
        is_blacklisted = self.tiktok.is_country_blacklisted()
        if not is_blacklisted:
            return False

        if self.room_id is None:
            raise TikTokException(TikTokError.COUNTRY_BLACKLISTED) # Use imported enum

        if self.mode == Mode.AUTOMATIC: # Use imported enum
            raise TikTokException(TikTokError.COUNTRY_BLACKLISTED_AUTO_MODE) # Use imported enum

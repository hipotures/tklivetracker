# Banner removed as requested due to import issues

# check and install dependencies - Disabled


import sys
import os
import subprocess
import signal
import stat
import datetime
import json
import re
import atexit
import tempfile

from .utils.args_handler import validate_and_parse_args # Relative import
from .utils.utils import read_cookies # Relative import
from .utils.logger_manager import logger # Relative import
from .utils.stream_parts import existing_stream_parts
from .utils.recording_metadata import write_recording_metadata

from .core.tiktok_recorder import TikTokRecorder # Relative import
from .utils.enums import TikTokError # Relative import
from .utils.custom_exceptions import LiveNotFound, ArgsParseError, \
    UserLiveException, IPBlockedByWAF, TikTokException # Relative import


_LIVE_ID_RESULT_PATTERN = re.compile(r'\{\s*"live_id"\s*:\s*(\d+)\s*\}')


def _pid_file_path(username: str, runtime_dir: str = "/tmp") -> str:
    """Return the compatibility PID path for an already validated username."""
    return os.path.join(runtime_dir, f"tiktok_live_{username}.pid")


def write_pid_file(username: str, pid: int, runtime_dir: str = "/tmp") -> str:
    """Create this recorder's PID file without following or replacing a path."""
    pid_file = _pid_file_path(username, runtime_dir)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(pid_file, flags, 0o600)
    try:
        os.write(fd, str(pid).encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return pid_file


def cleanup_pid_file(pid_file: str, expected_pid: int) -> bool:
    """Remove a PID file only when it is still our regular file and PID."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(pid_file, flags)
    except (FileNotFoundError, OSError):
        return False

    try:
        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            return False
        contents = os.read(fd, 64).decode("ascii", errors="strict").strip()
        if contents != str(expected_pid):
            return False
        path_stat = os.lstat(pid_file)
        if stat.S_ISLNK(path_stat.st_mode):
            return False
        if (path_stat.st_dev, path_stat.st_ino) != (opened_stat.st_dev, opened_stat.st_ino):
            return False
        os.unlink(pid_file)
        return True
    except (FileNotFoundError, OSError, UnicodeError):
        return False
    finally:
        os.close(fd)


def write_startup_metadata(metadata_path: str, metadata: dict) -> None:
    """Atomically publish private startup metadata in its destination directory."""
    metadata_path = os.path.abspath(metadata_path)
    parent = os.path.dirname(metadata_path)
    os.makedirs(parent, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".startup-", suffix=".tmp", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as metadata_file:
            json.dump(metadata, metadata_file)
            metadata_file.flush()
            os.fsync(metadata_file.fileno())
        os.replace(temporary_path, metadata_path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def cleanup_startup_metadata(metadata_path: str, expected_pid: int) -> bool:
    """Remove startup metadata only when it still identifies this recorder."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(metadata_path, flags)
    except (FileNotFoundError, OSError):
        return False

    try:
        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            return False
        with os.fdopen(fd, "r", encoding="utf-8") as metadata_file:
            fd = -1
            metadata = json.load(metadata_file)
        if not isinstance(metadata, dict) or metadata.get("pid") != expected_pid:
            return False
        path_stat = os.lstat(metadata_path)
        if stat.S_ISLNK(path_stat.st_mode):
            return False
        if (path_stat.st_dev, path_stat.st_ino) != (opened_stat.st_dev, opened_stat.st_ino):
            return False
        os.unlink(metadata_path)
        return True
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return False
    finally:
        if fd >= 0:
            os.close(fd)


def extract_db_helper_live_id(output: str) -> int:
    """Extract the recorder-owned live ID from noisy db_helper output."""
    matches = _LIVE_ID_RESULT_PATTERN.findall(output)
    if not matches:
        raise ValueError("db_helper start output does not contain a live_id result")
    return int(matches[-1])


def cleanup_own_zero_byte_output(output_file: str | None) -> bool:
    """Remove only this recorder's exact empty MP4 output."""
    if not output_file or not str(output_file).lower().endswith(".mp4"):
        return False

    try:
        file_stat = os.lstat(output_file)
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            return False
        if file_stat.st_size != 0:
            return False
        os.unlink(output_file)
        logger.info(
            f"Removed own 0-byte recorder output after PID={os.getpid()} finished: "
            f"{output_file}"
        )
        return True
    except FileNotFoundError:
        return False
    except OSError as error:
        logger.info(
            f"Could not remove own 0-byte recorder output after PID={os.getpid()}: "
            f"{error}"
        )
        return False


def main():
    # setup logging early

    try:
        # Wrap argument parsing and initial setup in a try block for early error capture
        try:
            args, mode = validate_and_parse_args()
            logger.debug("Successfully validated and parsed arguments.")

            # read cookies from file (after potential extraction)
            if args.no_cookies:
                cookies = None
                logger.info("Running without cookies (--no-cookies flag set)")
            else:
                cookies = read_cookies(args.cookie_file)
                if cookies:
                    if args.cookie_file:
                        logger.debug(f"Successfully read cookies from: {args.cookie_file}")
                    else:
                        logger.debug("Successfully read cookies file from default location.")
                else:
                    if args.cookie_file:
                        logger.warning(f"Cookie file not found: {args.cookie_file}")
                    else:
                        logger.warning("No cookies file found in default locations - running without authentication")

            # Create the recorder
            # In persistent mode, prefer output_file over output directory
            output_param = args.output_file if args.output_file else args.output
            is_exact_file_path = bool(args.output_file)  # True if --output-file was used

            recorder = TikTokRecorder(
                url=args.url,
                user=args.user,
                room_id=args.room_id,
                mode=mode,
                cookies=cookies,
                proxy=args.proxy,
                output=output_param,
                duration=args.duration,
                use_telegram=args.telegram,
                is_exact_file_path=is_exact_file_path,
                ffmpeg_remux=args.ffmpeg_remux,  # Pass FFmpeg remux flag
                segment_on_reconnect=args.segment_on_reconnect,
                config_path=args.config_path,
            )

            # Handle persistent mode setup
            if args.persistent_mode:
                # Setup signal handlers for graceful shutdown in persistent mode
                def signal_handler(signum, frame):
                    logger.info(f"🛑 Received signal {signum}, initiating graceful shutdown...")
                    try:
                        logger.info("📡 Requesting recorder to stop gracefully...")
                        recorder.request_stop()
                    except Exception as e:
                        logger.error(f"❌ Error requesting graceful stop: {e}")

                signal.signal(signal.SIGTERM, signal_handler)
                signal.signal(signal.SIGINT, signal_handler)

                # Write PID to temporary file for process tracking
                try:
                    pid_file = write_pid_file(recorder.user, os.getpid())
                    atexit.register(cleanup_pid_file, pid_file, os.getpid())
                    logger.debug(f"Wrote PID {os.getpid()} to {pid_file}")
                except Exception as e:
                    logger.warning(f"Could not write PID file: {e}")

            # For automatic mode, we'll modify the behavior to support the bash-based architecture
            if mode == 1:  # Mode.AUTOMATIC = 1
                # Check if room_id exists
                if not recorder.room_id:
                    logger.info(f"@{recorder.user}: {TikTokError.USER_NEVER_BEEN_LIVE}")
                    sys.exit(10)  # Exit code 10: No ROOM_ID or user never been live

                # Check if user is currently live
                if not recorder.tiktok.is_room_alive(recorder.room_id):
                    logger.info(f"@{recorder.user}: {TikTokError.USER_NOT_CURRENTLY_LIVE}")
                    sys.exit(11)  # Exit code 11: User not currently live

                # User is live, proceed with recording
                msg = f"{recorder.user} is live! Starting recording."
                logger.info(msg)
                print(f"INFO {msg}") # Print INFO message to stdout for console visibility
                # Additionally write to the main supervisor log
                # Use the log path passed from the supervisor via args
                if args.supervisor_log_path:
                    try:
                        # Use the provided path directly
                        log_path = args.supervisor_log_path
                        with open(log_path, "a", encoding="utf-8") as f:
                            # Use format similar to supervisor logger
                            f.write(f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} [REC] INFO {msg}\n")
                    except Exception as log_ex:
                        # Use logger.exception to include traceback for easier debugging
                        logger.exception(f"Could not write start message to supervisor log file '{args.supervisor_log_path}': {log_ex}")
                        # Also print directly to console for immediate visibility during debugging
                        print(f"ERROR WRITING TO SUPERVISOR LOG FILE '{args.supervisor_log_path}': {log_ex}", file=sys.stderr)
                # Correctly indented else block
                else:
                    logger.warning("Supervisor log path not provided via arguments, cannot write [REC] INFO message.")

                # --- ADDED: register live start in database ---
                live_id = None
                try:
                    helper_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "db_helper.py")
                    helper_command = [
                        sys.executable,
                        helper_path,
                        "--config",
                        str(args.config_path),
                    ]
                    # Set is_live=1
                    result_set_live = subprocess.run(
                        [*helper_command, "set_is_live", recorder.user, "1"],
                        capture_output=True, text=True, check=False # check=False to log errors
                    )
                    if result_set_live.returncode != 0:
                        logger.error(f"db_helper set_is_live failed: {result_set_live.stderr}")
                    else:
                        logger.info(f"Set is_live=1 in DB for {recorder.user}")
                    start_command = [
                        *helper_command,
                        "start",
                        recorder.user,
                        str(recorder.tiktok_stream_id or ""),
                        str(recorder.tiktok_started_at or ""),
                        str(recorder.tiktok_owner_user_id or ""),
                    ]
                    result_start = subprocess.run(
                        start_command,
                        capture_output=True, text=True, check=False # check=False to log errors
                    )
                    if result_start.returncode != 0:
                         logger.error(f"db_helper start failed: {result_start.stderr}")
                    else:
                        live_id = extract_db_helper_live_id(result_start.stdout)
                        logger.info(f"Started live session in DB, live_id={live_id}")
                        if args.startup_metadata_file:
                            metadata = {
                                "pid": os.getpid(),
                                "live_id": live_id,
                                "tiktok_stream_id": recorder.tiktok_stream_id,
                                "tiktok_started_at": recorder.tiktok_started_at,
                                "tiktok_owner_user_id": recorder.tiktok_owner_user_id,
                            }
                            try:
                                write_startup_metadata(args.startup_metadata_file, metadata)
                            except Exception as metadata_error:
                                logger.warning(
                                    f"Could not write startup metadata {args.startup_metadata_file}: {metadata_error}"
                                )
                except Exception as e:
                    logger.warning(f"Could not register live start in DB: {e}")

                recorded_duration = 0.0 # Initialize duration
                try:
                    recorded_duration = recorder.start_recording()  # Capture the returned duration
                finally:
                    if args.metadata_path and args.output_file:
                        try:
                            manifest_path = write_recording_metadata(
                                args.output_file,
                                recorder.user,
                                args.metadata_path,
                                args.compressed_output_path,
                            )
                            if manifest_path is not None:
                                logger.info(f"Published recording metadata: {manifest_path}")
                        except Exception as metadata_error:
                            metadata_error_message = (
                                f"[METADATA_ERROR] @{recorder.user}: Could not publish "
                                f"recording metadata in {args.metadata_path}: {metadata_error}"
                            )
                            logger.error(metadata_error_message)
                            if args.supervisor_log_path:
                                try:
                                    with open(
                                        args.supervisor_log_path,
                                        "a",
                                        encoding="utf-8",
                                    ) as supervisor_log:
                                        timestamp = datetime.datetime.now().strftime(
                                            '%Y-%m-%d %H:%M:%S,%f'
                                        )[:-3]
                                        supervisor_log.write(
                                            f"{timestamp} [REC] ERROR "
                                            f"{metadata_error_message}\n"
                                        )
                                except OSError:
                                    pass

                    # --- DEBUG: Log entry to finally block ---
                    logger.info(f"[DEBUG] Entering finally block for {recorder.user if recorder else 'unknown'}, live_id={live_id}")

                    # --- ADDED: register live end in database ---
                    # The recorder process should NOT reset is_live=0.
                    # The supervisor handles this based on process termination.
                    # We only need to ensure the live session end time is recorded.
                    if live_id is not None:
                        logger.info(f"[DEBUG] Attempting to end live session in DB for {recorder.user}, live_id={live_id}")
                        try:
                            if helper_path and recorder and recorder.user: # Ensure variables are defined
                                logger.debug(f"[DEBUG] Calling db_helper stop: {helper_path} stop {recorder.user} {live_id}")
                                result_stop = subprocess.run(
                                    [*helper_command, "stop", recorder.user, str(live_id)],
                                    capture_output=True, text=True, check=False # check=False to log errors
                                )
                                logger.debug(f"[DEBUG] db_helper stop result: returncode={result_stop.returncode}, stdout='{result_stop.stdout}', stderr='{result_stop.stderr}'")
                                if result_stop.returncode != 0:
                                    logger.error(f"db_helper stop failed: {result_stop.stderr}")
                                else:
                                    logger.info(f"Ended live session in DB, live_id={live_id}")
                            else:
                                logger.warning("Cannot record live session end: helper_path or recorder info missing.")
                                logger.debug(f"[DEBUG] Missing vars: helper_path={helper_path}, recorder={recorder}, recorder.user={recorder.user if recorder else 'N/A'}")
                        except Exception as e:
                            logger.exception(f"Could not register live end in DB: {e}")
                    else:
                        logger.warning("[DEBUG] Skipping DB update - live_id is None")

                    # --- Append Completion Message to Supervisor Log ---
                    try:
                        # Format duration simply for the log
                        duration_str = f"{recorded_duration:.2f} seconds"
                        if recorded_duration > 0:
                            try:
                                seconds = int(recorded_duration)
                                hours, remainder = divmod(seconds, 3600)
                                minutes, seconds = divmod(remainder, 60)
                                duration_str = f"{hours:02}:{minutes:02}:{seconds:02}"
                            except Exception:
                                pass # Keep the seconds format if conversion fails

                        # Get file size
                        file_size_str = "Unknown size"
                        if args.output_file:
                            try:
                                recorded_paths = list(existing_stream_parts(args.output_file))
                                if recorded_paths:
                                    file_size_bytes = sum(
                                        os.path.getsize(path)
                                        for path in recorded_paths
                                    )
                                    file_size_mb = file_size_bytes / (1024 * 1024)
                                    if file_size_mb >= 1024:
                                        file_size_str = f"{file_size_mb / 1024:.2f} GB"
                                    else:
                                        file_size_str = f"{file_size_mb:.1f} MB"
                            except Exception:
                                pass

                        no_stream_data = recorder.no_stream_data
                        completion_level = "WARNING" if no_stream_data else "INFO"
                        if no_stream_data:
                            completion_msg = (
                                f"@{recorder.user}: Recording failed: no stream data. "
                                f"Duration: {duration_str}, Size: {file_size_str}"
                            )
                        else:
                            completion_msg = f"@{recorder.user}: Recording completed. Duration: {duration_str}, Size: {file_size_str}"
                        # Use the same log path that was provided for the start message
                        log_path = args.supervisor_log_path if args.supervisor_log_path else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "aplikacja.log")
                        # Write to supervisor log file
                        try:
                            with open(log_path, "a", encoding="utf-8") as f:
                                f.write(
                                    f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} "
                                    f"[REC] {completion_level} {completion_msg}\n"
                                )
                        except Exception as log_ex:
                            logger.warning(f"Could not write completion message to supervisor log: {log_ex}")
                        # Also print to stdout for console visibility
                        print(f"{completion_level} {completion_msg}")
                    except Exception as format_ex:
                        logger.warning(f"Could not format or log completion message: {format_ex}")
                        # Fallback print if formatting failed
                        if recorder.no_stream_data:
                            print(
                                f"WARNING @{recorder.user}: "
                                "Recording failed: no stream data."
                            )
                        else:
                            print(f"INFO @{recorder.user}: Recording completed.")

                    cleanup_own_zero_byte_output(args.output_file)

                # Clean up PID file in persistent mode
                if args.persistent_mode:
                    try:
                        pid_file = _pid_file_path(recorder.user)
                        if cleanup_pid_file(pid_file, os.getpid()):
                            logger.debug(f"Cleaned up PID file: {pid_file}")
                    except Exception as e:
                        logger.warning(f"Could not clean up PID file: {e}")

                    if args.startup_metadata_file:
                        try:
                            cleanup_startup_metadata(
                                args.startup_metadata_file, os.getpid()
                            )
                        except Exception as e:
                            logger.warning(f"Could not clean up startup metadata file: {e}")

                # Note: The core recorder already logs its own "FINISH" message.
                # We don't need an additional logger.info() here in main.py.
                # A recorder that never received media must not look like a
                # successful, normally completed recording to its supervisor.
                sys.exit(12 if recorder.no_stream_data else 0)
            else:
                # For manual mode, behavior remains unchanged
                recorder.run()

        except Exception as early_ex:
            # Catch any exception during early setup
            # Check if the error is specifically related to ROOM_ID not found or user never live
            if (
                isinstance(early_ex, UserLiveException)
                and str(early_ex) == str(TikTokError.USER_NOT_CURRENTLY_LIVE)
            ):
                exit_code = 11
                error_message = (
                    f"{TikTokError.USER_NOT_CURRENTLY_LIVE} "
                    f"Exiting with code {exit_code}."
                )
                print(error_message, file=sys.stderr)
                try:
                    logger.debug(error_message)
                except NameError:
                    pass
            elif isinstance(early_ex, UserLiveException) and (str(early_ex) == str(TikTokError.ROOM_ID_ERROR) or str(early_ex) == str(TikTokError.USER_NEVER_BEEN_LIVE)):
                exit_code = 10 # Specific exit code for ROOM_ID not found or user never live
                error_message = f"Could not find ROOM_ID or user never live. Exiting with code {exit_code}."
                print(error_message, file=sys.stderr)
                try:
                    logger.warning(error_message) # Log as warning
                except NameError: pass # Logger might not be initialized
            else:
                # For any other early exception
                print(f"An early error occurred: {early_ex}", file=sys.stderr)
                exit_code = 1 # General early error exit code
                error_message = f"An unexpected early error occurred: {early_ex}. Exiting with code {exit_code}."
                print(error_message, file=sys.stderr)
                try:
                    logger.error(error_message)
                    logger.exception("Early Traceback:")
                except NameError: pass # Logger might not be initialized

            sys.exit(exit_code)

    except ArgsParseError as ex:
        logger.error(ex)
        sys.exit(1)

    except LiveNotFound as ex:
        logger.error(ex)
        sys.exit(1)

    except IPBlockedByWAF:
        logger.error(TikTokError.WAF_BLOCKED)
        # Use specific exit code 12 for WAF block
        sys.exit(12)

    except UserLiveException as ex:
        # This block catches UserLiveExceptions from recorder.run() (e.g., user goes offline during recording)
        logger.error(ex)
        # Do NOT exit here, the automatic loop handles this by increasing sleep

    except TikTokException as ex:
        logger.error(ex)
        sys.exit(1)

    except Exception as ex:
        # This outer except catches errors from recorder.run() or other later issues
        logger.error(f"An error occurred during execution: {ex}")
        logger.exception("Execution Traceback:")
        sys.exit(1)

if __name__ == "__main__":
    main()

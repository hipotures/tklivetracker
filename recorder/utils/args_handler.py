import argparse
import re
from pathlib import Path

from .custom_exceptions import ArgsParseError # Relative import
from .enums import Mode, Regex # Relative import
from utils.username import normalize_tiktok_username


def parse_args():
    """
    Parse command line arguments.
    """
    parser = argparse.ArgumentParser(
        description="TkLiveTracker integrated TikTok recording process.",
        formatter_class=argparse.RawTextHelpFormatter
    )

    parser.add_argument(
        "-url",
        dest="url",
        help="Record a live session from the TikTok URL.",
        action='store'
    )

    parser.add_argument(
        "-user",
        dest="user",
        help="Record a live session from the TikTok username.",
        action='store'
    )

    parser.add_argument(
        "-room_id",
        dest="room_id",
        help="Record a live session from the TikTok room ID.",
        action='store'
    )

    parser.add_argument(
        "-mode",
        dest="mode",
        help=(
            "Recording mode: (manual, automatic) [Default: manual]\n"
            "[manual] => Manual live recording.\n"
            "[automatic] => Automatic live recording when the user is live."
        ),
        default="manual",
        action='store'
    )

    # Removed -automatic_interval argument as it's no longer used by the dynamic sleep logic

    parser.add_argument(
        "-proxy",
        dest="proxy",
        help=(
            "Use HTTP proxy to bypass login restrictions in some countries.\n"
            "Example: -proxy http://127.0.0.1:8080"
        ),
        action='store'
    )

    parser.add_argument(
        "-output",
        dest="output",
        help=(
            "Specify the output directory where recordings will be saved.\n"
        ),
        action='store'
    )

    parser.add_argument(
        "--output-file",
        dest="output_file",
        help="Specify the exact output file path for the recording (persistent mode)",
        action='store'
    )

    parser.add_argument(
        "-duration",
        dest="duration",
        help="Specify the duration in seconds to record the live session [Default: None].",
        type=int,
        default=None,
        action='store'
    )

    parser.add_argument(
        "-telegram",
        dest="telegram",
        action="store_true",
        help="Activate the option to upload the video to Telegram at the end "
             "of the recording.\nRequires telegram.upload in config.yaml",
    )

    parser.add_argument(
        "-no-update-check",
        dest="update_check",
        action="store_false",
        help=(
            "Compatibility option retained for existing launchers; "
            "this integrated build does not perform update checks."
        )
    )
    # Add the new argument INSIDE the function
    parser.add_argument(
        "--supervisor-log-path",
        dest="supervisor_log_path",
        help="Internal use: Path to the main supervisor log file for appending messages.",
        default=None,
        action='store'
    )

    parser.add_argument(
        "--config",
        dest="config_path",
        default=str(Path(__file__).resolve().parents[2] / "config.yaml"),
        help="Internal use: Active application configuration file.",
        action='store'
    )

    parser.add_argument(
        "--persistent-mode",
        dest="persistent_mode",
        action="store_true",
        help="Internal use: Run in persistent mode for detached recording processes."
    )

    parser.add_argument(
        "--startup-metadata-file",
        dest="startup_metadata_file",
        help="Internal use: Write recorder startup identity metadata to this file.",
        default=None,
        action='store'
    )

    parser.add_argument(
        "--no-cookies",
        dest="no_cookies",
        action="store_true",
        help="Run without using cookies file (no authentication)."
    )

    parser.add_argument(
        "--cookie-file",
        dest="cookie_file",
        help="Path to JSON file containing cookies (overrides default locations).",
        action='store'
    )

    parser.add_argument(
        "--ffmpeg-remux",
        dest="ffmpeg_remux",
        action="store_true",
        help=(
            "Compatibility flag for FFmpeg remux mode; the current recorder "
            "falls back to raw stream recording."
        )
    )

    parser.add_argument(
        "--segment-on-reconnect",
        dest="segment_on_reconnect",
        action="store_true",
        help="Write successfully reconnected stream data to numbered part files."
    )

    parser.add_argument(
        "--metadata-path",
        dest="metadata_path",
        help="Internal use: Publish completed recording metadata in this directory.",
        default=None,
        action='store'
    )

    parser.add_argument(
        "--compressed-output-path",
        dest="compressed_output_path",
        help="Internal use: Root directory for the compressed output path.",
        default=None,
        action='store'
    )

    # Parse and return INSIDE the function
    args = parser.parse_args()
    return args


def validate_and_parse_args():
    args = parse_args()

    if not args.user and not args.room_id and not args.url:
        raise ArgsParseError("Missing URL, username, or room ID. Please provide one of these parameters.")

    if args.user:
        normalized_user = normalize_tiktok_username(args.user)
        if normalized_user is None:
            raise ArgsParseError(
                "Invalid TikTok username. Use 2 to 24 letters, numbers, "
                "underscores, or periods; the username cannot end with a period."
            )
        args.user = normalized_user

    if not args.mode:
        raise ArgsParseError("Missing mode value. Please specify the mode (manual or automatic).")
    if args.mode not in ["manual", "automatic"]:
        raise ArgsParseError("Incorrect mode value. Choose between 'manual' and 'automatic'.")

    if args.url and not re.match(str(Regex.IS_TIKTOK_LIVE), args.url):
        raise ArgsParseError("The provided URL does not appear to be a valid TikTok live URL.")

    # Allow user + room_id combination in persistent mode for better performance
    if not args.persistent_mode:
        if (args.user and args.room_id) or (args.user and args.url) or (args.room_id and args.url):
            raise ArgsParseError("Please provide only one among username, room ID, or URL.")
    else:
        # In persistent mode, allow user + room_id but still check other combinations
        if (args.user and args.url) or (args.room_id and args.url):
            raise ArgsParseError("Please provide only one among username, room ID, or URL.")

    # Removed validation for automatic_interval

    mode = Mode.MANUAL if args.mode == "manual" else Mode.AUTOMATIC

    return args, mode

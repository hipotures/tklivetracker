import json
import gc
import re

from curl_cffi.requests.exceptions import RequestException as CurlRequestException
from requests import RequestException as RequestsRequestException

from ..http_utils.http_client import HttpClient # Relative import (up one level)
from ..utils.enums import StatusCode, TikTokError # Relative import (up one level)
from ..utils.logger_manager import logger # Relative import (up one level)
from ..utils.custom_exceptions import UserLiveException, TikTokException, \
    LiveNotFound, IPBlockedByWAF # Relative import (up one level)


class TikTokAPI:
    CONTROL_PLANE_TIMEOUT = 15
    # Use __slots__ to reduce memory usage
    __slots__ = (
        'BASE_URL', 'WEBCAST_URL', 'API_URL', 'http_client', 'live_identity',
        '_proxy', '_cookies',
    )

    def __init__(self, proxy, cookies):
        self.BASE_URL = 'https://www.tiktok.com'
        self.WEBCAST_URL = 'https://webcast.tiktok.com'
        self.API_URL = 'https://www.tiktok.com/api-live/user/room/'

        self._proxy = proxy
        self._cookies = cookies
        self.http_client = HttpClient(proxy, cookies).req
        self.live_identity = None

    def is_country_blacklisted(self) -> bool:
        """
        Checks if the user is in a blacklisted country that requires login
        """
        response = self.http_client.get(
            f"{self.BASE_URL}/live",
            allow_redirects=False,
            timeout=self.CONTROL_PLANE_TIMEOUT,
        )

        return response.status_code == StatusCode.REDIRECT

    def is_room_alive(self, room_id: str) -> bool:
        """
        Checking whether the user is live. Returns True if live, False otherwise.
        Raises other exceptions for API errors, but not for user not being live.
        """
        if not room_id:
            # If room_id is empty, user is definitely not live
            logger.debug("[ROOM_ALIVE] room_id is empty, returning False")
            return False

        try:
            url = f"{self.WEBCAST_URL}/webcast/room/check_alive/?aid=1988&region=CH&room_ids={room_id}&user_is_login=true"
            logger.debug(f"[ROOM_ALIVE] Checking room {room_id}, URL: {url}")

            response = self.http_client.get(url, timeout=self.CONTROL_PLANE_TIMEOUT)
            logger.debug(f"[ROOM_ALIVE] HTTP response status: {response.status_code}")

            data = response.json()
            logger.debug(
                "[ROOM_ALIVE] Parsed response status_code=%r data_items=%d",
                data.get('status_code') if isinstance(data, dict) else None,
                len(data.get('data', [])) if isinstance(data, dict) and isinstance(data.get('data'), list) else 0,
            )

            if 'data' not in data or len(data['data']) == 0:
                logger.debug("[ROOM_ALIVE] No data in response or empty data array")
                return False

            # Check for specific error codes that indicate the user is not live
            # Based on previous code, 4003110 might indicate live restriction,
            # but we need to be careful not to suppress actual errors.
            # For now, rely on the 'alive' flag.
            if data.get('status_code') == 4003110:
                 # This status code might indicate a restriction, not just offline.
                 # Depending on desired behavior, might need to handle differently.
                 # For now, if 'alive' is False, we return False.
                 logger.debug("[ROOM_ALIVE] Status code 4003110 detected (live restriction)")
                 pass # Continue to check 'alive' flag

            alive_status = data['data'][0].get('alive', False)
            logger.debug(f"[ROOM_ALIVE] Final alive status: {alive_status}")
            if not alive_status:
                return False

            # check_alive can remain true after a room has finished. Treat an
            # explicit room status as authoritative, but preserve the primary
            # result if the secondary endpoint is temporarily unavailable.
            try:
                room_info = self.http_client.get(
                    f"{self.WEBCAST_URL}/webcast/room/info/"
                    f"?aid=1988&room_id={room_id}",
                    timeout=self.CONTROL_PLANE_TIMEOUT,
                ).json()
                room_data = (
                    room_info.get('data')
                    if isinstance(room_info, dict)
                    and isinstance(room_info.get('data'), dict)
                    else {}
                )
                room_status = room_data.get('status')
                if room_status is not None and room_status not in (2, '2'):
                    logger.info(
                        f"[ROOM_ALIVE] room_id={room_id} "
                        f"decision=OFFLINE room_status={room_status!r}"
                    )
                    return False
            except Exception as room_info_error:
                logger.debug(
                    f"[ROOM_ALIVE] Could not verify room status for "
                    f"room_id={room_id}: {room_info_error}; "
                    "keeping check_alive result"
                )

            return True

        except Exception as e:
            # Catch any other exceptions during the API call (e.g., network issues)
            # and re-raise them or handle appropriately.
            # For now, log and re-raise as a generic TikTokException
            logger.error(f"[ROOM_ALIVE] Error checking room alive status for room_id {room_id}: {e}")
            raise TikTokException(f"Failed to check live status: {e}") from e


    def get_user_from_room_id(self, room_id) -> str:
        """
        Given a room_id, I get the username
        """
        data = self.http_client.get(
            f"{self.BASE_URL}/api/live/detail/?aid=1988&roomID={room_id}",
            timeout=self.CONTROL_PLANE_TIMEOUT,
        ).json()

        unique_id = data.get('LiveRoomInfo', {}).get('ownerInfo', {}).get(
            'uniqueId', None)

        if unique_id is None:
            raise TikTokException(TikTokError.USERNAME_ERROR)

        return unique_id

    def get_room_and_user_from_url(self, live_url: str):
        """
        Given a url, get user and room_id.
        """
        found_user = None # Use a temporary variable for clarity
        response = self.http_client.get(
            live_url,
            allow_redirects=False,
            timeout=self.CONTROL_PLANE_TIMEOUT,
        )
        content = response.text

        if response.status_code == StatusCode.REDIRECT:
            raise UserLiveException(TikTokError.COUNTRY_BLACKLISTED)

        # Try to find user from mobile URL pattern first
        if response.status_code == StatusCode.MOVED:
            matches = re.findall("com/@(.*?)/live", content)
            if len(matches) > 0:
                found_user = matches[0]

        # If user not found yet, try standard URL pattern
        if found_user is None:
            match = re.match(
                r"https?://(?:www\.)?tiktok\.com/@([^/]+)/live",
                live_url
            )
            if match:
                found_user = match.group(1)

        # Ensure user was found before attempting to get room_id
        if found_user is None:
             raise LiveNotFound(TikTokError.INVALID_TIKTOK_LIVE_URL) # Or a more specific error

        room_id = self.get_room_id_from_user(found_user)

        return found_user, room_id

    def get_room_id_from_user(self, user: str) -> str:
        """
        Given a username, I get the room_id using direct API endpoint
        """
        logger.debug(f"[GET_ROOM_ID] Getting room ID for user {user} using API endpoint")

        response = self.http_client.get(self.API_URL, params={
            "uniqueId": user,
            "sourceType": 54,
            "aid": 1988
        }, timeout=self.CONTROL_PLANE_TIMEOUT)

        logger.debug(f"[GET_ROOM_ID] API response status: {response.status_code}")

        if response.status_code != 200:
            logger.debug(f"[GET_ROOM_ID] API returned non-200 status: {response.status_code}")
            fallback_room_id = self._get_room_id_from_web_pages(user)
            if fallback_room_id:
                return fallback_room_id
            raise UserLiveException(TikTokError.ROOM_ID_ERROR)

        try:
            data = response.json()
            logger.debug("[GET_ROOM_ID] Successfully parsed JSON response")
        except json.JSONDecodeError as e:
            logger.debug(f"[GET_ROOM_ID] JSON decode error: {e}")
            fallback_room_id = self._get_room_id_from_web_pages(user)
            if fallback_room_id:
                return fallback_room_id
            raise UserLiveException(TikTokError.ROOM_ID_ERROR)

        response_data = data.get('data') or {}
        user_data = response_data.get('user') or {}
        live_room = response_data.get('liveRoom') or {}
        self.live_identity = {
            'room_id': user_data.get('roomId'),
            'stream_id': live_room.get('streamId'),
            'start_time': live_room.get('startTime'),
            'status': live_room.get('status'),
            'owner_user_id': (
                user_data.get('id')
                or user_data.get('userId')
                or user_data.get('user_id')
            ),
        }
        logger.info(
            "[LIVE_IDENTITY] "
            f"user={user} "
            f"room_id={self.live_identity['room_id']!r} "
            f"stream_id={self.live_identity['stream_id']!r} "
            f"start_time={self.live_identity['start_time']!r} "
            f"status={self.live_identity['status']!r} "
            f"owner_user_id={self.live_identity['owner_user_id']!r}"
        )

        # Extract room_id from API response structure
        if (data.get('data') and
            data['data'].get('user') and
            data['data']['user'].get('roomId')):
            room_id = data['data']['user']['roomId']
            logger.debug(f"[GET_ROOM_ID] Successfully extracted room_id: '{room_id}'")

            # Free up memory
            del data
            gc.collect()

            return room_id
        else:
            logger.debug("[GET_ROOM_ID] No roomId found in API response structure")
            fallback_room_id = self._get_room_id_from_web_pages(user)
            if fallback_room_id:
                return fallback_room_id

            # Check if user is not live (empty response means user offline)
            if data.get('data') and data['data'].get('user') is None:
                logger.debug("[GET_ROOM_ID] User appears to be offline, returning empty string")
                return ""
            raise UserLiveException(TikTokError.ROOM_ID_ERROR)

    def get_live_identity(self, user: str, timeout: int = 15) -> dict:
        """Fetch current TikTok live identity without applying webpage fallbacks."""
        response = self.http_client.get(
            self.API_URL,
            params={
                "uniqueId": user,
                "sourceType": 54,
                "aid": 1988,
            },
            timeout=timeout,
        )
        if response.status_code != 200:
            raise TikTokException(
                f"Live identity endpoint returned HTTP {response.status_code}"
            )

        try:
            data = response.json()
        except json.JSONDecodeError as error:
            raise TikTokException("Live identity endpoint returned invalid JSON") from error

        response_data = data.get('data') if isinstance(data, dict) else None
        user_not_found = (
            isinstance(data, dict)
            and (
                data.get('statusCode') in (19881007, '19881007')
                or str(data.get('message', '')).lower() == 'user_not_found'
            )
        )
        explicit_offline = user_not_found or (
            isinstance(response_data, dict)
            and 'user' in response_data
            and response_data['user'] is None
        )
        user_data = (
            response_data.get('user')
            if isinstance(response_data, dict)
            and isinstance(response_data.get('user'), dict)
            else {}
        )
        live_room = (
            response_data.get('liveRoom')
            if isinstance(response_data, dict)
            and isinstance(response_data.get('liveRoom'), dict)
            else {}
        )
        room_id = user_data.get('roomId')
        status = live_room.get('status')
        if explicit_offline or (status is not None and status not in (2, '2')):
            state = 'offline'
        elif room_id and status in (2, '2'):
            state = 'live'
        else:
            state = 'unknown'
        identity = {
            'state': state,
            'room_id': room_id,
            'stream_id': live_room.get('streamId'),
            'start_time': live_room.get('startTime'),
            'status': status,
            'owner_user_id': (
                user_data.get('id')
                or user_data.get('userId')
                or user_data.get('user_id')
            ),
        }
        self.live_identity = identity
        logger.info(
            "[LIVE_IDENTITY] "
            f"user={user} "
            f"room_id={identity['room_id']!r} "
            f"stream_id={identity['stream_id']!r} "
            f"start_time={identity['start_time']!r} "
            f"status={identity['status']!r} "
            f"owner_user_id={identity['owner_user_id']!r} "
            f"state={identity['state']}"
        )
        return identity

    def get_room_owner_user_id(self, room_id: str) -> str | None:
        """Return the stable TikTok owner ID exposed by room metadata."""
        response = self.http_client.get(
            f"{self.WEBCAST_URL}/webcast/room/info/"
            f"?aid=1988&room_id={room_id}",
            timeout=self.CONTROL_PLANE_TIMEOUT,
        )
        data = response.json()
        room_data = data.get('data') if isinstance(data, dict) else None
        if not isinstance(room_data, dict):
            return None
        owner = room_data.get('owner')
        owner = owner if isinstance(owner, dict) else {}
        owner_user_id = (
            owner.get('id')
            or room_data.get('owner_user_id')
            or room_data.get('owner_user_id_str')
        )
        return str(owner_user_id) if owner_user_id else None

    def _fallback_stream_client_to_requests(self) -> None:
        """Replace curl_cffi after its TLS state becomes unusable."""
        previous_client = self.http_client
        self.http_client = HttpClient(
            getattr(self, '_proxy', None),
            getattr(self, '_cookies', None),
            force_requests=True,
        ).req
        close = getattr(previous_client, 'close', None)
        if callable(close):
            close()
        logger.warning(
            "[STREAM_TLS] curl_cffi BAD_DECRYPT; switched stream downloads "
            "to requests"
        )

    def _get_room_id_from_web_pages(self, user: str) -> str:
        """Fallback room_id extraction from TikTok web pages."""
        urls = (
            f"{self.BASE_URL}/@{user}/live",
            f"{self.BASE_URL}/@{user}",
        )

        for url in urls:
            try:
                logger.debug(f"[GET_ROOM_ID] Trying web fallback: {url}")
                response = self.http_client.get(
                    url, timeout=self.CONTROL_PLANE_TIMEOUT
                )
            except Exception as e:
                logger.debug(f"[GET_ROOM_ID] Web fallback request failed for {url}: {e}")
                continue

            if response.status_code != 200:
                logger.debug(f"[GET_ROOM_ID] Web fallback status {response.status_code} for {url}")
                continue

            room_id = self._extract_room_id_from_html(response.text)
            if room_id:
                logger.info(f"[GET_ROOM_ID] Extracted room_id from web fallback: {room_id}")
                return room_id

        return ""

    @classmethod
    def _extract_room_id_from_html(cls, html: str) -> str:
        """Extract roomId from TikTok page HTML."""
        for content in (html, html.replace('\\"', '"')):
            for pattern in (
                r'"roomId"\s*:\s*"(\d+)"',
                r'"roomId"\s*:\s*(\d+)',
                r'"room_id"\s*:\s*"(\d+)"',
                r'"room_id"\s*:\s*(\d+)',
            ):
                match = re.search(pattern, content)
                if match:
                    return match.group(1)

        script_patterns = (
            r'<script id="SIGI_STATE"[^>]*>(.*?)</script>',
            r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        )
        for pattern in script_patterns:
            match = re.search(pattern, html, re.DOTALL)
            if not match:
                continue
            try:
                payload = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            room_id = cls._find_room_id_in_payload(payload)
            if room_id:
                return room_id

        return ""

    @classmethod
    def _find_room_id_in_payload(cls, payload) -> str:
        if isinstance(payload, dict):
            for key in ("roomId", "room_id"):
                value = payload.get(key)
                if value:
                    room_id = str(value)
                    if room_id.isdigit() and room_id != "0":
                        return room_id
            for value in payload.values():
                room_id = cls._find_room_id_in_payload(value)
                if room_id:
                    return room_id
        elif isinstance(payload, list):
            for value in payload:
                room_id = cls._find_room_id_in_payload(value)
                if room_id:
                    return room_id
        return ""

    def _handle_waf_challenge_and_retry(self, user: str, challenge_html: str) -> str:
        """
        Handle WAF challenge and retry the request with solved challenge
        """
        try:
            # Solve WAF challenge
            challenge_cookies = self.waf_solver.solve_captcha_challenge(challenge_html)
            logger.info(f"[WAF_SOLVER] Successfully solved WAF challenge for user {user}")

            # Add challenge cookies to session
            self.http_client.cookies.update(challenge_cookies)

            # Retry the original request
            logger.debug(f"[WAF_SOLVER] Retrying API request for user {user} with challenge cookies")
            response = self.http_client.get(self.API_URL, params={
                "uniqueId": user,
                "sourceType": 54,
                "aid": 1988
            }, timeout=self.CONTROL_PLANE_TIMEOUT)

            if response.status_code != 200:
                logger.error(f"[WAF_SOLVER] Retry failed with status {response.status_code}")
                raise IPBlockedByWAF(f"WAF challenge solved but retry failed for user {user}")

            try:
                data = response.json()
            except json.JSONDecodeError as e:
                logger.error(f"[WAF_SOLVER] JSON decode error on retry: {e}")
                raise IPBlockedByWAF(f"WAF challenge solved but response invalid for user {user}")

            # Extract room_id from retry response
            if (data.get('data') and
                data['data'].get('user') and
                data['data']['user'].get('roomId')):
                room_id = data['data']['user']['roomId']
                logger.info(f"[WAF_SOLVER] Successfully recovered room_id after WAF challenge: {room_id}")

                # Free up memory
                del data
                gc.collect()

                return room_id
            else:
                logger.error("[WAF_SOLVER] No room_id found in retry response")
                raise UserLiveException(TikTokError.ROOM_ID_ERROR)

        except Exception as e:
            logger.error(f"[WAF_SOLVER] Failed to solve WAF challenge for user {user}: {e}")
            raise IPBlockedByWAF(f"WAF challenge could not be solved for user {user}: {e}")

    @staticmethod
    def _add_live_url_candidate(
        candidates: list[str],
        candidate: object,
    ) -> None:
        """Append non-empty stream URLs once while preserving their order."""
        if isinstance(candidate, str):
            candidate = candidate.strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)
            return

        if isinstance(candidate, dict):
            preferred_keys = ('FULL_HD1', 'HD1', 'SD2', 'SD1')
            for key in preferred_keys:
                TikTokAPI._add_live_url_candidate(candidates, candidate.get(key))
            for key, value in candidate.items():
                if key not in preferred_keys:
                    TikTokAPI._add_live_url_candidate(candidates, value)

    def get_live_url_candidates(self, room_id: str) -> list[str]:
        """Return ordered, unique CDN URLs advertised for a live room."""
        data = self.http_client.get(
            f"{self.WEBCAST_URL}/webcast/room/info/?aid=1988&room_id={room_id}",
            timeout=self.CONTROL_PLANE_TIMEOUT,
        ).json()

        # Check for follow requirement before processing the data
        if 'Follow the creator to watch their LIVE' in json.dumps(data):
            raise UserLiveException(TikTokError.ACCOUNT_PRIVATE_FOLLOW)

        # Check for private account before processing the data
        if isinstance(data, str) and 'This account is private' in data:
            raise UserLiveException(TikTokError.ACCOUNT_PRIVATE)

        # Extract only the needed information
        room_data = data.get('data') or {}
        room_status = room_data.get('status')
        if room_status is not None and room_status not in (2, '2'):
            logger.debug(
                f"[LIVE_URL] room_id={room_id!r} "
                f"decision=OFFLINE room_status={room_status!r}"
            )
            raise UserLiveException(TikTokError.USER_NOT_CURRENTLY_LIVE)

        stream_url_data = room_data.get('stream_url') or {}
        status_code = data.get('status_code')
        candidates = []

        # Prefer SDK streams ordered by TikTok's quality level.
        pull_data = (
            stream_url_data.get('live_core_sdk_data', {})
            .get('pull_data')
            or {}
        )
        sdk_data_str = pull_data.get('stream_data')
        if sdk_data_str:
            try:
                sdk_data = json.loads(sdk_data_str).get('data', {})
                qualities = pull_data.get('options', {}).get('qualities', [])
                level_map = {
                    quality.get('sdk_key'): quality.get('level', -1)
                    for quality in qualities
                    if isinstance(quality, dict) and quality.get('sdk_key')
                }
                ordered_sdk_keys = sorted(
                    sdk_data,
                    key=lambda key: level_map.get(key, -1),
                    reverse=True,
                )
                for sdk_key in ordered_sdk_keys:
                    entry = sdk_data.get(sdk_key) or {}
                    stream_main = entry.get('main') or {}
                    self._add_live_url_candidate(
                        candidates,
                        stream_main.get('flv'),
                    )
                    self._add_live_url_candidate(
                        candidates,
                        stream_main.get('hls') or stream_main.get('m3u8'),
                    )

            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(
                    "Failed to parse SDK stream data, falling back to "
                    f"legacy method: {e}"
                )
        else:
            logger.warning(
                "No SDK stream data found. Falling back to legacy URLs. "
                "Consider contacting the developer to update the code."
            )

        flv_pull_url = stream_url_data.get('flv_pull_url') or {}
        hls_pull_url = stream_url_data.get('hls_pull_url')
        hls_pull_url_map = stream_url_data.get('hls_pull_url_map')
        self._add_live_url_candidate(candidates, flv_pull_url)
        self._add_live_url_candidate(candidates, hls_pull_url)
        self._add_live_url_candidate(candidates, hls_pull_url_map)
        self._add_live_url_candidate(
            candidates,
            stream_url_data.get('rtmp_pull_url'),
        )

        if not candidates:
            logger.warning(
                f"[LIVE_URL_DIAGNOSTIC] room_id={room_id!r} "
                f"status_code={status_code!r} "
                f"room_status={room_status!r} "
                f"sdk={bool(sdk_data_str)} "
                f"flv={bool(flv_pull_url)} "
                f"hls={bool(hls_pull_url or hls_pull_url_map)} "
                f"rtmp={bool(stream_url_data.get('rtmp_pull_url'))} "
                f"restricted={status_code == 4003110}"
            )

        # Free up memory
        del data, room_data, stream_url_data
        gc.collect()

        if not candidates and status_code == 4003110:
            raise UserLiveException(TikTokError.LIVE_RESTRICTION)

        return candidates

    def get_live_url(self, room_id: str) -> str | None:
        """Return the preferred CDN URL for compatibility with older callers."""
        candidates = self.get_live_url_candidates(room_id)
        live_url = candidates[0] if candidates else None
        logger.info("Selected live media endpoint")

        return live_url

    @staticmethod
    def _validate_stream_response(stream) -> None:
        """Reject HTTP error responses before their body reaches the recorder."""
        status_code = getattr(stream, 'status_code', None)
        if status_code not in (200, 206):
            raise ConnectionError(
                f"Live stream endpoint returned HTTP {status_code}"
            )

        headers = getattr(stream, 'headers', {}) or {}
        content_type = str(
            headers.get('Content-Type') or headers.get('content-type') or ''
        ).lower()
        if (
            content_type.startswith('text/')
            or 'json' in content_type
            or 'xml' in content_type
        ):
            raise ConnectionError(
                f"Live stream endpoint returned non-video Content-Type: "
                f"{content_type or 'unknown'}"
            )

    @staticmethod
    def _validate_initial_stream_payload(payload: bytes) -> None:
        """Reject textual proxy/error bodies while accepting known video containers."""
        sample = payload[:512].lstrip()
        if not sample:
            return
        if (
            sample.startswith(b'FLV')
            or (len(sample) >= 8 and sample[4:8] == b'ftyp')
        ):
            return

        lower_sample = sample.lower()
        known_error = lower_sample.startswith((
            b'upstream connect error',
            b'<!doctype html',
            b'<html',
            b'{',
            b'[',
        ))
        printable_bytes = sum(
            byte in b'\t\n\r' or 32 <= byte <= 126
            for byte in sample
        )
        looks_like_text = printable_bytes / len(sample) >= 0.9
        if known_error or looks_like_text:
            preview = sample[:160].decode('utf-8', errors='replace')
            raise ConnectionError(
                f"Live stream endpoint returned a text error body: {preview}"
            )

    def download_live_stream(self, live_url: str, stop_event=None):
        """
        Generator che restituisce lo streaming live per un dato room_id.
        Uses a generator to avoid loading the entire stream into memory.

        Args:
            live_url: URL of the live stream
            stop_event: Optional threading.Event to signal graceful shutdown
        """
        stream = None
        chunk_count = 0
        initial_payload = bytearray()

        try:
            stream = self.http_client.get(live_url, stream=True)
            self._validate_stream_response(stream)

            for chunk in stream.iter_content(chunk_size=4096):
                # Check for stop signal periodically
                if stop_event and stop_event.is_set():
                    logger.info("📡 Stop signal received in download_live_stream, terminating generator")
                    break

                if not chunk:
                    continue

                if initial_payload is not None:
                    initial_payload.extend(chunk)
                    if len(initial_payload) < 16:
                        continue
                    self._validate_initial_stream_payload(bytes(initial_payload))
                    chunk = bytes(initial_payload)
                    initial_payload = None

                chunk_count += 1
                # Periodically force garbage collection
                if chunk_count % 1000 == 0:  # Every ~4MB
                    gc.collect()

                yield chunk

            if initial_payload:
                self._validate_initial_stream_payload(bytes(initial_payload))
                yield bytes(initial_payload)
        except CurlRequestException as error:
            if 'BAD_DECRYPT' in str(error).upper():
                close = getattr(stream, 'close', None)
                if callable(close):
                    close()
                stream = None
                self._fallback_stream_client_to_requests()
            raise ConnectionError(
                f"Live stream network error: {error}"
            ) from error
        except RequestsRequestException as error:
            raise ConnectionError(
                f"Live stream network error: {error}"
            ) from error
        finally:
            close = getattr(stream, 'close', None)
            if callable(close):
                close()
            gc.collect()

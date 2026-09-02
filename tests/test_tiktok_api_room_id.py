import json

import pytest

from recorder.core.tiktok_api import TikTokAPI
from recorder.utils.custom_exceptions import UserLiveException
from recorder.utils.enums import TikTokError
from scripts.selenium_live_monitor import LiveUserDetector


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise json.JSONDecodeError("missing", "", 0)
        return self._payload


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


def make_api(http_client):
    api = TikTokAPI.__new__(TikTokAPI)
    api.BASE_URL = "https://www.tiktok.com"
    api.WEBCAST_URL = "https://webcast.tiktok.com"
    api.API_URL = "https://www.tiktok.com/api-live/user/room/"
    api.http_client = http_client
    return api


def test_get_room_id_from_user_falls_back_to_live_page_html():
    http_client = FakeHttpClient([
        FakeResponse(payload={"data": {"user": {}}}),
        FakeResponse(text='{"liveRoom":{"status":2,"roomId":"7528462599294765846"}}'),
    ])
    api = make_api(http_client)

    room_id = api.get_room_id_from_user("madie_sky_hunter")

    assert room_id == "7528462599294765846"
    assert http_client.urls == [
        "https://www.tiktok.com/api-live/user/room/",
        "https://www.tiktok.com/@madie_sky_hunter/live",
    ]


def test_get_room_id_from_user_logs_live_identity(caplog):
    http_client = FakeHttpClient([
        FakeResponse(payload={
            "data": {
                "user": {
                    "roomId": "7528462599294765846",
                    "id": "7525149453251937313",
                },
                "liveRoom": {
                    "status": 2,
                    "streamId": "7528462600123456789",
                    "startTime": 1784325000,
                },
            }
        }),
    ])
    api = make_api(http_client)

    with caplog.at_level("INFO", logger="logger"):
        room_id = api.get_room_id_from_user("madie_sky_hunter")

    assert room_id == "7528462599294765846"
    assert api.live_identity == {
        "room_id": "7528462599294765846",
        "stream_id": "7528462600123456789",
        "start_time": 1784325000,
        "status": 2,
        "owner_user_id": "7525149453251937313",
    }
    assert (
        "[LIVE_IDENTITY] user=madie_sky_hunter "
        "room_id='7528462599294765846' "
        "stream_id='7528462600123456789' "
        "start_time=1784325000 status=2 "
        "owner_user_id='7525149453251937313'"
    ) in caplog.text


def test_get_live_identity_returns_direct_api_fields():
    http_client = FakeHttpClient([
        FakeResponse(payload={
            "data": {
                "user": {
                    "roomId": "7528462599294765846",
                    "id": "7525149453251937313",
                },
                "liveRoom": {
                    "status": 2,
                    "streamId": "7528462600123456789",
                    "startTime": 1784325000,
                },
            }
        }),
    ])
    api = make_api(http_client)

    identity = api.get_live_identity("madie_sky_hunter")

    assert identity == {
        "state": "live",
        "room_id": "7528462599294765846",
        "stream_id": "7528462600123456789",
        "start_time": 1784325000,
        "status": 2,
        "owner_user_id": "7525149453251937313",
    }


def test_get_live_identity_distinguishes_explicit_offline_from_missing_data():
    http_client = FakeHttpClient([
        FakeResponse(payload={"data": {"user": None}}),
        FakeResponse(payload=[]),
    ])
    api = make_api(http_client)

    offline = api.get_live_identity("offline_user")
    unknown = api.get_live_identity("malformed_response")

    assert offline["state"] == "offline"
    assert offline["status"] is None
    assert offline["owner_user_id"] is None
    assert unknown["state"] == "unknown"
    assert unknown["status"] is None
    assert unknown["owner_user_id"] is None


def test_get_live_identity_treats_user_not_found_as_offline():
    http_client = FakeHttpClient([
        FakeResponse(payload={
            "data": None,
            "message": "user_not_found",
            "statusCode": 19881007,
        }),
    ])
    api = make_api(http_client)

    identity = api.get_live_identity("freya._r")

    assert identity == {
        "state": "offline",
        "room_id": None,
        "stream_id": None,
        "start_time": None,
        "status": None,
        "owner_user_id": None,
    }


def test_finished_room_overrides_stale_check_alive_result():
    http_client = FakeHttpClient([
        FakeResponse(payload={"data": [{"alive": True}]}),
        FakeResponse(payload={"data": {"status": 4}}),
    ])
    api = make_api(http_client)

    assert api.is_room_alive("7664284156544961302") is False
    assert http_client.urls == [
        "https://webcast.tiktok.com/webcast/room/check_alive/"
        "?aid=1988&region=CH&room_ids=7664284156544961302&user_is_login=true",
        "https://webcast.tiktok.com/webcast/room/info/"
        "?aid=1988&room_id=7664284156544961302",
    ]


def test_live_room_confirms_check_alive_result():
    http_client = FakeHttpClient([
        FakeResponse(payload={"data": [{"alive": True}]}),
        FakeResponse(payload={"data": {"status": 2}}),
    ])
    api = make_api(http_client)

    assert api.is_room_alive("live-room") is True


def test_room_status_lookup_failure_preserves_check_alive_result():
    http_client = FakeHttpClient([
        FakeResponse(payload={"data": [{"alive": True}]}),
        FakeResponse(payload=None),
    ])
    api = make_api(http_client)

    assert api.is_room_alive("temporarily-unverifiable-room") is True


@pytest.mark.parametrize("room_status", [4, "4"])
def test_get_live_url_rejects_ended_room_with_stale_url(room_status):
    stale_url = "https://cdn.example/stale.flv?sign=expired"
    http_client = FakeHttpClient([
        FakeResponse(payload={
            "status_code": 0,
            "data": {
                "status": room_status,
                "stream_url": {
                    "flv_pull_url": {"HD1": stale_url},
                },
            },
        }),
    ])
    api = make_api(http_client)

    with pytest.raises(UserLiveException) as error:
        api.get_live_url("ended-room")

    assert str(error.value) == str(TikTokError.USER_NOT_CURRENTLY_LIVE)


def test_get_live_url_candidates_returns_ordered_unique_streams():
    http_client = FakeHttpClient([
        FakeResponse(payload={
            "status_code": 0,
            "data": {
                "status": 2,
                "stream_url": {
                    "live_core_sdk_data": {
                        "pull_data": {
                            "stream_data": json.dumps({
                                "data": {
                                    "sd": {
                                        "main": {"flv": "https://cdn/sd.flv"},
                                    },
                                    "hd": {
                                        "main": {
                                            "flv": "https://cdn/hd.flv",
                                            "hls": "https://cdn/hd.m3u8",
                                        },
                                    },
                                    "unknown": {
                                        "main": {"flv": "https://cdn/unknown.flv"},
                                    },
                                },
                            }),
                            "options": {
                                "qualities": [
                                    {"sdk_key": "sd", "level": 1},
                                    {"sdk_key": "hd", "level": 3},
                                ],
                            },
                        },
                    },
                    "flv_pull_url": {
                        "HD1": "https://cdn/hd.flv",
                        "SD1": "https://cdn/legacy-sd.flv",
                    },
                    "hls_pull_url": "https://cdn/legacy.m3u8",
                    "rtmp_pull_url": "rtmp://cdn/live",
                },
            },
        }),
    ])
    api = make_api(http_client)

    assert api.get_live_url_candidates("live-room") == [
        "https://cdn/hd.flv",
        "https://cdn/hd.m3u8",
        "https://cdn/sd.flv",
        "https://cdn/unknown.flv",
        "https://cdn/legacy-sd.flv",
        "https://cdn/legacy.m3u8",
        "rtmp://cdn/live",
    ]


def test_missing_live_url_logs_restricted_response_without_url_value(caplog):
    signed_hls_url = "https://cdn.example/live.m3u8?sign=secret"
    http_client = FakeHttpClient([
        FakeResponse(payload={
            "status_code": 4003110,
            "data": {
                "status": 2,
                "stream_url": {"unrecognized_url": signed_hls_url},
            },
        }),
    ])
    api = make_api(http_client)

    with caplog.at_level("WARNING", logger="logger"):
        with pytest.raises(UserLiveException):
            api.get_live_url("restricted-room")

    assert (
        "[LIVE_URL_DIAGNOSTIC] room_id='restricted-room' "
        "status_code=4003110 room_status=2 sdk=False flv=False "
        "hls=False rtmp=False restricted=True"
    ) in caplog.text
    assert signed_hls_url not in caplog.text


def test_get_room_owner_user_id_uses_room_metadata():
    http_client = FakeHttpClient([
        FakeResponse(payload={
            "data": {
                "owner": {"id": 7525149453251937313},
                "owner_user_id": 111,
            }
        }),
    ])
    api = make_api(http_client)

    assert (
        api.get_room_owner_user_id("7664284156544961302")
        == "7525149453251937313"
    )


def test_extract_room_id_from_nested_sigi_state_payload():
    html = '''
        <script id="SIGI_STATE" type="application/json">
            {"LiveRoom":{"liveRoomUserInfo":{"x":{"liveRoom":{"roomId":7528462599294765846}}}}}
        </script>
    '''

    assert TikTokAPI._extract_room_id_from_html(html) == "7528462599294765846"


def test_extract_username_from_live_href():
    href = "https://www.tiktok.com/@real.unique_id/live?lang=en"

    assert LiveUserDetector.extract_username_from_href(href) == "real.unique_id"


def test_extract_username_from_href_uses_canonical_validation():
    assert LiveUserDetector.extract_username_from_href(
        "https://www.tiktok.com/@leja..1/live"
    ) == "leja..1"
    assert LiveUserDetector.extract_username_from_href(
        "https://www.tiktok.com/@..%2Foutside/live"
    ) == ""

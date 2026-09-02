import pytest

import recorder.core.tiktok_recorder as recorder_module
from recorder.core.tiktok_recorder import TikTokRecorder
from recorder.utils.custom_exceptions import UserLiveException
from recorder.utils.enums import Mode, TikTokError


class ExplicitlyOfflineTikTokAPI:
    def __init__(self, proxy, cookies):
        self.live_identity = None

    def is_country_blacklisted(self):
        return False

    def get_room_id_from_user(self, user):
        self.live_identity = {
            'room_id': 'room-1',
            'stream_id': 'stream-1',
            'start_time': 1000,
            'status': 4,
        }
        return 'room-1'


class LiveOwnerTikTokAPI:
    def __init__(self, proxy, cookies):
        self.live_identity = None

    def is_country_blacklisted(self):
        return False

    def get_room_id_from_user(self, user):
        self.live_identity = {
            'room_id': 'room-1',
            'stream_id': 'stream-1',
            'start_time': 1000,
            'status': 2,
            'owner_user_id': None,
        }
        return 'room-1'

    def get_room_owner_user_id(self, room_id):
        assert room_id == 'room-1'
        return '7525149453251937313'


def test_recorder_rejects_explicit_non_live_identity(monkeypatch):
    monkeypatch.setattr(recorder_module, 'TikTokAPI', ExplicitlyOfflineTikTokAPI)

    with pytest.raises(
        UserLiveException,
        match=str(TikTokError.USER_NOT_CURRENTLY_LIVE),
    ):
        TikTokRecorder(
            url=None,
            user='offline_user',
            room_id=None,
            mode=Mode.AUTOMATIC,
            cookies=None,
            proxy=None,
            output=None,
            duration=None,
            use_telegram=False,
        )


def test_recorder_resolves_owner_user_id_from_room_metadata(monkeypatch):
    monkeypatch.setattr(recorder_module, 'TikTokAPI', LiveOwnerTikTokAPI)

    recorder = TikTokRecorder(
        url=None,
        user='alice',
        room_id=None,
        mode=Mode.AUTOMATIC,
        cookies=None,
        proxy=None,
        output=None,
        duration=None,
        use_telegram=False,
    )

    assert recorder.tiktok_owner_user_id == '7525149453251937313'

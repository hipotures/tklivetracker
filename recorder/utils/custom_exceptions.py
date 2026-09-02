from .enums import TikTokError # Relative import


class TikTokException(Exception):
    def __init__(self, message):
        super().__init__(message)


class UserLiveException(Exception):
    def __init__(self, message):
        super().__init__(message)


class IPBlockedByWAF(Exception):
    def __init__(self, message=TikTokError.WAF_BLOCKED):
        super().__init__(message)


class LiveNotFound(Exception):
    pass


class ArgsParseError(Exception):
    pass

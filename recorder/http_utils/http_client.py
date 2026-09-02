import requests as req

try:
    from curl_cffi import Session as CurlCffiSession
except ImportError:
    CurlCffiSession = None

from ..utils.enums import StatusCode # Relative import (up one level)
from ..utils.logger_manager import logger # Relative import (up one level)
from ..utils.security import redact_url_credentials


class HttpClient:

    def __init__(self, proxy=None, cookies=None, force_requests=False):
        self.req = None
        self.proxy = proxy
        self.cookies = cookies
        self.force_requests = force_requests
        self.uses_curl_cffi = False
        self.configure_session()

    def configure_session(self) -> None:
        if CurlCffiSession is not None and not self.force_requests:
            try:
                self.req = CurlCffiSession(impersonate="chrome136")
                self.uses_curl_cffi = True
            except Exception as e:
                logger.warning(f"Could not initialize curl_cffi session, falling back to requests: {e}")

        if self.req is None:
            self.req = req.Session()

        self.req.headers.update({
            "Sec-Ch-Ua": "\"Not/A)Brand\";v=\"8\", \"Chromium\";v=\"126\"",
            "Sec-Ch-Ua-Mobile": "?0", "Sec-Ch-Ua-Platform": "\"Windows\"",
            "Accept-Language": "en-US", "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.127 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,application/json,text/plain,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1", "Sec-Fetch-Dest": "document",
            "Priority": "u=0, i",
            "Referer": "https://www.tiktok.com/",
            "Origin": "https://www.tiktok.com"
        })

        if self.cookies is not None and self.cookies:
            self.req.cookies.update(self.cookies)

        self.check_proxy()

    def check_proxy(self) -> None:
        if self.proxy is None:
            return

        logger.info(f"Testing proxy {redact_url_credentials(self.proxy)}...")
        proxies = {'http': self.proxy, 'https': self.proxy}

        response = req.get(
            "https://ifconfig.me/ip",
            proxies=proxies,
            timeout=10
        )

        if response.status_code == StatusCode.OK:
            self.req.proxies.update(proxies)
            logger.info("Proxy set up successfully")

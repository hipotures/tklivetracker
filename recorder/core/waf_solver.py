import re
import base64
import json
from hashlib import sha256
from ..http_utils.http_client import HttpClient


class WAFSolver:
    """WAF bypass solver for TikTok captcha challenges"""

    def __init__(self, http_client: HttpClient):
        self.http_client = http_client

    def solve_captcha_challenge(self, html_content: str) -> dict:
        """
        Solve TikTok WAF captcha challenge and return cookies

        Args:
            html_content: HTML content containing WAF challenge

        Returns:
            dict: Cookie dictionary to add to session

        Raises:
            ValueError: If challenge cannot be solved
        """
        try:
            # Fix base64 padding helper
            def fix_base64_padding(b64_string):
                padding_needed = (4 - len(b64_string) % 4) % 4
                return b64_string + ('=' * padding_needed)

            # Extract wci (cookie name)
            pattern = r'<p\s+[^>]*id=["\']wci["\'][^>]*class=["\']([^"\']+)["\']'
            match = re.search(pattern, html_content)
            if not match:
                raise ValueError("wci not found in HTML - not a WAF challenge page")
            wci = match.group(1)

            # Extract cs (challenge string)
            pattern = r'<p\s+[^>]*id=["\']cs["\'][^>]*class=["\']([^"\']+)["\']'
            match = re.search(pattern, html_content)
            if not match:
                raise ValueError("cs not found in HTML - invalid WAF challenge")
            cs = match.group(1)

            # Decode challenge from base64
            challenge_data = json.loads(base64.b64decode(fix_base64_padding(cs)))

            # Extract challenge components
            prefix = base64.b64decode(challenge_data['v']['a'])
            expected_hash = base64.b64decode(challenge_data['v']['c']).hex()

            # Brute force solution (up to 1M iterations)
            for i in range(1000000):
                attempt = sha256(prefix + str(i).encode('utf-8')).hexdigest()
                if expected_hash == attempt:
                    # Found solution
                    solution = base64.b64encode(str(i).encode('utf-8')).decode('utf-8')
                    challenge_data['d'] = solution

                    # Create response cookie
                    result = json.dumps(challenge_data)
                    cookie_value = base64.b64encode(result.encode('utf-8')).decode('utf-8')

                    return {wci: cookie_value}

            raise ValueError("WAF challenge could not be solved within 1M iterations")

        except Exception as e:
            raise ValueError(f"Failed to solve WAF challenge: {e}")

    def detect_waf_challenge(self, content: str) -> bool:
        """
        Detect if content contains a WAF challenge

        Args:
            content: Response content to check

        Returns:
            bool: True if WAF challenge detected
        """
        waf_indicators = [
            'Please wait...',
            '<p id="wci"',
            '<p id="cs"',
            'challenge',
            'verification'
        ]

        content_lower = content.lower()
        return any(indicator.lower() in content_lower for indicator in waf_indicators)
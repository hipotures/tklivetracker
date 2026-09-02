import pytest
from curl_cffi.requests.exceptions import RequestException as CurlRequestException

from recorder.core.tiktok_api import TikTokAPI


class FakeStreamResponse:
    def __init__(self, chunks, status_code=200, content_type="video/x-flv"):
        self.chunks = chunks
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self.closed = False

    def iter_content(self, chunk_size):
        assert chunk_size == 4096
        yield from self.chunks

    def close(self):
        self.closed = True


class FakeHttpClient:
    def __init__(self, response):
        self.response = response

    def get(self, _url, stream):
        assert stream is True
        return self.response


class FailingHttpClient:
    def __init__(self, error):
        self.error = error

    def get(self, _url, stream):
        assert stream is True
        raise self.error


class FailingStreamResponse(FakeStreamResponse):
    def iter_content(self, chunk_size):
        assert chunk_size == 4096
        raise CurlRequestException("BAD_DECRYPT")
        yield


def make_api(response):
    api = TikTokAPI.__new__(TikTokAPI)
    api.http_client = FakeHttpClient(response)
    api.live_identity = None
    return api


def make_api_with_client(client):
    api = TikTokAPI.__new__(TikTokAPI)
    api.http_client = client
    api.live_identity = None
    return api


def test_download_rejects_http_error_before_yielding_body():
    response = FakeStreamResponse(
        [b"upstream connect error or disconnect/reset before headers"],
        status_code=503,
        content_type="text/plain",
    )

    with pytest.raises(ConnectionError, match="HTTP 503"):
        list(make_api(response).download_live_stream("https://stream"))

    assert response.closed is True


def test_download_rejects_text_content_type_before_yielding_body():
    response = FakeStreamResponse(
        [b"temporary proxy error"],
        content_type="text/plain; charset=utf-8",
    )

    with pytest.raises(ConnectionError, match="non-video Content-Type"):
        list(make_api(response).download_live_stream("https://stream"))

    assert response.closed is True


@pytest.mark.parametrize(
    "body",
    [
        b"upstream connect error or disconnect/reset before headers",
        b"Gateway connection failed",
        b"<html><body>Service unavailable</body></html>",
    ],
)
def test_download_rejects_text_error_disguised_as_binary(body):
    response = FakeStreamResponse(
        [body],
        content_type="application/octet-stream",
    )

    with pytest.raises(ConnectionError, match="text error body"):
        list(make_api(response).download_live_stream("https://stream"))

    assert response.closed is True


def test_download_yields_valid_flv_without_changing_bytes():
    chunks = [b"FL", b"V\x01\x05\x00\x00\x00\x09binary", b"video-data"]
    response = FakeStreamResponse(chunks)

    downloaded = b"".join(
        make_api(response).download_live_stream("https://stream")
    )

    assert downloaded == b"".join(chunks)
    assert response.closed is True


def test_download_converts_curl_connection_failure_to_retryable_error():
    api = make_api_with_client(
        FailingHttpClient(CurlRequestException("BAD_DECRYPT"))
    )
    previous_client = api.http_client

    with pytest.raises(ConnectionError, match="BAD_DECRYPT"):
        list(api.download_live_stream("https://stream"))

    assert api.http_client is not previous_client


def test_download_converts_curl_read_failure_and_closes_response():
    response = FailingStreamResponse([])

    with pytest.raises(ConnectionError, match="BAD_DECRYPT"):
        list(make_api(response).download_live_stream("https://stream"))

    assert response.closed is True

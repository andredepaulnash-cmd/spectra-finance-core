# Modified for Spectra Finance on 2026-10-10.
# Changes: disable implicit HTTP redirects and reject redirect responses.
# Original: Hummingbot core 9af100d6822da7d2d0291a906c730ef172284ee2 (Apache-2.0).

import aiohttp

from hummingbot.core.web_assistant.connections.data_types import RESTRequest, RESTResponse


SPECTRA_EXECUTION_CONTRACT = "spectra-single-attempt-v1"


class RESTConnection:
    SPECTRA_EXECUTION_CONTRACT = SPECTRA_EXECUTION_CONTRACT
    def __init__(self, aiohttp_client_session: aiohttp.ClientSession):
        self._client_session = aiohttp_client_session

    async def call(self, request: RESTRequest) -> RESTResponse:
        aiohttp_resp = await self._client_session.request(
            method=request.method.value,
            url=request.url,
            params=request.params,
            data=request.data,
            headers=request.headers,
            allow_redirects=False,
        )

        if 300 <= aiohttp_resp.status < 400:
            aiohttp_resp.release()
            raise IOError("Implicit REST redirects are disabled for mutation safety")

        resp = await self._build_resp(aiohttp_resp)
        return resp

    @staticmethod
    async def _build_resp(aiohttp_resp: aiohttp.ClientResponse) -> RESTResponse:
        resp = RESTResponse(aiohttp_resp)
        return resp

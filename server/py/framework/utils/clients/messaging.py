# Copyright 2024 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import contextlib
import copy
import http
import http.cookies
import re
import typing
import urllib
import urllib.parse

import aiohttp
import fastapi

import mlrun.common.schemas
import mlrun.errors
import mlrun.utils.singleton
from mlrun.utils import logger

import framework.utils.clients.discovery

PREFIX_GROUPING = re.compile(r"^([a-z/-]+)/((?:v\d+)?).*")


class Client(metaclass=mlrun.utils.singleton.AbstractSingleton):
    def __init__(self) -> None:
        super().__init__()
        # Session is used to forward request thus retry is disabled
        self._session: typing.Optional[mlrun.utils.AsyncClientWithRetry] = None
        # Retry session is for internal messaging
        self._retry_session: typing.Optional[mlrun.utils.AsyncClientWithRetry] = None
        self._discovery = framework.utils.clients.discovery.Client()

    async def proxy_request(self, request: fastapi.Request):
        method = request.method
        path = request.url.path

        path, version, service_instance = self._prepare_request_data(method, path)
        if not service_instance:
            raise mlrun.errors.MLRunNotFoundError(
                f"Failed to proxy request, service for path {path} not found"
            )

        # The service and version prefixes have been removed from the path earlier in the process.
        # The service prefix will be replaced with the new service name, and the version will be re-added
        # (or default to v1 if not present) during the final URL construction for the request.
        url = f"{service_instance.url}/{service_instance.name}/{version}/{path}"
        return await self.proxy_request_to_service(
            service_instance.name, method, url, request
        )

    async def proxy_request_to_service(
        self,
        service_name: str,
        method: str,
        url: str,
        request: fastapi.Request = None,
        json: typing.Optional[dict] = None,
        raise_on_failure: bool = False,
        **kwargs,
    ) -> fastapi.Response:
        request_kwargs = await self._resolve_request_kwargs_from_request(
            request, json, **kwargs
        )

        async with self.send_request(
            service_name=service_name,
            method=method,
            url=url,
            raise_on_failure=raise_on_failure,
            **request_kwargs,
        ) as service_response:
            return await self.convert_requests_response_to_fastapi_response(
                service_response
            )

    @contextlib.asynccontextmanager
    async def send_request(
        self,
        service_name: str,
        method: str,
        url: str,
        raise_on_failure: bool = False,
        **kwargs,
    ) -> aiohttp.ClientResponse:
        await self._ensure_session()
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = (
                mlrun.mlconf.httpdb.clusterization.worker.request_timeout or 20
            )

        kwargs_to_log = self._resolve_kwargs_to_log(kwargs)
        logger.debug(
            "Sending request to service",
            service_name=service_name,
            method=method,
            url=url,
            **kwargs_to_log,
        )
        response = None
        try:
            response = await self._session.request(
                method, url, verify_ssl=False, **kwargs
            )
            if not response.ok:
                await self._on_request_failure(
                    service_name=service_name,
                    method=method,
                    path=url,
                    response=response,
                    raise_on_failure=raise_on_failure,
                    **kwargs,
                )
            else:
                logger.debug(
                    "Request to service succeeded",
                    service_name=service_name,
                    method=method,
                    url=url,
                    **kwargs_to_log,
                )
            yield response
        finally:
            if response:
                response.release()

    @staticmethod
    async def convert_requests_response_to_fastapi_response(
        service_response: aiohttp.ClientResponse,
    ) -> fastapi.Response:
        # based on the way we implemented the exception handling for endpoints in MLRun we can expect the media type
        # of the response to be of type application/json, see services.api.http_status_error_handler for reference
        return fastapi.responses.Response(
            content=await service_response.text(),
            status_code=service_response.status,
            headers=dict(
                service_response.headers
            ),  # service_response.headers is of type CaseInsensitiveDict
            media_type="application/json",
        )

    def is_forwarded_request(self, request: fastapi.Request) -> bool:
        """
        Checks whether the request should be forwarded to another service based on
        the service and path being resolved.

        :param request: The incoming FastAPI request.
        :return: True if the request should be forwarded, False otherwise.
        """
        method = request.method
        path = request.url.path
        path, version, service_instance = self._prepare_request_data(method, path)
        return service_instance is not None

    async def _ensure_session(self):
        if not self._session:
            self._session = mlrun.utils.AsyncClientWithRetry(
                # This client handles forwarding requests from api to other services.
                # if we receive 5XX error, the code will be returned to the client.
                #  if client is the SDK - it will handle and retry the request itself, upon its own retry policy
                #  if the client is UI  - it will propagate the error to the user.
                # Thus, do not retry.
                # only exceptions (e.g.: connection initiating).
                raise_for_status=False,
            )

            # if we go any HTTP response, return it, do not retry.
            # by returning `True`, we tell the client the response is "legit" and so, it returns it to its callee.
            self._session.retry_options.evaluate_response_callback = lambda _: True

    async def _ensure_retry_session(self):
        if not self._retry_session:
            self._retry_session = mlrun.utils.AsyncClientWithRetry()

    @staticmethod
    async def _on_request_failure(
        service_name: str,
        method: str,
        path: str,
        response: aiohttp.ClientResponse,
        raise_on_failure: bool,
        **kwargs,
    ):
        log_kwargs = Client._resolve_kwargs_to_log(kwargs)
        log_kwargs.update({"method": method, "path": path})
        log_kwargs.update(
            {
                "service_name": service_name,
                "status_code": response.status,
                "reason": response.reason,
                "real_url": str(response.real_url),
            }
        )
        if response.content:
            try:
                data = await response.json()
                error = data.get("error")
                error_stack_trace = data.get("errorStackTrace")
            except Exception:
                pass
            else:
                log_kwargs.update(
                    {"error": error, "error_stack_trace": error_stack_trace}
                )
        logger.warning("Request to service failed", **log_kwargs)
        if raise_on_failure:
            mlrun.errors.raise_for_status(response)

    @staticmethod
    def _resolve_kwargs_to_log(kwargs: dict) -> dict:
        kwargs_to_log = {}
        for key in ["headers", "params", "timeout"]:
            kwargs_to_log[key] = copy.deepcopy(kwargs.get(key))

        # omit sensitive data from logs
        if headers := kwargs_to_log.get("headers", {}):
            for header in ["cookie", "authorization"]:
                if header in headers:
                    headers[header] = "****"
            kwargs_to_log["headers"] = headers
        return kwargs_to_log

    @staticmethod
    async def _resolve_request_kwargs_from_request(
        request: fastapi.Request = None, json: typing.Optional[dict] = None, **kwargs
    ) -> dict:
        request_kwargs = {}
        if request:
            # either explicitly passed json or read from request body
            content_length = request.headers.get("content-length", "0")
            if json is not None:
                request_kwargs.update({"json": json})
            elif content_length and content_length != "0":
                try:
                    request_kwargs.update({"json": await request.json()})
                except Exception as exc:
                    logger.warning(
                        "Failed to read request body",
                        error=mlrun.errors.err_to_str(exc),
                        request_id=request.state.request_id,
                    )
                    raise mlrun.errors.MLRunBadRequestError(
                        "Failed to read request body, expected json body"
                    ) from exc
            request_kwargs.update({"headers": dict(request.headers)})
            # Preserve duplicate query params as a list of (key, value) tuples
            request_kwargs.update({"params": list(request.query_params.multi_items())})
            request_kwargs.update({"cookies": request.cookies})
            request_kwargs["headers"].setdefault(
                "x-request-id", request.state.request_id
            )
            if service_name := request.app.extra.get("mlrun_service_name"):
                request_kwargs["headers"].setdefault(
                    "x-mlrun-origin-service-name", service_name
                )

        # mask clients host with worker's host
        origin_host = request_kwargs.get("headers", {}).pop("host", None)
        if origin_host:
            # original host requested by client
            request_kwargs["headers"]["x-forwarded-host"] = origin_host

        # let the http client calculate it itself
        # or we will hit serious issues with reverse-proxying (client<->worker<->chief) requests
        request_kwargs.get("headers", {}).pop("content-length", None)

        for cookie_name in list(request_kwargs.get("cookies", {}).keys()):
            # defensive programming - to avoid setting reserved cookie names and explode
            # e.g.: when setting "domain" cookie, it will explode, see python internal http client for more details.
            if http.cookies.Morsel().isReservedKey(cookie_name):
                del request_kwargs["cookies"][cookie_name]

            # iguazio auth cookies might include special characters. to ensure the http client wont escape them
            # we will url-encode them (aka quote), so the value would be safe against such escaping.
            # e.g.: instead of having "x":"y" being escaped to "\"x\":\"y\"", it will be escaped to "%22x%22:%22y%22"
            elif cookie_name == "session" and mlrun.mlconf.is_running_on_iguazio():
                # unquote first, to avoid double quoting ourselves, in case the cookie is already quoted
                unquoted_session = urllib.parse.unquote(
                    request_kwargs["cookies"][cookie_name]
                )
                request_kwargs["cookies"][cookie_name] = urllib.parse.quote(
                    unquoted_session
                )

        request_kwargs.update(**kwargs)
        return request_kwargs

    @staticmethod
    def _get_prefix_and_version(path: str):
        match = PREFIX_GROUPING.match(path)
        if not match:
            raise ValueError(f"Invalid path format: {path}")

        prefix = match.group(1)
        # default to v1 if not present
        version = match.group(2) or "v1"
        return prefix, version

    def _prepare_request_data(self, method: str, path: str):
        prefix, version = self._get_prefix_and_version(path)
        path = path.removeprefix(f"{prefix}/").removeprefix(f"{version}/")
        service_instance = self._discovery.resolve_service_by_request(method, path)
        return path, version, service_instance

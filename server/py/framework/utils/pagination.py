# Copyright 2023 Iguazio
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
#
import inspect
import typing

import orjson
import pydantic.v1
import sqlalchemy.orm

import mlrun.common.schemas
import mlrun.errors
import mlrun.utils.singleton
from mlrun import mlconf
from mlrun.utils import logger

import framework.utils.asyncio
import framework.utils.pagination_cache


def _generate_pydantic_schema_from_method_signature(
    method: typing.Callable,
) -> pydantic.v1.BaseModel:
    """
    Generate a Pydantic model based on the signature of a method.
    This is used to save the given parameters to the method in the pagination cache as a serialized Pydantic
    model that can then be deserialized to the correct types and passed back to the method when the cache is used.
    """

    class Config:
        arbitrary_types_allowed = True

    parameters = inspect.signature(method).parameters
    fields = {
        name: (
            parameter.annotation,
            # if the parameter has a default value, use it, otherwise use ... placeholder
            # to indicate that the parameter is required
            parameter.default if parameter.default != inspect.Parameter.empty else ...,
        )
        for name, parameter in parameters.items()
        # ignore the session parameter as the methods get a new session each time
        if parameter.annotation != sqlalchemy.orm.Session
    }
    return pydantic.v1.create_model(
        f"{method.__name__}_schema", __config__=Config, **fields
    )


class PaginatedMethods(metaclass=mlrun.utils.singleton.Singleton):
    _methods: list[typing.Callable] = []
    _method_map = {}

    @classmethod
    def add_method(cls, method: typing.Callable):
        cls._methods.append(method)
        cls._method_map[method.__name__] = {
            "method": method,
            "schema": _generate_pydantic_schema_from_method_signature(method),
        }

    @classmethod
    def method_is_supported(cls, method: typing.Union[str, typing.Callable]) -> bool:
        method_name = method if isinstance(method, str) else method.__name__
        return method_name in cls._method_map

    @classmethod
    def get_method(cls, method_name: str) -> typing.Callable:
        return cls._method_map[method_name]["method"]

    @classmethod
    def get_method_schema(cls, method_name: str) -> pydantic.v1.BaseModel:
        return cls._method_map[method_name]["schema"]


class Paginator(metaclass=mlrun.utils.singleton.Singleton):
    def __init__(self):
        self._logger = logger.get_child("paginator")
        self._pagination_cache = framework.utils.pagination_cache.PaginationCache()

    async def paginate_permission_filtered_request(
        self,
        session: sqlalchemy.orm.Session,
        method: typing.Callable,
        filter_: typing.Callable,
        auth_info: typing.Optional[mlrun.common.schemas.AuthInfo] = None,
        token: typing.Optional[str] = None,
        page: typing.Optional[int] = None,
        page_size: typing.Optional[int] = None,
        **method_kwargs,
    ) -> tuple[typing.Any, dict[str, typing.Union[str, int]]]:
        """
        Paginate a request and filter the results based on the provided filter function.
        If the result of the filter has fewer items than the page size, the pagination will request more items until
        the page size is reached.
        There is an option here to overflow and to receive more items than the page size.
        And actually the maximum number of items that can be returned is page_size * 2 - 1.
        """
        last_pagination_info = mlrun.common.schemas.pagination.PaginationInfo()
        current_page = page
        result = []

        while not page_size or len(result) < page_size:
            new_result, pagination_info = await self.paginate_request(
                session,
                method,
                auth_info,
                token,
                current_page,
                page_size,
                **method_kwargs,
            )
            new_result = await framework.utils.asyncio.await_or_call_in_threadpool(
                filter_, new_result
            )
            result.extend(new_result)

            if not pagination_info:
                # no more results
                break

            last_pagination_info = pagination_info
            current_page = last_pagination_info.page + 1
            page_size = last_pagination_info.page_size

        return result, last_pagination_info.dict(by_alias=True)

    async def paginate_request(
        self,
        session: sqlalchemy.orm.Session,
        method: typing.Callable,
        auth_info: typing.Optional[mlrun.common.schemas.AuthInfo] = None,
        token: typing.Optional[str] = None,
        page: typing.Optional[int] = None,
        page_size: typing.Optional[int] = None,
        **method_kwargs,
    ) -> tuple[
        typing.Any, typing.Optional[mlrun.common.schemas.pagination.PaginationInfo]
    ]:
        if not PaginatedMethods.method_is_supported(method):
            raise NotImplementedError(
                f"Pagination is not supported for method {method.__name__}"
            )

        if page_size is None and token is None:
            self._logger.debug(
                "No token or page size provided, returning all records",
                method=method.__name__,
            )
            return await framework.utils.asyncio.await_or_call_in_threadpool(
                method, session, **method_kwargs
            ), None

        if page is not None and page > mlconf.httpdb.pagination.page_limit:
            raise mlrun.errors.MLRunInvalidArgumentError(
                f"'page' must be less than or equal to {mlconf.httpdb.pagination.page_limit}"
            )

        if (
            page_size is not None
            and page_size > mlconf.httpdb.pagination.page_size_limit
        ):
            raise mlrun.errors.MLRunInvalidArgumentError(
                f"'page_size' must be less than or equal to {mlconf.httpdb.pagination.page_size_limit}"
            )

        page_size = page_size or mlconf.httpdb.pagination.default_page_size

        (
            token,
            page,
            page_size,
            method,
            method_kwargs,
        ) = self._create_or_update_pagination_cache_record(
            session,
            method,
            auth_info,
            token,
            page,
            page_size,
            **method_kwargs,
        )

        self._logger.debug(
            "Retrieving page",
            page=page,
            page_size=page_size,
            method=method.__name__,
        )
        offset, limit = self._calculate_offset_and_limit(page, page_size)
        items = await framework.utils.asyncio.await_or_call_in_threadpool(
            method, session, **method_kwargs, offset=offset, limit=limit
        )
        pagination_info = mlrun.common.schemas.pagination.PaginationInfo(
            page=page, page_size=page_size, page_token=token
        )

        # The following 2 conditions indicate the end of the pagination.
        # On the last page, we don't return the token, but we keep it live in the cache
        # so the client can access previous pages.
        # the token will be revoked after some time of none-usage.
        if len(items) == 0:
            return [], None
        if len(items) < page_size + 1:
            # If we got fewer items than the page_size + 1, we know that there are no more items
            # and this is the last page.
            pagination_info.page_token = None

        # truncate the items to the page size
        items = items[:page_size]
        return items, pagination_info

    def _create_or_update_pagination_cache_record(
        self,
        session: sqlalchemy.orm.Session,
        method: typing.Callable,
        auth_info: typing.Optional[mlrun.common.schemas.AuthInfo] = None,
        token: typing.Optional[str] = None,
        page: typing.Optional[int] = None,
        page_size: typing.Optional[int] = None,
        **method_kwargs,
    ) -> tuple[str, int, int, typing.Callable, dict]:
        if token:
            self._logger.debug(
                "Token provided, updating pagination cache record", token=token
            )
            pagination_cache_record = (
                self._pagination_cache.get_pagination_cache_record(session, key=token)
            )
            if pagination_cache_record is None:
                raise mlrun.errors.MLRunPaginationEndOfResultsError(
                    f"Token {token} not found in pagination cache"
                )
            method = PaginatedMethods.get_method(pagination_cache_record.function)
            method_kwargs = orjson.loads(pagination_cache_record.kwargs)
            page = page or pagination_cache_record.current_page + 1
            page_size = pagination_cache_record.page_size
            user = pagination_cache_record.user

            if user and (not auth_info or auth_info.user_id != user):
                raise mlrun.errors.MLRunAccessDeniedError(
                    "User is not allowed to access this token"
                )

        # upsert pagination cache record to update last_accessed time or create a new record
        method_schema = PaginatedMethods.get_method_schema(method.__name__)
        kwargs_schema = method_schema(**method_kwargs)
        kwargs_schema.offset = None
        kwargs_schema.limit = None
        self._logger.debug(
            "Storing pagination cache record",
            method=method.__name__,
            page=page,
            page_size=page_size,
        )
        token = self._pagination_cache.store_pagination_cache_record(
            session,
            user=auth_info.user_id if auth_info else None,
            method=method,
            current_page=page,
            page_size=page_size,
            kwargs=kwargs_schema.json(exclude_none=True),
        )
        return token, page, page_size, method, kwargs_schema.dict(exclude_none=True)

    @staticmethod
    def _calculate_offset_and_limit(
        page: typing.Optional[int] = None,
        page_size: typing.Optional[int] = None,
    ) -> tuple[typing.Optional[int], typing.Optional[int]]:
        if page is not None:
            page_size = page_size or mlconf.httpdb.pagination.default_page_size

            if page < 1 or page_size < 1:
                raise mlrun.errors.MLRunInvalidArgumentError(
                    "Page and page size must be greater than 0"
                )

            # returning the limit with 1 extra record to check if there are more records
            # and to know if we should return the token or not
            return (page - 1) * page_size, page_size + 1
        return None, None

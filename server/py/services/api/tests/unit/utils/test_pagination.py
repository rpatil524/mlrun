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
import datetime
import typing

import pytest
import sqlalchemy.orm

import mlrun.common.schemas
from mlrun import mlconf
from mlrun.utils import logger

import framework.db.sqldb.models
import framework.utils.pagination
import framework.utils.pagination_cache


def paginated_method(
    session: sqlalchemy.orm.Session,
    total_amount: int,
    since: typing.Optional[datetime.datetime] = None,
    offset: typing.Optional[int] = None,
    limit: typing.Optional[int] = None,
):
    items = [{"name": f"item{i}", "since": since} for i in range(total_amount)]
    offset = offset or 0
    limit = limit or total_amount
    if offset >= total_amount:
        return []
    return items[offset : offset + limit]


@pytest.fixture()
def mock_paginated_method(monkeypatch):
    class Schema:
        def __init__(self, **kwargs):
            self._dict = kwargs

        def dict(self):
            return self._dict

    monkeypatch.setattr(
        framework.utils.pagination.PaginatedMethods,
        "_method_map",
        {
            paginated_method.__name__: {
                "method": paginated_method,
                "schema": framework.utils.pagination._generate_pydantic_schema_from_method_signature(
                    paginated_method
                ),
            }
        },
    )
    yield paginated_method


@pytest.fixture()
def cleanup_pagination_cache_on_teardown(db: sqlalchemy.orm.Session):
    yield
    framework.utils.pagination_cache.PaginationCache().cleanup_pagination_cache(db)


def test_paginated_method():
    """
    Test the above paginated_method function, which is used as a mock for the paginated methods
    in the following tests.
    """
    total_amount = 10
    page_size = 3
    since = datetime.datetime.now()
    paginator = framework.utils.pagination.Paginator()

    offset, limit = paginator._calculate_offset_and_limit(1, page_size)
    items = paginated_method(None, total_amount, since, offset, limit - 1)
    assert len(items) == page_size
    assert items[0]["name"] == "item0"
    assert items[1]["name"] == "item1"
    assert items[2]["name"] == "item2"
    assert items[0]["since"] == items[1]["since"] == items[2]["since"] == since

    offset, limit = paginator._calculate_offset_and_limit(2, page_size)
    items = paginated_method(None, total_amount, since, offset, limit - 1)
    assert len(items) == page_size
    assert items[0]["name"] == "item3"
    assert items[1]["name"] == "item4"
    assert items[2]["name"] == "item5"
    assert items[0]["since"] == items[1]["since"] == items[2]["since"] == since

    offset, limit = paginator._calculate_offset_and_limit(3, page_size)
    items = paginated_method(None, total_amount, since, offset, limit - 1)
    assert len(items) == page_size
    assert items[0]["name"] == "item6"
    assert items[1]["name"] == "item7"
    assert items[2]["name"] == "item8"
    assert items[0]["since"] == items[1]["since"] == items[2]["since"] == since

    offset, limit = paginator._calculate_offset_and_limit(4, page_size)
    items = paginated_method(None, total_amount, since, offset, limit - 1)
    assert len(items) == 1
    assert items[0]["name"] == "item9"
    assert items[0]["since"] == since

    offset, limit = paginator._calculate_offset_and_limit(5, page_size)
    items = paginated_method(None, total_amount, since, offset, limit - 1)
    assert len(items) == 0


@pytest.mark.asyncio
async def test_paginate_request(
    mock_paginated_method,
    cleanup_pagination_cache_on_teardown,
    db: sqlalchemy.orm.Session,
):
    """
    Test pagination happy flow.
    Request paginated method with page and page size, and verify that the correct items are returned.
    Check the db for the pagination cache record.
    Continue requesting the next page until the end of the items. Meanwhile, check the db for the pagination
    cache record updates.
    Once response is empty, verify that the cache record was removed.
    """
    auth_info = mlrun.common.schemas.AuthInfo(user_id="user1")
    page_size = 3
    method_kwargs = {"total_amount": 5, "since": datetime.datetime.now()}

    paginator = framework.utils.pagination.Paginator()

    logger.info("Requesting first page")
    response, pagination_info = await paginator.paginate_request(
        db, paginated_method, auth_info, None, 1, page_size, **method_kwargs
    )
    _assert_paginated_response(
        response,
        pagination_info,
        1,
        page_size,
        ["item0", "item1", "item2"],
        method_kwargs["since"],
    )

    logger.info("Checking db cache record")
    cache_record = (
        framework.utils.pagination_cache.PaginationCache().get_pagination_cache_record(
            db, pagination_info.page_token
        )
    )
    _assert_cache_record(
        cache_record, auth_info.user_id, paginated_method, 1, page_size
    )

    logger.info("Requesting second page")
    response, pagination_info = await paginator.paginate_request(
        db, paginated_method, auth_info, pagination_info.page_token
    )
    _assert_paginated_response(
        response,
        pagination_info,
        2,
        page_size,
        ["item3", "item4"],
        method_kwargs["since"],
        last_page=True,
    )

    logger.info("Checking db cache record")
    cache_record = (
        framework.utils.pagination_cache.PaginationCache().get_pagination_cache_record(
            db, pagination_info.page_token
        )
    )
    _assert_cache_record(
        cache_record, auth_info.user_id, paginated_method, 2, page_size
    )


@pytest.mark.asyncio
async def test_paginate_other_users_token(
    mock_paginated_method,
    cleanup_pagination_cache_on_teardown,
    db: sqlalchemy.orm.Session,
):
    """
    Test pagination with a token that was created by a different user.
    Request paginated method with page and page size, and verify that the correct items are returned.
    Check the db for the pagination cache record.
    Request the next page with the token, and with different user, and verify that a AccessDeniedError is raised.
    """
    auth_info_1 = mlrun.common.schemas.AuthInfo(user_id="user1")
    auth_info_2 = mlrun.common.schemas.AuthInfo(user_id="user2")
    page_size = 3
    method_kwargs = {"total_amount": 5, "since": datetime.datetime.now()}

    paginator = framework.utils.pagination.Paginator()

    logger.info("Requesting first page with user1")
    response, pagination_info = await paginator.paginate_request(
        db, paginated_method, auth_info_1, None, 1, page_size, **method_kwargs
    )
    _assert_paginated_response(
        response,
        pagination_info,
        1,
        page_size,
        ["item0", "item1", "item2"],
        method_kwargs["since"],
    )

    logger.info("Checking db cache record")
    cache_record = (
        framework.utils.pagination_cache.PaginationCache().get_pagination_cache_record(
            db, pagination_info.page_token
        )
    )
    _assert_cache_record(
        cache_record, auth_info_1.user_id, paginated_method, 1, page_size
    )

    logger.info("Requesting second page with user2, should raise AccessDeniedError")
    with pytest.raises(mlrun.errors.MLRunAccessDeniedError):
        await paginator.paginate_request(
            db, paginated_method, auth_info_2, pagination_info.page_token
        )

    logger.info(
        "Requesting second page without auth info, should raise AccessDeniedError"
    )
    with pytest.raises(mlrun.errors.MLRunAccessDeniedError):
        await paginator.paginate_request(
            db, paginated_method, None, pagination_info.page_token
        )


@pytest.mark.asyncio
async def test_paginate_no_auth(
    mock_paginated_method,
    cleanup_pagination_cache_on_teardown,
    db: sqlalchemy.orm.Session,
):
    """
    Test pagination with no auth info.
    Request paginated method without auth info, verify that the correct items are returned.
    Check the db for the pagination cache record.
    Request the next page with auth info of some user, and verify that the request is successful.
    """
    page_size = 3
    method_kwargs = {"total_amount": 5, "since": datetime.datetime.now()}

    paginator = framework.utils.pagination.Paginator()

    logger.info("Requesting first page")
    response, pagination_info = await paginator.paginate_request(
        db, paginated_method, None, None, 1, page_size, **method_kwargs
    )
    _assert_paginated_response(
        response,
        pagination_info,
        1,
        page_size,
        ["item0", "item1", "item2"],
        method_kwargs["since"],
    )

    logger.info("Checking db cache record")
    cache_record = (
        framework.utils.pagination_cache.PaginationCache().get_pagination_cache_record(
            db, pagination_info.page_token
        )
    )
    _assert_cache_record(cache_record, None, paginated_method, 1, page_size)

    logger.info("Requesting second page with auth info of some user")
    auth_info = mlrun.common.schemas.AuthInfo(user_id="any-user")
    # save token as it will be overridden with None on the last page
    old_token = pagination_info.page_token
    response, pagination_info = await paginator.paginate_request(
        db, paginated_method, auth_info, pagination_info.page_token
    )
    _assert_paginated_response(
        response,
        pagination_info,
        2,
        page_size,
        ["item3", "item4"],
        method_kwargs["since"],
        last_page=True,
    )

    logger.info("Checking old db cache record")
    cache_record = (
        framework.utils.pagination_cache.PaginationCache().get_pagination_cache_record(
            db, old_token
        )
    )
    # The request with AuthInfo creates a new cache record, therefore the old one
    # should still be on page 1 and without a user.
    # The new one should be on page 2 and with the user, however, since it's on the last page,
    # we don't get a token back to check.
    _assert_cache_record(cache_record, None, paginated_method, 1, page_size)

    logger.info("Requesting second page without auth info")
    response, pagination_info = await paginator.paginate_request(
        db, paginated_method, None, old_token
    )
    _assert_paginated_response(
        response,
        pagination_info,
        2,
        page_size,
        ["item3", "item4"],
        method_kwargs["since"],
        last_page=True,
    )

    logger.info("Checking old db cache record again")
    cache_record = (
        framework.utils.pagination_cache.PaginationCache().get_pagination_cache_record(
            db, old_token
        )
    )
    _assert_cache_record(cache_record, None, paginated_method, 2, page_size)


@pytest.mark.asyncio
async def test_no_pagination(
    mock_paginated_method,
    cleanup_pagination_cache_on_teardown,
    db: sqlalchemy.orm.Session,
):
    """
    Test pagination with no page and page size.
    Request paginated method with no page and page size, and verify that all items are returned.
    """
    auth_info = mlrun.common.schemas.AuthInfo(user_id="user1")
    method_kwargs = {"total_amount": 5, "since": datetime.datetime.now()}

    paginator = framework.utils.pagination.Paginator()

    logger.info("Requesting all items")
    response, pagination_info = await paginator.paginate_request(
        db,
        paginated_method,
        auth_info,
        token=None,
        page=None,
        page_size=None,
        **method_kwargs,
    )
    assert len(response) == 5
    assert not pagination_info

    logger.info("Checking that no cache record was created")
    assert (
        len(
            framework.utils.pagination_cache.PaginationCache().list_pagination_cache_records(
                db
            )
        )
        == 0
    )


@pytest.mark.asyncio
async def test_pagination_not_supported(
    mock_paginated_method,
    cleanup_pagination_cache_on_teardown,
    db: sqlalchemy.orm.Session,
):
    """
    Test pagination with a method that is not supported.
    Request a method that is not supported for pagination, and verify that a NotImplementedError is raised.
    """
    auth_info = mlrun.common.schemas.AuthInfo(user_id="user1")
    method_kwargs = {"total_amount": 5, "since": datetime.datetime.now()}

    paginator = framework.utils.pagination.Paginator()

    logger.info("Requesting a method that is not supported for pagination")
    with pytest.raises(NotImplementedError):
        await paginator.paginate_request(
            db,
            lambda: paginated_method(5, 1, 3),
            auth_info,
            token=None,
            page=1,
            page_size=3,
            **method_kwargs,
        )


@pytest.mark.asyncio
async def test_pagination_cache_cleanup(
    mock_paginated_method,
    cleanup_pagination_cache_on_teardown,
    db: sqlalchemy.orm.Session,
):
    """
    Test pagination cache cleanup.
    Create paginated cache records and check that they are removed when calling cleanup_pagination_cache.
    """
    auth_info = mlrun.common.schemas.AuthInfo(user_id="user1")
    method_kwargs = {"total_amount": 5, "since": datetime.datetime.now()}
    page_size = 3
    token = None

    paginator = framework.utils.pagination.Paginator()

    logger.info("Creating paginated cache records")
    for i in range(3):
        _, pagination_info = await paginator.paginate_request(
            db,
            paginated_method,
            auth_info,
            None,
            1,
            page_size + i,
            **method_kwargs,
        )
        # get the token only once, so we don't override it with none on the last page
        if not token:
            token = pagination_info.page_token

    assert (
        len(
            framework.utils.pagination_cache.PaginationCache().list_pagination_cache_records(
                db
            )
        )
        == 3
    )

    logger.info("Cleaning up pagination cache")
    paginator._pagination_cache.cleanup_pagination_cache(db)
    db.commit()

    logger.info("Checking that all records were removed")
    assert (
        len(
            framework.utils.pagination_cache.PaginationCache().list_pagination_cache_records(
                db
            )
        )
        == 0
    )

    logger.info("Try to get page with token")
    with pytest.raises(mlrun.errors.MLRunNotFoundError):
        await paginator.paginate_request(
            db,
            paginated_method,
            auth_info,
            token,
            1,
            page_size,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permitted_items,target_page",
    [
        (
            [
                # page 1
                "item0",
                "item1",
                "item2",
                "item3",
            ],
            1,
        ),
        (
            [
                # page 1
                "item2",
                "item3",
                # page 2
                "item4",
                "item5",
            ],
            2,
        ),
        (
            [
                # page 1
                "item0",
                "item1",
                # page 2
                "item4",
                # page 3
                "item8",
            ],
            3,
        ),
        (
            [
                # page 1
                "item0",
                "item1",
                # page 2
                "item7",
                # page 3
                "item8",
                "item9",
            ],
            3,
        ),
        (
            ["item0"],
            5,  # only 1 item, we will go all the way to the end of the pagination adding 0 items each time
        ),
    ],
)
async def test_paginate_permission_filtered_request(
    mock_paginated_method,
    cleanup_pagination_cache_on_teardown,
    db: sqlalchemy.orm.Session,
    permitted_items,
    target_page,
):
    """
    Test paginate_permission_filtered_request.
    Request paginated method with page 1 and page size 4.
    The filter function will filter out the items that are not permitted. And the result should contain only the
    permitted items. With a minimum result of page size 4 (unless there are fewer items).
    """

    async def filter_(items):
        return [item for item in items if item["name"] in permitted_items]

    auth_info = mlrun.common.schemas.AuthInfo(user_id="user1")
    page_size = 4
    total = 20
    last_page = total // page_size
    method_kwargs = {"total_amount": total, "since": datetime.datetime.now()}

    paginator = framework.utils.pagination.Paginator()

    response, pagination_info = await paginator.paginate_permission_filtered_request(
        db,
        paginated_method,
        filter_,
        auth_info,
        None,
        1,
        page_size,
        **method_kwargs,
    )

    pagination_info = mlrun.common.schemas.PaginationInfo(**pagination_info)
    assert len(response) == len(permitted_items)
    for i, item in enumerate(permitted_items):
        assert response[i]["name"] == item

    if target_page < last_page:
        assert pagination_info.page_token is not None
    else:
        assert pagination_info.page_token is None
    assert pagination_info.page == target_page
    assert pagination_info.page_size == page_size


@pytest.mark.asyncio
async def test_paginate_permission_filtered_no_pagination(
    mock_paginated_method,
    cleanup_pagination_cache_on_teardown,
    db: sqlalchemy.orm.Session,
):
    """
    Test paginate_permission_filtered_request with no pagination.
    Request paginated method with no page and page size, and verify that all items are returned.
    """
    auth_info = mlrun.common.schemas.AuthInfo(user_id="user1")
    method_kwargs = {"total_amount": 5, "since": datetime.datetime.now()}

    paginator = framework.utils.pagination.Paginator()

    async def filter_(items):
        return items

    response, pagination_info = await paginator.paginate_permission_filtered_request(
        db,
        paginated_method,
        filter_,
        auth_info,
        None,
        None,
        None,
        **method_kwargs,
    )
    assert len(response) == 5
    assert not pagination_info["page"]


@pytest.mark.asyncio
async def test_paginate_permission_filtered_with_token(
    mock_paginated_method,
    cleanup_pagination_cache_on_teardown,
    db: sqlalchemy.orm.Session,
):
    """
    Test paginate_permission_filtered_request with token.
    Request paginated method with page 1 and page size 4.
    Then use the token to request the next filtered page.
    """
    permitted_items = [
        # page 1
        "item0",
        "item1",
        "item2",
        "item3",
        # page 2
        "item4",
        "item7",
        # page 3
        "item8",
        "item9",
        "item10",
        "item11",
        # page 4
        "item12",
    ]

    async def filter_(items):
        return [item for item in items if item["name"] in permitted_items]

    auth_info = mlrun.common.schemas.AuthInfo(user_id="user1")
    page_size = 4
    method_kwargs = {"total_amount": 20, "since": datetime.datetime.now()}

    paginator = framework.utils.pagination.Paginator()

    response, pagination_info = await paginator.paginate_permission_filtered_request(
        db,
        paginated_method,
        filter_,
        auth_info,
        None,
        1,
        page_size,
        **method_kwargs,
    )

    pagination_info = mlrun.common.schemas.PaginationInfo(**pagination_info)

    _assert_paginated_response(
        response,
        pagination_info,
        1,
        page_size,
        ["item0", "item1", "item2", "item3"],
        method_kwargs["since"],
    )

    token = pagination_info.page_token

    response, pagination_info = await paginator.paginate_permission_filtered_request(
        db, paginated_method, filter_, auth_info, token
    )
    pagination_info = mlrun.common.schemas.PaginationInfo(**pagination_info)
    _assert_paginated_response(
        response,
        pagination_info,
        3,
        page_size,
        ["item4", "item7", "item8", "item9", "item10", "item11"],
        method_kwargs["since"],
    )

    response, pagination_info = await paginator.paginate_permission_filtered_request(
        db, paginated_method, filter_, auth_info, token
    )
    pagination_info = mlrun.common.schemas.PaginationInfo(**pagination_info)
    _assert_paginated_response(
        response,
        pagination_info,
        5,
        page_size,
        ["item12"],
        method_kwargs["since"],
        last_page=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page, page_size",
    [
        (mlconf.httpdb.pagination.page_limit + 1, 1),  # page exceeds max allowed value
        (
            1,
            mlconf.httpdb.pagination.page_size_limit + 1,
        ),  # page_size exceeds max allowed value
    ],
)
async def test_paginate_request_invalid_page_or_page_size(
    mock_paginated_method,
    cleanup_pagination_cache_on_teardown,
    db: sqlalchemy.orm.Session,
    page,
    page_size,
):
    auth_info = mlrun.common.schemas.AuthInfo(user_id="user1")
    method_kwargs = {"total_amount": 5, "since": datetime.datetime.now()}

    paginator = framework.utils.pagination.Paginator()

    with pytest.raises(mlrun.errors.MLRunInvalidArgumentError):
        await paginator.paginate_request(
            db, paginated_method, auth_info, None, page, page_size, **method_kwargs
        )


def _assert_paginated_response(
    response,
    pagination_info,
    page,
    page_size,
    expected_items,
    since,
    last_page=False,
):
    assert len(response) == len(expected_items)
    for i, item in enumerate(expected_items):
        assert response[i]["name"] == item
        assert response[i]["since"] == since
    if not last_page:
        assert pagination_info.page_token is not None
    else:
        assert pagination_info.page_token is None
    assert pagination_info.page == page
    assert pagination_info.page_size == page_size


def _assert_cache_record(
    cache_record: framework.db.sqldb.models.PaginationCache,
    user: typing.Optional[str],
    method: typing.Callable,
    current_page: int,
    page_size: int,
):
    assert cache_record is not None
    assert cache_record.user == user
    assert cache_record.function == method.__name__
    assert cache_record.current_page == current_page
    assert cache_record.page_size == page_size

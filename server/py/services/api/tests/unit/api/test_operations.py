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
import http
import unittest.mock
from datetime import datetime

import fastapi.testclient
import igz_mgmt
import pytest
import sqlalchemy.orm

import mlrun
import mlrun.common.schemas
import mlrun.errors
import mlrun.runtimes
from mlrun.utils import logger

import framework.utils.background_tasks
import framework.utils.clients.iguazio as iguazio_client
import framework.utils.notifications.notification_pusher as notification_pusher
import services.api.api.endpoints.operations
import services.api.crud
import services.api.initial_data
import services.api.tests.unit.conftest as tests_unit_conftest
import services.api.utils.singletons.scheduler


def test_migrations_already_in_progress(
    db: sqlalchemy.orm.Session, client: fastapi.testclient.TestClient, monkeypatch
) -> None:
    background_task_name = "some-name"
    services.api.api.endpoints.operations.current_migration_background_task_name = (
        background_task_name
    )
    handler_mock = framework.utils.background_tasks.InternalBackgroundTasksHandler()
    handler_mock.get_background_task = unittest.mock.Mock(
        return_value=(_generate_background_task_schema(background_task_name))
    )
    monkeypatch.setattr(
        framework.utils.background_tasks,
        "InternalBackgroundTasksHandler",
        lambda *args, **kwargs: handler_mock,
    )
    mlrun.mlconf.httpdb.state = mlrun.common.schemas.APIStates.migrations_in_progress
    response = client.post("operations/migrations")
    assert response.status_code == http.HTTPStatus.ACCEPTED.value
    background_task = mlrun.common.schemas.BackgroundTask(**response.json())
    assert background_task_name == background_task.metadata.name
    services.api.api.endpoints.operations.current_migration_background_task_name = None


def test_migrations_failed(
    db: sqlalchemy.orm.Session, client: fastapi.testclient.TestClient
) -> None:
    mlrun.mlconf.httpdb.state = mlrun.common.schemas.APIStates.migrations_failed
    response = client.post("operations/migrations")
    assert response.status_code == http.HTTPStatus.PRECONDITION_FAILED.value
    assert "Migrations were already triggered and failed" in response.text


def test_migrations_not_needed(
    db: sqlalchemy.orm.Session, client: fastapi.testclient.TestClient
) -> None:
    mlrun.mlconf.httpdb.state = mlrun.common.schemas.APIStates.online
    response = client.post("operations/migrations")
    assert response.status_code == http.HTTPStatus.OK.value


def _mock_migration_process(*args, **kwargs):
    logger.info("Mocking migration process")
    mlrun.mlconf.httpdb.state = mlrun.common.schemas.APIStates.migrations_completed


@pytest.fixture
def _mock_waiting_for_migration():
    original_init_data = services.api.initial_data.init_data
    services.api.initial_data.init_data = unittest.mock.MagicMock()
    mlrun.mlconf.httpdb.state = mlrun.common.schemas.APIStates.waiting_for_migrations
    try:
        yield
    finally:
        services.api.initial_data.init_data = original_init_data


def test_migrations_success(
    # db calls init_data with from_scratch=True which means it will anyways do the migrations
    # therefore in order to make the api to be started as if its in a state where migrations are needed
    # we just add a middle fixture that sets the state
    db: sqlalchemy.orm.Session,
    _mock_waiting_for_migration,
    client: fastapi.testclient.TestClient,
) -> None:
    response = client.get("projects")
    # error cause we're waiting for migrations
    assert response.status_code == http.HTTPStatus.PRECONDITION_FAILED.value
    assert "API is waiting for migrations to be triggered" in response.text
    # not initialized until we're not doing migrations
    assert services.api.utils.singletons.scheduler.get_scheduler() is None
    # trigger migrations
    services.api.initial_data.init_data = _mock_migration_process
    response = client.post("operations/migrations")
    assert response.status_code == http.HTTPStatus.ACCEPTED.value
    background_task = mlrun.common.schemas.BackgroundTask(**response.json())
    assert (
        background_task.status.state == mlrun.common.schemas.BackgroundTaskState.running
    )
    response = client.get(f"background-tasks/{background_task.metadata.name}")
    assert response.status_code == http.HTTPStatus.OK.value
    background_task = mlrun.common.schemas.BackgroundTask(**response.json())
    assert (
        background_task.status.state
        == mlrun.common.schemas.BackgroundTaskState.succeeded
    )
    assert mlrun.mlconf.httpdb.state == mlrun.common.schemas.APIStates.online
    # now we should be able to get projects
    response = client.get("projects")
    assert response.status_code == http.HTTPStatus.OK.value
    # should be initialized
    assert services.api.utils.singletons.scheduler.get_scheduler() is not None


@pytest.mark.asyncio
async def test_perform_refresh_smtp(
    monkeypatch, k8s_secrets_mock: tests_unit_conftest.APIK8sSecretsMock
):
    assert (
        notification_pusher.RunNotificationPusher.mail_notification_default_params
        is None
    )
    smtp_configuration = igz_mgmt.SmtpConnection()
    smtp_configuration.server_address = "smtp.gmail.com:1234"
    smtp_configuration.sender_address = "a@a.com"
    smtp_configuration.auth_username = "user"
    smtp_configuration.auth_password = "pass"
    monkeypatch.setattr(
        iguazio_client.Client,
        "get_smtp_configuration",
        lambda *args, **kwargs: smtp_configuration,
    )
    monkeypatch.setattr(
        framework.utils.singletons.k8s, "get_k8s_helper", lambda: k8s_secrets_mock
    )
    await services.api.api.endpoints.operations._perform_refresh_smtp("")
    mail_notification_default_params = (
        notification_pusher.RunNotificationPusher.mail_notification_default_params
    )
    assert mail_notification_default_params == {
        "server_host": smtp_configuration.host,
        "server_port": str(smtp_configuration.port),
        "sender_address": smtp_configuration.sender_address,
        "username": smtp_configuration.auth_username,
        "password": smtp_configuration.auth_password,
    }


@pytest.mark.asyncio
async def test_failed_perform_refresh_smtp(
    monkeypatch, k8s_secrets_mock: tests_unit_conftest.APIK8sSecretsMock
):
    def raise_exception(*args, **kwargs):
        raise mlrun.errors.MLRunInternalServerError("Forbidden")

    monkeypatch.setattr(
        iguazio_client.Client,
        "get_smtp_configuration",
        raise_exception,
    )
    with pytest.raises(mlrun.errors.MLRunInternalServerError):
        await services.api.api.endpoints.operations._perform_refresh_smtp("")


def _generate_background_task_schema(
    background_task_name,
) -> mlrun.common.schemas.BackgroundTask:
    return mlrun.common.schemas.BackgroundTask(
        metadata=mlrun.common.schemas.BackgroundTaskMetadata(
            name=background_task_name,
            created=datetime.utcnow(),
            updated=datetime.utcnow(),
        ),
        status=mlrun.common.schemas.BackgroundTaskStatus(
            state=mlrun.common.schemas.BackgroundTaskState.running
        ),
        spec=mlrun.common.schemas.BackgroundTaskSpec(),
    )

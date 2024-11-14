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
import os
import pathlib
import typing
import unittest.mock
from collections.abc import Generator
from datetime import datetime
from tempfile import TemporaryDirectory

import fastapi
import httpx
import pytest
import pytest_asyncio
import semver
import sqlalchemy
import sqlalchemy.orm
from fastapi.testclient import TestClient

import mlrun.common.schemas
import mlrun.common.secrets
import mlrun.db.factory
import mlrun.launcher.factory
import mlrun.runtimes.utils
import mlrun.utils.singleton
from mlrun import mlconf
from mlrun.utils import logger

import framework.utils.clients.iguazio
import framework.utils.projects.remotes.leader
import framework.utils.runtimes.nuclio
import framework.utils.singletons.db
import framework.utils.singletons.k8s
import services.api.crud
import services.api.launcher
import services.api.runtime_handlers.mpijob
import services.api.utils.singletons.logs_dir
import services.api.utils.singletons.scheduler
from framework.tests.unit.common_fixtures import (
    K8sSecretsMock,
    TestServiceBase,
)
from services.api.daemon import daemon

# Importing here since mlrun_pipelines imports mlconf and it causes circular import
import mlrun_pipelines.utils  # isort:skip

tests_root_directory = pathlib.Path(__file__).absolute().parent
assets_path = tests_root_directory.joinpath("assets")

if str(tests_root_directory) in os.getcwd():
    # If this is the top level conftest - we need to explicitly declare the base common fixtures to
    # make pytest use them. If this is not the top level conftest (e.g. when running the tests from the project root)
    # then providing pytest_plugins is not allowed.
    pytest_plugins = [
        "tests.common_fixtures",
    ]


@pytest.fixture()
def app() -> fastapi.FastAPI:
    # TODO: This is a hack to remove the alerts app mount because it blocks the test router.
    #  Remove this when alerts is properly mounted with "alerts" prefix
    _app = daemon.app
    _app.routes.pop()
    yield _app


@pytest.fixture()
def prefix() -> str:
    yield daemon.service.BASE_VERSIONED_SERVICE_PREFIX


# TODO: This is a hack to allow sharing fixtures between services in non-root directives because pytest behavior
#  changes with respect to the directive in which the test is running from. To use the common fixtures we need to use
#  pytest plugins but it is not allowed in non-root directive which means the fixture must apply on all tests
#  including client side. The correct way to solve this is using classes like in alerts service unit tests but it is a
#  big refactor for this PR
test_service_base = TestServiceBase()
service_config_test = test_service_base.service_config_test
db = test_service_base.db
set_base_url_for_test_client = test_service_base.set_base_url_for_test_client
client = test_service_base.client


@pytest.fixture(autouse=True)
def api_config_test(service_config_test):
    framework.utils.singletons.project_member.project_member = None
    services.api.utils.singletons.scheduler.scheduler = None
    services.api.utils.singletons.logs_dir.logs_dir = None

    services.api.runtime_handlers.mpijob.cached_mpijob_crd_version = None

    # we need to override the launcher container manually because we run all unit tests in the same process in CI
    # so API is imported even when it's not needed
    launcher_factory = mlrun.launcher.factory.LauncherFactory()
    launcher_factory._launcher_container.override(
        services.api.launcher.ServerSideLauncherContainer
    )

    yield
    launcher_factory._launcher_container.reset_override()


# TODO: Move this to common fixtures similar to framework.tests.unit.common_fixtures.client
@pytest.fixture
def unversioned_client(db, app) -> Generator:
    """
    unversioned_client is a test client that doesn't have the version prefix in the url.
    When using this client, the version prefix must be added to the url manually.
    This is useful when tests use several endpoints that are not under the same version prefix.
    """
    with TemporaryDirectory(suffix="mlrun-logs") as log_dir:
        mlconf.httpdb.logs_path = log_dir
        mlconf.monitoring.runs.interval = 0
        mlconf.runtimes_cleanup_interval = 0
        mlconf.httpdb.projects.periodic_sync_interval = "0 seconds"

        with TestClient(app) as unversioned_test_client:
            set_base_url_for_test_client(
                unversioned_test_client, daemon.service.SERVICE_PREFIX
            )
            yield unversioned_test_client


@pytest_asyncio.fixture()
async def async_client(db, app, prefix) -> typing.AsyncIterator[httpx.AsyncClient]:
    with TemporaryDirectory(suffix="mlrun-logs") as log_dir:
        mlconf.httpdb.logs_path = log_dir
        mlconf.monitoring.runs.interval = 0
        mlconf.runtimes_cleanup_interval = 0
        mlconf.httpdb.projects.periodic_sync_interval = "0 seconds"

        async with httpx.AsyncClient(app=app, base_url="http://test") as async_client:
            set_base_url_for_test_client(async_client, prefix)
            yield async_client


@pytest.fixture
def kfp_client_mock(monkeypatch) -> mlrun_pipelines.utils.kfp.Client:
    framework.utils.singletons.k8s.get_k8s_helper().is_running_inside_kubernetes_cluster = unittest.mock.Mock(
        return_value=True
    )
    kfp_client_mock = unittest.mock.Mock()
    monkeypatch.setattr(
        mlrun_pipelines.utils.kfp, "Client", lambda *args, **kwargs: kfp_client_mock
    )
    mlrun.mlconf.kfp_url = "http://ml-pipeline.custom_namespace.svc.cluster.local:8888"
    return kfp_client_mock


@pytest.fixture()
def api_url() -> str:
    api_url = "http://iguazio-api-url:8080"
    mlrun.mlconf.iguazio_api_url = api_url
    return api_url


@pytest.fixture()
def iguazio_client(
    request: pytest.FixtureRequest,
) -> framework.utils.clients.iguazio.Client:
    if request.param == "async":
        client = framework.utils.clients.iguazio.AsyncClient()
    else:
        client = framework.utils.clients.iguazio.Client()

    # force running init again so the configured api url will be used
    client.__init__()
    client._wait_for_job_completion_retry_interval = 0
    client._wait_for_project_terminal_state_retry_interval = 0

    # inject the request param into client, so we can use it in tests
    setattr(client, "mode", request.param)
    return client


class MockedK8sHelper:
    @pytest.fixture(autouse=True)
    def mock_k8s_helper(self):
        """
        This fixture mocks the k8s helper singleton for all tests in the class that inherit from this class.
        Example:
            class TestSomething(MockedK8sHelper):
                # Automatically uses the mocked k8s helper
                def test_something(self):
                    ...
        """
        _mocked_k8s_helper()


@pytest.fixture()
def mocked_k8s_helper():
    _mocked_k8s_helper()


def _mocked_k8s_helper():
    # We don't need to restore the original functions since the k8s cluster is never configured in unit tests
    framework.utils.singletons.k8s.get_k8s_helper().get_project_secret_keys = (
        unittest.mock.Mock(return_value=[])
    )
    framework.utils.singletons.k8s.get_k8s_helper().v1api = unittest.mock.Mock()
    framework.utils.singletons.k8s.get_k8s_helper().crdapi = unittest.mock.Mock()
    framework.utils.singletons.k8s.get_k8s_helper().is_running_inside_kubernetes_cluster = unittest.mock.Mock(
        return_value=True
    )

    config_map = unittest.mock.Mock()
    config_map.items = []
    framework.utils.singletons.k8s.get_k8s_helper().v1api.list_namespaced_config_map = (
        unittest.mock.Mock(return_value=config_map)
    )
    pods_list = unittest.mock.Mock()
    pods_list.items = []
    pods_list.metadata._continue = None
    framework.utils.singletons.k8s.get_k8s_helper().v1api.list_namespaced_pod = (
        unittest.mock.Mock(return_value=pods_list)
    )
    service_list = unittest.mock.Mock()
    service_list.items = []
    framework.utils.singletons.k8s.get_k8s_helper().v1api.list_namespaced_service = (
        unittest.mock.Mock(return_value=service_list)
    )
    custom_object_list = {"items": []}
    framework.utils.singletons.k8s.get_k8s_helper().crdapi.list_namespaced_custom_object = unittest.mock.Mock(
        return_value=custom_object_list
    )
    secret_data = unittest.mock.Mock()
    secret_data.data = {}
    framework.utils.singletons.k8s.get_k8s_helper().v1api.read_namespaced_secret = (
        unittest.mock.Mock(return_value=secret_data)
    )


class APIK8sSecretsMock(K8sSecretsMock):
    def set_service_account_keys(
        self, project, default_service_account, allowed_service_accounts
    ):
        secrets = {}
        if default_service_account:
            secrets[
                services.api.crud.secrets.Secrets().generate_client_project_secret_key(
                    services.api.crud.secrets.SecretsClientType.service_accounts,
                    "default",
                )
            ] = default_service_account
        if allowed_service_accounts:
            secrets[
                services.api.crud.secrets.Secrets().generate_client_project_secret_key(
                    services.api.crud.secrets.SecretsClientType.service_accounts,
                    "allowed",
                )
            ] = ",".join(allowed_service_accounts)
        self.store_project_secrets(project, secrets)


@pytest.fixture()
def k8s_secrets_mock(monkeypatch) -> APIK8sSecretsMock:
    logger.info("Creating k8s secrets mock")
    k8s_secrets_mock = APIK8sSecretsMock()
    k8s_secrets_mock.mock_functions(
        framework.utils.singletons.k8s.get_k8s_helper(), monkeypatch
    )
    yield k8s_secrets_mock


class MockedProjectFollowerIguazioClient(
    framework.utils.projects.remotes.leader.Member,
    metaclass=mlrun.utils.singleton.AbstractSingleton,
):
    def __init__(self):
        self._db_session = None
        self._unversioned_client = None

    def create_project(
        self,
        session: str,
        project: mlrun.common.schemas.Project,
        wait_for_completion: bool = True,
    ) -> bool:
        services.api.crud.Projects().create_project(self._db_session, project)
        return False

    def update_project(
        self,
        session: str,
        name: str,
        project: mlrun.common.schemas.Project,
    ):
        pass

    def delete_project(
        self,
        session: str,
        name: str,
        deletion_strategy: mlrun.common.schemas.DeletionStrategy = mlrun.common.schemas.DeletionStrategy.default(),
        wait_for_completion: bool = True,
    ) -> bool:
        api_version = "v2"
        igz_version = mlrun.mlconf.get_parsed_igz_version()
        if igz_version and igz_version < semver.VersionInfo.parse("3.5.5"):
            api_version = "v1"

        self._unversioned_client.delete(
            f"{api_version}/projects/{name}",
            headers={
                mlrun.common.schemas.HeaderNames.projects_role: mlrun.mlconf.httpdb.projects.leader,
                mlrun.common.schemas.HeaderNames.deletion_strategy: deletion_strategy,
            },
        )

        # Mock waiting for completion in iguazio (return False to indicate 'not running in background')
        return False

    def list_projects(
        self,
        session: str,
        updated_after: typing.Optional[datetime] = None,
    ) -> tuple[list[mlrun.common.schemas.Project], typing.Optional[datetime]]:
        return [], None

    def get_project(
        self,
        session: str,
        name: str,
    ) -> mlrun.common.schemas.Project:
        pass

    def format_as_leader_project(
        self, project: mlrun.common.schemas.Project
    ) -> mlrun.common.schemas.IguazioProject:
        pass

    def get_project_owner(
        self,
        session: str,
        name: str,
    ) -> mlrun.common.schemas.ProjectOwner:
        pass


@pytest.fixture()
def mock_project_follower_iguazio_client(
    db: sqlalchemy.orm.Session, unversioned_client: TestClient
):
    """
    This fixture mocks the project leader iguazio client.
    """
    mlrun.mlconf.httpdb.projects.leader = "iguazio"
    mlrun.mlconf.httpdb.projects.iguazio_access_key = "access_key"
    old_iguazio_client = framework.utils.clients.iguazio.Client
    framework.utils.clients.iguazio.Client = MockedProjectFollowerIguazioClient
    framework.utils.singletons.project_member.initialize_project_member()
    iguazio_client = MockedProjectFollowerIguazioClient()
    iguazio_client._db_session = db
    iguazio_client._unversioned_client = unversioned_client

    yield iguazio_client

    framework.utils.clients.iguazio.Client = old_iguazio_client

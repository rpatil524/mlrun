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

import json
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Final, Optional

import botocore.exceptions

import mlrun.common.schemas
import mlrun.errors
import mlrun.model_monitoring.helpers
from mlrun.utils import logger


class ModelMonitoringSchedulesFile(AbstractContextManager):
    DEFAULT_SCHEDULES: Final = {}
    INITIAL_CONTENT = json.dumps(DEFAULT_SCHEDULES)
    ENCODING = "utf-8"

    def __init__(self, project: str, endpoint_id: str) -> None:
        """
        Initialize applications monitoring schedules file object.
        The JSON file stores a dictionary of registered application name as key and Unix timestamp as value.
        When working with the schedules data, use this class as a context manager to read and write the data.

        :param project:     The project name.
        :param endpoint_id: The endpoint ID.
        """
        # `self._item` is the persistent version of the monitoring schedules.
        self._item = mlrun.model_monitoring.helpers.get_monitoring_schedules_data(
            project=project, endpoint_id=endpoint_id
        )
        self._path = self._item.url
        self._fs = self._item.store.filesystem
        # `self._schedules` is an in-memory copy of the DB for all the applications for
        # the same model endpoint.
        self._schedules: dict[str, int] = self.DEFAULT_SCHEDULES.copy()
        # Does `self._schedules` hold the content of `self._item`?
        self._open_schedules = False

    @classmethod
    def from_model_endpoint(
        cls, model_endpoint: mlrun.common.schemas.ModelEndpoint
    ) -> "ModelMonitoringSchedulesFile":
        return cls(
            project=model_endpoint.metadata.project,
            endpoint_id=model_endpoint.metadata.uid,
        )

    def create(self) -> None:
        """Create a schedules file with initial content - an empty dictionary"""
        logger.debug("Creating model monitoring schedules file", path=self._item.url)
        self._item.put(self.INITIAL_CONTENT)

    def delete(self) -> None:
        """Delete schedules file if it exists"""
        if (
            self._fs is None  # In-memory store
            or self._fs.exists(self._path)
        ):
            logger.debug(
                "Deleting model monitoring schedules file", path=self._item.url
            )
            self._item.delete()
        else:
            logger.debug(
                "Model monitoring schedules file does not exist, nothing to delete",
                path=self._item.url,
            )

    def _open(self) -> None:
        try:
            content = self._item.get()
        except (
            mlrun.errors.MLRunNotFoundError,
            # Different errors are raised for S3 or local storage, see ML-8042
            botocore.exceptions.ClientError,
            FileNotFoundError,
        ) as err:
            if (
                isinstance(err, botocore.exceptions.ClientError)
                # Add a log only to "NoSuchKey" errors codes - equivalent to `FileNotFoundError`
                and err.response["Error"]["Code"] != "NoSuchKey"
            ):
                raise

            logger.exception(
                "The schedules file was not found. It should have been created "
                "as a part of the model endpoint's creation",
                path=self._path,
            )
            raise

        if isinstance(content, bytes):
            content = content.decode(encoding=self.ENCODING)
        self._schedules = json.loads(content)
        self._open_schedules = True

    def _close(self) -> None:
        self._item.put(json.dumps(self._schedules))
        self._schedules = self.DEFAULT_SCHEDULES
        self._open_schedules = False

    def __enter__(self) -> "ModelMonitoringSchedulesFile":
        self._open()
        return super().__enter__()

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> Optional[bool]:
        self._close()

    def _check_open_schedules(self) -> None:
        if not self._open_schedules:
            raise mlrun.errors.MLRunValueError(
                "Open the schedules file as a context manager first"
            )

    def get_application_time(self, application: str) -> Optional[int]:
        self._check_open_schedules()
        return self._schedules.get(application)

    def update_application_time(self, application: str, timestamp: int) -> None:
        self._check_open_schedules()
        self._schedules[application] = timestamp


def delete_model_monitoring_schedules_folder(project: str) -> None:
    """Delete the model monitoring schedules folder of the project"""
    folder = mlrun.model_monitoring.helpers._get_monitoring_schedules_folder_path(
        project
    )
    fs = mlrun.datastore.store_manager.object(folder).store.filesystem
    if fs and fs.exists(folder):
        logger.debug("Deleting model monitoring schedules folder", folder=folder)
        fs.rm(folder, recursive=True)
    elif fs is None:  # In-memory store
        raise mlrun.errors.MLRunValueError(
            "Cannot delete a folder without a file-system"
        )

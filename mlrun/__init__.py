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

__all__ = [
    "get_version",
    "set_environment",
    "code_to_function",
    "import_function",
    "handler",
    "ArtifactType",
    "get_secret_or_env",
    "mount_v3io",
    "v3io_cred",
    "auto_mount",
    "VolumeMount",
]

from os import environ, path
from typing import Optional

import dotenv

from .config import config as mlconf
from .datastore import DataItem, store_manager
from .db import get_run_db
from .errors import MLRunInvalidArgumentError, MLRunNotFoundError
from .execution import MLClientCtx
from .model import RunObject, RunTemplate, new_task
from .package import ArtifactType, DefaultPackager, Packager, handler
from .projects import (
    MlrunProject,
    ProjectMetadata,
    build_function,
    deploy_function,
    get_or_create_project,
    load_project,
    new_project,
    pipeline_context,
    run_function,
)
from .projects.project import _add_username_to_project_name_if_needed
from .run import (
    _run_pipeline,
    code_to_function,
    function_to_module,
    get_dataitem,
    get_object,
    get_or_create_ctx,
    get_pipeline,
    import_function,
    new_function,
    retry_pipeline,
    wait_for_pipeline_completion,
)
from .runtimes import mounts, new_model_server
from .secrets import get_secret_or_env
from .utils.version import Version

__version__ = Version().get()["version"]

VolumeMount = mounts.VolumeMount
mount_v3io = mounts.mount_v3io
v3io_cred = mounts.v3io_cred
auto_mount = mounts.auto_mount


def get_version():
    """get current mlrun version"""
    return __version__


if "IGZ_NAMESPACE_DOMAIN" in environ:
    igz_domain = environ["IGZ_NAMESPACE_DOMAIN"]
    kfp_ep = f"https://dashboard.{igz_domain}/pipelines"
    environ["KF_PIPELINES_UI_ENDPOINT"] = kfp_ep
    mlconf.remote_host = mlconf.remote_host or igz_domain


def set_environment(
    api_path: Optional[str] = None,
    artifact_path: str = "",
    access_key: Optional[str] = None,
    username: Optional[str] = None,
    env_file: Optional[str] = None,
    mock_functions: Optional[str] = None,
):
    """set and test default config for: api path, artifact_path and project

    this function will try and read the configuration from the environment/api
    and merge it with the user provided project name, artifacts path or api path/access_key.
    it returns the configured artifacts path, this can be used to define sub paths.

    Note: the artifact path is an mlrun data uri (e.g. `s3://bucket/path`) and can not be used with file utils.

    example::

        from os import path

        project_name, artifact_path = set_environment()
        set_environment("http://localhost:8080", artifact_path="./")
        set_environment(env_file="mlrun.env")
        set_environment("<remote-service-url>", access_key="xyz", username="joe")

    :param api_path:       location/url of mlrun api service
    :param artifact_path:  path/url for storing experiment artifacts
    :param access_key:     set the remote cluster access key (V3IO_ACCESS_KEY)
    :param username:       name of the user to authenticate
    :param env_file:       path/url to .env file (holding MLRun config and other env vars), see: set_env_from_file()
    :param mock_functions: set to True to create local/mock functions instead of real containers,
                           set to "auto" to auto determine based on the presence of k8s/Nuclio
    :returns:
        default project name
        actual artifact path/url, can be used to create subpaths per task or group of artifacts
    """
    if env_file:
        set_env_from_file(env_file)

    # set before the dbpath (so it will re-connect with the new credentials)
    if access_key:
        environ["V3IO_ACCESS_KEY"] = access_key
    if username:
        environ["V3IO_USERNAME"] = username

    mlconf.dbpath = api_path or mlconf.dbpath
    if not mlconf.dbpath:
        raise ValueError("DB/API path was not detected, please specify its address")

    # check connectivity and load remote defaults
    get_run_db()
    if api_path:
        environ["MLRUN_DBPATH"] = mlconf.dbpath
    mlconf.reload()

    if mock_functions is not None:
        mock_functions = "1" if mock_functions is True else mock_functions
        mlconf.force_run_local = mock_functions
        mlconf.mock_nuclio_deployment = mock_functions

    if not mlconf.artifact_path and not artifact_path:
        raise ValueError(
            "default artifact_path was not configured, please specify a valid artifact_path"
        )

    if artifact_path:
        if artifact_path.startswith("./"):
            artifact_path = path.abspath(artifact_path)
        elif not artifact_path.startswith("/") and "://" not in artifact_path:
            raise ValueError(
                "artifact_path must refer to an absolute path" " or a valid url"
            )
        mlconf.artifact_path = artifact_path

    return mlconf.default_project, mlconf.artifact_path


def get_current_project(silent: bool = False) -> Optional[MlrunProject]:
    if not pipeline_context.project and not silent:
        raise MLRunInvalidArgumentError(
            "No current project is initialized. Use new, get or load project functions first."
        )
    return pipeline_context.project


def get_sample_path(subpath=""):
    """
    return the url of a sample dataset or model
    """
    samples_path = environ.get(
        "SAMPLE_DATA_SOURCE_URL_PREFIX", mlconf.default_samples_path
    )
    if subpath:
        samples_path = path.join(samples_path, subpath.lstrip("/"))
    return samples_path


def set_env_from_file(env_file: str, return_dict: bool = False) -> Optional[dict]:
    """Read and set and/or return environment variables from a file
    the env file should have lines in the form KEY=VALUE, comment line start with "#"

    example file::

        # this is an env file
        MLRUN_DBPATH=https://mlrun-api.default-tenant.app.xxx.iguazio-cd1.com
        V3IO_USERNAME=admin
        V3IO_API=https://webapi.default-tenant.app.xxx.iguazio-cd1.com
        V3IO_ACCESS_KEY=MYKEY123
        AWS_ACCESS_KEY_ID-XXXX
        AWS_SECRET_ACCESS_KEY=YYYY

    usage::

        # set the env vars from a file + return the results as a dict
        env_dict = mlrun.set_env_from_file(env_path, return_dict=True)

    :param env_file:    path/url to env file
    :param return_dict: set to True to return the env as a dict
    :return: None or env dict
    """
    env_file = path.expanduser(env_file)
    if not path.isfile(env_file):
        raise MLRunNotFoundError(f"env file {env_file} does not exist")
    env_vars = dotenv.dotenv_values(env_file)
    if None in env_vars.values():
        raise MLRunInvalidArgumentError("env file lines must be in the form key=value")

    ordered_env_vars = order_env_vars(env_vars)
    for key, value in ordered_env_vars.items():
        environ[key] = value

    mlconf.reload()  # reload mlrun configuration
    return ordered_env_vars if return_dict else None


def order_env_vars(env_vars: dict[str, str]) -> dict[str, str]:
    """
    Order and process environment variables by first handling specific ordered keys,
    then processing the remaining keys in the given dictionary.

    The function ensures that environment variables defined in the `ordered_keys` list
    are added to the result dictionary first. Any other environment variables from
    `env_vars` are then added in the order they appear in the input dictionary.

    :param env_vars: A dictionary where each key is the name of an environment variable (str),
                      and each value is the corresponding environment variable value (str).
    :return: A dictionary with the processed environment variables, ordered with the specific
             keys first, followed by the rest in their original order.
    """
    ordered_keys = mlconf.get_ordered_keys()

    ordered_env_vars: dict[str, str] = {}

    # First, add the ordered keys to the dictionary
    for key in ordered_keys:
        if key in env_vars:
            ordered_env_vars[key] = env_vars[key]

    # Then, add the remaining keys (those not in ordered_keys)
    for key, value in env_vars.items():
        if key not in ordered_keys:
            ordered_env_vars[key] = value

    return ordered_env_vars

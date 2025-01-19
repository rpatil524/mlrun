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
from http import HTTPStatus
from typing import Optional

import fastapi.concurrency
from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

import mlrun.common.constants as mlrun_constants
import mlrun.common.helpers
import mlrun.common.schemas
import mlrun.utils.helpers
from mlrun.utils import logger

import framework.api.utils
import framework.utils.auth.verifier
import framework.utils.clients.chief
import framework.utils.singletons.project_member
from framework.api import deps

router = APIRouter()


@router.post("/submit")
@router.post("/submit/")
@router.post("/submit_job")
@router.post("/submit_job/")
async def submit_job(
    request: Request,
    username: Optional[str] = Header(None, alias="x-remote-user"),
    auth_info: mlrun.common.schemas.AuthInfo = Depends(deps.authenticate_request),
    db_session: Session = Depends(deps.get_db_session),
    client_version: Optional[str] = Header(
        None, alias=mlrun.common.schemas.HeaderNames.client_version
    ),
    client_python_version: Optional[str] = Header(
        None, alias=mlrun.common.schemas.HeaderNames.python_version
    ),
):
    data = None
    try:
        data = await request.json()
    except ValueError:
        framework.api.utils.log_and_raise(
            HTTPStatus.BAD_REQUEST.value, reason="bad JSON body"
        )

    await fastapi.concurrency.run_in_threadpool(
        framework.utils.singletons.project_member.get_project_member().ensure_project,
        db_session,
        data["task"]["metadata"]["project"],
        auth_info=auth_info,
    )
    function_dict, function_url, task = framework.api.utils.parse_submit_run_body(data)
    if function_url and "://" not in function_url:
        (
            function_project,
            function_name,
            _,
            _,
        ) = mlrun.common.helpers.parse_versioned_object_uri(function_url)
        await framework.utils.auth.verifier.AuthVerifier().query_project_resource_permissions(
            mlrun.common.schemas.AuthorizationResourceTypes.function,
            function_project,
            function_name,
            mlrun.common.schemas.AuthorizationAction.read,
            auth_info,
        )
    if data.get("schedule"):
        await framework.utils.auth.verifier.AuthVerifier().query_project_resource_permissions(
            mlrun.common.schemas.AuthorizationResourceTypes.schedule,
            data["task"]["metadata"]["project"],
            data["task"]["metadata"]["name"],
            mlrun.common.schemas.AuthorizationAction.create,
            auth_info,
        )
        # schedules are meant to be run solely by the chief, then if run is configured to run as scheduled
        # and we are in worker then we forward the request to the chief.
        # to reduce redundant load on the chief, we re-route the request only if the user has permissions
        if (
            mlrun.mlconf.httpdb.clusterization.role
            != mlrun.common.schemas.ClusterizationRole.chief
        ):
            logger.info(
                "Requesting to submit job with schedules, re-routing to chief",
                function=function_dict,
                url=function_url,
                task=task,
            )
            chief_client = framework.utils.clients.chief.Client()
            return await chief_client.submit_job(request=request, json=data)

    else:
        await framework.utils.auth.verifier.AuthVerifier().query_project_resource_permissions(
            mlrun.common.schemas.AuthorizationResourceTypes.run,
            data["task"]["metadata"]["project"],
            "",
            mlrun.common.schemas.AuthorizationAction.create,
            auth_info,
        )

    # enrich job task with the username from the request header
    if username:
        # if task is missing, we don't want to create one
        if "task" in data:
            labels = data["task"].setdefault("metadata", {}).setdefault("labels", {})
            labels.setdefault(mlrun_constants.MLRunInternalLabels.v3io_user, username)
            labels.setdefault(mlrun_constants.MLRunInternalLabels.owner, username)

    client_version = client_version or data["task"]["metadata"].get("labels", {}).get(
        mlrun_constants.MLRunInternalLabels.client_version
    )
    client_python_version = client_python_version or data["task"]["metadata"].get(
        "labels", {}
    ).get(mlrun_constants.MLRunInternalLabels.client_python_version)
    if client_version is not None:
        data["task"]["metadata"].setdefault("labels", {}).update(
            {mlrun_constants.MLRunInternalLabels.client_version: client_version}
        )
    if client_python_version is not None:
        data["task"]["metadata"].setdefault("labels", {}).update(
            {
                mlrun_constants.MLRunInternalLabels.client_python_version: client_python_version
            }
        )
    return await framework.api.utils.submit_run(db_session, auth_info, data)

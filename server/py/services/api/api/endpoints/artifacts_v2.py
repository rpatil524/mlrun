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

from fastapi import APIRouter, Depends, Query, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

import mlrun.common.formatters
import mlrun.common.schemas
from mlrun.common.schemas.artifact import ArtifactsDeletionStrategies
from mlrun.utils import logger

import framework.utils.auth.verifier
import framework.utils.pagination
import framework.utils.singletons.project_member
import services.api.crud
from framework.api import deps
from framework.api.utils import artifact_project_and_resource_name_extractor

router = APIRouter()


@router.post("/projects/{project}/artifacts", status_code=HTTPStatus.CREATED.value)
async def create_artifact(
    project: str,
    artifact: mlrun.common.schemas.Artifact,
    auth_info: mlrun.common.schemas.AuthInfo = Depends(deps.authenticate_request),
    db_session: Session = Depends(deps.get_db_session),
):
    await run_in_threadpool(
        framework.utils.singletons.project_member.get_project_member().ensure_project,
        db_session,
        project,
        auth_info=auth_info,
    )

    key = artifact.metadata.key or None
    tag = artifact.metadata.tag or None
    iteration = artifact.metadata.iter or 0
    tree = artifact.metadata.tree or None
    logger.debug("Creating artifact", project=project, key=key, tag=tag, iter=iteration)
    await (
        framework.utils.auth.verifier.AuthVerifier().query_project_resource_permissions(
            mlrun.common.schemas.AuthorizationResourceTypes.artifact,
            project,
            key,
            mlrun.common.schemas.AuthorizationAction.store,
            auth_info,
        )
    )
    artifact_uid = await run_in_threadpool(
        services.api.crud.Artifacts().create_artifact,
        db_session,
        key,
        artifact.dict(exclude_none=True),
        tag,
        iteration,
        producer_id=tree,
        project=project,
        auth_info=auth_info,
    )
    return await run_in_threadpool(
        services.api.crud.Artifacts().get_artifact,
        db_session,
        key,
        tag,
        iteration,
        project,
        producer_id=tree,
        object_uid=artifact_uid,
    )


@router.put("/projects/{project}/artifacts/{key:path}")
async def store_artifact(
    project: str,
    artifact: mlrun.common.schemas.Artifact,
    key: str,
    tree: Optional[str] = None,
    tag: Optional[str] = None,
    iter: Optional[int] = None,
    object_uid: str = Query(None, alias="object-uid"),
    auth_info: mlrun.common.schemas.AuthInfo = Depends(deps.authenticate_request),
    db_session: Session = Depends(deps.get_db_session),
):
    await run_in_threadpool(
        framework.utils.singletons.project_member.get_project_member().ensure_project,
        db_session,
        project,
        auth_info=auth_info,
    )

    producer_id = tree
    if iter is None:
        iter = artifact.metadata.iter or 0
    logger.debug(
        "Storing artifact",
        project=project,
        key=key,
        tag=tag,
        producer_id=producer_id,
        iter=iter,
    )

    await (
        framework.utils.auth.verifier.AuthVerifier().query_project_resource_permissions(
            mlrun.common.schemas.AuthorizationResourceTypes.artifact,
            project,
            key,
            mlrun.common.schemas.AuthorizationAction.store,
            auth_info,
        )
    )
    artifact_uid = await run_in_threadpool(
        services.api.crud.Artifacts().store_artifact,
        db_session,
        key,
        artifact.dict(exclude_none=True),
        object_uid,
        tag,
        iter,
        project,
        producer_id=producer_id,
        auth_info=auth_info,
    )
    return await run_in_threadpool(
        services.api.crud.Artifacts().get_artifact,
        db_session,
        key,
        tag,
        iter,
        project,
        producer_id=producer_id,
        object_uid=artifact_uid,
    )


@router.get("/projects/{project}/artifacts")
async def list_artifacts(
    project: str,
    name: Optional[str] = None,
    tag: Optional[str] = None,
    kind: Optional[str] = None,
    category: mlrun.common.schemas.ArtifactCategories = None,
    labels: list[str] = Query([], alias="label"),
    iter: int = Query(None, ge=0),
    tree: Optional[str] = None,
    producer_uri: Optional[str] = None,
    best_iteration: bool = Query(False, alias="best-iteration"),
    format_: str = Query(mlrun.common.formatters.ArtifactFormat.full, alias="format"),
    limit: int = Query(
        None,
        deprecated=True,
        description="Use page and page_size, will be removed in the 1.10.0",
    ),
    since: Optional[str] = None,
    until: Optional[str] = None,
    partition_by: Optional[mlrun.common.schemas.ArtifactPartitionByField] = Query(
        None, alias="partition-by"
    ),
    rows_per_partition: Optional[int] = Query(1, alias="rows-per-partition", gt=0),
    partition_sort_by: Optional[mlrun.common.schemas.SortField] = Query(
        mlrun.common.schemas.SortField.updated, alias="partition-sort-by"
    ),
    partition_order: Optional[mlrun.common.schemas.OrderType] = Query(
        mlrun.common.schemas.OrderType.desc, alias="partition-order"
    ),
    page: int = Query(None, gt=0),
    page_size: int = Query(None, alias="page-size", gt=0),
    page_token: str = Query(None, alias="page-token"),
    auth_info: mlrun.common.schemas.AuthInfo = Depends(deps.authenticate_request),
    db_session: Session = Depends(deps.get_db_session),
):
    await framework.utils.auth.verifier.AuthVerifier().query_project_permissions(
        project,
        mlrun.common.schemas.AuthorizationAction.read,
        auth_info,
    )

    # TODO: deprecate the limit parameter in the list_artifacts method in 1.10.0
    if limit and (page_size or page):
        raise mlrun.errors.MLRunConflictError(
            "'page/page_size' and 'limit' are conflicting, only one can be specified."
        )

    paginator = framework.utils.pagination.Paginator()

    async def _filter_artifacts(_artifacts):
        return await framework.utils.auth.verifier.AuthVerifier().filter_project_resources_by_permissions(
            mlrun.common.schemas.AuthorizationResourceTypes.artifact,
            _artifacts,
            artifact_project_and_resource_name_extractor,
            auth_info,
        )

    artifacts, page_info = await paginator.paginate_permission_filtered_request(
        db_session,
        services.api.crud.Artifacts().list_artifacts,
        _filter_artifacts,
        auth_info,
        token=page_token,
        page=page,
        page_size=page_size,
        project=project,
        name=name,
        tag=tag,
        labels=labels,
        since=mlrun.utils.datetime_from_iso(since),
        until=mlrun.utils.datetime_from_iso(until),
        kind=kind,
        category=category,
        iter=iter,
        best_iteration=best_iteration,
        format_=format_,
        producer_id=tree,
        producer_uri=producer_uri,
        limit=limit,
        partition_by=partition_by,
        rows_per_partition=rows_per_partition,
        partition_sort_by=partition_sort_by,
        partition_order=partition_order,
    )
    return {
        "artifacts": artifacts,
        "pagination": page_info,
    }


@router.get("/projects/{project}/artifacts/{key:path}")
async def get_artifact(
    project: str,
    key: str,
    tree: Optional[str] = None,
    tag: Optional[str] = None,
    iter: Optional[int] = None,
    object_uid: str = Query(None, alias="object-uid"),
    # TODO: remove deprecated uid parameter in 1.9.0
    # we support both uid and object-uid for backward compatibility
    uid: str = Query(
        None,
        deprecated=True,
        description="Use object-uid instead, will be removed in the 1.9.0",
    ),
    format_: str = Query(mlrun.common.formatters.ArtifactFormat.full, alias="format"),
    auth_info: mlrun.common.schemas.AuthInfo = Depends(deps.authenticate_request),
    db_session: Session = Depends(deps.get_db_session),
):
    await (
        framework.utils.auth.verifier.AuthVerifier().query_project_resource_permissions(
            mlrun.common.schemas.AuthorizationResourceTypes.artifact,
            project,
            key,
            mlrun.common.schemas.AuthorizationAction.read,
            auth_info,
        )
    )

    # Older clients (pre-1.8.0) do not support parsing "uid" and treat "tree^uid" as a single "tree" value.
    # To ensure compatibility, we split "tree" here to extract "uid" if it exists.
    if tree and "^" in tree:
        tree, uri_object_uid = tree.split("^", 1)
        if object_uid and object_uid != uri_object_uid:
            mlrun.utils.logger.warning(
                "Conflicting UIDs detected",
                object_uid=object_uid,
                extracted_object_uid=uri_object_uid,
            )
        # If object_uid is not set, assign it from the URI
        object_uid = object_uid or uri_object_uid

    artifact = await run_in_threadpool(
        services.api.crud.Artifacts().get_artifact,
        db_session,
        key,
        tag,
        iter,
        project,
        format_,
        producer_id=tree,
        object_uid=object_uid or uid,
    )
    return artifact


@router.delete("/projects/{project}/artifacts/{key:path}")
async def delete_artifact(
    project: str,
    key: str,
    tree: Optional[str] = None,
    tag: Optional[str] = None,
    object_uid: str = Query(None, alias="object-uid"),
    # TODO: remove deprecated uid parameter in 1.9.0
    # we support both uid and object-uid for backward compatibility
    uid: str = Query(
        None,
        deprecated=True,
        description="Use object-uid instead, will be removed in the 1.9.0",
    ),
    iteration: int = Query(None, alias="iter"),
    deletion_strategy: ArtifactsDeletionStrategies = ArtifactsDeletionStrategies.metadata_only,
    secrets: Optional[dict] = None,
    auth_info: mlrun.common.schemas.AuthInfo = Depends(deps.authenticate_request),
    db_session: Session = Depends(deps.get_db_session),
):
    logger.debug(
        "Deleting artifact",
        project=project,
        key=key,
        tag=tag,
        producer_id=tree,
        deletion_strategy=deletion_strategy,
        iteration=iteration,
        object_uid=object_uid or uid,
    )

    await (
        framework.utils.auth.verifier.AuthVerifier().query_project_resource_permissions(
            mlrun.common.schemas.AuthorizationResourceTypes.artifact,
            project,
            key,
            mlrun.common.schemas.AuthorizationAction.delete,
            auth_info,
        )
    )
    await run_in_threadpool(
        services.api.crud.Artifacts().delete_artifact,
        db_session=db_session,
        key=key,
        tag=tag,
        project=project,
        object_uid=object_uid or uid,
        producer_id=tree,
        deletion_strategy=deletion_strategy,
        secrets=secrets,
        auth_info=auth_info,
        iteration=iteration,
    )
    return Response(status_code=HTTPStatus.NO_CONTENT.value)


@router.delete("/projects/{project}/artifacts")
async def delete_artifacts(
    project: Optional[str] = None,
    name: str = "",
    tag: str = "",
    tree: Optional[str] = None,
    labels: list[str] = Query([], alias="label"),
    limit: int = Query(None),
    auth_info: mlrun.common.schemas.AuthInfo = Depends(deps.authenticate_request),
    db_session: Session = Depends(deps.get_db_session),
):
    project = project or mlrun.mlconf.default_project
    artifacts = await run_in_threadpool(
        services.api.crud.Artifacts().list_artifacts,
        db_session,
        project,
        name,
        tag,
        labels,
        producer_id=tree,
        limit=limit,
    )
    await framework.utils.auth.verifier.AuthVerifier().query_project_resources_permissions(
        mlrun.common.schemas.AuthorizationResourceTypes.artifact,
        artifacts,
        artifact_project_and_resource_name_extractor,
        mlrun.common.schemas.AuthorizationAction.delete,
        auth_info,
    )
    await run_in_threadpool(
        services.api.crud.Artifacts().delete_artifacts,
        db_session,
        project,
        name,
        tag,
        labels,
        auth_info,
        producer_id=tree,
    )
    return Response(status_code=HTTPStatus.NO_CONTENT.value)

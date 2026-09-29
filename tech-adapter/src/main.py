from __future__ import annotations

import uuid

from fastapi import BackgroundTasks, Request
from loguru import logger
from starlette.background import BackgroundTask
from starlette.responses import Response

from src.app_config import app
from src.check_return_type import check_response
from src.dependencies import (
    UnpackedProvisioningRequestDep,
    UnpackedUnprovisioningRequestDep,
)
from src.models.api_models import (
    ProvisioningStatus,
    SystemErr,
    ValidationError,
    ValidationRequest,
    ValidationResult,
    ValidationStatus,
)
from src.models.task_store import task_store
from src.services.provision_service import run_provisioning
from src.services.unprovision_service import run_unprovisioning
from src.services.validate_service import validate_soda_workload


def log_info(method, path, req_body, res_code, res_body):
    id = str(uuid.uuid4())
    logger.info("[{}] {} {} -> {}", id, method, path, res_code)
    logger.debug("[{}] REQUEST: {}", id, req_body.decode("utf-8"))
    logger.debug("[{}] RESPONSE({}): {}", id, res_code, res_body.decode("utf-8"))


@app.middleware("http")
async def log_request_response_middleware(request: Request, call_next):
    req_body = await request.body()
    response = await call_next(request)
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    res_body = b"".join(chunks)
    task = BackgroundTask(log_info, request.method, request.url.path, req_body, response.status_code, res_body)
    return Response(
        content=res_body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
        background=task,
    )


@app.post(
    "/v1/provision",
    response_model=None,
    responses={
        "200": {"model": ProvisioningStatus},
        "202": {"model": str},
        "400": {"model": ValidationError},
        "500": {"model": SystemErr},
    },
    tags=["TechAdapter"],
)
def provision(request: UnpackedProvisioningRequestDep, background_tasks: BackgroundTasks) -> Response:
    """
    Deploy a data product or a single component starting from a provisioning descriptor
    """

    if isinstance(request, ValidationError):
        return check_response(out_response=request)

    data_product, component_id = request

    logger.info("Provisioning component with id: " + component_id)

    token = task_store.create_running_task()
    background_tasks.add_task(run_provisioning, data_product, component_id, token)

    return check_response(out_response=token)


@app.get(
    "/v1/provision/{token}/status",
    response_model=None,
    responses={
        "200": {"model": ProvisioningStatus},
        "400": {"model": ValidationError},
        "500": {"model": SystemErr},
    },
    tags=["TechAdapter"],
)
def get_status(token: str) -> Response:
    """
    Get the status for a provisioning request
    """

    task_state = task_store.get(token)

    if task_state is None:
        return check_response(out_response=ValidationError(errors=[f"Unknown task token '{token}'."]))

    resp = ProvisioningStatus(status=task_state.status, result=task_state.result, info=task_state.info)

    return check_response(out_response=resp)


@app.post(
    "/v1/unprovision",
    response_model=None,
    responses={
        "200": {"model": ProvisioningStatus},
        "202": {"model": str},
        "400": {"model": ValidationError},
        "500": {"model": SystemErr},
    },
    tags=["TechAdapter"],
)
def unprovision(request: UnpackedUnprovisioningRequestDep, background_tasks: BackgroundTasks) -> Response:
    """
    Undeploy a data product or a single component
    given the provisioning descriptor relative to the latest complete provisioning request
    """  # noqa: E501

    if isinstance(request, ValidationError):
        return check_response(out_response=request)

    data_product, component_id, remove_data = request

    logger.info("Unprovisioning component with id: " + component_id)

    token = task_store.create_running_task()
    background_tasks.add_task(run_unprovisioning, data_product, component_id, token)

    return check_response(out_response=token)


@app.post(
    "/v1/validate",
    response_model=None,
    responses={"200": {"model": ValidationResult}, "500": {"model": SystemErr}},
    tags=["TechAdapter"],
)
def validate(request: UnpackedProvisioningRequestDep) -> Response:
    """
    Validate a provisioning request
    """

    if isinstance(request, ValidationError):
        return check_response(ValidationResult(valid=False, error=request))

    data_product, component_id = request

    result = validate_soda_workload(data_product, component_id)

    return check_response(out_response=result)


@app.post(
    "/v2/validate",
    response_model=None,
    responses={
        "202": {"model": str},
        "400": {"model": ValidationError},
        "500": {"model": SystemErr},
    },
    tags=["TechAdapter"],
)
def async_validate(
    body: ValidationRequest,
) -> Response:
    """
    Validate a deployment request
    """

    resp = SystemErr(error="Response not yet implemented")

    return check_response(out_response=resp)


@app.get(
    "/v2/validate/{token}/status",
    response_model=None,
    responses={
        "200": {"model": ValidationStatus},
        "400": {"model": ValidationError},
        "500": {"model": SystemErr},
    },
    tags=["TechAdapter"],
)
def get_validation_status(
    token: str,
) -> Response:
    """
    Get the status for a validation request
    """

    resp = SystemErr(error="Response not yet implemented")

    return check_response(out_response=resp)

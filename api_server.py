

from __future__ import annotations

import argparse
import dataclasses
import ipaddress
import os
import secrets
import socket
import sys
import tempfile
import threading
import urllib.parse
import uuid
from collections import OrderedDict
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, NoReturn, TypeVar

import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from linode_api4.errors import ApiError
from pydantic import BaseModel

import linode_engine as engine
import instance_manager as im


UTC = timezone.utc

LINODE_OAUTH_AUTHORIZE_URL = "https://login.linode.com/oauth/authorize"
LINODE_OAUTH_TOKEN_URL = "https://login.linode.com/oauth/token"
LINODE_PROFILE_URL = "https://api.linode.com/v4/profile"


OAUTH_SCOPES = "account:read_only"

DEFAULT_SAVINGS_WINDOW_DAYS = 7


def _oauth_config() -> tuple[str, str, str]:

    load_dotenv(engine.BASE_DIR / ".env")
    client_id = os.environ.get("LINODE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("LINODE_OAUTH_CLIENT_SECRET")
    redirect_uri = os.environ.get("LINODE_OAUTH_REDIRECT_URI")
    missing = [
        name for name, value in (
            ("LINODE_OAUTH_CLIENT_ID", client_id),
            ("LINODE_OAUTH_CLIENT_SECRET", client_secret),
            ("LINODE_OAUTH_REDIRECT_URI", redirect_uri),
        ) if not value
    ]
    if missing:
        raise engine.ConfigError(
            f"{', '.join(missing)} not set. Register an OAuth Client in your own Linode account "
            "(Cloud Manager -> Profile -> OAuth Apps), then copy .env.example to .env and fill "
            "these in alongside LINODE_API_TOKEN."
        )
    assert client_id and client_secret and redirect_uri
    return client_id, client_secret, redirect_uri


_pending_oauth_states: dict[str, datetime] = {}
_pending_oauth_states_lock = threading.Lock()
_OAUTH_STATE_LIFETIME = timedelta(minutes=10)


def _sweep_old_oauth_states_locked() -> None:

    now = datetime.now(UTC)
    stale = [s for s, issued_at in _pending_oauth_states.items() if now - issued_at > _OAUTH_STATE_LIFETIME]
    for s in stale:
        del _pending_oauth_states[s]


@asynccontextmanager
async def lifespan(app: FastAPI):

    token = engine.load_token()
    app.state.linode_client = engine.build_client(token)
    app.state.ssh_key = os.environ.get(
        "LINODE_SSH_KEY_PATH", str(Path.home() / ".ssh" / "linode_spike_key"),
    )
    yield


app = FastAPI(
    title="Linode Instance Scheduler API",
    description="Customer-facing REST API for the Linode Instance Scheduler.",
    lifespan=lifespan,
)


_allowed_origins = [o.strip() for o in os.environ.get("API_ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _allowed_origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=_allowed_origins, allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )


def _client(request: Request):
    return request.app.state.linode_client


def _ssh_key(request: Request) -> str:
    return request.app.state.ssh_key


@app.get("/login")
def login() -> RedirectResponse:

    client_id, _secret, redirect_uri = _oauth_config()
    state = secrets.token_urlsafe(24)
    with _pending_oauth_states_lock:
        _sweep_old_oauth_states_locked()
        _pending_oauth_states[state] = datetime.now(UTC)


    query = urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "scope": OAUTH_SCOPES,
        "redirect_uri": redirect_uri, "state": state,
    })
    return RedirectResponse(f"{LINODE_OAUTH_AUTHORIZE_URL}?{query}")


@app.get("/oauth/callback")
def oauth_callback(code: str, state: str) -> RedirectResponse:

    with _pending_oauth_states_lock:
        issued_at = _pending_oauth_states.pop(state, None)
    if issued_at is None:
        raise HTTPException(400, "invalid or already-used OAuth state -- start over at /login.")
    if datetime.now(UTC) - issued_at > _OAUTH_STATE_LIFETIME:
        raise HTTPException(400, "OAuth login took too long -- start over at /login.")

    client_id, client_secret, redirect_uri = _oauth_config()
    try:
        token_response = requests.post(
            LINODE_OAUTH_TOKEN_URL,
            data={
                "grant_type": "authorization_code", "code": code,
                "client_id": client_id, "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
            timeout=15,
        )
        token_response.raise_for_status()
        access_token = token_response.json()["access_token"]

        profile_response = requests.get(
            LINODE_PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=15,
        )
        profile_response.raise_for_status()
        linode_username = profile_response.json()["username"]
    except requests.exceptions.RequestException as e:
        raise HTTPException(502, f"could not complete login with Linode: {e}") from e
    except (KeyError, ValueError) as e:
        raise HTTPException(502, f"unexpected response from Linode during login: {e}") from e

    session = im.create_api_session(linode_username)
    query = urllib.parse.urlencode({
        "session_token": session["token"], "expires_at": session["expires_at"],
    })
    return RedirectResponse(f"/ui/?{query}")


@app.post("/logout")
def logout(authorization: str | None = Header(default=None)) -> dict:
    token = _bearer_token(authorization)
    if token is not None:
        im.delete_api_session(token)
    return {"ok": True}


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None or not authorization.startswith("Bearer "):
        return None
    return authorization[len("Bearer "):]


_ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("POST", "/instances/{name}/start"): "operate",
    ("POST", "/instances/{name}/stop"): "operate",
    ("POST", "/instances/{name}/extend"): "operate",
    ("POST", "/instances/{name}/hooks/run"): "operate",
    ("POST", "/groups/{group_name}/start"): "operate",
    ("POST", "/groups/{group_name}/stop"): "operate",
    ("POST", "/instances/{name}/schedule"): "configure",
    ("DELETE", "/instances/{name}/schedule"): "configure",
    ("PATCH", "/instances/{name}"): "configure",
    ("POST", "/groups"): "configure",
    ("PATCH", "/groups/{group_name}"): "configure",
    ("DELETE", "/groups/{group_name}"): "configure",
    ("POST", "/groups/{group_name}/schedule"): "configure",

    ("PUT", "/instances/{name}/hooks"): "admin",
    ("DELETE", "/instances/{name}/hooks"): "admin",
    ("PUT", "/groups/{group_name}/hooks"): "admin",
    ("DELETE", "/groups/{group_name}/hooks"): "admin",
}

_ADMIN_READ_PREFIXES = ("/linode/", "/tokens")


_LIMITED_TOKEN_OPEN_ROUTES = {
    ("GET", "/instances"), ("GET", "/groups"), ("GET", "/operations/{op_id}"),
}


def _required_scope(method: str, path: str) -> str:
    if (method, path) in _ROUTE_SCOPES:
        return _ROUTE_SCOPES[(method, path)]
    if method == "GET":
        return "admin" if path.startswith(_ADMIN_READ_PREFIXES) else "read"
    return "admin"


def _token_is_limited(meta: dict) -> bool:
    return meta.get("instances") is not None or meta.get("groups") is not None


def require_session(
    request: Request, authorization: str | None = Header(default=None),
) -> str:

    token = _bearer_token(authorization)
    if token is None:
        raise HTTPException(401, "missing or malformed Authorization header -- log in at /login.")
    username = im.get_api_session(token)
    if username is not None:
        request.state.api_token = None
        return username
    meta = im.verify_api_token(token)
    if meta is None:
        raise HTTPException(401, "session token is invalid or has expired -- log in again at /login.")
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    method = request.method
    needed = _required_scope(method, path)
    if needed not in meta["scopes"] and "admin" not in meta["scopes"]:
        raise HTTPException(403, f"this token doesn't have the '{needed}' scope.")
    if _token_is_limited(meta):
        params = request.path_params
        if "name" in params and not im.api_token_allows_instance(meta, params["name"]):
            raise HTTPException(403, f"this token isn't allowed to act on '{params['name']}'.")
        if "group_name" in params and not im.api_token_allows_group(meta, params["group_name"]):
            raise HTTPException(
                403, f"this token isn't allowed to act on group '{params['group_name']}'.")
        if (not {"name", "group_name"} & set(params)
                and (method, path) not in _LIMITED_TOKEN_OPEN_ROUTES):
            raise HTTPException(
                403, "this token is limited to particular nodes/groups and can't use this route.")
    request.state.api_token = meta
    return f"token:{meta['name']}"


def _limited_token(request: Request) -> dict | None:
    meta = getattr(request.state, "api_token", None)
    return meta if meta is not None and _token_is_limited(meta) else None


@app.get("/health")
def health() -> dict:

    return {"ok": True}


@app.exception_handler(im.NotOnboardedError)
def _handle_not_onboarded(request: Request, exc: im.NotOnboardedError):
    return _error_response(404, exc)


@app.exception_handler(im.GroupNotFoundError)
def _handle_group_not_found(request: Request, exc: im.GroupNotFoundError):
    return _error_response(404, exc)


@app.exception_handler(im.NeedsManualRecoveryError)
def _handle_needs_manual_recovery(request: Request, exc: im.NeedsManualRecoveryError):
    return _error_response(409, exc)


@app.exception_handler(im.DependencyNotSatisfiedError)
def _handle_dependency_not_satisfied(request: Request, exc: im.DependencyNotSatisfiedError):
    return _error_response(409, exc)


@app.exception_handler(im.InstanceLockedError)
def _handle_instance_locked(request: Request, exc: im.InstanceLockedError):
    return _error_response(409, exc)


@app.exception_handler(engine.ResourceOwnershipConflict)
def _handle_ownership_conflict(request: Request, exc: engine.ResourceOwnershipConflict):
    return _error_response(409, exc)


@app.exception_handler(engine.IPNotReservedError)
def _handle_ip_not_reserved(request: Request, exc: engine.IPNotReservedError):


    return JSONResponse(
        status_code=409,
        content=_content_with_any_warnings({"detail": str(exc), "unreserved_ip": exc.address}, exc),
    )


@app.exception_handler(engine.NotMigratedError)
def _handle_not_migrated(request: Request, exc: engine.NotMigratedError):


    return JSONResponse(
        status_code=409,
        content=_content_with_any_warnings(
            {"detail": str(exc), "not_migrated_instance_id": exc.instance_id}, exc,
        ),
    )


@app.exception_handler(engine.TagVerificationError)
def _handle_tag_verification(request: Request, exc: engine.TagVerificationError):
    return _error_response(409, exc)


@app.exception_handler(engine.AlreadyOnboardedError)
def _handle_already_onboarded(request: Request, exc: engine.AlreadyOnboardedError):


    return JSONResponse(
        status_code=409,
        content=_content_with_any_warnings(
            {"detail": str(exc), "already_onboarded_name": exc.name}, exc,
        ),
    )


@app.exception_handler(engine.ConfigError)
def _handle_config_error(request: Request, exc: engine.ConfigError):
    return _error_response(400, exc)


@app.exception_handler(ApiError)
def _handle_linode_api_error(request: Request, exc: ApiError):


    return _error_response(502, exc)


@app.exception_handler(requests.exceptions.RequestException)
def _handle_request_exception(request: Request, exc: requests.exceptions.RequestException):


    return _error_response(502, exc)


def _content_with_any_warnings(content: dict, exc: Exception) -> dict:

    warnings = getattr(exc, "warnings", None)
    if warnings:
        content["warnings"] = warnings
    return content


def _reraise_with_warnings(exc: Exception, warnings: list[str]) -> NoReturn:

    if warnings:
        exc.warnings = warnings
    raise exc


_T = TypeVar("_T")


def _call_collecting_warnings(fn: Callable[[Callable[[str], None]], _T]) -> tuple[_T, list[str]]:

    warnings: list[str] = []
    try:
        result = fn(warnings.append)
    except Exception as e:
        _reraise_with_warnings(e, warnings)
    return result, warnings


def _call_collecting_warnings_with_progress(
    fn: Callable[[Callable[[str], None], Callable[[str], None]], _T],
) -> tuple[_T, list[str]]:

    warnings: list[str] = []

    def _on_progress(message: str) -> None:
        if _progress_message_is_warning(message):
            warnings.append(message)

    try:
        result = fn(_on_progress, warnings.append)
    except Exception as e:
        _reraise_with_warnings(e, warnings)
    return result, warnings


def _error_response(status_code: int, exc: Exception) -> JSONResponse:


    return JSONResponse(
        status_code=status_code, content=_content_with_any_warnings({"detail": str(exc)}, exc),
    )


_TOTAL_STEPS: dict[str, int] = {


    "start": 3, "stop": 2,


    "migrate_start": 5, "migrate_resume": 8,
}
_OPERATION_TTL = timedelta(minutes=10)


@dataclasses.dataclass
class _Operation:
    action: str
    total_steps: int
    status: Literal["running", "done", "error"] = "running"
    completed_steps: int = 0
    current_step: str | None = None


    warnings: list[str] = dataclasses.field(default_factory=list)
    result: dict | None = None
    error_status: int = 500
    error_body: dict | None = None


    created_at: datetime = dataclasses.field(default_factory=lambda: datetime.now(UTC))


    finished_at: datetime | None = None
    actor: str | None = None


_operations: OrderedDict[str, _Operation] = OrderedDict()
_operations_lock = threading.Lock()


def _classify_exception(e: Exception) -> tuple[int, dict]:

    if isinstance(e, engine.IPNotReservedError):
        return 409, {"detail": str(e), "unreserved_ip": e.address}


    if isinstance(e, engine.NotMigratedError):
        return 409, {"detail": str(e), "not_migrated_instance_id": e.instance_id}
    if isinstance(e, engine.AlreadyOnboardedError):
        return 409, {"detail": str(e), "already_onboarded_name": e.name}
    if isinstance(e, (im.NotOnboardedError, im.GroupNotFoundError)):
        return 404, {"detail": str(e)}
    if isinstance(
        e, (im.NeedsManualRecoveryError, im.InstanceLockedError, im.DependencyNotSatisfiedError,
            engine.ResourceOwnershipConflict, engine.TagVerificationError),
    ):
        return 409, {"detail": str(e)}
    if isinstance(e, engine.ConfigError):
        return 400, {"detail": str(e)}
    if isinstance(e, (ApiError, requests.exceptions.RequestException)):


        return 502, {"detail": str(e)}
    return 500, {"detail": str(e)}


def _sweep_old_operations_locked() -> None:

    now = datetime.now(UTC)
    stale = [
        op_id for op_id, op in _operations.items()
        if op.status != "running" and now - (op.finished_at or op.created_at) > _OPERATION_TTL
    ]
    for op_id in stale:
        del _operations[op_id]


def _start_operation(action: str, total_steps: int | None = None, actor: str | None = None) -> str:
    op_id = uuid.uuid4().hex
    with _operations_lock:
        _sweep_old_operations_locked()
        _operations[op_id] = _Operation(
            action=action, total_steps=total_steps if total_steps is not None else _TOTAL_STEPS[action],
            actor=actor,
        )
    return op_id


def _make_progress_reporter(op_id: str) -> Callable[[str], None]:
    def _on_progress(message: str) -> None:
        with _operations_lock:
            op = _operations.get(op_id)
            if op is None:
                return
            op.completed_steps += 1
            op.current_step = message
    return _on_progress


def _make_warning_reporter(op_id: str) -> Callable[[str], None]:
    def _on_warning(message: str) -> None:
        with _operations_lock:
            op = _operations.get(op_id)
            if op is None:
                return
            op.warnings.append(message)
    return _on_warning


def _progress_message_is_warning(message: str) -> bool:

    return message.strip().startswith("WARNING")


def _make_progress_and_warning_reporter(op_id: str) -> Callable[[str], None]:

    progress = _make_progress_reporter(op_id)
    warn = _make_warning_reporter(op_id)

    def _on_progress(message: str) -> None:
        progress(message)
        if _progress_message_is_warning(message):
            warn(message)
    return _on_progress


def _run_job(op_id: str, fn: Callable[[], object]) -> None:

    try:
        result = fn()
        with _operations_lock:
            op = _operations.get(op_id)
            if op is not None:
                op.status = "done"
                op.result = _asdict(result)
                op.finished_at = datetime.now(UTC)
    except Exception as e:


        status, body = _classify_exception(e)
        with _operations_lock:
            op = _operations.get(op_id)
            if op is not None:
                op.status = "error"
                op.error_status = status
                op.error_body = body
                op.finished_at = datetime.now(UTC)


WAIT_MAX_SECONDS = 3600


def _kickoff(
    action: str, total_steps: int, fn: Callable[[str], object], wait: bool,
    actor: str | None = None,
) -> JSONResponse:

    op_id = _start_operation(action, total_steps=total_steps, actor=actor)
    thread = threading.Thread(target=_run_job, args=(op_id, lambda: fn(op_id)), daemon=True)
    thread.start()
    if not wait:
        return JSONResponse({"operation_id": op_id, "total_steps": total_steps})
    thread.join(timeout=WAIT_MAX_SECONDS)
    response = _operation_response(op_id)
    if thread.is_alive():
        response.status_code = 202
    return response


def _operation_response(op_id: str) -> JSONResponse:
    with _operations_lock:
        op = _operations.get(op_id)
        if op is None:
            raise HTTPException(404, "unknown or expired operation id.")
        percent = (
            round(min(op.completed_steps, op.total_steps) / op.total_steps * 100)
            if op.total_steps else 100
        )
        body: dict = {
            "status": op.status, "action": op.action, "percent": percent,
            "current_step": op.current_step, "warnings": op.warnings, "operation_id": op_id,
        }
        if op.status == "done":
            body["result"] = op.result
            return JSONResponse(status_code=200, content=body)
        if op.status == "error":
            assert op.error_body is not None
            body.update(op.error_body)
            return JSONResponse(status_code=op.error_status, content=body)
        return JSONResponse(status_code=200, content=body)


@app.get("/operations/{op_id}")
def api_get_operation(
    op_id: str, request: Request, user: str = Depends(require_session),
) -> JSONResponse:
    if _limited_token(request) is not None:
        with _operations_lock:
            op = _operations.get(op_id)
            if op is not None and op.actor != user:
                raise HTTPException(404, "unknown or expired operation id.")
    return _operation_response(op_id)


class ScheduleRule(BaseModel):
    days_of_week: list[str]
    start_time: str
    stop_time: str


class ScheduleSetRequest(BaseModel):
    timezone: str
    rules: list[ScheduleRule]
    enabled: bool = True


class StartRequest(BaseModel):
    override_window_hours: float | None = None
    skip_hooks: bool = False

    respect_dependencies: bool = False


class StopRequest(BaseModel):
    skip_precapture: bool = False
    force: bool = False
    skip_hooks: bool = False
    respect_dependencies: bool = False


class HookConfigRequest(BaseModel):

    pre_stop: dict | None = None
    post_start: dict | None = None


class HookRunRequest(BaseModel):
    hook: Literal["pre_stop", "post_start"]


class ExtendRequest(BaseModel):
    hours: float | None = None


class OnboardRequest(BaseModel):

    name: str
    instance_id: int
    vpc_id: int | None = None
    force: bool = False


    ssh_private_key: str | None = None
    ssh_password: str | None = None


class OffboardRequest(BaseModel):
    delete_volumes: bool = False


class MigrateStartRequest(BaseModel):
    instance_id: int


    ssh_private_key: str | None = None
    ssh_password: str | None = None


class GroupCreateRequest(BaseModel):
    name: str
    timezone: str


class GroupPatchInstanceRequest(BaseModel):

    group_name: str | None = None
    copy_group_rules_as_individual: bool | None = None


    schedule_mode: Literal["auto", "manual"] | None = None


def _rules_as_dicts(rules: list[ScheduleRule]) -> list[dict]:
    return [r.model_dump() for r in rules]


@app.get("/linode/instances")
def api_list_linode_instances(request: Request, user: str = Depends(require_session)) -> list[dict]:

    client = _client(request)
    registry = im.load_registry()
    onboarded_by_linode_id = {
        rec["current_linode_id"]: name
        for name, rec in registry.items() if rec.get("current_linode_id")
    }


    instances = engine.retry_transient(lambda: list(client.linode.instances()))
    return [
        {
            "id": inst.id,
            "label": inst.label,
            "region": inst.region.id,
            "status": inst.status,
            "tags": list(inst.tags) if inst.tags else [],
            "ipv4": [str(ip) for ip in inst.ipv4] if inst.ipv4 else [],
            "onboarded_as": onboarded_by_linode_id.get(inst.id),
        }
        for inst in instances
        if inst.lke_cluster_id is None
    ]


@app.post("/linode/ips/{address}/reserve")
def api_reserve_ip(address: str, request: Request, user: str = Depends(require_session)) -> dict:

    engine.reserve_existing_ip(_client(request), address)
    return {"reserved": True}


def _reject_internal_network_target(host: str) -> None:

    try:
        candidates = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError as e:
        raise HTTPException(422, f"could not resolve host {host!r}: {e}") from e
    for candidate in candidates:
        addr = ipaddress.ip_address(candidate)


        if (
            not addr.is_global or addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified
        ):
            raise HTTPException(
                422, f"host {host!r} resolves to a private/internal address ({candidate}) -- "
                "this check is only for reaching a real, public instance IP.",
            )


@app.get("/linode/ssh-check")
def api_ssh_check(host: str, port: int = 22, user: str = Depends(require_session)) -> dict:

    _reject_internal_network_target(host)
    return engine.check_ssh_port_reachable(host, port)


class TokenCreateRequest(BaseModel):
    name: str
    scopes: list[str]
    instances: list[str] | None = None
    groups: list[str] | None = None
    expires_days: float | None = None


@app.post("/tokens")
def api_create_token(body: TokenCreateRequest, user: str = Depends(require_session)) -> dict:

    return im.create_api_token(
        body.name, body.scopes, instances=body.instances, groups=body.groups,
        expires_days=body.expires_days, created_by=user,
    )


@app.get("/tokens")
def api_list_tokens(user: str = Depends(require_session)) -> list[dict]:
    return im.list_api_tokens()


@app.delete("/tokens/{token_name}")
def api_revoke_token(token_name: str, user: str = Depends(require_session)) -> dict:
    if not im.revoke_api_token(token_name):
        raise HTTPException(404, f"no active token named '{token_name}'.")
    return {"revoked": True}


@app.get("/instances")
def api_list_instances(request: Request, user: str = Depends(require_session)) -> dict:
    instances = im.list_instances()
    meta = _limited_token(request)
    if meta is not None:
        instances = {n: r for n, r in instances.items() if im.api_token_allows_instance(meta, n)}
    return instances


def _write_temp_ssh_key(key_text: str) -> str:

    fd, path = tempfile.mkstemp(prefix="onboard-ssh-key-")
    try:
        text = key_text if key_text.endswith("\n") else key_text + "\n"
        with os.fdopen(fd, "w") as f:
            f.write(text)
    except BaseException:
        os.remove(path)
        raise
    os.chmod(path, 0o600)
    return path


def _default_public_key_text(request: Request) -> str:

    pub_path = Path(_ssh_key(request) + ".pub")
    try:
        return pub_path.read_text().strip()
    except OSError as e:
        raise engine.ConfigError(
            f"could not read this deployment's own public key at {pub_path} -- needed to "
            "install it on the target instance after a one-time credential is used."
        ) from e


def _resolve_ssh_credential(
    request: Request, ssh_private_key: str | None, ssh_password: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:

    if ssh_private_key:
        temp_key_path = _write_temp_ssh_key(ssh_private_key)
        try:
            return temp_key_path, None, _default_public_key_text(request), temp_key_path
        except Exception:
            with suppress(FileNotFoundError):
                os.remove(temp_key_path)
            raise
    if ssh_password:
        return None, ssh_password, _default_public_key_text(request), None
    return _ssh_key(request), None, None, None


@app.post("/instances")
def api_onboard(
    body: OnboardRequest, request: Request, user: str = Depends(require_session),
) -> dict:
    try:
        name = engine.validate_instance_name(body.name)
    except argparse.ArgumentTypeError as e:
        raise engine.ConfigError(str(e)) from e
    if body.ssh_private_key and body.ssh_password:
        raise HTTPException(
            422, "ssh_private_key and ssh_password are mutually exclusive -- provide at most one."
        )

    temp_key_path: str | None = None
    try:
        ssh_key, ssh_password, install_public_key, temp_key_path = _resolve_ssh_credential(
            request, body.ssh_private_key, body.ssh_password
        )


        result, warnings = _call_collecting_warnings_with_progress(
            lambda on_progress, on_warning: im.onboard_instance(
                _client(request), name, body.instance_id, ssh_key,
                vpc_id=body.vpc_id, force=body.force, ssh_password=ssh_password,
                install_public_key=install_public_key,
                on_progress=on_progress, on_warning=on_warning,
            )
        )
        body_dict = _asdict(result)
        if warnings:
            body_dict["warnings"] = warnings
        return body_dict
    finally:


        if temp_key_path is not None:
            with suppress(FileNotFoundError):
                os.remove(temp_key_path)


@app.post("/instances/{name}/offboard")
def api_offboard(
    name: str, body: OffboardRequest, request: Request, user: str = Depends(require_session),
) -> dict:

    result, warnings = _call_collecting_warnings_with_progress(
        lambda on_progress, on_warning: im.offboard_instance(
            _client(request), name, delete_volumes=body.delete_volumes,
            on_progress=on_progress, on_warning=on_warning,
        )
    )
    body_dict = _asdict(result)
    if warnings:
        body_dict["warnings"] = warnings
    return body_dict


@app.post("/instances/{name}/deregister")
def api_deregister(name: str, user: str = Depends(require_session)) -> dict:

    result = im.deregister_instance(name)
    return _asdict(result)


@app.get("/instances/{name}/status")
def api_get_status(name: str, user: str = Depends(require_session)) -> dict:
    return im.get_instance_status(name)


@app.get("/instances/{name}/history")
def api_get_history(name: str, limit: int = 50, user: str = Depends(require_session)) -> list[dict]:
    return im.get_schedule_events(name, limit=limit)


@app.post("/instances/{name}/start")
def api_start(
    name: str, body: StartRequest, request: Request, wait: bool = False,
    user: str = Depends(require_session),
) -> JSONResponse:
    client, ssh_key = _client(request), _ssh_key(request)
    if body.respect_dependencies:
        im.require_dependency(name, "start")
    total_steps = _TOTAL_STEPS["start"] + _hook_progress_steps(name, "post_start", body.skip_hooks)

    def _do(op_id: str) -> im.StartResult:
        return im.start_instance(
            client, name, ssh_key, triggered_by="api",
            override_window_hours=body.override_window_hours, actor=user,
            on_progress=_make_progress_reporter(op_id), on_warning=_make_warning_reporter(op_id),
            skip_hooks=body.skip_hooks, respect_dependencies=body.respect_dependencies,
        )

    return _kickoff("start", total_steps, _do, wait, actor=user)


@app.post("/instances/{name}/stop")
def api_stop(
    name: str, body: StopRequest, request: Request, wait: bool = False,
    user: str = Depends(require_session),
) -> JSONResponse:
    client, ssh_key = _client(request), _ssh_key(request)
    if body.respect_dependencies:
        im.require_dependency(name, "stop")
    total_steps = _TOTAL_STEPS["stop"] + _hook_progress_steps(
        name, "pre_stop", body.skip_hooks or body.skip_precapture,
    )

    def _do(op_id: str) -> im.StopResult:
        return im.stop_instance(
            client, name, ssh_key,
            skip_precapture=body.skip_precapture, force=body.force, skip_hooks=body.skip_hooks,
            triggered_by="api", actor=user,
            on_progress=_make_progress_reporter(op_id), on_warning=_make_warning_reporter(op_id),
            respect_dependencies=body.respect_dependencies,
        )

    return _kickoff("stop", total_steps, _do, wait, actor=user)


def _hook_progress_steps(name: str, hook_type: str, skipped: bool) -> int:

    if skipped:
        return 0
    record = im.load_registry().get(name)
    if record is None:
        return 0
    return 1 if im.resolve_effective_hooks(name, record)[hook_type] is not None else 0


@app.post("/instances/{name}/migrate-start")
def api_migrate_start(
    name: str, body: MigrateStartRequest, request: Request, user: str = Depends(require_session),
) -> dict:

    if body.ssh_private_key and body.ssh_password:


        raise HTTPException(
            422, "ssh_private_key and ssh_password are mutually exclusive -- provide at most one."
        )
    client = _client(request)


    ssh_key, ssh_password, install_public_key, temp_key_path = _resolve_ssh_credential(
        request, body.ssh_private_key, body.ssh_password
    )


    total_steps = _TOTAL_STEPS["migrate_start"] + (1 if install_public_key is not None else 0)
    op_id = _start_operation("migrate_start", total_steps=total_steps)

    def _do() -> im.MigrateStartResult:
        try:
            return im.migrate_start_instance(
                client, name, body.instance_id, ssh_key,
                ssh_password=ssh_password, install_public_key=install_public_key,
                on_progress=_make_progress_and_warning_reporter(op_id),
                on_warning=_make_warning_reporter(op_id),
            )
        finally:
            if temp_key_path is not None:
                with suppress(FileNotFoundError):
                    os.remove(temp_key_path)

    threading.Thread(target=_run_job, args=(op_id, _do), daemon=True).start()
    return {"operation_id": op_id, "total_steps": total_steps}


@app.get("/instances/{name}/migrate-status")
def api_migrate_status(name: str, user: str = Depends(require_session)) -> dict:

    state = im.load_migrations().get(name)
    if state is None:
        return {"in_progress": False}
    return {
        "in_progress": True,
        "phase": state.get("phase"),
        "instance_id": state.get("instance_id"),
        "dest_volume_id": state.get("dest_volume_id"),
        "dest_volume_size_gb": state.get("dest_volume_size_gb"),
        "local_disk_size_mb": state.get("local_disk_size_mb"),
    }


@app.post("/instances/{name}/migrate-resume")
def api_migrate_resume(name: str, request: Request, user: str = Depends(require_session)) -> dict:

    client, ssh_key = _client(request), _ssh_key(request)
    op_id = _start_operation("migrate_resume")

    def _do() -> im.MigrateResumeResult:


        return im.migrate_resume_instance(
            client, name, ssh_key, on_progress=_make_progress_and_warning_reporter(op_id),
            on_warning=_make_warning_reporter(op_id),
        )

    threading.Thread(target=_run_job, args=(op_id, _do), daemon=True).start()
    return {"operation_id": op_id, "total_steps": _TOTAL_STEPS["migrate_resume"]}


@app.post("/instances/{name}/extend")
def api_extend(name: str, body: ExtendRequest, user: str = Depends(require_session)) -> dict:
    new_expiry = im.extend_manual_override(name, body.hours)
    return {"manual_override_expires_at": new_expiry}


@app.get("/instances/{name}/hooks")
def api_get_instance_hooks(name: str, user: str = Depends(require_session)) -> dict:

    record = im.load_registry().get(name)
    if record is None:
        raise im.NotOnboardedError(f"'{name}' is not onboarded.")
    return {
        "own": im.get_instance_hooks(name),
        "effective": im.resolve_effective_hooks(name, record),
        "last_post_start_failure": im.get_last_hook_failure(name, "post_start"),
    }


@app.put("/instances/{name}/hooks")
def api_set_instance_hooks(
    name: str, body: HookConfigRequest, request: Request, user: str = Depends(require_session),
) -> dict:

    own, warnings = _call_collecting_warnings(
        lambda on_warning: im.set_instance_hooks(
            name, body.model_dump(), triggered_by="api", actor=user,
            client=_client(request), on_warning=on_warning,
        )
    )
    result: dict = {"own": own}
    if warnings:
        result["warnings"] = warnings
    return result


@app.delete("/instances/{name}/hooks")
def api_clear_instance_hooks(
    name: str, request: Request, user: str = Depends(require_session),
) -> dict:
    if name not in im.load_registry():
        raise im.NotOnboardedError(f"'{name}' is not onboarded.")
    cleared, warnings = _call_collecting_warnings(
        lambda on_warning: im.clear_instance_hooks(
            name, triggered_by="api", actor=user, client=_client(request), on_warning=on_warning,
        )
    )
    result: dict = {"cleared": cleared}
    if warnings:
        result["warnings"] = warnings
    return result


@app.post("/instances/{name}/hooks/run")
def api_run_instance_hook(
    name: str, body: HookRunRequest, request: Request, user: str = Depends(require_session),
) -> dict:

    result = im.run_hook_now(
        name, body.hook, _ssh_key(request), triggered_by="api", actor=user,
    )
    return {
        "ok": result.ok, "summary": result.summary, "exit_code": result.exit_code,
        "output_tail": result.output_tail, "attempts": result.attempts,
    }


@app.get("/instances/{name}/hook-events")
def api_get_hook_events(
    name: str, limit: int = 50, user: str = Depends(require_session),
) -> list[dict]:
    return im.get_hook_events(name, limit=limit)


@app.get("/groups/{group_name}/hooks")
def api_get_group_hooks(group_name: str, user: str = Depends(require_session)) -> dict:
    return {"hooks": im.get_group_hooks(group_name)}


@app.put("/groups/{group_name}/hooks")
def api_set_group_hooks(
    group_name: str, body: HookConfigRequest, request: Request,
    user: str = Depends(require_session),
) -> dict:
    hooks, warnings = _call_collecting_warnings(
        lambda on_warning: im.set_group_hooks(
            group_name, body.model_dump(), triggered_by="api", actor=user,
            client=_client(request), on_warning=on_warning,
        )
    )
    result: dict = {"hooks": hooks}
    if warnings:
        result["warnings"] = warnings
    return result


@app.delete("/groups/{group_name}/hooks")
def api_clear_group_hooks(
    group_name: str, request: Request, user: str = Depends(require_session),
) -> dict:
    cleared, warnings = _call_collecting_warnings(
        lambda on_warning: im.clear_group_hooks(
            group_name, triggered_by="api", actor=user, client=_client(request),
            on_warning=on_warning,
        )
    )
    result: dict = {"cleared": cleared}
    if warnings:
        result["warnings"] = warnings
    return result


@app.get("/instances/{name}/schedule")
def api_get_schedule(name: str, user: str = Depends(require_session)) -> dict | None:
    return im.get_instance_schedule(name)


@app.post("/instances/{name}/schedule")
def api_set_schedule(
    name: str, body: ScheduleSetRequest, request: Request, user: str = Depends(require_session),
) -> dict:


    _, warnings = _call_collecting_warnings(
        lambda on_warning: im.save_instance_schedule(
            _client(request), name, body.timezone, _rules_as_dicts(body.rules),
            enabled=body.enabled, on_warning=on_warning,
        )
    )
    schedule = im.get_instance_schedule(name)
    if schedule is None:


        raise im.NotOnboardedError(f"'{name}' is not onboarded. Run `onboard` first.")
    result: dict = dict(schedule)
    if warnings:
        result["warnings"] = warnings
    return result


@app.delete("/instances/{name}/schedule")
def api_clear_schedule(name: str, request: Request, user: str = Depends(require_session)) -> dict:
    cleared, warnings = _call_collecting_warnings(
        lambda on_warning: im.clear_instance_schedule(_client(request), name, on_warning=on_warning)
    )
    result: dict = {"cleared": cleared}
    if warnings:
        result["warnings"] = warnings
    return result


@app.patch("/instances/{name}")
def api_patch_instance(
    name: str, body: GroupPatchInstanceRequest, request: Request,
    user: str = Depends(require_session),
) -> dict:
    if body.schedule_mode is not None and "group_name" not in body.model_fields_set:
        mode: str = body.schedule_mode
        _, warnings = _call_collecting_warnings(
            lambda on_warning: im.set_schedule_mode(
                _client(request), name, mode, on_warning=on_warning,
            )
        )
        result: dict = {"schedule_mode": mode}
        if warnings:
            result["warnings"] = warnings
        return result
    if body.group_name is not None:


        group_name: str = body.group_name
        group_id, warnings = _call_collecting_warnings(
            lambda on_warning: im.assign_instance_to_group(
                _client(request), name, group_name, on_warning=on_warning,
            )
        )
        result = {"group_id": group_id}
        if warnings:
            result["warnings"] = warnings
        return result
    if body.copy_group_rules_as_individual is None:
        raise HTTPException(
            422,
            "clearing group_name requires copy_group_rules_as_individual (true/false) -- there's "
            "no interactive prompt over HTTP, so the caller must decide up front, the same "
            "choice the CLI's own --keep-manual/--copy-schedule flags make explicit.",
        )
    copy_group_rules_as_individual: bool = body.copy_group_rules_as_individual
    outcome, warnings = _call_collecting_warnings(
        lambda on_warning: im.remove_instance_from_group(
            _client(request), name,
            copy_group_rules_as_individual=copy_group_rules_as_individual,
            on_warning=on_warning,
        )
    )
    result = {"outcome": outcome}
    if warnings:
        result["warnings"] = warnings
    return result


@app.post("/groups")
def api_create_group(body: GroupCreateRequest, user: str = Depends(require_session)) -> dict:
    im.create_schedule_group(body.name, body.timezone)
    group = im.get_schedule_group(body.name)
    if group is None:


        raise im.GroupNotFoundError(f"no schedule group named '{body.name}' exists.")
    return group


@app.get("/groups")
def api_list_groups(request: Request, user: str = Depends(require_session)) -> list[dict]:
    groups = im.list_schedule_groups()
    meta = _limited_token(request)
    if meta is not None:
        groups = [g for g in groups if im.api_token_allows_group(meta, g["name"])]
    return groups


@app.get("/groups/{group_name}")
def api_get_group(group_name: str, user: str = Depends(require_session)) -> dict:
    group = im.get_schedule_group(group_name)
    if group is None:
        raise HTTPException(404, f"no schedule group named '{group_name}' exists.")
    return group


class GroupActionRequest(BaseModel):


    with_dependencies: bool = False
    respect_dependencies: bool = False
    skip_hooks: bool = False
    max_parallel: int = im.DEFAULT_GROUP_ACTION_MAX_PARALLEL


def _group_action_route(
    group_name: str, action: str, body: GroupActionRequest, request: Request, wait: bool,
    user: str,
) -> JSONResponse:
    client, ssh_key = _client(request), _ssh_key(request)
    act: Literal["start", "stop"] = "start" if action == "start" else "stop"
    stages = im._group_chain(group_name, act) if body.with_dependencies else [group_name]
    if not body.with_dependencies and im.get_schedule_group(group_name) is None:
        raise im.GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    if body.respect_dependencies and not body.with_dependencies:
        for member in im.get_schedule_group(group_name)["members"]:
            im.require_dependency(member, act)
    total = sum(len((im.get_schedule_group(g) or {}).get("members", [])) for g in stages)

    def _do(op_id: str) -> im.GroupActionResult:
        def _step(message: str) -> None:
            with _operations_lock:
                op = _operations.get(op_id)
                if op is not None:
                    op.current_step = message

        def _member_done(member: im.GroupMemberResult) -> None:
            with _operations_lock:
                op = _operations.get(op_id)
                if op is not None:
                    op.completed_steps += 1
                    op.current_step = f"{member.group}/{member.name}: {member.outcome}"

        return im.group_action(
            client, group_name, act, ssh_key,
            include_dependencies=body.with_dependencies,
            respect_dependencies=body.respect_dependencies,
            triggered_by="api", actor=user, skip_hooks=body.skip_hooks,
            max_parallel=body.max_parallel,
            client_factory=im._per_thread_client_factory() if body.max_parallel > 1 else None,
            on_progress=_step, on_warning=_make_warning_reporter(op_id),
            on_member_done=_member_done,
        )

    return _kickoff(f"group_{act}", max(total, 1), _do, wait, actor=user)


@app.post("/groups/{group_name}/start")
def api_group_start(
    group_name: str, body: GroupActionRequest, request: Request, wait: bool = False,
    user: str = Depends(require_session),
) -> JSONResponse:
    return _group_action_route(group_name, "start", body, request, wait, user)


@app.post("/groups/{group_name}/stop")
def api_group_stop(
    group_name: str, body: GroupActionRequest, request: Request, wait: bool = False,
    user: str = Depends(require_session),
) -> JSONResponse:
    return _group_action_route(group_name, "stop", body, request, wait, user)


class GroupPatchRequest(BaseModel):


    depends_on: list[str] | str | None


@app.patch("/groups/{group_name}")
def api_patch_group(
    group_name: str, body: GroupPatchRequest, request: Request,
    user: str = Depends(require_session),
) -> dict:
    meta = _limited_token(request)
    if meta is not None:


        targets = im.normalize_dependency_list(body.depends_on)
        outside = [t for t in targets if not im.api_token_allows_group(meta, t)]
        if outside:
            raise HTTPException(
                status_code=403,
                detail=f"this token isn't allowed to act on group(s) {', '.join(outside)}.",
            )
    _, warnings = _call_collecting_warnings(
        lambda on_warning: im.set_group_dependencies(
            _client(request), group_name, body.depends_on, on_warning=on_warning,
        )
    )
    group = im.get_schedule_group(group_name)
    if group is None:
        raise im.GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    result: dict = dict(group)
    if warnings:
        result["warnings"] = warnings
    return result


@app.delete("/groups/{group_name}")
def api_delete_group(group_name: str, user: str = Depends(require_session)) -> dict:
    im.delete_schedule_group(group_name)
    return {"deleted": True}


@app.post("/groups/{group_name}/schedule")
def api_set_group_schedule(
    group_name: str, body: ScheduleSetRequest, request: Request,
    user: str = Depends(require_session),
) -> dict:


    _, warnings = _call_collecting_warnings(
        lambda on_warning: im.set_group_schedule(
            _client(request), group_name, body.timezone, _rules_as_dicts(body.rules),
            enabled=body.enabled, on_warning=on_warning,
        )
    )
    group = im.get_schedule_group(group_name)
    if group is None:


        raise im.GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    result: dict = dict(group)
    if warnings:
        result["warnings"] = warnings
    return result


MAX_SAVINGS_WINDOW_DAYS = 3650


def _validate_savings_days(days: int) -> None:
    if not (0 <= days <= MAX_SAVINGS_WINDOW_DAYS):
        raise engine.ConfigError(
            f"days must be between 0 and {MAX_SAVINGS_WINDOW_DAYS}, got {days}."
        )


def _savings_for_schedule(name_for_events: str, schedule: dict | None, days: int) -> dict:

    _validate_savings_days(days)
    if not im.schedule_is_active(schedule):
        return {
            "scheduled_savings_percent": None, "actual_savings_percent": None,
            "window_days": days, "schedule_state": "none" if schedule is None else "disabled",
        }
    scheduled = im.compute_scheduled_savings_percent(schedule)
    window_end = datetime.now(UTC)
    window_start = window_end - timedelta(days=days)
    events = im.get_schedule_events_since(name_for_events, window_start)
    actual = im.compute_actual_savings_percent(events, window_start, window_end)
    return {
        "scheduled_savings_percent": scheduled, "actual_savings_percent": actual,
        "window_days": days, "schedule_state": "active",
    }


@app.get("/instances/{name}/savings")
def api_instance_savings(
    name: str, days: int = DEFAULT_SAVINGS_WINDOW_DAYS, user: str = Depends(require_session),
) -> dict:
    record = im.load_registry().get(name)
    if record is None:
        raise HTTPException(404, f"'{name}' is not onboarded.")
    schedule, _via_group = im.resolve_effective_schedule(name, record)
    return _savings_for_schedule(name, schedule, days)


@app.get("/groups/{group_name}/savings")
def api_group_savings(
    group_name: str, days: int = DEFAULT_SAVINGS_WINDOW_DAYS, user: str = Depends(require_session),
) -> dict:


    group = im.get_schedule_group(group_name)
    if group is None:
        raise HTTPException(404, f"no schedule group named '{group_name}' exists.")
    _validate_savings_days(days)
    schedule = {"timezone": group["timezone"], "rules": group["rules"], "enabled": group["enabled"]}


    active = im.schedule_is_active(schedule)
    scheduled = im.compute_scheduled_savings_percent(schedule) if active else None


    schedule_state = "none" if not schedule["rules"] else ("active" if active else "disabled")
    return {
        "scheduled_savings_percent": scheduled,
        "actual_savings_percent": None,
        "window_days": days,
        "schedule_state": schedule_state,
        "note": "actual_savings_percent isn't computed at the group level -- each member has its "
        "own uptime history; check GET /instances/{name}/savings per member.",
    }


def _asdict(result) -> dict:

    data = dataclasses.asdict(result)
    if isinstance(result, im.GroupActionResult):
        data["ok"] = result.ok
    return data


_WEB_DIST = engine.BASE_DIR / "web" / "dist"
_DASHBOARD_BUILT = _WEB_DIST.is_dir()
if _DASHBOARD_BUILT:
    from fastapi.staticfiles import StaticFiles

    app.mount("/ui", StaticFiles(directory=str(_WEB_DIST), html=True), name="ui")


@app.get("/", include_in_schema=False, response_model=None)
def root_redirect() -> RedirectResponse | dict:


    if not _DASHBOARD_BUILT:
        return {"message": "Dashboard not built (web/dist missing) -- API docs at /docs."}
    return RedirectResponse("/ui/")


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Linode Instance Scheduler API server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    try:
        run_server(args.host, args.port)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import math
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import types
import uuid
from collections.abc import Callable
from contextlib import contextmanager, suppress
from pathlib import Path

import requests.exceptions
from dotenv import load_dotenv
from linode_api4 import Instance, LinodeClient, Volume
from linode_api4.errors import ApiError
from linode_api4.objects.linode import InstancePlacementGroupAssignment
from linode_api4.objects.networking import IPAddress, ReservedIPAddress

BASE_DIR = Path(__file__).resolve().parent


NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,27}")


def validate_instance_name(value: str) -> str:


    if not NAME_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a valid name -- must start with a lowercase letter or "
            "digit, contain only lowercase letters, digits, '-', or '_', and be 1-28 "
            "characters (e.g. 'redis-standby-1')."
        )
    return value


def validate_ip_address(value: str) -> str:

    try:
        ipaddress.ip_address(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid IP address: {e}") from e
    return value


REQUIRED_REGION_CAPABILITIES = ("Block Storage",)


class ConfigError(RuntimeError):
    pass


class ResourceOwnershipConflict(ConfigError):
    pass


class TagVerificationError(ConfigError):
    pass


class IPNotReservedError(ConfigError):


    def __init__(self, address: str):
        self.address = address
        super().__init__(
            f"{address} is not a reserved IP. If this instance is ever stopped by this "
            "tool, the address would return to Linode's pool and could be lost/reassigned "
            "to someone else. Reserve it first (Cloud Manager, or the reserved-IP API) "
            "before onboarding."
        )


class NotMigratedError(ConfigError):


    def __init__(self, instance_id: int):
        self.instance_id = instance_id
        super().__init__(
            f"instance {instance_id}'s /dev/sda is not a Block Storage volume -- this instance "
            "hasn't been migrated off local disk yet (a one-time rescue-mode disk copy). "
            "Onboarding requires that to be done first."
        )


class AlreadyOnboardedError(ConfigError):


    def __init__(self, name: str):
        self.name = name
        super().__init__(
            f"'{name}' is already onboarded. Use --force to re-onboard (re-captures everything "
            "from scratch)."
        )


def load_token() -> str:

    load_dotenv(BASE_DIR / ".env")
    token = os.environ.get("LINODE_API_TOKEN")
    if not token:
        raise ConfigError(
            "LINODE_API_TOKEN is not set. Set it as an environment variable directly "
            "(e.g. a Kubernetes Secret), or, if you're running from a local checkout, "
            "copy .env.example to .env and fill in a real Linode Personal "
            "Access Token."
        )
    return token


def load_json_file(path: Path) -> dict:

    if not path.exists():
        return {}
    with path.open("r") as f:
        return json.load(f)


def save_json_file(path: Path, data: dict, *, tmp_prefix: str) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=tmp_prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


def build_client(token: str) -> LinodeClient:
    return LinodeClient(token)


def auth_check(client: LinodeClient) -> None:

    try:
        retry_transient(client.account)
    except ApiError as e:
        if e.status in (401, 403):
            raise ConfigError(
                f"Linode API authentication failed (HTTP {e.status}). "
                "Check that the configured LINODE_API_TOKEN is valid and has "
                "the required scopes."
            ) from e
        raise


def check_region_capabilities(
    client: LinodeClient,
    region_id: str,
    required: tuple[str, ...] = REQUIRED_REGION_CAPABILITIES,
) -> None:

    regions = {r.id: r for r in retry_transient(lambda: list(client.regions()))}
    region = regions.get(region_id)
    if region is None:
        raise ConfigError(
            f"Region '{region_id}' was not found in the Linode API's region list."
        )
    missing = [cap for cap in required if cap not in region.capabilities]
    if missing:
        raise ConfigError(
            f"Region '{region_id}' is missing required capabilities: {missing}. "
            f"It supports: {sorted(region.capabilities)}"
        )


def capture_instance_attributes(instance) -> dict:

    firewall_ids = (
        [f.id for f in instance.firewalls()]
        if instance.interface_generation == INTERFACE_MODEL_LEGACY
        else []
    )
    return {
        "label": instance.label,
        "tags": list(instance.tags),
        "plan_type": instance.type.id,
        "firewall_ids": firewall_ids,
        "placement_group_id": (
            instance.placement_group.id if instance.placement_group else None
        ),
        "maintenance_policy": instance.maintenance_policy,
        "watchdog_enabled": instance.watchdog_enabled,
    }


def build_instance_create_kwargs(attrs: dict, *, network_interface_model: str) -> dict:

    kwargs = {
        "maintenance_policy": attrs.get("maintenance_policy"),
    }
    if attrs.get("placement_group_id") is not None:
        kwargs["placement_group"] = InstancePlacementGroupAssignment(
            id=attrs["placement_group_id"]
        )
    firewall_ids = attrs.get("firewall_ids") or []
    if network_interface_model == INTERFACE_MODEL_LEGACY and firewall_ids:
        kwargs["firewall"] = firewall_ids[0]
    return kwargs


INTERFACE_MODEL_LEGACY = "legacy_config"
INTERFACE_MODEL_LINODE = "linode"
_VALID_INTERFACE_MODELS = (INTERFACE_MODEL_LEGACY, INTERFACE_MODEL_LINODE)


def capture_network_config(instance, *, configs: list | None = None) -> dict:

    model = instance.interface_generation
    if model not in _VALID_INTERFACE_MODELS:
        raise ConfigError(
            f"Instance {instance.id} has unrecognized interface_generation "
            f"{model!r}; expected one of {_VALID_INTERFACE_MODELS}"
        )

    if configs is None:
        configs = list(instance.configs)
    if not configs:
        raise ConfigError(
            f"Instance {instance.id} has no boot config; can't capture "
            "network_helper_enabled from it"
        )
    if len(configs) != 1:


        raise ConfigError(
            f"instance {instance.id} has {len(configs)} boot configs -- can't tell which "
            "one is actually active (Linode's API has no such field) and refuses to "
            "guess. Delete or consolidate the extra config(s) manually first (Cloud "
            "Manager), then retry."
        )
    network_helper_enabled = configs[0].helpers.network

    if model == INTERFACE_MODEL_LEGACY:
        network_config = [iface.dict for iface in configs[0].interfaces]
    else:
        interfaces = instance.linode_interfaces
        if interfaces is None:
            raise ConfigError(
                f"Instance {instance.id} reports interface_generation="
                "'linode' but linode_interfaces is None"
            )
        network_config = [
            {
                "public": iface.public.dict if iface.public else None,
                "vlan": iface.vlan.dict if iface.vlan else None,
                "vpc": iface.vpc.dict if iface.vpc else None,
                "default_route": (
                    iface.default_route.dict if iface.default_route else None
                ),


                "firewall_id": (
                    firewalls[0].id if (firewalls := iface.firewalls()) else None
                ),
            }
            for iface in interfaces
        ]

    return {
        "network_interface_model": model,
        "network_config": network_config,
        "network_helper_enabled": network_helper_enabled,
    }


def build_create_kwargs(captured: dict) -> dict:

    model = captured["network_interface_model"]
    if model not in _VALID_INTERFACE_MODELS:
        raise ConfigError(f"Unrecognized network_interface_model {model!r}")

    network_helper = captured["network_helper_enabled"]

    if model == INTERFACE_MODEL_LEGACY:


        return {
            "instance_create_kwargs": {
                "interface_generation": INTERFACE_MODEL_LEGACY,
                "network_helper": network_helper,
            },
            "config_create_kwargs": {
                "interfaces": captured["network_config"],
            },
        }

    return {
        "instance_create_kwargs": {
            "interface_generation": INTERFACE_MODEL_LINODE,
            "network_helper": network_helper,
            "interfaces": [
                _rebuild_linode_interface(iface)
                for iface in captured["network_config"]
            ],
        },
        "config_create_kwargs": {},
    }


def _rebuild_linode_interface(iface: dict) -> dict:


    result = {"firewall_id": iface.get("firewall_id") if iface.get("firewall_id") is not None else -1}
    if iface.get("default_route"):
        dr = iface["default_route"]
        result["default_route"] = {"ipv4": dr.get("ipv4"), "ipv6": dr.get("ipv6")}
    if iface.get("public"):
        result["public"] = _rebuild_public(iface["public"])
    if iface.get("vlan"):
        vlan = iface["vlan"]
        result["vlan"] = {
            "vlan_label": vlan["vlan_label"],
            "ipam_address": vlan.get("ipam_address"),
        }
    if iface.get("vpc"):
        result["vpc"] = _rebuild_vpc(iface["vpc"])
    return result


def _rebuild_public(public: dict) -> dict:
    result = {}
    ipv4 = public.get("ipv4")
    if ipv4 and ipv4.get("addresses"):
        result["ipv4"] = {
            "addresses": [
                {"address": a["address"], "primary": a.get("primary")}
                for a in ipv4["addresses"]
            ]
        }
    return result


def _rebuild_vpc(vpc: dict) -> dict:
    result = {"subnet_id": vpc["subnet_id"]}
    ipv4 = vpc.get("ipv4")
    if ipv4 and ipv4.get("addresses"):
        result["ipv4"] = {
            "addresses": [
                {
                    "address": a["address"],
                    "primary": a.get("primary"),
                    "nat_1_1_address": a.get("nat_1_1_address"),
                }
                for a in ipv4["addresses"]
            ]
        }
    return result


def _legacy_interface_lines(
    idx: int, iface: dict, *, public_ip, public_gateway, public_prefix, vpc_prefix, dns_servers
) -> list[str]:

    purpose = iface.get("purpose")
    if purpose == "public":
        if not public_ip or not public_gateway:
            raise ConfigError(
                "public_ip and public_gateway are required to build "
                "user_data for a 'public' interface"
            )
        lines = [
            "[Match]", f"Name=eth{idx}", "", "[Network]", "DHCP=no",
            f"Address={public_ip}/{public_prefix}",
            f"Gateway={public_gateway}",
        ]
        if dns_servers:
            lines.append(f"DNS={' '.join(dns_servers)}")
        return lines
    if purpose == "vpc":
        vpc_ip = (iface.get("ipv4") or {}).get("vpc")
        if not vpc_ip:
            raise ConfigError(
                f"VPC interface at index {idx} has no explicit static IP address recorded -- "
                "an explicit address is required (VPC/VLAN interfaces can't rely on DHCP)."
            )
        if not vpc_prefix:
            raise ConfigError(
                "vpc_prefix is required to build user_data for a 'vpc' interface"
            )
        return [
            "[Match]", f"Name=eth{idx}", "", "[Network]", "DHCP=no",
            f"Address={vpc_ip}/{vpc_prefix}",
        ]
    if purpose == "vlan":
        ipam_address = iface.get("ipam_address")
        if not ipam_address:
            raise ConfigError(
                f"VLAN interface at index {idx} has no explicit static IP address recorded -- "
                "an explicit address is required (VPC/VLAN interfaces can't rely on DHCP)."
            )
        return [
            "[Match]", f"Name=eth{idx}", "", "[Network]", "DHCP=no",
            f"Address={ipam_address}",
        ]
    raise ConfigError(f"Unrecognized interface purpose {purpose!r} at index {idx}")


def _linode_interface_address(addresses: list[dict], idx: int, kind: str) -> str:

    if not addresses:
        raise ConfigError(
            f"{kind} interface at index {idx} has no ipv4 addresses recorded -- an explicit "
            "static address is required here, not assigned automatically."
        )
    return next((a["address"] for a in addresses if a.get("primary")), addresses[0]["address"])


def _linode_interface_lines(
    idx: int, iface: dict, *, public_ip, public_gateway, public_prefix, vpc_prefix, dns_servers
) -> list[str]:

    default_route = iface.get("default_route") or {}
    carries_default_route = bool(default_route.get("ipv4"))

    if iface.get("public"):
        addresses = ((iface["public"].get("ipv4") or {}).get("addresses")) or []
        address = _linode_interface_address(addresses, idx, "public")
        lines = [
            "[Match]", f"Name=eth{idx}", "", "[Network]", "DHCP=no",
            f"Address={address}/{public_prefix}",
        ]
        if carries_default_route:
            if not public_gateway:
                raise ConfigError(
                    f"public_gateway is required -- interface at index {idx} carries "
                    "the default route"
                )
            lines.append(f"Gateway={public_gateway}")
        if dns_servers:
            lines.append(f"DNS={' '.join(dns_servers)}")
        return lines

    if iface.get("vpc"):
        if carries_default_route:
            raise ConfigError(
                f"VPC interface at index {idx} carries the default route (VPC 1:1 NAT mode) -- "
                "not supported yet. Only a dedicated public interface carrying the default "
                "route is supported for this network model so far."
            )
        addresses = ((iface["vpc"].get("ipv4") or {}).get("addresses")) or []
        address = _linode_interface_address(addresses, idx, "VPC")
        if not vpc_prefix:
            raise ConfigError("vpc_prefix is required to build user_data for a 'vpc' interface")
        return [
            "[Match]", f"Name=eth{idx}", "", "[Network]", "DHCP=no",
            f"Address={address}/{vpc_prefix}",
        ]

    if iface.get("vlan"):
        ipam_address = iface["vlan"].get("ipam_address")
        if not ipam_address:
            raise ConfigError(
                f"VLAN interface at index {idx} has no explicit static IP address recorded -- "
                "an explicit address is required (VPC/VLAN interfaces can't rely on DHCP)."
            )
        return [
            "[Match]", f"Name=eth{idx}", "", "[Network]", "DHCP=no",
            f"Address={ipam_address}",
        ]

    raise ConfigError(f"Interface at index {idx} has no public/vpc/vlan purpose set")


_INTERFACE_LINE_BUILDERS = {
    INTERFACE_MODEL_LEGACY: _legacy_interface_lines,
    INTERFACE_MODEL_LINODE: _linode_interface_lines,
}


def build_user_data(
    network_config: list[dict],
    network_interface_model: str,
    *,
    public_ip: str | None = None,
    public_gateway: str | None = None,
    public_prefix: int = 24,
    vpc_prefix: int | None = None,
    dns_servers: list[str] | None = None,
    preserve_host_keys: bool = True,
) -> str:

    line_builder = _INTERFACE_LINE_BUILDERS.get(network_interface_model)
    if line_builder is None:
        raise ConfigError(f"Unrecognized network_interface_model {network_interface_model!r}")
    if not network_config:
        raise ConfigError("build_user_data() requires at least one interface")

    file_blocks = []
    for idx, iface in enumerate(network_config):
        lines = line_builder(
            idx, iface,
            public_ip=public_ip, public_gateway=public_gateway, public_prefix=public_prefix,
            vpc_prefix=vpc_prefix, dns_servers=dns_servers,
        )
        indented = "\n".join(f"      {line}" if line else "" for line in lines)
        file_blocks.append(
            f"  - path: /etc/systemd/network/05-eth{idx}.network\n"
            f"    permissions: '0644'\n"
            f"    content: |\n"
            f"{indented}\n"
        )

    header = "#cloud-config\n"
    if preserve_host_keys:
        header += "ssh_deletekeys: false\n"
    return (
        header
        + "write_files:\n"
        + "".join(file_blocks)
        + "runcmd:\n  - systemctl restart systemd-networkd\n"
    )


def reserve_ip(client: LinodeClient, region: str) -> str:

    reserved = client.networking.reserved_ip_create(region)
    return reserved.address


def get_ip_details(client: LinodeClient, address: str) -> dict:

    return client.get(f"/networking/ips/{address}")


def reserve_existing_ip(client: LinodeClient, address: str) -> None:

    try:
        ip = client.load(IPAddress, address)
    except (ApiError, requests.exceptions.RequestException) as e:
        raise ConfigError(f"could not load IP {address}: {e}") from e
    if ip.reserved:
        return
    try:
        ip.reserved = True
        retry_transient(ip.save)
    except (ApiError, requests.exceptions.RequestException) as e:
        raise ConfigError(f"could not reserve {address}: {e}") from e


def check_ssh_port_reachable(host: str, port: int = 22, timeout_s: float = 5.0) -> dict:

    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return {"reachable": True}
    except OSError as e:
        return {"reachable": False, "detail": str(e)}


def get_vpc_subnet_prefix(client: LinodeClient, vpc_id: int, subnet_id: int) -> int:

    from linode_api4 import VPCSubnet

    subnet = client.load(VPCSubnet, subnet_id, vpc_id)
    return int(subnet.ipv4.split("/")[1])


def poll_until_status(
    load_fn,
    target_statuses: tuple[str | None, ...],
    *,
    timeout_s: int = 180,
    interval_s: int = 5,
    status_attr: str = "status",
):

    deadline = time.monotonic() + timeout_s
    last_status = None
    while time.monotonic() < deadline:
        try:
            resource = load_fn()
        except ApiError as e:
            if e.status >= 500:
                time.sleep(interval_s)
                continue
            raise
        except requests.exceptions.RequestException:
            time.sleep(interval_s)
            continue
        last_status = getattr(resource, status_attr)
        if last_status in target_statuses:
            return resource
        time.sleep(interval_s)
    raise TimeoutError(
        f"Timed out after {timeout_s}s waiting for status in {target_statuses}; "
        f"last observed status: {last_status!r}"
    )


def clone_golden_volume(
    client: LinodeClient,
    template_volume,
    label: str,
    *,
    timeout_s: int = 300,
    interval_s: int = 10,
    on_created=None,
):

    if not isinstance(template_volume, Volume):
        template_volume = client.load(Volume, template_volume)
    clone = template_volume.clone(label)
    if on_created is not None:
        on_created(clone.id)
    return poll_until_status(
        lambda: client.load(Volume, clone.id),
        ("active",),
        timeout_s=timeout_s,
        interval_s=interval_s,
    )


def _truncated_config_label(base: str, suffix: str) -> str:

    return f"{base[: 48 - len(suffix)]}{suffix}"


def truncated_random_label(base_label: str, *, max_len: int = 32) -> str:

    suffix = uuid.uuid4().hex[:8]
    return f"{base_label[: max_len - len(suffix) - 1]}-{suffix}"


class AmbiguousCreateError(RuntimeError):
    pass


def create_and_boot_instance(
    client: LinodeClient,
    *,
    region: str,
    label: str,
    golden_volume,
    reserved_ip: str,
    captured_network: dict,
    authorized_keys: list[str],
    root_pass: str,
    authorized_users: list[str] | None = None,
    vpc_prefix: int | None = None,
    dns_servers: list[str] | None = None,
    preserve_host_keys: bool = True,
    data_volume_devices: dict[str, dict] | None = None,
    tags: list[str] | None = None,
    instance_attrs: dict | None = None,
    on_created=None,
):

    ip_info = get_ip_details(client, reserved_ip)
    kwargs = build_create_kwargs(captured_network)
    user_data = build_user_data(
        captured_network["network_config"],
        captured_network["network_interface_model"],
        public_ip=reserved_ip,
        public_gateway=ip_info["gateway"],
        public_prefix=ip_info["prefix"],
        vpc_prefix=vpc_prefix,
        dns_servers=dns_servers,
        preserve_host_keys=preserve_host_keys,
    )
    metadata = client.linode.build_instance_metadata(user_data=user_data)


    if captured_network["network_interface_model"] == INTERFACE_MODEL_LEGACY:


        has_public_interface = any(
            iface.get("purpose") == "public" for iface in captured_network["network_config"]
        )
        if not has_public_interface:
            nat_interface = next(
                (
                    iface for iface in captured_network["network_config"]
                    if iface.get("purpose") == "vpc" and (iface.get("ipv4") or {}).get("nat_1_1")
                ),
                None,
            )
            if nat_interface is not None:
                raise ConfigError(
                    "This instance appears to use VPC 1:1 NAT for its public IP (no dedicated "
                    "public interface) -- this topology isn't supported for automated recreate "
                    "yet. Assigning the reserved IP via the normal path regardless would risk "
                    "creating an unwanted additional public interface alongside the existing "
                    "NAT config. Not supported for now; investigate manually before "
                    "recreating this instance."
                )
        extra_create_kwargs = {"ipv4": [reserved_ip]}
    else:
        extra_create_kwargs = {}
    plan_type = (instance_attrs or {}).get("plan_type", "g6-nanode-1")
    attr_kwargs = build_instance_create_kwargs(
        instance_attrs or {}, network_interface_model=captured_network["network_interface_model"]
    )

    try:
        instance = client.linode.instance_create(
            plan_type,
            region,
            label=label,
            tags=tags,
            authorized_keys=authorized_keys,
            authorized_users=authorized_users,
            root_pass=root_pass,
            booted=False,
            metadata=metadata,
            **extra_create_kwargs,
            **attr_kwargs,
            **kwargs["instance_create_kwargs"],
        )
    except (ApiError, requests.exceptions.RequestException) as e:


        raise AmbiguousCreateError(
            f"instance_create() itself failed ({e}) -- this may or may not have actually "
            f"created a real, billing instance server-side despite the error. Check Cloud "
            f"Manager for a stray Linode labeled {label!r} in region {region!r} before "
            "retrying by hand; do not assume nothing was created."
        ) from e

    try:


        if on_created is not None:
            on_created(instance.id)


        watchdog_enabled = (instance_attrs or {}).get("watchdog_enabled")
        if watchdog_enabled is not None:
            instance.watchdog_enabled = watchdog_enabled
            instance.save()
        devices = {"sda": golden_volume}
        devices.update(data_volume_devices or {})
        config = instance.config_create(
            label=_truncated_config_label(label, "-boot-config"),
            devices=devices,
            root_device="/dev/sda",
            helpers={"network": captured_network["network_helper_enabled"]},
            **kwargs["config_create_kwargs"],
        )


        retry_transient_or_already_done(
            lambda: instance.boot(config=config),
            lambda: client.load(Instance, instance.id).status in ("running", "booting"),
        )
    except Exception as original_error:


        try:
            retry_transient_or_already_done(
                instance.delete, lambda: _resource_confirmed_gone(client, instance)
            )
        except Exception as cleanup_error:

            raise RuntimeError(
                f"Instance {instance.id} was created but a later setup step failed "
                f"({original_error!r}), and the automatic rollback delete ALSO failed "
                f"({cleanup_error!r}). This instance is now an orphaned, billing resource -- "
                f"delete it manually: instance id {instance.id}."
            ) from original_error
        raise
    return instance, config


CREATE_RETRY_BACKOFF_S = (30, 60, 120)


def _is_transient_create_error(exc: ApiError) -> bool:

    if exc.status >= 500:
        return True


    return any(reason and "capacity" in reason.lower() for reason in exc.errors)


def create_and_boot_instance_with_retry(
    client: LinodeClient,
    *,
    backoff_s: tuple[int, ...] = CREATE_RETRY_BACKOFF_S,
    on_retry=None,
    **kwargs,
):

    last_exc = None
    for attempt, delay in enumerate((0, *backoff_s)):
        if delay:
            if on_retry:
                on_retry(attempt, delay, last_exc)
            time.sleep(delay)
        try:
            return create_and_boot_instance(client, **kwargs)
        except ApiError as e:
            last_exc = e
            if not _is_transient_create_error(e):
                raise
        except requests.exceptions.RequestException as e:


            last_exc = e
    raise RuntimeError(
        f"create_and_boot_instance failed after {len(backoff_s) + 1} attempts "
        f"(all retries exhausted, last error was transient): {last_exc}"
    ) from last_exc


class TransitioningError(RuntimeError):
    pass


def acquire_transition_lock(state: dict, persist_fn) -> dict:

    if state.get("transitioning"):
        raise TransitioningError(
            f"Another operation is already in progress (phase="
            f"{state.get('phase')!r}); refusing to start a new one until it "
            "completes or the lock is cleared."
        )
    state["transitioning"] = True
    persist_fn(state)
    return state


def release_transition_lock(state: dict, persist_fn) -> dict:

    state["transitioning"] = False
    persist_fn(state)
    return state


@contextmanager
def transitioning(state: dict, persist_fn):

    state = acquire_transition_lock(state, persist_fn=persist_fn)
    try:
        yield state
    finally:
        release_transition_lock(state, persist_fn=persist_fn)


def delete_instance_and_detach_volumes(client: LinodeClient, instance, volumes: list) -> None:


    if instance.status != "offline":
        retry_transient_or_already_done(
            instance.shutdown,
            lambda: client.load(type(instance), instance.id).status == "offline",
        )
        poll_until_status(lambda: client.load(type(instance), instance.id), ("offline",))

    for volume in volumes:


        fresh_volume = retry_transient(lambda v=volume: client.load(type(v), v.id))
        if fresh_volume.linode_id is None:
            continue
        retry_transient_or_already_done(
            volume.detach,
            lambda v=volume: client.load(type(v), v.id).linode_id is None,
        )
        poll_until_status(
            lambda v=volume: client.load(type(v), v.id),
            (None,),
            status_attr="linode_id",
        )


    retry_transient_or_already_done(
        instance.delete, lambda: _resource_confirmed_gone(client, instance)
    )


    deadline = time.monotonic() + 60
    while True:
        try:
            client.load(type(instance), instance.id)
        except ApiError as e:
            if e.status == 404:
                return
            if e.status >= 500 and time.monotonic() < deadline:
                time.sleep(5)
                continue
            raise
        except requests.exceptions.RequestException:


            if time.monotonic() < deadline:
                time.sleep(5)
                continue
            raise
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Instance {instance.id} still loadable 60s after delete()"
            )
        time.sleep(5)


@contextmanager
def known_hosts_file_lock(known_hosts_path: str | Path | None = None):

    if known_hosts_path is None:
        known_hosts_path = BASE_DIR / "state" / "known_hosts"
    known_hosts_path = Path(known_hosts_path)
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = known_hosts_path.parent / f".{known_hosts_path.name}.lock"
    with open(lock_path, "w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def ssh_run(
    host: str,
    ssh_key_path: str | None,
    command: str,
    *,
    timeout_s: int = 10,
    retries: int = 8,
    retry_delay_s: int = 10,
    trust_new: bool = False,
    known_hosts_path: str | Path | None = None,
    password: str | None = None,
    _skip_lock: bool = False,
) -> str:

    if known_hosts_path is None:
        known_hosts_path = BASE_DIR / "state" / "known_hosts"
    known_hosts_path = Path(known_hosts_path)
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    strict_mode = "accept-new" if trust_new else "yes"

    if password is None and ssh_key_path is None:
        raise ConfigError("ssh_run() needs either ssh_key_path or password.")

    def _run_once():
        if password is not None:
            return _ssh_exec_with_password(
                host, password, command, timeout_s=timeout_s,
                known_hosts_path=known_hosts_path, trust_new=trust_new,
            )
        assert ssh_key_path is not None
        return subprocess.run(
            [
                "ssh",
                "-o", f"ConnectTimeout={timeout_s}",
                "-o", "BatchMode=yes",
                "-o", f"StrictHostKeyChecking={strict_mode}",
                "-o", f"UserKnownHostsFile={known_hosts_path}",


                "-o", "HashKnownHosts=no",
                "-i", ssh_key_path,
                f"root@{host}",
                command,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    last_error = None
    for attempt in range(retries):


        if trust_new and not _skip_lock:
            with known_hosts_file_lock(known_hosts_path):
                result = _run_once()
        else:
            result = _run_once()
        if result.returncode == 0:
            return result.stdout
        last_error = result.stderr
        if attempt < retries - 1:
            time.sleep(retry_delay_s)
    raise RuntimeError(
        f"SSH to {host} failed after {retries} attempts: {last_error}"
    )


def _ssh_exec_with_password(
    host: str, password: str, command: str, *,
    timeout_s: int, known_hosts_path: Path, trust_new: bool,
) -> types.SimpleNamespace:

    import paramiko


    client = paramiko.SSHClient()
    if known_hosts_path.exists():
        client.load_host_keys(str(known_hosts_path))
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy() if trust_new else paramiko.RejectPolicy())
    try:
        client.connect(
            host, username="root", password=password, timeout=timeout_s,
            allow_agent=False, look_for_keys=False,
        )
        if trust_new:
            client.save_host_keys(str(known_hosts_path))
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout_s)
        exit_status = stdout.channel.recv_exit_status()
        return types.SimpleNamespace(
            returncode=exit_status,
            stdout=stdout.read().decode("utf-8", errors="replace"),
            stderr=stderr.read().decode("utf-8", errors="replace"),
        )
    except paramiko.AuthenticationException as e:
        return types.SimpleNamespace(returncode=1, stdout="", stderr=f"Authentication failed: {e}")
    except (paramiko.SSHException, OSError) as e:
        return types.SimpleNamespace(returncode=1, stdout="", stderr=str(e))
    finally:
        client.close()


def _atomic_write_text(path: Path, content: str, *, tmp_prefix: str) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=tmp_prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


def _reset_known_host_unlocked(host: str, known_hosts_path: Path) -> None:

    if not known_hosts_path.exists():
        return
    result = subprocess.run(
        ["ssh-keygen", "-R", host, "-f", str(known_hosts_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ssh-keygen -R {host} failed (exit {result.returncode}): {result.stderr.strip()}"
        )


def reset_known_host(host: str, known_hosts_path: str | Path | None = None) -> None:

    if known_hosts_path is None:
        known_hosts_path = BASE_DIR / "state" / "known_hosts"
    known_hosts_path = Path(known_hosts_path)
    with known_hosts_file_lock(known_hosts_path):
        _reset_known_host_unlocked(host, known_hosts_path)


def _read_known_host_entry_unlocked(host: str, known_hosts_path: Path) -> str | None:

    if not known_hosts_path.exists():
        return None
    lines = [
        line for line in known_hosts_path.read_text().splitlines()
        if line.split(" ", 1)[0] == host
    ]
    return "".join(line + "\n" for line in lines) if lines else None


def read_known_host_entry(host: str, known_hosts_path: str | Path | None = None) -> str | None:

    if known_hosts_path is None:
        known_hosts_path = BASE_DIR / "state" / "known_hosts"
    known_hosts_path = Path(known_hosts_path)
    with known_hosts_file_lock(known_hosts_path):
        return _read_known_host_entry_unlocked(host, known_hosts_path)


def _restore_known_host_entry_unlocked(host: str, entry: str, known_hosts_path: Path) -> None:

    existing_lines = (
        known_hosts_path.read_text().splitlines(keepends=True) if known_hosts_path.exists() else []
    )
    kept = [line for line in existing_lines if line.split(" ", 1)[0] != host]
    new_content = "".join(kept) + entry
    _atomic_write_text(known_hosts_path, new_content, tmp_prefix="known_hosts-restore-")


def restore_known_host_entry(
    host: str, entry: str, known_hosts_path: str | Path | None = None
) -> None:

    if known_hosts_path is None:
        known_hosts_path = BASE_DIR / "state" / "known_hosts"
    known_hosts_path = Path(known_hosts_path)
    with known_hosts_file_lock(known_hosts_path):
        _restore_known_host_entry_unlocked(host, entry, known_hosts_path)


def reset_and_reestablish_trust(
    host: str, ssh_key_path: str, *, known_hosts_path: str | Path | None = None,
    timeout_s: int = 10, retries: int = 8, retry_delay_s: int = 10,
) -> None:

    if known_hosts_path is None:
        known_hosts_path = BASE_DIR / "state" / "known_hosts"
    known_hosts_path = Path(known_hosts_path)
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    with known_hosts_file_lock(known_hosts_path):
        backup = _read_known_host_entry_unlocked(host, known_hosts_path)
        try:
            _reset_known_host_unlocked(host, known_hosts_path)
            ssh_run(
                host, ssh_key_path, "echo ok", trust_new=True,
                known_hosts_path=known_hosts_path, timeout_s=timeout_s,
                retries=retries, retry_delay_s=retry_delay_s, _skip_lock=True,
            )
        except BaseException:
            if backup is not None:
                _restore_known_host_entry_unlocked(host, backup, known_hosts_path)
            else:


                _reset_known_host_unlocked(host, known_hosts_path)
            raise


def write_marker(host: str, ssh_key_path: str) -> str:

    marker = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}-{uuid.uuid4()}"
    ssh_run(host, ssh_key_path, f"echo {marker} > /root/spike_marker.txt")
    return marker


def read_marker(host: str, ssh_key_path: str) -> str:

    return ssh_run(host, ssh_key_path, "cat /root/spike_marker.txt").strip()


def capture_data_volumes(
    instance, os_volume_id: int, *, configs: list | None = None,
) -> list[dict]:

    if configs is None:
        configs = list(instance.configs)
    if not configs:
        raise ConfigError(
            f"Instance {instance.id} has no boot config; can't capture data volumes"
        )
    if len(configs) != 1:


        raise ConfigError(
            f"instance {instance.id} has {len(configs)} boot configs -- can't tell which "
            "one is actually active (Linode's API has no such field) and refuses to "
            "guess, since capturing data volumes from the wrong one would silently miss "
            "or misattribute them. Delete or consolidate the extra config(s) manually "
            "first (Cloud Manager), then retry."
        )
    devices = configs[0].devices.dict

    slots = []
    for slot in sorted(devices.keys()):
        device = devices[slot]
        if not device:
            continue
        volume_id = device.get("id")
        if volume_id is None or volume_id == os_volume_id:
            continue
        filesystem_path = device.get("filesystem_path")
        if not filesystem_path:


            continue
        slots.append((slot, volume_id, filesystem_path))

    return [
        {"volume_id": volume_id, "device_slot": slot, "fstab_identifier": fstab_identifier}
        for slot, volume_id, fstab_identifier in slots
    ]


DATA_VOLUME_DEVICE_SLOTS = [f"sd{c}" for c in "bcdefgh"]


def build_data_volume_devices(data_volumes: list[dict]) -> dict[str, dict]:

    return {dv["device_slot"]: {"volume_id": dv["volume_id"]} for dv in data_volumes}


_NETWORK_INSPECTION_COMMAND = (
    "echo '=== systemd-network ==='; ls -1 /etc/systemd/network/ 2>/dev/null; "
    "echo '=== netplan ==='; "
    "for f in /etc/netplan/*.yaml; do "
    "if [ -f \"$f\" ]; then echo \"--- $f ---\"; grep -v '^#' \"$f\" | grep -v '^[[:space:]]*$'; fi; "
    "done; "
    "echo '=== networkmanager ==='; ls -1 /etc/NetworkManager/system-connections/ 2>/dev/null; "
    "true"


)

_NETWORK_HELPER_FILENAME = re.compile(r"05-eth\d+\.network")


def _parse_network_inspection(output: str) -> list[dict]:

    section = None
    systemd_network_files: list[str] = []
    netplan_files_with_content: dict[str, bool] = {}
    current_netplan_file = None
    networkmanager_files: list[str] = []

    for line in output.splitlines():
        stripped = line.strip()
        if stripped == "=== systemd-network ===":
            section = "systemd-network"
            continue
        if stripped == "=== netplan ===":
            section = "netplan"
            continue
        if stripped == "=== networkmanager ===":
            section = "networkmanager"
            continue
        if section == "netplan" and stripped.startswith("--- ") and stripped.endswith(" ---"):
            current_netplan_file = stripped[4:-4]
            netplan_files_with_content.setdefault(current_netplan_file, False)
            continue
        if not stripped:
            continue
        if section == "systemd-network":
            systemd_network_files.append(stripped)
        elif section == "netplan" and current_netplan_file is not None:
            netplan_files_with_content[current_netplan_file] = True
        elif section == "networkmanager":
            networkmanager_files.append(stripped)

    suspects = []
    for filename in systemd_network_files:


        if not filename.endswith(".network"):
            continue
        if not _NETWORK_HELPER_FILENAME.fullmatch(filename):
            suspects.append({
                "path": f"/etc/systemd/network/{filename}",
                "reason": "not Network-Helper-generated (expected only 05-eth<N>.network)",
            })
    for path, has_content in netplan_files_with_content.items():
        if has_content:
            suspects.append({"path": path, "reason": "contains active network configuration"})
    for filename in networkmanager_files:
        suspects.append({
            "path": f"/etc/NetworkManager/system-connections/{filename}",
            "reason": "NetworkManager connection profile present",
        })
    return suspects


def check_hand_configured_networking(
    host: str, ssh_key_path: str | None, *, trust_new: bool = False, password: str | None = None,
) -> list[dict]:

    output = ssh_run(
        host, ssh_key_path, _NETWORK_INSPECTION_COMMAND, trust_new=trust_new, password=password,
    )
    return _parse_network_inspection(output)


MIN_CLOUD_INIT_VERSION = (23, 3, 1)


CLOUD_INIT_UPGRADE_INSTRUCTIONS = (
    "most distro package repos are too old for this. Either upgrade the OS itself to a release "
    "whose default repos already ship cloud-init >= 23.3.1, or build cloud-init from source, "
    "following its own current instructions for your distro (its build system has changed "
    "before and will again, so don't trust a cached/copied guide): "
    "https://docs.cloud-init.io/en/latest/development/index.html"
)


def _parse_cloud_init_version(raw: str) -> tuple[int, int, int]:

    try:
        version_token = raw.strip().split()[-1]
        core = version_token.split("-")[0]
        parts = core.split(".")
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
        return (major, minor, patch)
    except (IndexError, ValueError) as e:
        raise ConfigError(f"Could not parse a cloud-init version from {raw!r}: {e}") from e


def check_path_b_preflight(
    host: str, ssh_key_path: str | None, *, trust_new: bool = True, password: str | None = None,
) -> dict:

    raw_version = ssh_run(
        host, ssh_key_path, "cloud-init --version", trust_new=trust_new, password=password,
    ).strip()
    cloud_init_version = _parse_cloud_init_version(raw_version)
    datasource = ssh_run(
        host, ssh_key_path, "cloud-init query cloud_name 2>/dev/null",
        trust_new=trust_new, password=password,
    ).strip()
    return {
        "cloud_init_version": raw_version,
        "cloud_init_ok": cloud_init_version >= MIN_CLOUD_INIT_VERSION,
        "datasource": datasource,
        "datasource_ok": datasource == "akamai",
        "hand_configured_networking": check_hand_configured_networking(
            host, ssh_key_path, trust_new=trust_new, password=password,
        ),
    }


MAX_VOLUME_SIZE_GB = 16384


def compute_migration_volume_size(disk_size_mb: int) -> int:

    disk_gb = math.ceil(disk_size_mb / 1024)
    size = max(10, disk_gb + 5)
    if size > MAX_VOLUME_SIZE_GB:
        raise ConfigError(
            f"Source disk is {disk_gb}GB -- the required destination volume "
            f"({size}GB) would exceed Linode's {MAX_VOLUME_SIZE_GB}GB per-volume maximum. "
            "Path B migration for a disk this large is not supported by this tool "
            "(would need a multi-volume approach, not implemented)."
        )
    return size


def start_path_b_migration(
    client: LinodeClient, instance, *, name: str, region: str | None = None, persist_fn=None
) -> dict:

    if instance.status != "running":
        raise ConfigError(
            f"Instance {instance.id} is not running (status={instance.status!r}) -- "
            "must be running to read its current disk layout and run pre-flight checks."
        )
    configs = list(instance.configs)
    if not configs:
        raise ConfigError(f"Instance {instance.id} has no boot config.")
    if len(configs) != 1:


        raise ConfigError(
            f"Instance {instance.id} has {len(configs)} boot configs -- can't tell which "
            "one is the real one to migrate from (Linode's API has no such field) and "
            "refuses to guess. Delete or consolidate the extra config(s) manually first "
            "(Cloud Manager), then retry."
        )
    devices = configs[0].devices.dict
    sda = devices.get("sda")
    if not sda:
        raise ConfigError(f"Instance {instance.id} has no device at /dev/sda.")


    if sda.get("filesystem_path"):
        raise ConfigError(
            f"Instance {instance.id}'s /dev/sda is already a Block Storage volume -- "
            "nothing to migrate (Path B is for local-disk instances only)."
        )
    disk_id = sda.get("id")
    if disk_id is None:
        raise ConfigError(f"Instance {instance.id}'s /dev/sda has no disk id.")

    disk = next((d for d in instance.disks if d.id == disk_id), None)
    if disk is None:
        raise ConfigError(f"Could not find disk {disk_id} on instance {instance.id}.")

    region = region or instance.region.id
    dest_size = compute_migration_volume_size(disk.size)


    label = truncated_random_label(f"{instance.label}-os-vol")


    dest_volume = client.volume_create(
        label=label,
        region=region,
        size=dest_size,
        tags=[f"{REGISTRY_NAME_TAG_PREFIX}:{name}", REGISTRY_ROLE_TAG_ACTIVE_MIGRATION],
    )
    state = {
        "instance_id": instance.id,
        "region": region,
        "local_disk_id": disk_id,
        "local_disk_size_mb": disk.size,
        "dest_volume_id": dest_volume.id,
        "dest_volume_size_gb": dest_size,
        "phase": "volume_created",
    }


    if persist_fn is not None:
        persist_fn(dict(state))
    try:
        tag_active_migration_volume(client, dest_volume.id, name)
        state["remote_tagged"] = True
    except (ApiError, ConfigError, requests.exceptions.RequestException):


        state["remote_tagged"] = False


    if persist_fn is not None:
        persist_fn(dict(state))

    dest_volume = poll_until_status(
        lambda: client.load(Volume, dest_volume.id), ("active",), timeout_s=180
    )


    state["phase"] = "requesting_rescue"
    if persist_fn is not None:
        persist_fn(dict(state))

    client.post(
        f"/linode/instances/{instance.id}/rescue",
        data={
            "devices": {
                "sda": {"disk_id": disk_id},
                "sdb": {"volume_id": dest_volume.id},
            }
        },
    )

    state["phase"] = "awaiting_manual_dd"
    if persist_fn is not None:
        persist_fn(dict(state))
    return state


def _root_device_matches_volume_command(filesystem_path: str) -> str:

    target = shlex.quote(filesystem_path)
    return (
        f"target=$(readlink -f {target}); "
        "node=$(findmnt -no SOURCE /); root=$node; hops=0; result=UNKNOWN; "
        "while [ $hops -lt 6 ]; do "
        '  parent=$(lsblk -no pkname "$node" 2>/dev/null); '
        "  parent_count=$(printf '%s\\n' \"$parent\" | grep -c .); "
        '  if [ "$parent_count" -gt 1 ]; then result=MULTI_PARENT; break; fi; '
        '  if [ -z "$parent" ]; then result=RESOLVED; break; fi; '
        '  node="/dev/$parent"; hops=$((hops+1)); '
        "done; "
        'if [ "$result" = "MULTI_PARENT" ]; then '
        '  echo "MIGRATED_VOLUME_UNRESOLVABLE root=$root -- device $node has multiple parent '
        'block devices (unsupported topology, e.g. multi-disk LVM/mdraid), investigate '
        'manually"; '
        'elif [ "$result" != "RESOLVED" ]; then '
        '  echo "MIGRATED_VOLUME_UNRESOLVABLE root=$root -- ancestry chain did not terminate '
        'within $hops hops, investigate manually"; '
        'elif [ "$node" = "$target" ]; then echo MIGRATED_VOLUME_CONFIRMED; '
        'else echo "MIGRATED_VOLUME_MISMATCH root=$root target=$target resolved=$node"; fi'
    )


_FSTAB_DEVICE_REF_RE = re.compile(r"^(UUID|PARTUUID|LABEL)=(.+)$")

_FSTAB_OCTAL_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
_UDEV_HEX_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})")


def _unescape_fstab_field(value: str) -> str:

    return _FSTAB_OCTAL_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 8)), value)


def _unescape_udev_symlink_name(value: str) -> str:

    return _UDEV_HEX_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), value)


def disable_stale_fstab_entries(
    fstab_text: str, existing_uuids: set[str], existing_partuuids: set[str],
    existing_labels: set[str],
) -> tuple[str, list[str]]:


    existing_by_kind = {
        "UUID": {_unescape_udev_symlink_name(v) for v in existing_uuids},
        "PARTUUID": {_unescape_udev_symlink_name(v) for v in existing_partuuids},
        "LABEL": {_unescape_udev_symlink_name(v) for v in existing_labels},
    }
    disabled = []
    out_lines = []
    for line in fstab_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out_lines.append(line)
            continue
        first_field = stripped.split(maxsplit=1)[0]
        m = _FSTAB_DEVICE_REF_RE.match(first_field)
        if not m or _unescape_fstab_field(m.group(2)) in existing_by_kind[m.group(1)]:
            out_lines.append(line)
            continue
        out_lines.append(f"# {line}  # disabled: device not present (linode-instance-scheduler)")
        disabled.append(line)
    new_text = "\n".join(out_lines)
    if fstab_text.endswith("\n"):
        new_text += "\n"
    return new_text, disabled


def _parse_device_symlink_listing(output: str) -> tuple[set[str], set[str], set[str]]:

    uuids: set[str] = set()
    partuuids: set[str] = set()
    labels: set[str] = set()
    current = uuids
    for line in output.splitlines():
        line = line.strip()
        if line == "---partuuid---":
            current = partuuids
        elif line == "---label---":
            current = labels
        elif line:
            current.add(line)
    return uuids, partuuids, labels


def harden_fstab_against_stale_devices(
    host: str, ssh_key_path: str | None, *, trust_new: bool = False,
) -> list[str]:

    fstab_text = ssh_run(host, ssh_key_path, "cat /etc/fstab", trust_new=trust_new)
    listing = ssh_run(
        host, ssh_key_path,
        "ls -1 /dev/disk/by-uuid/ 2>/dev/null; echo '---partuuid---'; "
        "ls -1 /dev/disk/by-partuuid/ 2>/dev/null; echo '---label---'; "
        "ls -1 /dev/disk/by-label/ 2>/dev/null",
    )
    uuids, partuuids, labels = _parse_device_symlink_listing(listing)
    new_text, disabled = disable_stale_fstab_entries(fstab_text, uuids, partuuids, labels)
    if not disabled:
        return []
    backup_suffix = int(time.time())


    body = new_text if new_text.endswith("\n") else new_text + "\n"
    backup_path = f"/etc/fstab.bak.{backup_suffix}"


    ssh_run(
        host, ssh_key_path,
        f"[ -f {shlex.quote(backup_path)} ] || cp /etc/fstab {shlex.quote(backup_path)} && "
        f"cat > /etc/fstab << 'CLAUDE_FSTAB_EOF'\n"
        f"{body}CLAUDE_FSTAB_EOF",
    )
    return disabled


def resume_path_b_migration(
    client: LinodeClient, migration_state: dict, *, ssh_key_path: str, persist_fn=None,
    on_progress: Callable[[str], None] | None = None,
) -> dict:

    instance = client.load(Instance, migration_state["instance_id"])
    dest_volume = client.load(Volume, migration_state["dest_volume_id"])
    state = dict(migration_state)
    phase = state.get("resume_phase")

    def _checkpoint(new_phase: str, **extra) -> None:
        state["resume_phase"] = new_phase
        state.update(extra)
        if persist_fn is not None:
            persist_fn(dict(state))

    if phase is None:
        if on_progress is not None:
            on_progress("Creating the boot configuration for the migrated volume...")
        expected_label = _truncated_config_label(instance.label, "-migrated-boot")
        existing_configs = list(instance.configs)


        reused = next(
            (
                c for c in existing_configs
                if c.label == expected_label
                and c.devices.dict.get("sda", {}).get("id") == dest_volume.id
            ),
            None,
        )
        other_configs = [c for c in existing_configs if c is not reused]
        if len(other_configs) != 1:


            raise ConfigError(
                f"instance {instance.id} has {len(other_configs)} boot config(s) other than "
                "this migration's own (if any) -- can't tell which one is the real "
                "pre-migration boot config (Linode's API has no such field) and refuses to "
                "guess, since the stale-cleanup step later deletes every config it didn't "
                "pick. Delete or consolidate the extra config(s) manually first (Cloud "
                "Manager), then retry."
            )
        old_config = other_configs[0]
        original_devices = old_config.devices.dict
        network_helper = old_config.helpers.network


        extra_devices = {}


        for slot, dev in original_devices.items():
            if slot == "sda" or not dev or not dev.get("filesystem_path"):
                continue
            extra_devices[slot] = {"volume_id": dev["id"]}


        if reused is not None:
            new_config = reused


            reused_devices = reused.devices.dict
            expected_ids = {"sda": dest_volume.id, **{
                slot: dev["volume_id"] for slot, dev in extra_devices.items()
            }}
            reused_ids = {
                slot: dev["id"] for slot, dev in reused_devices.items() if dev
            }
            if reused_ids != expected_ids and on_progress is not None:
                on_progress(
                    "WARNING: reusing an already-created boot config from an earlier "
                    f"attempt (id {reused.id}), but its device map ({reused_ids!r}) no "
                    f"longer matches what this instance's data volumes currently look like "
                    f"({expected_ids!r}) -- likely changed via Cloud Manager during this "
                    "migration's own Rescue Mode window. Not reconciled automatically; "
                    "verify the boot config's devices in Cloud Manager once this migration "
                    "completes."
                )
        else:
            new_config = instance.config_create(
                label=expected_label,
                devices={"sda": dest_volume, **extra_devices},
                root_device="/dev/sda",
                interfaces=[{"purpose": "public", "primary": False}],
                helpers={"network": network_helper},
            )
        _checkpoint("config_created", new_config_id=new_config.id)
        phase = "config_created"
    else:
        new_config = next((c for c in instance.configs if c.id == state["new_config_id"]), None)
        if new_config is None:
            raise ConfigError(
                f"Migration for instance {instance.id} recorded config "
                f"{state['new_config_id']!r} as already created, but it's no longer on the "
                "instance -- can't safely resume; investigate and clean up manually."
            )

    if phase == "config_created":
        if on_progress is not None:
            on_progress("Shutting down to exit Rescue Mode...")
        instance = client.load(Instance, instance.id)
        if instance.status not in ("offline", "shutting_down"):


            retry_transient_or_already_done(
                instance.shutdown,
                lambda: client.load(type(instance), instance.id).status
                in ("offline", "shutting_down"),
            )
        _checkpoint("shutdown_requested")
        phase = "shutdown_requested"

    if phase == "shutdown_requested":
        if on_progress is not None:
            on_progress("Booting from the migrated volume...")
        instance = client.load(Instance, instance.id)
        if instance.status not in ("running", "booting"):


            instance = poll_until_status(
                lambda: client.load(Instance, instance.id),
                ("offline", "running", "booting"), timeout_s=120,
            )
            if instance.status == "offline":


                retry_transient_or_already_done(
                    lambda: instance.boot(config=new_config),
                    lambda: client.load(type(instance), instance.id).status
                    in ("running", "booting"),
                )
        _checkpoint("boot_requested")
        phase = "boot_requested"

    if phase == "boot_requested":
        if on_progress is not None:
            on_progress(
                "Waiting for the instance to come up, then verifying the migrated volume is "
                "genuinely serving as root over SSH (this can take a few minutes)..."
            )
        instance = poll_until_status(
            lambda: client.load(Instance, instance.id), ("running",), timeout_s=180
        )
        if not instance.ipv4:


            raise RuntimeError(
                f"instance {instance.id} has no public IPv4 address (it may have been "
                "removed/reassigned out-of-band since migrate-start ran) -- can't determine "
                "the reserved IP to finish this migration."
            )
        reserved_ip = instance.ipv4[0]


        check_cmd = _root_device_matches_volume_command(dest_volume.filesystem_path)
        output = ssh_run(reserved_ip, ssh_key_path, check_cmd, retries=12, retry_delay_s=10)
        if "MIGRATED_VOLUME_CONFIRMED" not in output:


            raise ConfigError(
                f"Migration for instance {instance.id}: SSH succeeded at {reserved_ip}, but "
                f"the guest's root filesystem is not on the migrated destination volume "
                f"(volume {dest_volume.id}, {dest_volume.filesystem_path}) -- {output.strip()!r}. "
                "This can happen if the instance was already 'running'/'booting' when this "
                "resume started and therefore never issued its own shutdown/boot (see "
                "resume_path_b_migration()'s docstring), e.g. an out-of-band boot of the "
                "original config. Refusing to report migration success. Investigate directly "
                f"(Cloud Manager -> Configs), boot config {new_config.id} manually if that's "
                "confirmed correct, then re-run migrate-resume."
            )


        try:
            disabled = harden_fstab_against_stale_devices(reserved_ip, ssh_key_path)
        except (ApiError, RuntimeError, requests.exceptions.RequestException):
            disabled = []
        _checkpoint("ssh_verified", reserved_ip=reserved_ip, fstab_entries_disabled=disabled)
        phase = "ssh_verified"

    if phase == "ssh_verified":
        if on_progress is not None:
            on_progress("Verified. Cleaning up the old, pre-migration boot configuration...")


        instance = client.load(Instance, instance.id)
        stale_configs = [c for c in instance.configs if c.id != new_config.id]
        if len(stale_configs) != 1:


            raise ConfigError(
                f"instance {instance.id} has {len(stale_configs)} boot config(s) other than "
                "the migrated one -- can't safely tell which is the real pre-migration config "
                "to clean up (Linode's API has no 'which config is active' field, and a new "
                "config may have appeared since migrate-start's own preflight check). "
                "Refusing to guess, since deleting the wrong one would destroy a config (and "
                "any local disk only it references) this migration never intended to touch. "
                "Delete or consolidate the extra config(s) manually first (Cloud Manager), "
                "then retry migrate-resume."
            )
        stale_disk_ids = {
            dev["id"]
            for c in stale_configs
            for dev in c.devices.dict.values()
            if dev and not dev.get("filesystem_path")
        }
        _checkpoint(
            "stale_cleanup_planned",
            stale_config_ids=[c.id for c in stale_configs],
            stale_disk_ids=sorted(stale_disk_ids),
        )
        phase = "stale_cleanup_planned"

    if phase == "stale_cleanup_planned":
        if on_progress is not None:
            on_progress("Deleting the old, pre-migration boot configuration and disk...")


        stale_config_ids = set(state.get("stale_config_ids", []))
        stale_disk_ids = set(state.get("stale_disk_ids", []))
        instance = client.load(Instance, instance.id)


        def _is_busy_400(e: ApiError) -> bool:
            return e.status == 400 and "busy" in str(e).lower()

        def _delete_with_busy_retry(resource, attempts=8, delay_s=5):
            retry_transient_or_already_done(
                resource.delete,
                lambda: _resource_confirmed_gone(client, resource),
                attempts=attempts, delay_s=delay_s, is_transient=_is_busy_400,
            )

        for c in instance.configs:
            if c.id in stale_config_ids:
                _delete_with_busy_retry(c)
        if stale_disk_ids:
            for disk in instance.disks:
                if disk.id in stale_disk_ids:
                    _delete_with_busy_retry(disk)
        _checkpoint("stale_config_cleaned_up")
        phase = "stale_config_cleaned_up"

    if phase == "stale_config_cleaned_up":
        if on_progress is not None:
            on_progress("Reserving the instance's public IP address...")
        ip = client.load(IPAddress, state["reserved_ip"])
        if not ip.reserved:
            ip.reserved = True
            ip.save()
        _checkpoint("ip_reserved")

    return {
        "instance_id": instance.id,
        "reserved_ip": state["reserved_ip"],
        "new_config_id": state["new_config_id"],
        "os_volume_id": dest_volume.id,
        "fstab_entries_disabled": state.get("fstab_entries_disabled", []),
    }


REGISTRY_NAME_TAG_PREFIX = "linode-scheduler-name"
REGISTRY_ROLE_TAG_OS = "linode-scheduler-role:os"
REGISTRY_ROLE_TAG_DATA = "linode-scheduler-role:data"
REGISTRY_ROLE_TAG_IP = "linode-scheduler-role:ip"


REGISTRY_ROLE_TAG_ORPHANED_MIGRATION = "linode-scheduler-role:orphaned-migration-attempt"


REGISTRY_ROLE_TAG_ACTIVE_MIGRATION = "linode-scheduler-role:active-migration"


def names_from_tags(tags) -> list[str]:

    prefix = f"{REGISTRY_NAME_TAG_PREFIX}:"
    return [t[len(prefix):] for t in (tags or []) if t.startswith(prefix)]


def _scan_tagged_resources(client: LinodeClient) -> tuple[dict[str, dict], list[dict]]:

    by_name: dict[str, dict] = {}
    conflicts: list[dict] = []

    def _entry(name: str) -> dict:
        return by_name.setdefault(
            name, {"os_volume_ids": [], "data_volume_ids": [], "reserved_ips": [], "regions": set()}
        )


    for vol in retry_transient(lambda: list(client.volumes())):
        names = names_from_tags(vol.tags)
        if not names:
            continue
        vol_tags = vol.tags or []
        is_os = REGISTRY_ROLE_TAG_OS in vol_tags
        is_data = REGISTRY_ROLE_TAG_DATA in vol_tags
        if len(names) > 1:
            conflicts.append({"kind": "volume", "id": vol.id, "reason": "multiple_names", "names": names})
            continue
        if is_os and is_data:
            conflicts.append({"kind": "volume", "id": vol.id, "reason": "multiple_roles", "names": names})
            continue
        if not (is_os or is_data):


            continue
        entry = _entry(names[0])
        entry["regions"].add(vol.region.id)
        if is_os:
            entry["os_volume_ids"].append(vol.id)
        elif is_data:
            entry["data_volume_ids"].append(vol.id)

    for ip in retry_transient(lambda: list(client.networking.reserved_ips())):
        names = names_from_tags(ip.tags)
        if not names:
            continue
        if len(names) > 1:
            conflicts.append(
                {"kind": "reserved_ip", "id": ip.address, "reason": "multiple_names", "names": names}
            )
            continue
        if REGISTRY_ROLE_TAG_IP not in (ip.tags or []):
            continue
        entry = _entry(names[0])
        entry["regions"].add(ip.region.id)
        entry["reserved_ips"].append(ip.address)

    return by_name, conflicts


def tag_managed_resources(
    client: LinodeClient,
    name: str,
    *,
    os_volume_id: int,
    data_volume_ids: list[int],
    reserved_ip: str,
) -> None:

    name_tag = f"{REGISTRY_NAME_TAG_PREFIX}:{name}"

    by_name, _conflicts = _scan_tagged_resources(client)
    current = by_name.get(name, {})

    def _check_not_owned_by_another_name(resource) -> None:
        other_names = [n for n in names_from_tags(resource.tags) if n != name]
        if other_names:
            resource_id = getattr(resource, "id", None) or getattr(resource, "address", "?")
            raise ResourceOwnershipConflict(
                f"Resource {resource_id} is already tagged for {other_names!r}, not {name!r} -- "
                "refusing to add a second name's ownership tag on top (this would make the "
                "resource ambiguously owned by both). Offboard the conflicting name first "
                f"(`offboard --name {other_names[0]}`) if this resource should belong to "
                f"{name!r} instead."
            )


    os_volume = retry_transient(lambda: client.load(Volume, os_volume_id))
    _check_not_owned_by_another_name(os_volume)


    data_volumes = [
        (vol_id, retry_transient(lambda vol_id=vol_id: client.load(Volume, vol_id)))
        for vol_id in data_volume_ids
    ]
    for _vol_id, vol in data_volumes:
        _check_not_owned_by_another_name(vol)
    ip = retry_transient(lambda: client.load(ReservedIPAddress, reserved_ip))
    _check_not_owned_by_another_name(ip)


    def _tag_and_verify(resource, resource_cls, resource_id, *tags, remove: tuple = ()) -> None:

        current = list(resource.tags or [])
        desired = [t for t in current if t not in remove] + [t for t in tags if t not in current]
        if desired == current:
            return
        resource.tags = desired


        retry_transient(resource.save)
        fresh = retry_transient(lambda: client.load(resource_cls, resource_id))
        fresh_tags = fresh.tags or []
        missing = [t for t in tags if t not in fresh_tags]
        not_removed = [t for t in remove if t in fresh_tags]
        if missing or not_removed:
            problems = []
            if missing:
                problems.append(f"expected tags {missing!r} still missing")
            if not_removed:
                problems.append(f"stale tags {not_removed!r} still present")
            raise TagVerificationError(
                f"Tagging {resource_cls.__name__} {resource_id} for '{name}' did not take "
                f"effect as expected ({'; '.join(problems)}) -- refusing to remove any stale "
                "mapping until this is confirmed working."
            )

    _tag_and_verify(
        os_volume, Volume, os_volume_id, name_tag, REGISTRY_ROLE_TAG_OS,
        remove=(REGISTRY_ROLE_TAG_ACTIVE_MIGRATION, REGISTRY_ROLE_TAG_ORPHANED_MIGRATION),
    )
    for vol_id, vol in data_volumes:


        _tag_and_verify(
            vol, Volume, vol_id, name_tag, REGISTRY_ROLE_TAG_DATA,
            remove=(REGISTRY_ROLE_TAG_ACTIVE_MIGRATION, REGISTRY_ROLE_TAG_ORPHANED_MIGRATION),
        )
    _tag_and_verify(ip, ReservedIPAddress, reserved_ip, name_tag, REGISTRY_ROLE_TAG_IP)


    for old_os_id in current.get("os_volume_ids", []):
        if old_os_id != os_volume_id:
            try:
                old_vol = client.load(Volume, old_os_id)
            except ApiError as e:


                if e.status == 404:
                    continue
                raise
            _untag_stale(client, old_vol, Volume, old_os_id, name, name_tag, REGISTRY_ROLE_TAG_OS)
    for old_data_id in current.get("data_volume_ids", []):
        if old_data_id not in data_volume_ids:
            try:
                old_vol = client.load(Volume, old_data_id)
            except ApiError as e:
                if e.status == 404:
                    continue
                raise
            _untag_stale(client, old_vol, Volume, old_data_id, name, name_tag, REGISTRY_ROLE_TAG_DATA)
    for old_ip in current.get("reserved_ips", []):
        if old_ip != reserved_ip:
            try:
                old_ip_resource = client.load(ReservedIPAddress, old_ip)
            except ApiError as e:
                if e.status == 404:
                    continue
                raise
            _untag_stale(
                client, old_ip_resource, ReservedIPAddress, old_ip, name, name_tag,
                REGISTRY_ROLE_TAG_IP,
            )


def _untag_stale(
    client: LinodeClient, resource, resource_cls, resource_id, name: str, name_tag: str, role_tag: str,
) -> None:

    other_names = [n for n in names_from_tags(resource.tags) if n != name]
    remove = (name_tag,) if other_names else (name_tag, role_tag)
    current = list(resource.tags or [])
    desired = [t for t in current if t not in remove]
    if desired == current:
        return
    resource.tags = desired


    retry_transient(resource.save)
    fresh = retry_transient(lambda: client.load(resource_cls, resource_id))
    not_removed = [t for t in remove if t in (fresh.tags or [])]
    if not_removed:
        raise TagVerificationError(
            f"Untagging {resource_cls.__name__} {resource_id} for '{name}' did not take "
            f"effect as expected (stale tags {not_removed!r} still present) -- refusing to "
            "treat this as resolved."
        )


def find_managed_resources_by_tag(client: LinodeClient) -> tuple[dict[str, dict], list[dict]]:

    by_name, conflicts = _scan_tagged_resources(client)
    for entry in by_name.values():
        os_ids = sorted(set(entry.pop("os_volume_ids")))
        data_ids = sorted(set(entry.pop("data_volume_ids")))
        ips = sorted(set(entry.pop("reserved_ips")))
        regions = entry.pop("regions")
        entry["os_volume_id"] = os_ids[0] if len(os_ids) == 1 else None
        entry["os_volume_id_candidates"] = os_ids
        entry["data_volume_ids"] = data_ids
        entry["reserved_ip"] = ips[0] if len(ips) == 1 else None
        entry["reserved_ip_candidates"] = ips
        entry["region"] = next(iter(regions)) if len(regions) == 1 else None
        entry["region_candidates"] = sorted(regions)
    return by_name, conflicts


def find_migration_volumes_by_tag(client: LinodeClient) -> tuple[dict[str, dict], list[dict]]:

    by_name: dict[str, dict] = {}
    conflicts: list[dict] = []


    for vol in retry_transient(lambda: list(client.volumes())):
        vol_tags = vol.tags or []
        is_active = REGISTRY_ROLE_TAG_ACTIVE_MIGRATION in vol_tags
        is_orphaned = REGISTRY_ROLE_TAG_ORPHANED_MIGRATION in vol_tags
        if not is_active and not is_orphaned:
            continue
        names = names_from_tags(vol_tags)
        managed_roles = [
            t for t in (REGISTRY_ROLE_TAG_OS, REGISTRY_ROLE_TAG_DATA, REGISTRY_ROLE_TAG_IP)
            if t in vol_tags
        ]
        if managed_roles:
            conflicts.append({
                "kind": "volume", "id": vol.id, "reason": "managed_role_conflict",
                "names": names, "roles": managed_roles,
            })
            continue
        if is_active and is_orphaned:
            conflicts.append(
                {"kind": "volume", "id": vol.id, "reason": "multiple_roles", "names": names}
            )
            continue
        if len(names) > 1:
            conflicts.append(
                {"kind": "volume", "id": vol.id, "reason": "multiple_names", "names": names}
            )
            continue
        if not names:
            conflicts.append({"kind": "volume", "id": vol.id, "reason": "no_name", "names": []})
            continue
        entry = by_name.setdefault(
            names[0], {"active_migration_volume_ids": [], "orphaned_migration_volume_ids": []}
        )
        if is_active:
            entry["active_migration_volume_ids"].append(vol.id)
        else:
            entry["orphaned_migration_volume_ids"].append(vol.id)
    for entry in by_name.values():
        entry["active_migration_volume_ids"] = sorted(set(entry["active_migration_volume_ids"]))
        entry["orphaned_migration_volume_ids"] = sorted(set(entry["orphaned_migration_volume_ids"]))
    return by_name, conflicts


def verify_migration_volume_orphaned_and_unmanaged(
    client: LinodeClient, volume_id: int, name: str,
) -> tuple[list[str], Volume]:

    vol = retry_transient(lambda: client.load(Volume, volume_id))
    tags = list(vol.tags or [])
    names = names_from_tags(tags)
    problems = []
    if set(names) != {name}:
        problems.append(f"scheduler name set is {names!r}, expected exactly [{name!r}]")
    if REGISTRY_ROLE_TAG_ORPHANED_MIGRATION not in tags:
        problems.append("no longer tagged orphaned-migration-attempt")
    if REGISTRY_ROLE_TAG_ACTIVE_MIGRATION in tags:
        problems.append("now tagged active-migration")
    for managed_tag in (REGISTRY_ROLE_TAG_OS, REGISTRY_ROLE_TAG_DATA, REGISTRY_ROLE_TAG_IP):
        if managed_tag in tags:
            problems.append(f"also carries {managed_tag!r} (a managed resource role)")
    return problems, vol


def _verify_managed_resource_owned_by_name(
    client: LinodeClient, resource_type, resource_id, name: str, expected_role_tag: str,
):

    resource = retry_transient(lambda: client.load(resource_type, resource_id))
    tags = list(resource.tags or [])
    names = names_from_tags(tags)
    problems = []
    if set(names) != {name}:
        problems.append(f"scheduler name set is {names!r}, expected exactly [{name!r}]")
    if expected_role_tag not in tags:
        problems.append(f"no longer tagged {expected_role_tag!r}")
    return problems, resource


def verify_managed_volume_owned_by_name(
    client: LinodeClient, volume_id: int, name: str, expected_role_tag: str,
) -> tuple[list[str], Volume]:

    return _verify_managed_resource_owned_by_name(
        client, Volume, volume_id, name, expected_role_tag
    )


def verify_managed_ip_owned_by_name(
    client: LinodeClient, reserved_ip: str, name: str,
) -> tuple[list[str], ReservedIPAddress]:

    return _verify_managed_resource_owned_by_name(
        client, ReservedIPAddress, reserved_ip, name, REGISTRY_ROLE_TAG_IP
    )


def untag_managed_resources(
    client: LinodeClient,
    name: str,
    *,
    os_volume_id: int | None,
    data_volume_ids: list[int],
) -> None:

    name_tag = f"{REGISTRY_NAME_TAG_PREFIX}:{name}"
    for vol_id in (os_volume_id, *data_volume_ids):
        if vol_id is None:
            continue
        vol = client.load(Volume, vol_id)
        _untag_stale(client, vol, Volume, vol_id, name, name_tag, REGISTRY_ROLE_TAG_OS)
        _untag_stale(client, vol, Volume, vol_id, name, name_tag, REGISTRY_ROLE_TAG_DATA)


def _transition_migration_volume_to_orphaned(
    client: LinodeClient, volume_id: int, name: str,
) -> None:

    name_tag = f"{REGISTRY_NAME_TAG_PREFIX}:{name}"


    vol = retry_transient(lambda: client.load(Volume, volume_id))
    tags = list(vol.tags or [])
    other_names = [n for n in names_from_tags(tags) if n != name]
    if other_names:
        raise ResourceOwnershipConflict(
            f"Volume {volume_id} is already tagged for {other_names!r}, not {name!r} -- "
            "refusing to add a second name's ownership tag on top (this would make the "
            "volume ambiguously owned by both)."
        )

    desired = [t for t in tags if t != REGISTRY_ROLE_TAG_ACTIVE_MIGRATION]
    if REGISTRY_ROLE_TAG_ORPHANED_MIGRATION not in desired:
        desired.append(REGISTRY_ROLE_TAG_ORPHANED_MIGRATION)
    if name_tag not in desired:
        desired.append(name_tag)
    if desired != tags:
        vol.tags = desired
        retry_transient(vol.save)

    fresh = retry_transient(lambda: client.load(Volume, volume_id))
    fresh_tags = list(fresh.tags or [])
    fresh_names = names_from_tags(fresh_tags)
    problems = []


    if set(fresh_names) != {name}:
        problems.append(f"scheduler name set is {fresh_names!r}, expected exactly [{name!r}]")
    if REGISTRY_ROLE_TAG_ORPHANED_MIGRATION not in fresh_tags:
        problems.append(f"missing {REGISTRY_ROLE_TAG_ORPHANED_MIGRATION!r}")
    if REGISTRY_ROLE_TAG_ACTIVE_MIGRATION in fresh_tags:
        problems.append(f"still has stale {REGISTRY_ROLE_TAG_ACTIVE_MIGRATION!r}")
    if problems:
        raise TagVerificationError(
            f"Retagging volume {volume_id} as orphaned for {name!r} did not fully take "
            f"effect (re-read shows: {', '.join(problems)}) -- refusing to treat this as "
            "complete."
        )


def tag_orphaned_migration_volume(client: LinodeClient, volume_id: int, name: str) -> None:

    _transition_migration_volume_to_orphaned(client, volume_id, name)


def _transition_migration_volume_to_active(
    client: LinodeClient, volume_id: int, name: str,
) -> None:

    name_tag = f"{REGISTRY_NAME_TAG_PREFIX}:{name}"


    vol = retry_transient(lambda: client.load(Volume, volume_id))
    tags = list(vol.tags or [])
    other_names = [n for n in names_from_tags(tags) if n != name]
    if other_names:
        raise ResourceOwnershipConflict(
            f"Volume {volume_id} is already tagged for {other_names!r}, not {name!r} -- "
            "refusing to add a second name's ownership tag on top (this would make the "
            "volume ambiguously owned by both)."
        )
    if REGISTRY_ROLE_TAG_ORPHANED_MIGRATION in tags:
        raise ResourceOwnershipConflict(
            f"Volume {volume_id} already carries the orphaned-migration-attempt role -- "
            "refusing to also tag it active (a volume must never carry both migration "
            "roles at once). This should be impossible for a newly created destination "
            "volume -- investigate how this volume got that tag before reusing its ID."
        )

    desired = list(tags)
    if name_tag not in desired:
        desired.append(name_tag)
    if REGISTRY_ROLE_TAG_ACTIVE_MIGRATION not in desired:
        desired.append(REGISTRY_ROLE_TAG_ACTIVE_MIGRATION)
    if desired != tags:
        vol.tags = desired
        retry_transient(vol.save)

    fresh = retry_transient(lambda: client.load(Volume, volume_id))
    fresh_tags = list(fresh.tags or [])
    fresh_names = names_from_tags(fresh_tags)
    problems = []


    if set(fresh_names) != {name}:
        problems.append(f"scheduler name set is {fresh_names!r}, expected exactly [{name!r}]")
    if REGISTRY_ROLE_TAG_ACTIVE_MIGRATION not in fresh_tags:
        problems.append(f"missing {REGISTRY_ROLE_TAG_ACTIVE_MIGRATION!r}")
    if REGISTRY_ROLE_TAG_ORPHANED_MIGRATION in fresh_tags:
        problems.append(f"unexpectedly has {REGISTRY_ROLE_TAG_ORPHANED_MIGRATION!r}")
    if problems:
        raise TagVerificationError(
            f"Tagging volume {volume_id} as {name!r}'s active migration attempt did not "
            f"fully take effect (re-read shows: {', '.join(problems)}) -- refusing to "
            "treat this as complete."
        )


def tag_active_migration_volume(client: LinodeClient, volume_id: int, name: str) -> None:

    _transition_migration_volume_to_active(client, volume_id, name)


def retag_migration_volume_as_orphaned(client: LinodeClient, volume_id: int, name: str) -> None:

    _transition_migration_volume_to_orphaned(client, volume_id, name)


SIMPLE_PUBLIC_ONLY_NETWORK = {
    "network_interface_model": INTERFACE_MODEL_LEGACY,
    "network_config": [{"purpose": "public", "primary": False}],
    "network_helper_enabled": True,
}


_SECRET_STATE_FIELDS = ("root_pass",)


def retry_transient(fn, attempts=5, delay_s=5):

    for attempt in range(attempts):
        try:
            return fn()
        except ApiError as e:
            if e.status < 500 or attempt == attempts - 1:
                raise
        except requests.exceptions.RequestException:
            if attempt == attempts - 1:
                raise
        time.sleep(delay_s)


def _resource_confirmed_gone(client, resource) -> bool:

    parent_id_name = getattr(type(resource), "parent_id_name", None)
    try:
        if parent_id_name is not None:
            client.load(type(resource), resource.id, getattr(resource, parent_id_name))
        else:
            client.load(type(resource), resource.id)
        return False
    except ApiError as e:
        return e.status == 404
    except requests.exceptions.RequestException:
        return False


def retry_transient_or_already_done(
    fn, already_done, attempts=5, delay_s=5, is_transient=None,
):

    for attempt in range(attempts):
        try:
            return fn()
        except ApiError as e:
            if attempt > 0:
                try:
                    if already_done():
                        return None
                except Exception:
                    pass
            is_retryable = e.status >= 500 or (is_transient is not None and is_transient(e))
            if not is_retryable or attempt == attempts - 1:
                raise
        except requests.exceptions.RequestException:
            if attempt > 0:
                try:
                    if already_done():
                        return None
                except Exception:
                    pass
            if attempt == attempts - 1:
                raise
        time.sleep(delay_s)



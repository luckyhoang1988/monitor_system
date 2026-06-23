"""SNMP client dùng chung — easysnmp (Linux) hoặc pysnmp fallback (Windows)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"


@dataclass
class SnmpResult:
    oid: str
    value: str


def _safe_close(dispatcher) -> None:
    """Đóng SnmpDispatcher mà không để lỗi cleanup làm hỏng kết quả/che lỗi gốc.

    pysnmp v1arch: nếu còn request đang chờ khi close (vd walk bị hủy/timeout),
    callback nội bộ ném `TypeError: __callback() missing 'cbCtx'`. Lỗi này nằm
    trong `finally` nên sẽ che mất exception thật và làm task chết — nuốt nó.
    """
    try:
        dispatcher.close()
    except Exception as exc:  # noqa: BLE001 — cleanup không bao giờ được phép ném
        logger.debug("SnmpDispatcher.close() lỗi (bỏ qua): %s", exc)


class PySnmpSession:
    """Fallback SNMP session dùng pysnmp cho môi trường không có easysnmp (Windows)."""

    def __init__(self, *, hostname: str, version: int, community: str, timeout: int, retries: int) -> None:
        if version not in (1, 2):
            raise RuntimeError("Fallback pysnmp hiện chỉ hỗ trợ SNMP v1/v2c.")
        self.hostname = hostname
        self.mp_model = 0 if version == 1 else 1
        self.community = community
        self.timeout = float(timeout)
        self.retries = int(retries)

    async def _build_target(self):
        from pysnmp.hlapi.v1arch import UdpTransportTarget
        return await UdpTransportTarget.create(
            (self.hostname, 161),
            timeout=self.timeout,
            retries=self.retries,
        )

    def get(self, oid: str) -> SnmpResult:
        return asyncio.run(self._get(oid))

    async def _get(self, oid: str) -> SnmpResult:
        from pysnmp.hlapi.v1arch import (
            CommunityData,
            ObjectIdentity,
            ObjectType,
            SnmpDispatcher,
            get_cmd,
        )
        dispatcher = SnmpDispatcher()
        try:
            target = await self._build_target()
            err_ind, err_status, err_index, var_binds = await get_cmd(
                dispatcher,
                CommunityData(self.community, mpModel=self.mp_model),
                target,
                ObjectType(ObjectIdentity(oid)),
            )
            if err_ind:
                raise ConnectionError(str(err_ind))
            if err_status:
                raise ConnectionError(f"{err_status.prettyPrint()} at {err_index}")
            if not var_binds:
                raise ConnectionError("Không nhận được dữ liệu SNMP.")
            var_bind = var_binds[0]
            return SnmpResult(oid=str(var_bind[0]), value=str(var_bind[1]))
        finally:
            _safe_close(dispatcher)

    def walk(self, oid_prefix: str) -> list[SnmpResult]:
        return asyncio.run(self._walk(oid_prefix))

    async def _walk(self, oid_prefix: str) -> list[SnmpResult]:
        from pysnmp.hlapi.v1arch import (
            CommunityData,
            ObjectIdentity,
            ObjectType,
            SnmpDispatcher,
            next_cmd,
        )
        dispatcher = SnmpDispatcher()
        try:
            target = await self._build_target()
            results: list[SnmpResult] = []
            current_oid = oid_prefix
            max_steps = 4096
            for _ in range(max_steps):
                err_ind, err_status, err_index, var_binds = await next_cmd(
                    dispatcher,
                    CommunityData(self.community, mpModel=self.mp_model),
                    target,
                    ObjectType(ObjectIdentity(current_oid)),
                    lexicographicMode=False,
                )
                if err_ind:
                    raise ConnectionError(str(err_ind))
                if err_status:
                    raise ConnectionError(f"{err_status.prettyPrint()} at {err_index}")
                if not var_binds:
                    break
                var_bind = var_binds[0]
                oid = str(var_bind[0])
                if not oid.startswith(f"{oid_prefix}."):
                    break
                value = str(var_bind[1])
                results.append(SnmpResult(oid=oid, value=value))
                if oid == current_oid:
                    break
                current_oid = oid
            return results
        finally:
            _safe_close(dispatcher)


def resolve_snmp_backend() -> str:
    try:
        import easysnmp  # noqa: F401
        return "easysnmp"
    except Exception:
        return "pysnmp"


def create_snmp_session(snmp_kwargs: dict[str, Any], backend: str | None = None) -> Any:
    """Tạo SNMP session — easysnmp Session hoặc PySnmpSession (v1/v2c only)."""
    backend = backend or resolve_snmp_backend()
    if backend == "easysnmp":
        from easysnmp import Session
        return Session(**snmp_kwargs)

    version = snmp_kwargs.get("version")
    if version not in (1, 2):
        raise RuntimeError("pysnmp fallback chỉ hỗ trợ SNMP v1/v2c.")
    return PySnmpSession(
        hostname=snmp_kwargs["hostname"],
        version=version,
        community=snmp_kwargs.get("community", ""),
        timeout=snmp_kwargs.get("timeout", 10),
        retries=snmp_kwargs.get("retries", 2),
    )


def snmp_get_value(session: Any, oid: str) -> str | None:
    try:
        result = session.get(oid)
        return str(getattr(result, "value", result))
    except Exception as exc:
        logger.debug("snmp get failed oid=%s: %s", oid, exc)
        return None


def snmp_walk_pairs(session: Any, oid_prefix: str) -> list[tuple[str, str]]:
    try:
        results = session.walk(oid_prefix)
        return [(r.oid, r.value) for r in results]
    except Exception as exc:
        logger.debug("snmp walk failed prefix=%s: %s", oid_prefix, exc)
        return []


def probe_snmp_v2c(
    ip: str,
    community: str = "public",
    *,
    timeout: int = 1,
    retries: int = 1,
) -> tuple[bool, str]:
    """Probe SNMP v2c cho discovery — cùng backend với collector."""
    snmp_kwargs = {
        "hostname": ip,
        "community": community,
        "version": 2,
        "timeout": timeout,
        "retries": retries,
    }
    try:
        backend = resolve_snmp_backend()
        session = create_snmp_session(snmp_kwargs, backend=backend)
        descr = snmp_get_value(session, OID_SYS_DESCR) or ""
        name = snmp_get_value(session, OID_SYS_NAME) or ""
        if not descr and not name:
            return False, ""
        return True, f"{name} - {descr}"[:300]
    except Exception:
        return False, ""

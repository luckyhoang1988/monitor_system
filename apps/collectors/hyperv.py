"""HyperV Collector — WinRM + PowerShell để thu thập metrics VM và host."""
import json
import logging
import re
from requests import exceptions as req_exc
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .base import BaseCollector, NormalizedData

if TYPE_CHECKING:
    from apps.devices.models import Device

logger = logging.getLogger(__name__)

PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$vms   = Get-VM | Select-Object Name,State,CPUUsage,MemoryAssigned
$repls = @(try { Get-VMReplication | Select-Object VMName,Health } catch { @() })
$hostCpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
$hostMem = Get-CimInstance Win32_OperatingSystem
$result = @{
    host_cpu_percent = [math]::Round($hostCpu, 1)
    host_mem_percent = [math]::Round((($hostMem.TotalVisibleMemorySize - $hostMem.FreePhysicalMemory) / $hostMem.TotalVisibleMemorySize * 100), 1)
    host_boot_time   = $hostMem.LastBootUpTime.ToUniversalTime().ToString("o")
    vms = @($vms | ForEach-Object {
        $vm   = $_
        $repl = $repls | Where-Object { $_.VMName -eq $vm.Name } | Select-Object -First 1
        @{
            name        = $vm.Name
            state       = $vm.State.ToString()
            cpu_percent = if ($vm.CPUUsage -ne $null) { $vm.CPUUsage } else { 0 }
            mem_mb      = if ($vm.MemoryAssigned) { [math]::Round($vm.MemoryAssigned / 1MB, 0) } else { 0 }
            repl_health = if ($repl) { $repl.Health.ToString() } else { 'NotConfigured' }
        }
    })
}
$result | ConvertTo-Json -Depth 4
"""


class HyperVCollector(BaseCollector):
    def __init__(self, device: "Device") -> None:
        super().__init__(device)

    def _run_ps(self, script: str) -> dict:
        import winrm
        import winrm.exceptions
        from django.conf import settings as _s
        if not self.device.ssh_username or not self.device.ssh_password:
            raise ValueError(f"WinRM credentials missing for device {self.device.name}")
        cert_validation = getattr(_s, "WINRM_CERT_VALIDATE", "validate")
        targets = [
            f"http://{self.device.ip_address}:5985/wsman",
            f"https://{self.device.ip_address}:5986/wsman",
        ]
        result = None
        last_exc: Exception | None = None
        for idx, target in enumerate(targets, start=1):
            session = winrm.Session(
                target=target,
                auth=(self.device.ssh_username, self.device.ssh_password),
                transport="ntlm",
                server_cert_validation=cert_validation,
                operation_timeout_sec=60,
                read_timeout_sec=70,
            )
            try:
                result = session.run_ps(script)
                break
            except winrm.exceptions.InvalidCredentialsError as exc:
                raise ConnectionError(f"WinRM auth failed for {self.device.name}: {exc}") from exc
            except (winrm.exceptions.WinRMOperationTimeoutError, req_exc.ConnectTimeout, req_exc.ConnectionError) as exc:
                last_exc = exc
                if idx == len(targets):
                    raise TimeoutError(f"WinRM connect timeout for {self.device.name}: {exc}") from exc
                continue

        if result is None:
            raise RuntimeError(f"No WinRM result returned for {self.device.name}: {last_exc}")
        if result.status_code != 0:
            err = result.std_err.decode("utf-8", errors="replace")
            raise RuntimeError(f"PowerShell error (exit {result.status_code}): {err}")
        try:
            return json.loads(result.std_out.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raw = result.std_out[:300]
            raise RuntimeError(f"HyperV JSON parse failed: {exc}. Raw: {raw!r}") from exc

    def test_connection(self) -> str:
        _ = self._run_ps('$env:COMPUTERNAME | ConvertTo-Json')
        return "hyperv_winrm"

    def collect_raw(self) -> dict:
        return self._run_ps(PS_SCRIPT)

    def adapt(self, raw: dict) -> NormalizedData:
        uptime_secs = 0
        boot_str = raw.get("host_boot_time", "")
        if boot_str:
            try:
                normalized = re.sub(r"(\.\d{6})\d*(Z|[+-]\d{2}:\d{2})$", r"\1+00:00", boot_str)
                boot_dt = datetime.fromisoformat(normalized)
                uptime_secs = int((datetime.now(tz=timezone.utc) - boot_dt).total_seconds())
            except (ValueError, TypeError):
                logger.warning("Device %s: could not parse boot time %r", self.device.name, boot_str)
        return NormalizedData(
            device_name=self.device.name,
            ip_address=self.device.ip_address,
            timestamp=datetime.now(tz=timezone.utc),
            os_family="hyperv_winrm",
            cpu_percent=float(raw.get("host_cpu_percent", 0)),
            mem_percent=float(raw.get("host_mem_percent", 0)),
            uptime_secs=uptime_secs,
            interfaces=[],
            extra={"vms": raw.get("vms", [])},
        )

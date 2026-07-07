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

# PS_SCRIPT nén tối đa (không comment/whitespace thừa) — WinRM run_ps encode UTF-16LE +
# base64 rồi truyền qua cmd.exe (giới hạn ~8191 ký tự dòng lệnh). Bản đầy đủ comment +
# biến tên dài (7 host-perf field) vượt giới hạn → lỗi "The command line is too long"
# (verify runtime 2026-07-07). Thứ tự $c[0..7] khớp ĐÚNG thứ tự $p (verify Get-Counter
# giữ nguyên thứ tự request trên cả Hyperv-01/02 thật) — tránh chuỗi match theo Path
# (tốn nhiều ký tự). $c[8..] luôn là các instance Network Interface(*) (do path đó liệt
# kê wildcard cuối cùng trong $p). Chi tiết đầy đủ (comment, tên biến rõ nghĩa) xem
# scratchpad/plan_hyperv.md — giữ file này gọn để không lặp lỗi command-line-too-long.
PS_SCRIPT = r"""
$ErrorActionPreference='Stop'
$vms=Get-VM|Select-Object Name,State,CPUUsage,MemoryAssigned
$repls=@(try{Get-VMReplication|Select-Object VMName,Health}catch{@()})
$hostCpu=(Get-CimInstance Win32_Processor|Measure-Object -Property LoadPercentage -Average).Average
$hostMem=Get-CimInstance Win32_OperatingSystem
$hp=$null
try{
$ex='(?i)(isatap|teredo|tunnel|loopback|pseudo-interface|qos packet scheduler|wfp|kernel debug)'
$p=@('\Processor Information(_Total)\% Processor Utility','\Hyper-V Hypervisor Logical Processor(_Total)\% Total Run Time','\Memory\Available MBytes','\Memory\% Committed Bytes In Use','\PhysicalDisk(_Total)\Disk Reads/sec','\PhysicalDisk(_Total)\Disk Writes/sec','\PhysicalDisk(_Total)\Avg. Disk sec/Read','\PhysicalDisk(_Total)\Avg. Disk sec/Write','\Network Interface(*)\Bytes Total/sec')
$ss=Get-Counter -Counter $p -SampleInterval 2 -MaxSamples 5 -ErrorAction Stop
$cu=@();$hv=@();$ma=@();$mp=@();$dr=@();$dw=@();$rl=@();$wl=@();$nm=@()
foreach($s in $ss){
$c=$s.CounterSamples
$cu+=$c[0].CookedValue;$hv+=$c[1].CookedValue;$ma+=$c[2].CookedValue;$mp+=$c[3].CookedValue
$dr+=$c[4].CookedValue;$dw+=$c[5].CookedValue;$rl+=$c[6].CookedValue;$wl+=$c[7].CookedValue
$nt=0.0
for($i=8;$i -lt $c.Count;$i++){if($c[$i].InstanceName -notmatch $ex){$nt+=$c[$i].CookedValue}}
$nm+=$nt
}
function A($l){if($l.Count -eq 0){$null}else{($l|Measure-Object -Average).Average}}
function M($l){if($l.Count -eq 0){$null}else{($l|Measure-Object -Maximum).Maximum}}
$hp=@{
cpu_processor_utility=if($cu.Count -gt 0){[math]::Round((A $cu),1)}else{$null}
cpu_hv_percent=if($hv.Count -gt 0){[math]::Round((A $hv),1)}else{$null}
mem_committed_percent=if($mp.Count -gt 0){[math]::Round((A $mp),1)}else{$null}
mem_available_mb=if($ma.Count -gt 0){[math]::Round((A $ma),0)}else{$null}
disk_read_iops=if($dr.Count -gt 0){[math]::Round((A $dr),1)}else{$null}
disk_write_iops=if($dw.Count -gt 0){[math]::Round((A $dw),1)}else{$null}
disk_read_latency_ms=if($rl.Count -gt 0){[math]::Round((M $rl)*1000,2)}else{$null}
disk_write_latency_ms=if($wl.Count -gt 0){[math]::Round((M $wl)*1000,2)}else{$null}
net_mbps_total=if($nm.Count -gt 0){[math]::Round((A $nm)*8/1MB,2)}else{$null}
}
}catch{$hp=$null}
$result=@{
host_cpu_percent=[math]::Round($hostCpu,1)
host_mem_percent=[math]::Round((($hostMem.TotalVisibleMemorySize-$hostMem.FreePhysicalMemory)/$hostMem.TotalVisibleMemorySize*100),1)
host_boot_time=$hostMem.LastBootUpTime.ToUniversalTime().ToString("o")
host_perf=$hp
vms=@($vms|ForEach-Object{
$vm=$_
$repl=$repls|Where-Object{$_.VMName -eq $vm.Name}|Select-Object -First 1
@{name=$vm.Name;state=$vm.State.ToString();cpu_percent=if($vm.CPUUsage -ne $null){$vm.CPUUsage}else{0};mem_mb=if($vm.MemoryAssigned){[math]::Round($vm.MemoryAssigned/1MB,0)}else{0};repl_health=if($repl){$repl.Health.ToString()}else{'NotConfigured'}}
})
}
$result|ConvertTo-Json -Depth 4
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
                # Chặn uptime âm khi đồng hồ host lệch (boot_time tương lai).
                uptime_secs = max(0, int((datetime.now(tz=timezone.utc) - boot_dt).total_seconds()))
            except (ValueError, TypeError):
                logger.warning("Device %s: could not parse boot time %r", self.device.name, boot_str)

        # host_perf = None nếu block Get-Counter lỗi (try/catch cô lập trong PS_SCRIPT) —
        # fallback cpu/mem về WMI (host_cpu_percent/host_mem_percent) như trước.
        host_perf = raw.get("host_perf") or {}
        cpu_percent = host_perf.get("cpu_processor_utility")
        if cpu_percent is None:
            cpu_percent = raw.get("host_cpu_percent", 0)
        mem_percent = host_perf.get("mem_committed_percent")
        if mem_percent is None:
            mem_percent = raw.get("host_mem_percent", 0)

        return NormalizedData(
            device_name=self.device.name,
            ip_address=self.device.ip_address,
            timestamp=datetime.now(tz=timezone.utc),
            os_family="hyperv_winrm",
            cpu_percent=float(cpu_percent or 0),
            mem_percent=float(mem_percent or 0),
            uptime_secs=uptime_secs,
            interfaces=[],
            cpu_hv_percent=host_perf.get("cpu_hv_percent"),
            mem_available_mb=host_perf.get("mem_available_mb"),
            disk_read_iops=host_perf.get("disk_read_iops"),
            disk_write_iops=host_perf.get("disk_write_iops"),
            disk_read_latency_ms=host_perf.get("disk_read_latency_ms"),
            disk_write_latency_ms=host_perf.get("disk_write_latency_ms"),
            net_mbps_total=host_perf.get("net_mbps_total"),
            extra={"vms": raw.get("vms", [])},
        )

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
# biến tên dài (host-perf field) vượt giới hạn → lỗi "The command line is too long"
# (verify runtime 2026-07-07). Thứ tự $c[0..11] khớp ĐÚNG thứ tự $p (verify Get-Counter
# giữ nguyên thứ tự request trên cả Hyperv-01/02 thật) — tránh chuỗi match theo Path
# (tốn nhiều ký tự). $c[12..] luôn là các instance Network Interface(*) (do path đó liệt
# kê wildcard cuối cùng trong $p). ⚠️ Helper function rút gọn round đặt tên `RA` (KHÔNG
# đặt tên `R` — `r` là alias built-in PowerShell của `Invoke-History` với tham số positional
# `-Id`; gọi `R $arr 1` bị PowerShell resolve thành alias thay vì function, ném lỗi runtime
# "Cannot convert 'System.Object[]' to type 'System.String' required by parameter 'Id'" —
# dính thật + verify runtime 2026-07-07, mọi function helper 1 ký tự phải kiểm tra trước
# xem có trùng alias built-in không, vd `Get-Alias r`). Key JSON `rtm/wtm/dql/aio` trong $hp
# viết tắt disk_read_throughput_mbps/disk_write_throughput_mbps/disk_queue_length/avg_io_size_kb
# (rút ngắn thêm để giữ margin base64 — adapt() bên dưới map lại sang tên field đầy đủ).
PS_SCRIPT = r"""
$ErrorActionPreference='Stop'
$vms=Get-VM|Select-Object Name,State,CPUUsage,MemoryAssigned
$repls=@(try{Get-VMReplication|Select-Object VMName,Health}catch{@()})
$hostCpu=(Get-CimInstance Win32_Processor|Measure-Object -Property LoadPercentage -Average).Average
$hostMem=Get-CimInstance Win32_OperatingSystem
function A($l){if($l.Count -eq 0){$null}else{($l|Measure-Object -Average).Average}}
function M($l){if($l.Count -eq 0){$null}else{($l|Measure-Object -Maximum).Maximum}}
function RA($l,$n){$v=A $l;if($v -eq $null){$null}else{[math]::Round($v,$n)}}
function RS($l,$u,$n){$v=A $l;if($v -eq $null){$null}else{[math]::Round($v*$u,$n)}}
function RX($l,$u,$n){$v=M $l;if($v -eq $null){$null}else{[math]::Round($v*$u,$n)}}
$hp=$null
try{
$ex='(?i)(isatap|teredo|tunnel|loopback|pseudo-interface|qos packet scheduler|wfp|kernel debug)'
$bd='\PhysicalDisk(_Total)\'
$p=@('\Processor Information(_Total)\% Processor Utility','\Hyper-V Hypervisor Logical Processor(_Total)\% Total Run Time','\Memory\Available MBytes','\Memory\% Committed Bytes In Use',($bd+'Disk Reads/sec'),($bd+'Disk Writes/sec'),($bd+'Avg. Disk sec/Read'),($bd+'Avg. Disk sec/Write'),($bd+'Disk Read Bytes/sec'),($bd+'Disk Write Bytes/sec'),($bd+'Avg. Disk Queue Length'),($bd+'Avg. Disk Bytes/Transfer'),'\Network Interface(*)\Bytes Total/sec')
$ss=Get-Counter -Counter $p -SampleInterval 2 -MaxSamples 5 -ErrorAction Stop
$cu=@();$hv=@();$ma=@();$mp=@();$dr=@();$dw=@();$rl=@();$wl=@();$rt=@();$wt=@();$ql=@();$io=@();$nm=@()
foreach($s in $ss){
$c=$s.CounterSamples
$cu+=$c[0].CookedValue;$hv+=$c[1].CookedValue;$ma+=$c[2].CookedValue;$mp+=$c[3].CookedValue
$dr+=$c[4].CookedValue;$dw+=$c[5].CookedValue;$rl+=$c[6].CookedValue;$wl+=$c[7].CookedValue
$rt+=$c[8].CookedValue;$wt+=$c[9].CookedValue;$ql+=$c[10].CookedValue;$io+=$c[11].CookedValue
$nt=0.0
for($i=12;$i -lt $c.Count;$i++){if($c[$i].InstanceName -notmatch $ex){$nt+=$c[$i].CookedValue}}
$nm+=$nt
}
$hp=@{
cpu_processor_utility=RA $cu 1
cpu_hv_percent=RA $hv 1
mem_committed_percent=RA $mp 1
mem_available_mb=RA $ma 0
disk_read_iops=RA $dr 1
disk_write_iops=RA $dw 1
disk_read_latency_ms=RX $rl 1000 2
disk_write_latency_ms=RX $wl 1000 2
rtm=RS $rt (8/1MB) 2
wtm=RS $wt (8/1MB) 2
dql=RA $ql 2
aio=RS $io (1/1024) 1
net_mbps_total=RS $nm (8/1MB) 2
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

# PS_SCRIPT_VOLUME — script RIÊNG (WinRM call thứ 2), KHÔNG gộp vào PS_SCRIPT ở trên.
# Lý do: gộp chung từng vượt 15408 ký tự base64 (đo runtime 2026-07-07, gần gấp đôi giới
# hạn ~8191) vì cardinality động (N volume/host) buộc phải match theo Path/InstanceName
# (tốn ký tự) thay vì positional index như khối scalar host. Chấp nhận thêm 1 vòng NTLM
# handshake/host (script riêng, session riêng) — timing vẫn còn margin so với
# POLL_HYPERV_INTERVAL_SECS=120 (đo lại ở bước verify). InstanceName trả về lowercase
# "c:"/"d:"/"harddiskvolume1"/"_total"; VHD Path luôn ổ cục bộ "D:\..." (không CSV) —
# verify runtime trên cả Hyperv-01/02 thật 2026-07-07.
PS_SCRIPT_VOLUME = r"""
$ErrorActionPreference='Stop'
function A($l){if($l.Count -eq 0){$null}else{($l|Measure-Object -Average).Average}}
function M($l){if($l.Count -eq 0){$null}else{($l|Measure-Object -Maximum).Maximum}}
$vol=$null
try{
$vhds=@(Get-VM|Get-VMHardDiskDrive|Select-Object VMName,Path)
$vm2=@{}
foreach($vh in $vhds){
if($vh.Path -match '^([A-Za-z]):\\'){
$k=$matches[1].ToLower()+':'
if(-not $vm2.ContainsKey($k)){$vm2[$k]=@()}
if($vm2[$k] -notcontains $vh.VMName){$vm2[$k]+=$vh.VMName}
}
}
$lp=@('\LogicalDisk(*)\Disk Reads/sec','\LogicalDisk(*)\Disk Writes/sec','\LogicalDisk(*)\Disk Read Bytes/sec','\LogicalDisk(*)\Disk Write Bytes/sec','\LogicalDisk(*)\Avg. Disk sec/Read','\LogicalDisk(*)\Avg. Disk sec/Write','\LogicalDisk(*)\Avg. Disk Queue Length','\LogicalDisk(*)\Avg. Disk Bytes/Transfer')
$vs=Get-Counter -Counter $lp -SampleInterval 2 -MaxSamples 3 -ErrorAction Stop
$vd=@{}
foreach($s in $vs){
foreach($cs in $s.CounterSamples){
$inst=$cs.InstanceName
if($inst -eq '_total'){continue}
if(-not $vd.ContainsKey($inst)){$vd[$inst]=@{ri=@();wi=@();rb=@();wb=@();rl=@();wl=@();ql=@();io=@()}}
$pth=$cs.Path
if($pth -match 'disk reads/sec'){$vd[$inst].ri+=$cs.CookedValue}
elseif($pth -match 'disk writes/sec'){$vd[$inst].wi+=$cs.CookedValue}
elseif($pth -match 'disk read bytes/sec'){$vd[$inst].rb+=$cs.CookedValue}
elseif($pth -match 'disk write bytes/sec'){$vd[$inst].wb+=$cs.CookedValue}
elseif($pth -match 'avg\. disk sec/read'){$vd[$inst].rl+=$cs.CookedValue}
elseif($pth -match 'avg\. disk sec/write'){$vd[$inst].wl+=$cs.CookedValue}
elseif($pth -match 'avg\. disk queue length'){$vd[$inst].ql+=$cs.CookedValue}
elseif($pth -match 'avg\. disk bytes/transfer'){$vd[$inst].io+=$cs.CookedValue}
}
}
$vol=@($vd.Keys|ForEach-Object{
$k=$_;$v=$vd[$k]
@{
name=$k
read_iops=if($v.ri.Count -gt 0){[math]::Round((A $v.ri),1)}else{$null}
write_iops=if($v.wi.Count -gt 0){[math]::Round((A $v.wi),1)}else{$null}
read_mbps=if($v.rb.Count -gt 0){[math]::Round((A $v.rb)*8/1MB,2)}else{$null}
write_mbps=if($v.wb.Count -gt 0){[math]::Round((A $v.wb)*8/1MB,2)}else{$null}
read_latency_ms=if($v.rl.Count -gt 0){[math]::Round((M $v.rl)*1000,2)}else{$null}
write_latency_ms=if($v.wl.Count -gt 0){[math]::Round((M $v.wl)*1000,2)}else{$null}
queue_length=if($v.ql.Count -gt 0){[math]::Round((A $v.ql),2)}else{$null}
avg_io_size_kb=if($v.io.Count -gt 0){[math]::Round((A $v.io)/1024,1)}else{$null}
vm_names=@(if($vm2.ContainsKey($k)){$vm2[$k]}else{@()})
}
})
}catch{$vol=$null}
@{volumes=$vol}|ConvertTo-Json -Depth 4
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
        raw = self._run_ps(PS_SCRIPT)
        # Volume block chạy WinRM call riêng (xem PS_SCRIPT_VOLUME) — cô lập lỗi ở tầng
        # Python để 1 host cũ chưa hỗ trợ Get-VMHardDiskDrive (hoặc timeout) không làm
        # hỏng phần host/VM chính đã collect thành công.
        try:
            vol_raw = self._run_ps(PS_SCRIPT_VOLUME)
            raw["volumes"] = vol_raw.get("volumes")
        except Exception as exc:
            logger.warning("Device %s: collect volume stats failed: %s", self.device.name, exc)
            raw["volumes"] = None
        return raw

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
            # rtm/wtm/dql/aio: key JSON rút gọn trong PS_SCRIPT (xem comment ở khai báo).
            disk_read_throughput_mbps=host_perf.get("rtm"),
            disk_write_throughput_mbps=host_perf.get("wtm"),
            disk_queue_length=host_perf.get("dql"),
            avg_io_size_kb=host_perf.get("aio"),
            net_mbps_total=host_perf.get("net_mbps_total"),
            extra={"vms": raw.get("vms", []), "volumes": raw.get("volumes") or []},
        )

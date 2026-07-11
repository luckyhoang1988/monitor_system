import time, logging
logging.disable(logging.CRITICAL)

from apps.devices.models import Device
from apps.collectors.switch_snmp import SwitchSNMPCollector, _load_oid_profile

d = Device.objects.get(name__icontains="PFVN_Router")
print(f"Device: {d.name} ({d.ip_address}), type={d.device_type}, vendor={d.vendor}")

c = SwitchSNMPCollector(d)

t0 = time.time()
up = c._snmp_get("1.3.6.1.2.1.1.3.0")
t1 = time.time()
print(f"[1] sysUpTime probe: {t1-t0:.2f}s")

os_f = c.detect_os_family()
t2 = time.time()
print(f"[2] detect_os_family={os_f}: {t2-t1:.2f}s")

prof = _load_oid_profile(os_f)

# CPU/Mem - use the correct method based on os_family
t3 = time.time()
if os_f == "huawei_vrp":
    cpu_val, mem_val = c._collect_cpu_mem_huawei(prof)
elif os_f == "cisco_business":
    cpu_val, mem_val = c._collect_cpu_mem_cisco_business(prof)
else:
    cpu_val = float(c._snmp_get(prof["cpu"]["cpu_5min"]) or 0)
    mem_p = prof.get("memory", {})
    mu = int(c._snmp_get(mem_p.get("mem_used") or mem_p.get("mem_processor_used")) or 0)
    mf = int(c._snmp_get(mem_p.get("mem_free") or mem_p.get("mem_processor_free")) or 1)
    mem_val = mu / (mu + mf) * 100 if (mu + mf) else 0
t4 = time.time()
print(f"[3] CPU/Mem: {t4-t3:.2f}s  cpu={cpu_val:.1f}% mem={mem_val:.1f}%")

ifaces = c._collect_interfaces()
t5 = time.time()
print(f"[4] interfaces ({len(ifaces)}): {t5-t4:.2f}s")

vlans = c._collect_access_vlans(prof)
t6 = time.time()
print(f"[5] access_vlans ({len(vlans)} entries): {t6-t5:.2f}s")

modes = c._collect_port_modes(prof)
t7 = time.time()
print(f"[6] port_modes ({len(modes)} entries): {t7-t6:.2f}s")

print(f"\n{'='*50}")
print(f"TOTAL: {t7-t0:.2f}s")
print(f"  detect+cpu/mem:  {t4-t0:.2f}s")
print(f"  interfaces:      {t5-t4:.2f}s")
print(f"  vlan+port_mode:  {t7-t5:.2f}s")

"""Static, blended AWS-flavored pricing -- a snapshot, not a live pricing API.

Derived from m5.xlarge on-demand list price (4 vCPU / 16 GiB, us-east-1,
~$0.192/hr as of 2026-07). AWS bills the instance, not its parts, so that one
price has to be split between CPU and memory -- each rate gets a share, not
the whole. 65% goes to CPU and 35% to memory, close to OpenCost's default
CPU/RAM ratio:
    CPU: $0.192 * 0.65 / 4 vCPU  = $0.0312 per vCPU-hr
    Mem: $0.192 * 0.35 / 16 GiB  = $0.0042 per GiB-hr
so a full node (4 * 0.0312 + 16 * 0.0042) prices back to exactly $0.192/hr.
A single blended rate (rather than per-instance-type pricing) is the right
amount of precision for this project -- see README for why.
"""

CPU_HOURLY_RATE_PER_CORE = 0.0312  # USD per vCPU-hour
MEM_HOURLY_RATE_PER_GIB = 0.0042  # USD per GiB-hour

HOURS_PER_MONTH = 730  # 365 * 24 / 12, the standard cloud-billing convention

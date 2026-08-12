"""Static, blended AWS-flavored pricing -- a snapshot, not a live pricing API.

Derived from m5.xlarge on-demand list price (4 vCPU / 16 GiB, us-east-1,
~$0.192/hr as of 2026-07): $0.192 / 4 = $/vCPU-hr, $0.192 / 16 = $/GiB-hr.
A single blended rate (rather than per-instance-type pricing) is the right
amount of precision for this project -- see README for why.
"""

CPU_HOURLY_RATE_PER_CORE = 0.048  # USD per vCPU-hour
MEM_HOURLY_RATE_PER_GIB = 0.012  # USD per GiB-hour

HOURS_PER_MONTH = 730  # 365 * 24 / 12, the standard cloud-billing convention

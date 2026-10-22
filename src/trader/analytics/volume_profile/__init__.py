"""Volume Profile（docs/volume_profile_spec.md，决策 0017）。只属于研究版。

    from trader.analytics.volume_profile import ProfileParams, VolumeProfile

    vpf = VolumeProfile()                       # 或 VolumeProfile(ProfileParams(...))
    vpf.get_levels("NVDA", "2026-10-15")                    # 当天开盘前可用的前一日 POC / VAH / VAL
    vpf.get_levels("NVDA", "2026-10-15", "composite", n=5)
    vpf.get_developing("NVDA", "2026-10-15", until="11:00")  # 当天的 dPOC / dVAH / dVAL
    vpf.get_profile("NVDA", "2026-10-01", "2026-10-15")     # 任意区间的分布（研究用）
    vpf.levels_table(["NVDA", "AMD"], "2026-09-01", "2026-09-30")
    vpf.naked_at("NVDA", "2026-10-15")                      # 尚未回测的 POC

纯计算在 trader.core.volume_profile；本包负责读数据、清洗、缓存和时间窗口。
"""

from trader.analytics.volume_profile.params import ProfileParams
from trader.analytics.volume_profile.service import RangeProfile, VolumeProfile, WindowProfile

__all__ = ["ProfileParams", "RangeProfile", "VolumeProfile", "WindowProfile"]

from dataclasses import dataclass
from config.settings import TOP_N, LIVE_TOP_N, SIM_TOP_N, SCAN_SECONDS, LIVE_NO_REPEAT_MIN, MC_SIMS

PREMATCH, LIVE, SIM = "prematch", "live", "sim"


@dataclass(frozen=True)
class ModeSpec:
    name: str
    top_n: int
    every_sec: int
    no_repeat_min: int
    real_games_only: bool
    sims: int = 0


MODES = {
    PREMATCH: ModeSpec(PREMATCH, TOP_N, 1800, 0, True),
    LIVE: ModeSpec(LIVE, LIVE_TOP_N, SCAN_SECONDS, LIVE_NO_REPEAT_MIN, True),
    SIM: ModeSpec(SIM, SIM_TOP_N, 3600, 0, True, MC_SIMS),
}

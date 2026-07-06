from fastapi import APIRouter

from forven import api_core as core
from forven.control_plane import status as control_plane_status
from forven.soak import collect_backend_soak_report

router = APIRouter(tags=["status"])


@router.get("/")
def root():
    return control_plane_status.root()


# Sync `def` on purpose: these do DB reads (scheduler-job scan, kv), and an
# `async def` handler would run them ON the request loop — the liveness check
# itself starving the loop it reports on. Starlette runs sync handlers in the
# threadpool.
@router.get("/api/health")
def health_check():
    return control_plane_status.health_check()


@router.get("/health")
def health_check_compat():
    return control_plane_status.health_check_compat()


@router.get("/api/system/status")
def get_system_status():
    return control_plane_status.get_system_status()


@router.get("/api/system/heartbeat")
def get_system_heartbeat():
    return core.json_safe_payload(control_plane_status.get_system_heartbeat())


@router.get("/api/system/soak-report")
def get_system_soak_report(
    require_exchange_connection: bool = False,
    stale_task_minutes: int = 30,
):
    return collect_backend_soak_report(
        require_exchange_connection=require_exchange_connection,
        stale_task_minutes=stale_task_minutes,
    )


@router.get("/api/dashboard")
def get_dashboard():
    return control_plane_status.get_dashboard(require_account_connection=False)


@router.get("/api/regime")
def get_regime():
    return control_plane_status.get_regime()


@router.get("/api/regime/series")
def get_regime_series(symbol: str, timeframe: str = "1h", bars: int = 1000):
    return control_plane_status.get_regime_series(symbol, timeframe=timeframe, bars=bars)


@router.get("/api/risk")
def get_risk():
    return control_plane_status.get_risk()


@router.get("/api/sentiment")
def get_sentiment():
    return control_plane_status.get_sentiment()


@router.get("/api/equity-history")
def get_equity_history():
    return control_plane_status.get_equity_history()


@router.get("/api/scanner/state")
def get_scanner_state():
    return control_plane_status.get_scanner_state()

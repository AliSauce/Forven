from pydantic import BaseModel, Field

from forven.task_timeouts import recommended_stale_recovery_minutes


class ApprovalDecisionBody(BaseModel):
    actor: str | None = None
    feedback: str | None = None
    reason: str | None = None
    # GO-LIVE-1: approving a paper→live_graduated promotion requires the typed
    # confirmation phrase ("GO LIVE") and an initial per-asset notional ceiling
    # in USD (enforced per live order by forven.exchange.risk).
    confirm: str | None = Field(default=None, max_length=32)
    live_notional_ceiling_usd: float | None = Field(default=None, gt=0)


class ApprovalHandoffBody(BaseModel):
    to_owner: str
    reason: str | None = None


class ApprovalTroubleshootBody(BaseModel):
    agent_id: str | None = Field(default="full-stack-engineer", max_length=128)


class ExecutionModeBody(BaseModel):
    mode: str
    confirm: bool


class SystemModeBody(BaseModel):
    mode: str = Field(..., min_length=1, max_length=32)


class ConfirmBody(BaseModel):
    confirm: bool


class RecoveryRollbackBody(BaseModel):
    confirm: bool
    batch_id: str = Field(..., min_length=1, max_length=128)


class SchedulerJobUpdate(BaseModel):
    schedule_type: str | None = Field(default=None, max_length=16)
    schedule_expr: str | None = Field(default=None, max_length=128)
    enabled: bool | None = None


class QueueProcessingBody(BaseModel):
    process_agent_tasks: bool = True
    process_brain_tasks: bool = True
    recover_stale: bool = True
    stale_minutes: int = recommended_stale_recovery_minutes()
    fail_agents: list[str] = Field(default_factory=list)


class NotificationPreferencesBody(BaseModel):
    updates: dict[str, object]


class NotificationBulkAcknowledgeBody(BaseModel):
    ids: list[int] = Field(default_factory=list)


class NotificationTestBody(BaseModel):
    event_type: str | None = Field(default="system_degraded", max_length=64)


class NotificationRepairTaskBody(BaseModel):
    agent_id: str | None = Field(default="full-stack-engineer", max_length=128)


__all__ = [
    "ApprovalDecisionBody",
    "ApprovalHandoffBody",
    "ApprovalTroubleshootBody",
    "ConfirmBody",
    "ExecutionModeBody",
    "NotificationBulkAcknowledgeBody",
    "NotificationPreferencesBody",
    "NotificationRepairTaskBody",
    "NotificationTestBody",
    "QueueProcessingBody",
    "RecoveryRollbackBody",
    "SchedulerJobUpdate",
]

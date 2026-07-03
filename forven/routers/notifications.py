from fastapi import APIRouter, Depends

from forven.api_security import require_operator_access
from forven.control_plane import notifications as control_plane_notifications
from forven.control_plane.models import (
    NotificationBulkAcknowledgeBody,
    NotificationPreferencesBody,
    NotificationRepairTaskBody,
    NotificationTestBody,
)

router = APIRouter(tags=["notifications"], dependencies=[Depends(require_operator_access)])


@router.get("/api/notifications")
def get_notifications(
    limit: int = 25,
    status: str | None = None,
    severity: str | None = None,
    source: str | None = None,
    event_type: str | None = None,
    group_key: str | None = None,
    before_id: int | None = None,
    actionable: bool = False,
):
    return control_plane_notifications.get_notifications_list(
        limit=limit,
        status=status,
        severity=severity,
        source=source,
        event_type=event_type,
        group_key=group_key,
        before_id=before_id,
        actionable=actionable,
    )


@router.get("/api/notifications/grouped")
def get_notifications_grouped(
    limit: int = 25,
    status: str | None = None,
    severity: str | None = None,
    source: str | None = None,
    event_type: str | None = None,
    cursor: str | None = None,
):
    return control_plane_notifications.get_notifications_grouped(
        limit=limit,
        status=status,
        severity=severity,
        source=source,
        event_type=event_type,
        cursor=cursor,
    )


@router.post("/api/notifications/{notification_id}/acknowledge")
def post_notification_acknowledge(notification_id: int):
    return control_plane_notifications.post_notification_acknowledge(notification_id)


@router.post("/api/notifications/acknowledge-all")
def post_notifications_acknowledge_all(body: NotificationBulkAcknowledgeBody):
    return control_plane_notifications.post_notifications_acknowledge_all(body)


@router.post("/api/notifications/{notification_id}/repair-task")
def post_notification_repair_task(notification_id: int, body: NotificationRepairTaskBody | None = None):
    return control_plane_notifications.post_notification_repair_task(notification_id, body)


@router.get("/api/notifications/{notification_id}/deliveries")
def get_notification_deliveries(notification_id: int, limit: int = 20):
    return control_plane_notifications.get_notification_delivery_history(notification_id, limit=limit)


@router.post("/api/notifications/{notification_id}/resend")
def post_notification_resend(notification_id: int):
    return control_plane_notifications.post_notification_resend(notification_id)


@router.get("/api/notifications/preferences")
def get_notifications_preferences():
    return control_plane_notifications.get_notifications_preferences()


@router.put("/api/notifications/preferences")
def put_notifications_preferences(body: NotificationPreferencesBody):
    return control_plane_notifications.put_notifications_preferences(body)


@router.post("/api/notifications/test")
def post_notification_test(body: NotificationTestBody):
    return control_plane_notifications.post_notification_test(body)

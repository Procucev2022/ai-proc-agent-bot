"""
Dashboard API Endpoints.

Provides REST and real-time streaming endpoints (Server-Sent Events / SSE)
for the live executive analytics dashboard.
"""

import asyncio
import json
import logging
from typing import Optional
from fastapi import APIRouter, Request, Query, Depends, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db_session
from app.config import get_settings
from app.services.dashboard_aggregation_service import DashboardAggregationService
from app.services.realtime_analytics_service import get_realtime_analytics_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard"])


def require_dashboard_operator(x_dashboard_key: Optional[str] = Header(None)) -> None:
    """Require the configured operator key for dashboard data."""
    expected = get_settings().dashboard_api_key
    if not expected or x_dashboard_key != expected:
        raise HTTPException(status_code=401, detail="Dashboard authentication required")


def get_dashboard_db():
    db = get_db_session()
    try:
        yield db
    finally:
        db.close()


router.dependencies.append(Depends(require_dashboard_operator))


@router.get("/stats")
async def get_dashboard_stats(
    date_preset: str = Query("today", description="today, yesterday, 7d, 30d, 90d, custom"),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD for custom range"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD for custom range"),
    role: Optional[str] = Query("all", description="all, buyer, seller"),
    category: Optional[str] = Query(None, description="Filter by product category"),
    location: Optional[str] = Query(None, description="Filter by geographic location"),
    rfq_status: Optional[str] = Query(None, description="Filter by RFQ status"),
    db: Session = Depends(get_dashboard_db)
):
    """
    Retrieve comprehensive aggregated KPIs, funnels, and breakdown metrics.
    """
    try:
        service = DashboardAggregationService(db_session=db)
        stats = await service.get_dashboard_stats(
            date_preset=date_preset,
            start_date=start_date,
            end_date=end_date,
            role=role,
            category=category,
            location=location,
            rfq_status=rfq_status,
        )
        return JSONResponse(content=stats)
    except Exception as e:
        logger.error(f"[DASHBOARD_API] Error computing stats: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": f"Failed to compute dashboard stats: {str(e)}"}
        )


@router.get("/export-user-classification-csv")
async def export_user_classification_csv(
    date_preset: str = Query("today", description="today, yesterday, 7d, 30d, 90d, custom"),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD for custom range"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD for custom range"),
    filter_type: str = Query("all", description="all, unknown, buyer, buyer_registered, buyer_not_registered, seller, seller_registered, seller_not_registered, seller_subscribed, seller_without_subscription"),
    db: Session = Depends(get_dashboard_db)
):
    """
    Export phone numbers and classification metrics for User Classification & Funnel as a CSV file.
    """
    try:
        service = DashboardAggregationService(db_session=db)
        csv_content = await asyncio.to_thread(
            service.export_user_classification_csv,
            date_preset,
            start_date,
            end_date,
            filter_type
        )
        filename = f"user_classification_{filter_type}_{date_preset}.csv"
        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        logger.error(f"[DASHBOARD_API] Error exporting CSV: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": f"Failed to export CSV: {str(e)}"}
        )



@router.get("/user-classification-details")
async def get_user_classification_details(
    date_preset: str = Query("today", description="today, yesterday, 7d, 30d, 90d, custom"),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD for custom range"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD for custom range"),
    filter_type: str = Query("all", description="all, unknown, buyer, buyer_registered, buyer_not_registered, seller, seller_registered, seller_not_registered, seller_subscribed, seller_without_subscription"),
    db: Session = Depends(get_dashboard_db)
):
    """
    Retrieve structured user classification details JSON for the details view.
    """
    try:
        service = DashboardAggregationService(db_session=db)
        data = await asyncio.to_thread(
            service.get_user_classification_details_json,
            date_preset,
            start_date,
            end_date,
            filter_type
        )
        return data
    except Exception as e:
        logger.error(f"[DASHBOARD_API] Error fetching classification details: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": f"Failed to fetch user classification details: {str(e)}"}
        )


@router.get("/active-users")
async def get_active_users():
    """
    Retrieve snapshot of currently active WhatsApp visitors.
    """
    try:
        service = get_realtime_analytics_service()
        counts = await service.get_active_users_count()
        return JSONResponse(content={"status": "success", "active_users": counts})
    except Exception as e:
        logger.error(f"[DASHBOARD_API] Error fetching active users: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@router.get("/feed")
async def get_recent_feed(limit: int = Query(50, ge=1, le=100)):
    """
    Retrieve recent live activity stream events.
    """
    try:
        service = get_realtime_analytics_service()
        feed = await service.get_recent_feed(limit=limit)
        return JSONResponse(content={"status": "success", "feed": feed})
    except Exception as e:
        logger.error(f"[DASHBOARD_API] Error fetching feed: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@router.get("/live-stream")
async def live_events_stream(request: Request):
    """
    Server-Sent Events (SSE) endpoint streaming real-time events to connected clients.
    """
    service = get_realtime_analytics_service()

    async def event_generator():
        try:
            # Yield initial connection confirmation
            init_payload = json.dumps({
                "event_type": "stream_connected",
                "message": "Connected to real-time analytics event stream",
                "timestamp": asyncio.get_event_loop().time()
            })
            yield f"data: {init_payload}\n\n"

            async for event in service.subscribe_events():
                if await request.is_disconnected():
                    break
                payload = json.dumps(event)
                yield f"data: {payload}\n\n"
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"[DASHBOARD_SSE] Stream error: {e}")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


@router.get("/export")
async def export_dashboard_data(
    date_preset: str = Query("today"),
    db: Session = Depends(get_dashboard_db)
):
    """
    Export current dashboard data as JSON.
    """
    try:
        service = DashboardAggregationService(db_session=db)
        stats = await service.get_dashboard_stats(date_preset=date_preset)
        return JSONResponse(
            content=stats,
            headers={"Content-Disposition": f"attachment; filename=procurement_analytics_{date_preset}.json"}
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@router.get("/daily-visitors")
async def get_daily_visitors(
    date_preset: str = Query("7d", description="today, yesterday, 7d, 30d, custom"),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD for custom range"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD for custom range"),
    db: Session = Depends(get_dashboard_db)
):
    """
    Return day-wise unique visitor counts over the selected period.
    All DB work runs in a thread pool to avoid blocking the async event loop.
    Response: {"status": "success", "data": [{"date": "YYYY-MM-DD", "visitors": N}, ...]}
    """
    try:
        service = DashboardAggregationService(db_session=db)
        data = await asyncio.to_thread(
            service.get_daily_visitors,
            date_preset,
            start_date,
            end_date,
        )
        return JSONResponse(content={"status": "success", "data": data})
    except Exception as e:
        logger.error(f"[DASHBOARD_API] Error fetching daily visitors: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@router.get("/today-conversations")
async def get_today_conversations(
    db: Session = Depends(get_dashboard_db)
):
    """
    Return the list of users who visited or interacted today for the live chat viewer.
    """
    try:
        service = DashboardAggregationService(db_session=db)
        conversations = await service.get_today_conversations()
        return JSONResponse(content={"status": "success", "data": conversations})
    except Exception as e:
        logger.error(f"[DASHBOARD_API] Error fetching today conversations: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@router.get("/conversation-messages")
async def get_conversation_messages(
    phone: Optional[str] = Query(None, description="User phone number"),
    session_id: Optional[str] = Query(None, description="Specific session ID"),
    db: Session = Depends(get_dashboard_db)
):
    """
    Return the full interactive chat message history for the selected user/session.
    """
    try:
        service = DashboardAggregationService(db_session=db)
        result = await service.get_conversation_messages(phone=phone, session_id=session_id)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"[DASHBOARD_API] Error fetching conversation messages: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

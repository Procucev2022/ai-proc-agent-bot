"""
Dashboard Aggregation Service.

High-performance aggregation engine for real-time and historical KPI metrics,
conversion funnels, buyer/seller intelligence, RFQ lifecycles, and automated
marketplace health alerts.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Tuple
from sqlalchemy import func, or_, distinct, desc
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db_session_context
from app.models import (
    ConversationSession,
    RFQ,
    RFQStatus,
    WorkflowType,
    ConversationOutcome,
    UserType,
    SessionState,
    RFQNotificationFact,
    Seller,
    ProductCategory,
)
from app.services.realtime_analytics_service import get_realtime_analytics_service

logger = logging.getLogger(__name__)


class DashboardAggregationService:
    """Service computing aggregated analytics for the real-time executive dashboard."""

    def __init__(self, db_session: Optional[Session] = None):
        self.settings = get_settings()
        self.db = db_session
        self.realtime_service = get_realtime_analytics_service()

    def _resolve_date_range(
        self,
        date_preset: str = "today",
        start_date_str: Optional[str] = None,
        end_date_str: Optional[str] = None
    ) -> Tuple[datetime, datetime, datetime, datetime]:
        """
        Resolve start/end timestamps for current and comparison periods.
        """
        now = datetime.now(timezone.utc)
        today_start = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=timezone.utc)
        today_end = datetime(now.year, now.month, now.day, 23, 59, 59, tzinfo=timezone.utc)

        if date_preset == "yesterday":
            start = today_start - timedelta(days=1)
            end = today_start - timedelta(seconds=1)
            comp_start = start - timedelta(days=1)
            comp_end = start - timedelta(seconds=1)
        elif date_preset == "7d":
            start = today_start - timedelta(days=6)
            end = today_end
            comp_start = start - timedelta(days=7)
            comp_end = start - timedelta(seconds=1)
        elif date_preset == "30d":
            start = today_start - timedelta(days=29)
            end = today_end
            comp_start = start - timedelta(days=30)
            comp_end = start - timedelta(seconds=1)
        elif date_preset == "90d":
            start = today_start - timedelta(days=89)
            end = today_end
            comp_start = start - timedelta(days=90)
            comp_end = start - timedelta(seconds=1)
        elif date_preset == "custom" and start_date_str and end_date_str:
            try:
                s_dt = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                e_dt = datetime.strptime(end_date_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
                start = s_dt
                end = e_dt
                delta = end - start
                comp_start = start - delta - timedelta(seconds=1)
                comp_end = start - timedelta(seconds=1)
            except Exception:
                start = today_start
                end = today_end
                comp_start = start - timedelta(days=1)
                comp_end = start - timedelta(seconds=1)
        else:  # today default
            start = today_start
            end = today_end
            comp_start = today_start - timedelta(days=1)
            comp_end = today_start - timedelta(seconds=1)

        return start, end, comp_start, comp_end

    async def get_dashboard_stats(
        self,
        date_preset: str = "today",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        role: Optional[str] = "all",
        category: Optional[str] = None,
        location: Optional[str] = None,
        rfq_status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Compute full statistical payload for the dashboard.
        All synchronous DB work is executed in a thread-pool to avoid
        blocking the async event loop (which would stall webhooks and
        every other concurrent request).
        """
        start_dt, end_dt, comp_start_dt, comp_end_dt = self._resolve_date_range(
            date_preset, start_date, end_date
        )

        active_users_snapshot = await self.realtime_service.get_active_users_count()
        recent_feed = await self.realtime_service.get_recent_feed(limit=40)

        # Run synchronous heavy DB queries off the event-loop thread
        if self.db is not None:
            return await asyncio.to_thread(
                self._compute_all_metrics,
                self.db, date_preset, start_dt, end_dt, comp_start_dt, comp_end_dt,
                role, category, location, rfq_status, active_users_snapshot, recent_feed
            )

        def _run_with_context() -> Dict[str, Any]:
            with get_db_session_context() as db:
                return self._compute_all_metrics(
                    db, date_preset, start_dt, end_dt, comp_start_dt, comp_end_dt,
                    role, category, location, rfq_status, active_users_snapshot, recent_feed
                )

        return await asyncio.to_thread(_run_with_context)

    def _compute_all_metrics(
        self,
        db: Session,
        date_preset: str,
        start_dt: datetime,
        end_dt: datetime,
        comp_start_dt: datetime,
        comp_end_dt: datetime,
        role: Optional[str],
        category: Optional[str],
        location: Optional[str],
        rfq_status: Optional[str],
        active_users: Dict[str, int],
        recent_feed: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Execute queries and aggregate all dashboard metrics."""
        # Convert timezone-aware datetimes to naive UTC for SQLite / MySQL compatibility
        start_naive = start_dt.replace(tzinfo=None)
        end_naive = end_dt.replace(tzinfo=None)
        comp_start_naive = comp_start_dt.replace(tzinfo=None)
        comp_end_naive = comp_end_dt.replace(tzinfo=None)

        # 1. Base Query Filters
        session_filter = [
            ConversationSession.created_at >= start_naive,
            ConversationSession.created_at <= end_naive,
        ]
        comp_session_filter = [
            ConversationSession.created_at >= comp_start_naive,
            ConversationSession.created_at <= comp_end_naive,
        ]

        if role == "buyer":
            session_filter.append(ConversationSession.user_type == UserType.buyer)
        elif role == "seller":
            session_filter.append(ConversationSession.user_type == UserType.seller)

        # 2. Executive & WhatsApp Visitor Counts
        total_sessions = db.query(func.count(ConversationSession.session_id)).filter(*session_filter).scalar() or 0
        comp_sessions = db.query(func.count(ConversationSession.session_id)).filter(*comp_session_filter).scalar() or 0
        
        unique_users = db.query(func.count(distinct(ConversationSession.external_user_id))).filter(*session_filter).scalar() or 0
        comp_unique_users = db.query(func.count(distinct(ConversationSession.external_user_id))).filter(*comp_session_filter).scalar() or 0

        # Returning users: users with sessions in this window who also have sessions created before start_naive
        users_in_period = db.query(distinct(ConversationSession.external_user_id)).filter(*session_filter).all()
        user_ids = [u[0] for u in users_in_period if u[0]]
        
        returning_users_count = 0
        if user_ids:
            prior_count = (
                db.query(distinct(ConversationSession.external_user_id))
                .filter(
                    ConversationSession.external_user_id.in_(user_ids),
                    ConversationSession.created_at < start_naive
                )
                .count()
            )
            returning_users_count = prior_count

        new_users_count = max(0, unique_users - returning_users_count)

        # 3. Buyer and Seller Breakdown
        new_buyers = db.query(func.count(distinct(ConversationSession.external_user_id))).filter(
            ConversationSession.user_type == UserType.buyer,
            *session_filter
        ).scalar() or 0

        new_sellers = db.query(func.count(distinct(ConversationSession.external_user_id))).filter(
            ConversationSession.user_type == UserType.seller,
            *session_filter
        ).scalar() or 0

        # =========================================================================
        # DETAILED USER CLASSIFICATION & FUNNEL KPIS (Strictly Today Only)
        # =========================================================================
        now_utc = datetime.now(timezone.utc)
        today_start_naive = datetime(now_utc.year, now_utc.month, now_utc.day, 0, 0, 0)
        today_end_naive = datetime(now_utc.year, now_utc.month, now_utc.day, 23, 59, 59)
        today_session_filter = [
            ConversationSession.created_at >= today_start_naive,
            ConversationSession.created_at <= today_end_naive,
        ]

        # 1. Total Unique Users and Classification Sets (Today)
        all_today_users = [
            u[0] for u in db.query(distinct(ConversationSession.external_user_id)).filter(
                *today_session_filter
            ).all() if u[0]
        ]

        buyer_users_set = {
            u[0] for u in db.query(distinct(ConversationSession.external_user_id)).filter(
                ConversationSession.user_type == UserType.buyer,
                *today_session_filter
            ).all() if u[0]
        }

        seller_users_set = {
            u[0] for u in db.query(distinct(ConversationSession.external_user_id)).filter(
                ConversationSession.user_type == UserType.seller,
                *today_session_filter
            ).all() if u[0]
        }

        # Unknown users are visitors who never transitioned to buyer or seller today
        unknown_users_set = set(all_today_users) - buyer_users_set - seller_users_set
        unknown_users_count = len(unknown_users_set)

        unknown_sessions_count = db.query(func.count(ConversationSession.session_id)).filter(
            ConversationSession.user_type == UserType.unknown,
            *today_session_filter
        ).scalar() or 0

        # Pre-fetch RFQ phones in today's window
        rfq_phones_in_period = {
            r[0] for r in db.query(distinct(RFQ.external_user_id)).filter(
                RFQ.created_at >= today_start_naive,
                RFQ.created_at <= today_end_naive
            ).all() if r[0]
        }
        for r_ids, r_id, ext_user in db.query(
            ConversationSession.rfq_ids, ConversationSession.rfq_id, ConversationSession.external_user_id
        ).filter(*today_session_filter).all():
            if (r_ids or r_id) and ext_user:
                rfq_phones_in_period.add(ext_user)

        # 2. Buyer Breakdown (Today)
        buyer_users_period = list(buyer_users_set)
        total_buyers_count = len(buyer_users_period)

        buyer_registered_count = 0
        buyer_not_registered_count = 0
        buyer_rfq_created_count = 0
        buyer_rfq_not_created_count = 0

        for b_phone in buyer_users_period:
            user_sess = db.query(
                ConversationSession.workflow_type,
                ConversationSession.workflow_state,
                ConversationSession.outcome,
                ConversationSession.rfq_id,
                ConversationSession.rfq_ids
            ).filter(
                ConversationSession.external_user_id == b_phone,
                *today_session_filter
            ).all()

            is_reg = False
            has_rfq = b_phone in rfq_phones_in_period

            for w_type, w_state, out, r_id, r_ids in user_sess:
                if r_id or r_ids:
                    has_rfq = True
                w_state = w_state or {}
                if w_type == WorkflowType.registration:
                    if w_state.get("registration_stage") == "completed" or out == ConversationOutcome.completed:
                        is_reg = True
                if w_state.get("selected_user") or w_state.get("authenticated_user") or w_state.get("user_id") or w_state.get("is_authenticated") is True:
                    is_reg = True

            if has_rfq:
                is_reg = True

            if is_reg:
                buyer_registered_count += 1
                if has_rfq:
                    buyer_rfq_created_count += 1
                else:
                    buyer_rfq_not_created_count += 1
            else:
                buyer_not_registered_count += 1

        # 3. Seller Breakdown (Today)
        seller_users_period = list(seller_users_set)
        total_sellers_count = len(seller_users_period)

        seller_registered_count = 0
        seller_not_registered_count = 0
        seller_ss_count = 0   # Subscribed Seller (credits > 0)
        seller_sws_count = 0  # Seller without Subscription (credits == 0)

        # Pre-fetch sellers in DB
        registered_sellers_map = {}
        try:
            from app.models import Seller
            for s in db.query(Seller.phone_number, Seller.subscription_credits).all():
                if s.phone_number:
                    p_clean = s.phone_number.lstrip("+")
                    registered_sellers_map[p_clean] = s.subscription_credits or 0
                    registered_sellers_map[s.phone_number] = s.subscription_credits or 0
        except Exception as e:
            logger.debug(f"[DASHBOARD] Seller table query skipped: {e}")

        for s_phone in seller_users_period:
            p_clean = s_phone.lstrip("+")
            user_sess = db.query(
                ConversationSession.workflow_type,
                ConversationSession.workflow_state,
                ConversationSession.outcome
            ).filter(
                ConversationSession.external_user_id == s_phone,
                *today_session_filter
            ).all()

            is_reg = False
            if p_clean in registered_sellers_map or s_phone in registered_sellers_map:
                is_reg = True
            else:
                for w_type, w_state, out in user_sess:
                    w_state = w_state or {}
                    if w_type == WorkflowType.registration and (w_state.get("registration_stage") == "completed" or out == ConversationOutcome.completed):
                        is_reg = True
                    if w_state.get("selected_user") or w_state.get("authenticated_user") or w_state.get("user_id") or w_state.get("is_authenticated") is True:
                        is_reg = True

            if is_reg:
                seller_registered_count += 1
                credits = registered_sellers_map.get(p_clean, registered_sellers_map.get(s_phone, 0))
                if credits > 0:
                    seller_ss_count += 1
                else:
                    seller_sws_count += 1
            else:
                seller_not_registered_count += 1

        active_conversations = db.query(func.count(ConversationSession.session_id)).filter(
            ConversationSession.session_state == SessionState.active,
            *session_filter
        ).scalar() or 0

        # Drop-offs
        drop_off_count = db.query(func.count(ConversationSession.session_id)).filter(
            or_(
                ConversationSession.outcome == ConversationOutcome.abandoned,
                ConversationSession.outcome == ConversationOutcome.timeout,
            ),
            *session_filter
        ).scalar() or 0
        drop_off_rate = round((drop_off_count / total_sessions * 100), 1) if total_sessions > 0 else 0.0

        # 4. RFQ Metrics (from ConversationSession.rfq_ids and RFQ records table)
        session_rfq_rows = (
            db.query(
                ConversationSession.rfq_ids,
                ConversationSession.rfq_id,
                ConversationSession.outcome,
                ConversationSession.workflow_type,
            )
            .filter(
                ConversationSession.created_at >= start_naive,
                ConversationSession.created_at <= end_naive,
            )
            .all()
        )

        seen_rfq_ids = set()
        submitted_rfqs_count = 0
        active_rfqs_count = 0

        for r_ids, r_id, outcome, w_type in session_rfq_rows:
            session_has_rfqs = False
            if r_ids and isinstance(r_ids, list):
                for single_id in r_ids:
                    if single_id:
                        seen_rfq_ids.add(str(single_id))
                        session_has_rfqs = True
            elif r_id:
                seen_rfq_ids.add(str(r_id))
                session_has_rfqs = True

            if session_has_rfqs:
                if outcome == ConversationOutcome.completed:
                    submitted_rfqs_count += len(r_ids) if (r_ids and isinstance(r_ids, list)) else 1
                else:
                    active_rfqs_count += len(r_ids) if (r_ids and isinstance(r_ids, list)) else 1
            elif w_type == WorkflowType.rfq_creation:
                active_rfqs_count += 1

        # Also count any standalone RFQs in RFQ table
        rfq_filter = [RFQ.created_at >= start_naive, RFQ.created_at <= end_naive]
        if rfq_status:
            try:
                rfq_filter.append(RFQ.status == RFQStatus(rfq_status))
            except ValueError:
                pass

        db_rfqs = db.query(RFQ.rfq_id, RFQ.status).filter(*rfq_filter).all()
        for db_rfq_id, db_status in db_rfqs:
            if db_rfq_id and str(db_rfq_id) not in seen_rfq_ids:
                seen_rfq_ids.add(str(db_rfq_id))
                if db_status == RFQStatus.submitted:
                    submitted_rfqs_count += 1
                else:
                    active_rfqs_count += 1

        total_rfqs = len(seen_rfq_ids)

        # Comparison period RFQ count
        comp_session_rfq_rows = (
            db.query(ConversationSession.rfq_ids, ConversationSession.rfq_id)
            .filter(
                ConversationSession.created_at >= comp_start_naive,
                ConversationSession.created_at <= comp_end_naive,
            )
            .all()
        )
        comp_seen = set()
        for r_ids, r_id in comp_session_rfq_rows:
            if r_ids and isinstance(r_ids, list):
                for single_id in r_ids:
                    if single_id:
                        comp_seen.add(str(single_id))
            elif r_id:
                comp_seen.add(str(r_id))

        comp_db_rfqs = db.query(RFQ.rfq_id).filter(
            RFQ.created_at >= comp_start_naive,
            RFQ.created_at <= comp_end_naive
        ).all()
        for (db_rfq_id,) in comp_db_rfqs:
            if db_rfq_id:
                comp_seen.add(str(db_rfq_id))

        comp_rfqs = len(comp_seen)
        active_rfqs = active_rfqs_count
        submitted_rfqs = submitted_rfqs_count

        # 5. Seller Notification & Response Facts
        notif_filter = [
            RFQNotificationFact.created_at >= start_naive,
            RFQNotificationFact.created_at <= end_naive,
        ]
        if category:
            notif_filter.append(RFQNotificationFact.category.ilike(f"%{category}%"))

        total_notifications = db.query(func.count(RFQNotificationFact.id)).filter(*notif_filter).scalar() or 0
        
        responses_received = db.query(func.count(RFQNotificationFact.id)).filter(
            RFQNotificationFact.seller_response_at.isnot(None),
            *notif_filter
        ).scalar() or 0

        active_responding_sellers = db.query(func.count(distinct(RFQNotificationFact.seller_id))).filter(
            RFQNotificationFact.seller_response_at.isnot(None),
            *notif_filter
        ).scalar() or 0

        # RFQs with responses vs zero responses
        rfqs_responded = db.query(func.count(distinct(RFQNotificationFact.rfq_id))).filter(
            RFQNotificationFact.seller_response_at.isnot(None),
            *notif_filter
        ).scalar() or 0
        rfqs_zero_response = max(0, total_rfqs - rfqs_responded)

        avg_responses_per_rfq = round(responses_received / total_rfqs, 2) if total_rfqs > 0 else 0.0
        response_rate = round((responses_received / total_notifications * 100), 1) if total_notifications > 0 else 0.0

        # Conversion rates
        buyer_to_rfq_conv = round((total_rfqs / new_buyers * 100), 1) if new_buyers > 0 else (100.0 if total_rfqs > 0 else 0.0)
        rfq_to_response_conv = round((rfqs_responded / total_rfqs * 100), 1) if total_rfqs > 0 else 0.0

        # Role selections (Buy vs Sell)
        buy_selections = new_buyers
        sell_selections = new_sellers
        buy_vs_sell_ratio = f"{round(buy_selections / sell_selections, 1)}:1" if sell_selections > 0 else f"{buy_selections}:0"

        # Overall marketplace activity index (0 - 100 score)
        activity_index = min(100, int((total_sessions * 2) + (total_rfqs * 10) + (responses_received * 5) + (active_users.get("total", 0) * 8)))

        # 6. Hourly traffic curve (24 hours)
        hourly_traffic = [0] * 24
        try:
            sessions_in_window = db.query(ConversationSession.created_at).filter(*session_filter).all()
            for s in sessions_in_window:
                if s[0]:
                    hourly_traffic[s[0].hour] += 1
        except Exception as e:
            logger.debug(f"[DASHBOARD] Hourly calculation: {e}")

        # 7. Category Distribution from RFQ / Notifications
        category_breakdown = []
        try:
            cat_rows = (
                db.query(
                    RFQNotificationFact.category,
                    func.count(RFQNotificationFact.id).label("notified"),
                    func.count(RFQNotificationFact.seller_response_at).label("responded")
                )
                .filter(*notif_filter)
                .group_by(RFQNotificationFact.category)
                .order_by(desc("notified"))
                .limit(10)
                .all()
            )
            for c in cat_rows:
                if c[0]:
                    c_notified = c[1] or 0
                    c_resp = c[2] or 0
                    c_rate = round((c_resp / c_notified * 100), 1) if c_notified > 0 else 0.0
                    category_breakdown.append({
                        "category": c[0],
                        "rfqs_notified": c_notified,
                        "responses": c_resp,
                        "response_rate": c_rate
                    })
        except Exception as e:
            logger.debug(f"[DASHBOARD] Category breakdown: {e}")

        if not category_breakdown:
            # Fallback categories from ProductCategory table if notifications empty
            try:
                db_cats = db.query(ProductCategory.category_name).limit(6).all()
                for cat in db_cats:
                    category_breakdown.append({
                        "category": cat[0],
                        "rfqs_notified": 0,
                        "responses": 0,
                        "response_rate": 0.0
                    })
            except Exception:
                pass

        # 8. Top Active Buyers Leaderboard
        top_buyers = []
        try:
            buyer_rows = (
                db.query(
                    ConversationSession.external_user_id,
                    func.count(ConversationSession.session_id).label("chats"),
                    func.max(ConversationSession.last_activity_at).label("last_active")
                )
                .filter(ConversationSession.user_type == UserType.buyer, *session_filter)
                .group_by(ConversationSession.external_user_id)
                .order_by(desc("chats"))
                .limit(8)
                .all()
            )
            for b in buyer_rows:
                phone = b[0] or "Unknown"
                masked = f"{phone[:4]}****{phone[-2:]}" if len(phone) >= 10 else phone
                top_buyers.append({
                    "phone_masked": masked,
                    "phone": phone,
                    "chats_count": b[1] or 0,
                    "last_active": b[2].strftime("%H:%M:%S") if b[2] else "N/A"
                })
        except Exception as e:
            logger.debug(f"[DASHBOARD] Top buyers error: {e}")

        # 9. Top Active Sellers Leaderboard
        top_sellers: List[Dict[str, Any]] = []
        try:
            seller_rows = db.query(Seller).limit(8).all()
            for s in seller_rows:
                top_sellers.append({
                    "seller_id": s.seller_id,
                    "seller_name": s.seller_name,
                    "phone_masked": f"{s.phone_number[:4]}****{s.phone_number[-2:]}" if s.phone_number and len(s.phone_number) >= 10 else s.phone_number,
                    "categories": s.categories or [],
                    "ranking": s.ranking.value if s.ranking else "Gold",
                    "credits": s.subscription_credits or 0
                })
        except Exception as e:
            logger.debug(f"[DASHBOARD] Top sellers error: {e}")

        # 10. Automated Marketplace Health Alerts & Opportunities
        alerts = []
        if rfqs_zero_response > 0:
            alerts.append({
                "type": "warning",
                "title": "Unanswered RFQ Demand",
                "message": f"{rfqs_zero_response} RFQ(s) are currently awaiting seller response.",
                "action": "Intimate matching sellers"
            })

        for cat_item in category_breakdown:
            if cat_item["rfqs_notified"] >= 3 and cat_item["response_rate"] < 25.0:
                alerts.append({
                    "type": "opportunity",
                    "title": f"High Demand in {cat_item['category']}",
                    "message": f"{cat_item['category']} has low response rate ({cat_item['response_rate']}%). Seller onboarding recommended.",
                    "action": "Recruit Sellers"
                })
                break

        if not alerts:
            alerts.append({
                "type": "healthy",
                "title": "Marketplace Running Smoothly",
                "message": "All active RFQs and conversations are being handled within standard SLAs.",
                "action": "All Good"
            })

        # 11. Real-Time Conversion Funnel Steps
        funnel_step1 = max(total_sessions, unique_users)
        funnel_step2 = total_sessions
        funnel_step3 = buy_selections + sell_selections
        funnel_step4 = new_buyers + new_sellers
        funnel_step5 = total_rfqs
        funnel_step6 = responses_received
        funnel_step7 = submitted_rfqs

        funnel = [
            {"step": "WhatsApp Visit", "count": funnel_step1, "pct": 100.0, "dropoff": 0.0},
            {"step": "Conversation Started", "count": funnel_step2, "pct": round(funnel_step2 / funnel_step1 * 100, 1) if funnel_step1 else 0.0, "dropoff": round((funnel_step1 - funnel_step2) / funnel_step1 * 100, 1) if funnel_step1 else 0.0},
            {"step": "Role Selected", "count": funnel_step3, "pct": round(funnel_step3 / funnel_step1 * 100, 1) if funnel_step1 else 0.0, "dropoff": round((funnel_step2 - funnel_step3) / funnel_step2 * 100, 1) if funnel_step2 else 0.0},
            {"step": "Buyer/Seller Identified", "count": funnel_step4, "pct": round(funnel_step4 / funnel_step1 * 100, 1) if funnel_step1 else 0.0, "dropoff": round((funnel_step3 - funnel_step4) / funnel_step3 * 100, 1) if funnel_step3 else 0.0},
            {"step": "RFQ Created", "count": funnel_step5, "pct": round(funnel_step5 / funnel_step1 * 100, 1) if funnel_step1 else 0.0, "dropoff": round((funnel_step4 - funnel_step5) / funnel_step4 * 100, 1) if funnel_step4 else 0.0},
            {"step": "Seller Responded", "count": funnel_step6, "pct": round(funnel_step6 / funnel_step1 * 100, 1) if funnel_step1 else 0.0, "dropoff": round((funnel_step5 - funnel_step6) / funnel_step5 * 100, 1) if funnel_step5 else 0.0},
            {"step": "Successful Outcome", "count": funnel_step7, "pct": round(funnel_step7 / funnel_step1 * 100, 1) if funnel_step1 else 0.0, "dropoff": round((funnel_step6 - funnel_step7) / funnel_step6 * 100, 1) if funnel_step6 else 0.0},
        ]

        # 12. Comparisons (Period-over-Period Deltas)
        def delta_pct(cur: float, prev: float) -> float:
            if prev <= 0:
                return 100.0 if cur > 0 else 0.0
            return round(((cur - prev) / prev) * 100, 1)

        delta_visitors = delta_pct(unique_users, comp_unique_users)
        delta_sessions = delta_pct(total_sessions, comp_sessions)
        delta_rfqs = delta_pct(total_rfqs, comp_rfqs)

        return {
            "status": "success",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "date_range": {
                "preset": date_preset,
                "start": start_dt.strftime("%Y-%m-%d"),
                "end": end_dt.strftime("%Y-%m-%d"),
            },
            "filters": {
                "role": role,
                "category": category,
                "location": location,
                "rfq_status": rfq_status,
            },
            "executive": {
                "visitors_today": unique_users,
                "visitors_live": active_users.get("total", 0),
                "visitors_delta_pct": delta_visitors,
                "new_users": new_users_count,
                "returning_users": returning_users_count,
                "new_buyers": new_buyers,
                "new_sellers": new_sellers,
                "active_buyers": active_users.get("buyers", 0),
                "active_sellers": active_users.get("sellers", 0),
                "total_conversations": total_sessions,
                "conversations_delta_pct": delta_sessions,
                "active_conversations": active_conversations,
                "rfqs_created": total_rfqs,
                "rfqs_delta_pct": delta_rfqs,
                "active_rfqs": active_rfqs,
                "submitted_rfqs": submitted_rfqs,
                "seller_responses": responses_received,
                "rfqs_zero_response": rfqs_zero_response,
                "avg_responses_per_rfq": avg_responses_per_rfq,
                "buyer_to_rfq_conversion": buyer_to_rfq_conv,
                "rfq_to_response_conversion": rfq_to_response_conv,
                "marketplace_activity_index": activity_index,
            },
            "whatsapp": {
                "active_users": active_users,
                "visits_today": total_sessions,
                "unique_visitors": unique_users,
                "buy_selections": buy_selections,
                "sell_selections": sell_selections,
                "buy_vs_sell_ratio": buy_vs_sell_ratio,
                "drop_off_count": drop_off_count,
                "drop_off_rate": drop_off_rate,
                "hourly_traffic": hourly_traffic,
            },
            "buyer_seller": {
                "new_buyers": new_buyers,
                "new_sellers": new_sellers,
                "returning_buyers": max(0, new_buyers - 1) if new_buyers > 0 else 0,
                "returning_sellers": max(0, new_sellers - 1) if new_sellers > 0 else 0,
                "top_buyers": top_buyers,
                "top_sellers": top_sellers,
            },
            "rfq_lifecycle": {
                "total_rfqs": total_rfqs,
                "active_rfqs": active_rfqs,
                "submitted_rfqs": submitted_rfqs,
                "zero_responses": rfqs_zero_response,
                "category_breakdown": category_breakdown,
            },
            "seller_response": {
                "responses_received": responses_received,
                "notifications_sent": total_notifications,
                "active_responding_sellers": active_responding_sellers,
                "response_rate": response_rate,
                "avg_responses_per_rfq": avg_responses_per_rfq,
            },
            "marketplace_health": {
                "alerts": alerts,
                "activity_index": activity_index,
            },
            "user_classification": {
                "unknown": {
                    "sessions": unknown_sessions_count,
                    "users": unknown_users_count,
                },
                "buyer": {
                    "total": total_buyers_count,
                    "registered": buyer_registered_count,
                    "registered_rfq_created": buyer_rfq_created_count,
                    "registered_rfq_not_created": buyer_rfq_not_created_count,
                    "not_registered": buyer_not_registered_count,
                },
                "seller": {
                    "total": total_sellers_count,
                    "registered": seller_registered_count,
                    "registered_ss": seller_ss_count,
                    "registered_sws": seller_sws_count,
                    "not_registered": seller_not_registered_count,
                }
            },
            "funnel": funnel,
            "activity_feed": recent_feed,
        }

    def export_user_classification_csv(
        self,
        date_preset: str = "today",
        start_date_str: Optional[str] = None,
        end_date_str: Optional[str] = None,
        filter_type: str = "all"
    ) -> str:
        """
        Export phone numbers and classification metrics for User Classification & Funnel as CSV.
        """
        import io
        import csv

        start_dt, end_dt, _, _ = self._resolve_date_range(date_preset, start_date_str, end_date_str)
        start_naive = start_dt.replace(tzinfo=None)
        end_naive = end_dt.replace(tzinfo=None)
        today_session_filter = [
            ConversationSession.created_at >= start_naive,
            ConversationSession.created_at <= end_naive,
        ]

        def _generate(db: Session) -> str:
            all_period_users = [
                u[0] for u in db.query(distinct(ConversationSession.external_user_id)).filter(
                    *today_session_filter
                ).all() if u[0]
            ]

            buyer_users_set = {
                u[0] for u in db.query(distinct(ConversationSession.external_user_id)).filter(
                    ConversationSession.user_type == UserType.buyer,
                    *today_session_filter
                ).all() if u[0]
            }

            seller_users_set = {
                u[0] for u in db.query(distinct(ConversationSession.external_user_id)).filter(
                    ConversationSession.user_type == UserType.seller,
                    *today_session_filter
                ).all() if u[0]
            }

            unknown_users_set = set(all_period_users) - buyer_users_set - seller_users_set

            rfq_phones_in_period = {
                r[0] for r in db.query(distinct(RFQ.external_user_id)).filter(
                    RFQ.created_at >= start_naive,
                    RFQ.created_at <= end_naive
                ).all() if r[0]
            }
            for r_ids, r_id, ext_user in db.query(
                ConversationSession.rfq_ids, ConversationSession.rfq_id, ConversationSession.external_user_id
            ).filter(*today_session_filter).all():
                if (r_ids or r_id) and ext_user:
                    rfq_phones_in_period.add(ext_user)

            registered_sellers_map = {}
            try:
                from app.models import Seller
                for s in db.query(Seller.phone_number, Seller.subscription_credits).all():
                    if s.phone_number:
                        p_clean = s.phone_number.lstrip("+")
                        registered_sellers_map[p_clean] = s.subscription_credits or 0
                        registered_sellers_map[s.phone_number] = s.subscription_credits or 0
            except Exception as e:
                logger.debug(f"[DASHBOARD] Seller query skipped in CSV export: {e}")

            user_rows = []

            for u_phone in sorted(unknown_users_set):
                sess_info = db.query(
                    func.count(ConversationSession.session_id),
                    func.max(ConversationSession.created_at)
                ).filter(
                    ConversationSession.external_user_id == u_phone,
                    *today_session_filter
                ).first()
                sessions_count = sess_info[0] if sess_info else 0
                last_active = sess_info[1].strftime("%Y-%m-%d %H:%M:%S") if sess_info and sess_info[1] else "N/A"

                user_rows.append({
                    "phone": u_phone,
                    "category": "Unknown",
                    "registration_status": "Not Registered",
                    "sub_status": "No Role Selected / Greeting Only",
                    "sessions_count": sessions_count,
                    "last_active": last_active,
                    "key": "unknown"
                })

            for b_phone in sorted(buyer_users_set):
                user_sess = db.query(
                    ConversationSession.workflow_type,
                    ConversationSession.workflow_state,
                    ConversationSession.outcome,
                    ConversationSession.rfq_id,
                    ConversationSession.rfq_ids,
                    ConversationSession.created_at
                ).filter(
                    ConversationSession.external_user_id == b_phone,
                    *today_session_filter
                ).all()

                is_reg = False
                has_rfq = b_phone in rfq_phones_in_period
                last_active_dt = None

                for w_type, w_state, out, r_id, r_ids, c_at in user_sess:
                    if c_at and (last_active_dt is None or c_at > last_active_dt):
                        last_active_dt = c_at
                    if r_id or r_ids:
                        has_rfq = True
                    w_state = w_state or {}
                    if w_type == WorkflowType.registration:
                        if w_state.get("registration_stage") == "completed" or out == ConversationOutcome.completed:
                            is_reg = True
                    if w_state.get("selected_user") or w_state.get("authenticated_user") or w_state.get("user_id") or w_state.get("is_authenticated") is True:
                        is_reg = True

                if has_rfq:
                    is_reg = True

                last_active_str = last_active_dt.strftime("%Y-%m-%d %H:%M:%S") if last_active_dt else "N/A"
                sessions_count = len(user_sess)

                if is_reg:
                    reg_status = "Registered"
                    if has_rfq:
                        sub_status = "RFQ Created"
                        key = "buyer_registered_rfq_created"
                    else:
                        sub_status = "RFQ Not Created"
                        key = "buyer_registered_rfq_not_created"
                else:
                    reg_status = "Not Registered"
                    sub_status = "Buyer Intent Expressed"
                    key = "buyer_not_registered"

                user_rows.append({
                    "phone": b_phone,
                    "category": "Buyer",
                    "registration_status": reg_status,
                    "sub_status": sub_status,
                    "sessions_count": sessions_count,
                    "last_active": last_active_str,
                    "key": key
                })

            for s_phone in sorted(seller_users_set):
                p_clean = s_phone.lstrip("+")
                user_sess = db.query(
                    ConversationSession.workflow_type,
                    ConversationSession.workflow_state,
                    ConversationSession.outcome,
                    ConversationSession.created_at
                ).filter(
                    ConversationSession.external_user_id == s_phone,
                    *today_session_filter
                ).all()

                is_reg = False
                last_active_dt = None
                if p_clean in registered_sellers_map or s_phone in registered_sellers_map:
                    is_reg = True

                for w_type, w_state, out, c_at in user_sess:
                    if c_at and (last_active_dt is None or c_at > last_active_dt):
                        last_active_dt = c_at
                    w_state = w_state or {}
                    if w_type == WorkflowType.registration and (w_state.get("registration_stage") == "completed" or out == ConversationOutcome.completed):
                        is_reg = True
                    if w_state.get("selected_user") or w_state.get("authenticated_user") or w_state.get("user_id") or w_state.get("is_authenticated") is True:
                        is_reg = True

                last_active_str = last_active_dt.strftime("%Y-%m-%d %H:%M:%S") if last_active_dt else "N/A"
                sessions_count = len(user_sess)

                if is_reg:
                    reg_status = "Registered"
                    credits = registered_sellers_map.get(p_clean, registered_sellers_map.get(s_phone, 0))
                    if credits > 0:
                        sub_status = f"Subscribed Seller ({credits} credits)"
                        key = "seller_subscribed"
                    else:
                        sub_status = "Seller without Subscription (0 credits)"
                        key = "seller_without_subscription"
                else:
                    reg_status = "Not Registered"
                    sub_status = "Seller Intent Expressed"
                    key = "seller_not_registered"

                user_rows.append({
                    "phone": s_phone,
                    "category": "Seller",
                    "registration_status": reg_status,
                    "sub_status": sub_status,
                    "sessions_count": sessions_count,
                    "last_active": last_active_str,
                    "key": key
                })

            if filter_type and filter_type != "all":
                f_lower = filter_type.lower()
                if f_lower == "unknown":
                    user_rows = [r for r in user_rows if r["category"].lower() == "unknown"]
                elif f_lower == "buyer":
                    user_rows = [r for r in user_rows if r["category"].lower() == "buyer"]
                elif f_lower == "buyer_registered":
                    user_rows = [r for r in user_rows if r["category"].lower() == "buyer" and r["registration_status"] == "Registered"]
                elif f_lower == "buyer_not_registered":
                    user_rows = [r for r in user_rows if r["category"].lower() == "buyer" and r["registration_status"] == "Not Registered"]
                elif f_lower == "buyer_rfq_created":
                    user_rows = [r for r in user_rows if r["key"] == "buyer_registered_rfq_created"]
                elif f_lower == "buyer_rfq_not_created":
                    user_rows = [r for r in user_rows if r["key"] == "buyer_registered_rfq_not_created"]
                elif f_lower == "seller":
                    user_rows = [r for r in user_rows if r["category"].lower() == "seller"]
                elif f_lower == "seller_registered":
                    user_rows = [r for r in user_rows if r["category"].lower() == "seller" and r["registration_status"] == "Registered"]
                elif f_lower == "seller_not_registered":
                    user_rows = [r for r in user_rows if r["category"].lower() == "seller" and r["registration_status"] == "Not Registered"]
                elif f_lower == "seller_subscribed":
                    user_rows = [r for r in user_rows if r["key"] == "seller_subscribed"]
                elif f_lower == "seller_without_subscription":
                    user_rows = [r for r in user_rows if r["key"] == "seller_without_subscription"]

            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["Phone Number", "Category", "Registration Status", "Sub Status / Funnel Details", "Sessions Count", "Last Active Time"])
            for row in user_rows:
                writer.writerow([
                    row["phone"],
                    row["category"],
                    row["registration_status"],
                    row["sub_status"],
                    row["sessions_count"],
                    row["last_active"]
                ])

            return output.getvalue()

        if self.db is not None:
            return _generate(self.db)
        with get_db_session_context() as db:
            return _generate(db)

    def get_user_classification_details_json(
        self,
        date_preset: str = "today",
        start_date_str: Optional[str] = None,
        end_date_str: Optional[str] = None,
        filter_type: str = "all"
    ) -> Dict[str, Any]:
        """
        Return structured JSON payload of user classification details for the UI details view.
        """
        start_dt, end_dt, _, _ = self._resolve_date_range(date_preset, start_date_str, end_date_str)
        start_naive = start_dt.replace(tzinfo=None)
        end_naive = end_dt.replace(tzinfo=None)
        today_session_filter = [
            ConversationSession.created_at >= start_naive,
            ConversationSession.created_at <= end_naive,
        ]

        def _generate(db: Session) -> Dict[str, Any]:
            all_period_users = [
                u[0] for u in db.query(distinct(ConversationSession.external_user_id)).filter(
                    *today_session_filter
                ).all() if u[0]
            ]

            buyer_users_set = {
                u[0] for u in db.query(distinct(ConversationSession.external_user_id)).filter(
                    ConversationSession.user_type == UserType.buyer,
                    *today_session_filter
                ).all() if u[0]
            }

            seller_users_set = {
                u[0] for u in db.query(distinct(ConversationSession.external_user_id)).filter(
                    ConversationSession.user_type == UserType.seller,
                    *today_session_filter
                ).all() if u[0]
            }

            unknown_users_set = set(all_period_users) - buyer_users_set - seller_users_set

            rfq_phones_in_period = {
                r[0] for r in db.query(distinct(RFQ.external_user_id)).filter(
                    RFQ.created_at >= start_naive,
                    RFQ.created_at <= end_naive
                ).all() if r[0]
            }
            for r_ids, r_id, ext_user in db.query(
                ConversationSession.rfq_ids, ConversationSession.rfq_id, ConversationSession.external_user_id
            ).filter(*today_session_filter).all():
                if (r_ids or r_id) and ext_user:
                    rfq_phones_in_period.add(ext_user)

            registered_sellers_map = {}
            try:
                from app.models import Seller
                for s in db.query(Seller.phone_number, Seller.subscription_credits).all():
                    if s.phone_number:
                        p_clean = s.phone_number.lstrip("+")
                        registered_sellers_map[p_clean] = s.subscription_credits or 0
                        registered_sellers_map[s.phone_number] = s.subscription_credits or 0
            except Exception as e:
                logger.debug(f"[DASHBOARD] Seller query skipped in details JSON: {e}")

            user_rows = []

            for u_phone in sorted(unknown_users_set):
                sess_info = db.query(
                    func.count(ConversationSession.session_id),
                    func.max(ConversationSession.created_at),
                    func.max(ConversationSession.session_id)
                ).filter(
                    ConversationSession.external_user_id == u_phone,
                    *today_session_filter
                ).first()
                sessions_count = sess_info[0] if sess_info else 0
                last_active = sess_info[1].strftime("%Y-%m-%d %H:%M:%S") if sess_info and sess_info[1] else "N/A"
                session_id = sess_info[2] if sess_info else None

                user_rows.append({
                    "phone": u_phone,
                    "category": "Unknown",
                    "registration_status": "Not Registered",
                    "sub_status": "No Role Selected / Greeting Only",
                    "sessions_count": sessions_count,
                    "last_active": last_active,
                    "session_id": session_id,
                    "key": "unknown"
                })

            for b_phone in sorted(buyer_users_set):
                user_sess = db.query(
                    ConversationSession.workflow_type,
                    ConversationSession.workflow_state,
                    ConversationSession.outcome,
                    ConversationSession.rfq_id,
                    ConversationSession.rfq_ids,
                    ConversationSession.created_at,
                    ConversationSession.session_id
                ).filter(
                    ConversationSession.external_user_id == b_phone,
                    *today_session_filter
                ).all()

                is_reg = False
                has_rfq = b_phone in rfq_phones_in_period
                last_active_dt = None
                latest_session_id = None

                for w_type, w_state, out, r_id, r_ids, c_at, s_id in user_sess:
                    if c_at and (last_active_dt is None or c_at > last_active_dt):
                        last_active_dt = c_at
                        latest_session_id = s_id
                    if r_id or r_ids:
                        has_rfq = True
                    w_state = w_state or {}
                    if w_type == WorkflowType.registration:
                        if w_state.get("registration_stage") == "completed" or out == ConversationOutcome.completed:
                            is_reg = True
                    if w_state.get("selected_user") or w_state.get("authenticated_user") or w_state.get("user_id") or w_state.get("is_authenticated") is True:
                        is_reg = True

                if has_rfq:
                    is_reg = True

                last_active_str = last_active_dt.strftime("%Y-%m-%d %H:%M:%S") if last_active_dt else "N/A"
                sessions_count = len(user_sess)

                if is_reg:
                    reg_status = "Registered"
                    if has_rfq:
                        sub_status = "RFQ Created"
                        key = "buyer_registered_rfq_created"
                    else:
                        sub_status = "RFQ Not Created"
                        key = "buyer_registered_rfq_not_created"
                else:
                    reg_status = "Not Registered"
                    sub_status = "Buyer Intent Expressed"
                    key = "buyer_not_registered"

                user_rows.append({
                    "phone": b_phone,
                    "category": "Buyer",
                    "registration_status": reg_status,
                    "sub_status": sub_status,
                    "sessions_count": sessions_count,
                    "last_active": last_active_str,
                    "session_id": latest_session_id,
                    "key": key
                })

            for s_phone in sorted(seller_users_set):
                p_clean = s_phone.lstrip("+")
                user_sess = db.query(
                    ConversationSession.workflow_type,
                    ConversationSession.workflow_state,
                    ConversationSession.outcome,
                    ConversationSession.created_at,
                    ConversationSession.session_id
                ).filter(
                    ConversationSession.external_user_id == s_phone,
                    *today_session_filter
                ).all()

                is_reg = False
                last_active_dt = None
                latest_session_id = None
                if p_clean in registered_sellers_map or s_phone in registered_sellers_map:
                    is_reg = True

                for w_type, w_state, out, c_at, s_id in user_sess:
                    if c_at and (last_active_dt is None or c_at > last_active_dt):
                        last_active_dt = c_at
                        latest_session_id = s_id
                    w_state = w_state or {}
                    if w_type == WorkflowType.registration and (w_state.get("registration_stage") == "completed" or out == ConversationOutcome.completed):
                        is_reg = True
                    if w_state.get("selected_user") or w_state.get("authenticated_user") or w_state.get("user_id") or w_state.get("is_authenticated") is True:
                        is_reg = True

                last_active_str = last_active_dt.strftime("%Y-%m-%d %H:%M:%S") if last_active_dt else "N/A"
                sessions_count = len(user_sess)

                if is_reg:
                    reg_status = "Registered"
                    credits = registered_sellers_map.get(p_clean, registered_sellers_map.get(s_phone, 0))
                    if credits > 0:
                        sub_status = f"Subscribed Seller ({credits} credits)"
                        key = "seller_subscribed"
                    else:
                        sub_status = "Seller without Subscription (0 credits)"
                        key = "seller_without_subscription"
                else:
                    reg_status = "Not Registered"
                    sub_status = "Seller Intent Expressed"
                    key = "seller_not_registered"

                user_rows.append({
                    "phone": s_phone,
                    "category": "Seller",
                    "registration_status": reg_status,
                    "sub_status": sub_status,
                    "sessions_count": sessions_count,
                    "last_active": last_active_str,
                    "session_id": latest_session_id,
                    "key": key
                })

            total_before_filter = len(user_rows)

            if filter_type and filter_type != "all":
                f_lower = filter_type.lower()
                if f_lower == "unknown":
                    user_rows = [r for r in user_rows if r["category"].lower() == "unknown"]
                elif f_lower == "buyer":
                    user_rows = [r for r in user_rows if r["category"].lower() == "buyer"]
                elif f_lower == "buyer_registered":
                    user_rows = [r for r in user_rows if r["category"].lower() == "buyer" and r["registration_status"] == "Registered"]
                elif f_lower == "buyer_not_registered":
                    user_rows = [r for r in user_rows if r["category"].lower() == "buyer" and r["registration_status"] == "Not Registered"]
                elif f_lower == "buyer_rfq_created":
                    user_rows = [r for r in user_rows if r["key"] == "buyer_registered_rfq_created"]
                elif f_lower == "buyer_rfq_not_created":
                    user_rows = [r for r in user_rows if r["key"] == "buyer_registered_rfq_not_created"]
                elif f_lower == "seller":
                    user_rows = [r for r in user_rows if r["category"].lower() == "seller"]
                elif f_lower == "seller_registered":
                    user_rows = [r for r in user_rows if r["category"].lower() == "seller" and r["registration_status"] == "Registered"]
                elif f_lower == "seller_not_registered":
                    user_rows = [r for r in user_rows if r["category"].lower() == "seller" and r["registration_status"] == "Not Registered"]
                elif f_lower == "seller_subscribed":
                    user_rows = [r for r in user_rows if r["key"] == "seller_subscribed"]
                elif f_lower == "seller_without_subscription":
                    user_rows = [r for r in user_rows if r["key"] == "seller_without_subscription"]

            return {
                "status": "success",
                "filter_type": filter_type,
                "date_preset": date_preset,
                "total_users": len(user_rows),
                "total_overall_users": total_before_filter,
                "users": user_rows
            }

        if self.db is not None:
            return _generate(self.db)
        with get_db_session_context() as db:
            return _generate(db)

    def get_daily_visitors(
        self,
        date_preset: str = "today",
        start_date_str: Optional[str] = None,
        end_date_str: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Return day-wise unique user visit counts over the resolved date range.
        Each entry is: {"date": "YYYY-MM-DD", "visitors": <int>}
        """
        start_dt, end_dt, _, _ = self._resolve_date_range(date_preset, start_date_str, end_date_str)
        start_naive = start_dt.replace(tzinfo=None)
        end_naive = end_dt.replace(tzinfo=None)

        # Build a list of days spanning the range
        total_days = (end_naive.date() - start_naive.date()).days + 1
        days: List[Dict[str, Any]] = []

        def _query(db: Session) -> List[Dict[str, Any]]:
            # Pull all sessions in the window with their date and external_user_id
            rows = (
                db.query(
                    func.date(ConversationSession.created_at).label("day"),
                    ConversationSession.external_user_id,
                )
                .filter(
                    ConversationSession.created_at >= start_naive,
                    ConversationSession.created_at <= end_naive,
                    ConversationSession.external_user_id.isnot(None),
                )
                .all()
            )
            # Group distinct users per day in Python (DB-agnostic)
            day_users: Dict[str, set] = {}
            for row in rows:
                day_str = str(row.day)[:10]
                if day_str not in day_users:
                    day_users[day_str] = set()
                day_users[day_str].add(row.external_user_id)

            result: List[Dict[str, Any]] = []
            for i in range(total_days):
                current = (start_naive + timedelta(days=i)).date()
                day_str = current.isoformat()
                result.append({"date": day_str, "visitors": len(day_users.get(day_str, set()))})
            return result

        try:
            if self.db is not None:
                return _query(self.db)
            with get_db_session_context() as db:
                return _query(db)
        except Exception as e:
            logger.error(f"[DASHBOARD] get_daily_visitors error: {e}")
            return days

    async def get_today_conversations(self) -> List[Dict[str, Any]]:
        """
        Retrieve all conversations from today, aggregated by unique user phone number.
        Merges MySQL persistent sessions with live active Redis sessions for real-time accuracy.
        """
        now = datetime.now(timezone.utc)
        today_start_naive = datetime(now.year, now.month, now.day, 0, 0, 0)

        def _norm_phone(p: str) -> str:
            raw = str(p or "").lstrip("+").replace(" ", "").replace("-", "").strip()
            if len(raw) == 12 and raw.startswith("91"):
                return raw[2:]
            return raw

        # 1. Fetch active online users and active sessions from Redis
        active_phones = set()
        redis_sessions = []
        try:
            from app.redis_db import AsyncRedisConnectionManager
            from app.services.realtime_analytics_service import REDIS_KEY_ACTIVE_USERS
            client = await AsyncRedisConnectionManager.get_client()
            if client:
                # Online presence
                members = await client.zrange(REDIS_KEY_ACTIVE_USERS, 0, -1)
                for m in members:
                    try:
                        p_str = m.decode("utf-8") if isinstance(m, (bytes, bytearray)) else str(m)
                        data = json.loads(p_str)
                        phone = _norm_phone(data.get("phone", ""))
                        if phone:
                            active_phones.add(phone)
                    except Exception:
                        pass

                # Active session keys
                s_keys = await client.keys("session:*")
                for k in s_keys:
                    try:
                        raw = await client.get(k)
                        if raw:
                            s_data = json.loads(raw)
                            redis_sessions.append(s_data)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"[DASHBOARD] Failed to get Redis sessions: {e}")

        # 2. Query MySQL sessions
        def _query_db(db: Session) -> List[Dict[str, Any]]:
            return (
                db.query(ConversationSession)
                .filter(
                    or_(
                        ConversationSession.created_at >= today_start_naive,
                        ConversationSession.last_activity_at >= today_start_naive,
                    ),
                    ConversationSession.external_user_id.isnot(None)
                )
                .order_by(ConversationSession.created_at.asc())
                .all()
            )

        try:
            if self.db is not None:
                db_sessions = _query_db(self.db)
            else:
                with get_db_session_context() as db:
                    db_sessions = _query_db(db)
        except Exception as e:
            logger.error(f"[DASHBOARD] DB error in get_today_conversations: {e}")
            db_sessions = []

        # 3. Group all sessions (MySQL + Redis) by normalized phone number
        users_map: Dict[str, Dict[str, Any]] = {}
        processed_session_ids = set()

        for s in db_sessions:
            phone = str(s.external_user_id).strip()
            norm = _norm_phone(phone)
            if not norm:
                continue

            processed_session_ids.add(s.session_id)
            history = s.conversation_history if isinstance(s.conversation_history, dict) else {}
            messages = history.get("messages", [])
            s_msgs_count = len(messages)

            u_type = s.user_type.value if hasattr(s.user_type, "value") else str(s.user_type or "unknown")
            s_outcome = s.outcome.value if hasattr(s.outcome, "value") else str(s.outcome or "")
            rfq_val = s.rfq_id or (s.rfq_ids[0] if s.rfq_ids and len(s.rfq_ids) > 0 else None)

            if norm not in users_map:
                users_map[norm] = {
                    "phone": phone,
                    "normalized_phone": norm,
                    "session_id": s.session_id,
                    "user_type": u_type,
                    "outcome": s_outcome,
                    "started_at": s.started_at.isoformat() if s.started_at else (s.created_at.isoformat() if s.created_at else None),
                    "last_activity_at": s.last_activity_at.isoformat() if s.last_activity_at else None,
                    "total_messages": s_msgs_count,
                    "all_messages": list(messages),
                    "latest_message_preview": "",
                    "is_online": norm in active_phones,
                    "rfq_id": rfq_val,
                    "session_count": 1,
                }
            else:
                user_entry = users_map[norm]
                user_entry["session_count"] += 1
                user_entry["total_messages"] += s_msgs_count
                user_entry["all_messages"].extend(messages)
                if s.last_activity_at:
                    user_entry["last_activity_at"] = s.last_activity_at.isoformat()
                if u_type and u_type != "unknown":
                    user_entry["user_type"] = u_type
                if s_outcome:
                    user_entry["outcome"] = s_outcome
                if rfq_val:
                    user_entry["rfq_id"] = rfq_val
                user_entry["is_online"] = norm in active_phones

        # Merge active Redis sessions (taking latest messages if session in both or adding if new)
        for rs in redis_sessions:
            r_phone = str(rs.get("external_user_id") or "").strip()
            r_norm = _norm_phone(r_phone)
            if not r_norm:
                continue

            r_sid = rs.get("session_id")
            r_history = rs.get("conversation_history") if isinstance(rs.get("conversation_history"), dict) else {}
            r_messages = r_history.get("messages", [])
            r_type = rs.get("user_type") or "unknown"
            r_outcome = rs.get("outcome") or ""
            r_last_active = rs.get("last_activity_at") or rs.get("started_at")
            r_rfq = rs.get("rfq_id")

            if r_norm not in users_map:
                users_map[r_norm] = {
                    "phone": r_phone,
                    "normalized_phone": r_norm,
                    "session_id": r_sid,
                    "user_type": r_type,
                    "outcome": r_outcome,
                    "started_at": rs.get("started_at"),
                    "last_activity_at": r_last_active,
                    "total_messages": len(r_messages),
                    "all_messages": list(r_messages),
                    "latest_message_preview": "",
                    "is_online": True,
                    "rfq_id": r_rfq,
                    "session_count": 1,
                }
            else:
                user_entry = users_map[r_norm]
                # If this session was already in MySQL, Redis has newer messages for it
                if r_sid in processed_session_ids:
                    # Session exists in DB, append the latest active messages from Redis
                    if r_messages:
                        user_entry["total_messages"] += len(r_messages)
                        user_entry["all_messages"].extend(r_messages)
                else:
                    # Brand new active session in Redis not yet in MySQL
                    user_entry["session_count"] += 1
                    user_entry["total_messages"] += len(r_messages)
                    user_entry["all_messages"].extend(r_messages)

                if r_last_active:
                    user_entry["last_activity_at"] = r_last_active
                if r_type and r_type != "unknown":
                    user_entry["user_type"] = r_type
                if r_rfq:
                    user_entry["rfq_id"] = r_rfq
                user_entry["is_online"] = True

        # Filter and finalize user previews
        result = []
        for norm, user_entry in users_map.items():
            msgs = user_entry.pop("all_messages", [])
            user_entry["total_messages"] = len(msgs)
            if user_entry["total_messages"] == 0:
                continue

            last_msg_text = ""
            if msgs:
                last_msg = msgs[-1]
                c = last_msg.get("content", "")
                if isinstance(c, dict):
                    last_msg_text = c.get("body") or c.get("header") or "Interactive message"
                else:
                    last_msg_text = str(c)
                if len(last_msg_text) > 80:
                    last_msg_text = last_msg_text[:77] + "..."

            user_entry["latest_message_preview"] = last_msg_text or "Message received"
            result.append(user_entry)

        # Sort by latest activity descending
        result.sort(key=lambda u: u.get("last_activity_at") or "", reverse=True)
        return result

    async def get_conversation_messages(self, phone: Optional[str] = None, session_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetch and combine the full message history across all sessions for a user/session,
        merging MySQL persistent sessions with live active Redis sessions.
        """
        if not phone and not session_id:
            return {"status": "error", "message": "Phone number or session_id is required", "messages": []}

        def _norm_phone(p: str) -> str:
            raw = str(p or "").lstrip("+").replace(" ", "").replace("-", "").strip()
            if len(raw) == 12 and raw.startswith("91"):
                return raw[2:]
            return raw

        norm = _norm_phone(phone) if phone else None

        # 1. Fetch live active sessions from Redis
        redis_sessions = []
        try:
            from app.redis_db import AsyncRedisConnectionManager
            client = await AsyncRedisConnectionManager.get_client()
            if client:
                s_keys = await client.keys("session:*")
                for k in s_keys:
                    try:
                        raw = await client.get(k)
                        if raw:
                            s_data = json.loads(raw)
                            r_phone = _norm_phone(s_data.get("external_user_id", ""))
                            r_sid = s_data.get("session_id", "")
                            if (norm and r_phone == norm) or (session_id and r_sid == session_id):
                                redis_sessions.append(s_data)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"[DASHBOARD] Failed to get Redis session in get_conversation_messages: {e}")

        # 2. Fetch MySQL sessions
        def _query_db(db: Session) -> List[ConversationSession]:
            q = db.query(ConversationSession)
            if session_id and not phone:
                return q.filter(ConversationSession.session_id == session_id).all()
            elif phone:
                possible_phones = [
                    phone,
                    norm,
                    f"+{norm}",
                    f"91{norm}",
                    f"+91{norm}",
                ]
                return (
                    q.filter(ConversationSession.external_user_id.in_(possible_phones))
                    .order_by(ConversationSession.created_at.asc())
                    .all()
                )
            return []

        try:
            if self.db is not None:
                db_sessions = _query_db(self.db)
            else:
                with get_db_session_context() as db:
                    db_sessions = _query_db(db)
        except Exception as e:
            logger.error(f"[DASHBOARD] DB query error in get_conversation_messages: {e}")
            db_sessions = []

        if not db_sessions and not redis_sessions:
            return {"status": "success", "messages": [], "session_info": None}

        # 3. Merge messages
        raw_messages = []
        active_rfq_id = None
        detected_role = "unknown"
        latest_outcome = ""
        latest_active = None
        started_time = None
        seen_sessions = set()

        # Add MySQL sessions
        for s in db_sessions:
            seen_sessions.add(s.session_id)
            if s.rfq_id:
                active_rfq_id = s.rfq_id
            elif s.rfq_ids and len(s.rfq_ids) > 0:
                active_rfq_id = s.rfq_ids[0]
            
            u_type = s.user_type.value if hasattr(s.user_type, "value") else str(s.user_type or "unknown")
            if u_type and u_type != "unknown":
                detected_role = u_type

            if s.outcome:
                latest_outcome = s.outcome.value if hasattr(s.outcome, "value") else str(s.outcome)
            if s.last_activity_at:
                latest_active = s.last_activity_at.isoformat()
            if not started_time and s.started_at:
                started_time = s.started_at.isoformat()

            history = s.conversation_history if isinstance(s.conversation_history, dict) else {}
            msgs = history.get("messages", [])

            # Check if this session is also in Redis with more recent messages
            redis_match = next((rs for rs in redis_sessions if rs.get("session_id") == s.session_id), None)
            if redis_match:
                r_history = redis_match.get("conversation_history") if isinstance(redis_match.get("conversation_history"), dict) else {}
                r_msgs = r_history.get("messages", [])
                if len(r_msgs) >= len(msgs):
                    msgs = r_msgs

            raw_messages.extend(msgs)

        # Add any active Redis sessions not yet in MySQL
        for rs in redis_sessions:
            r_sid = rs.get("session_id")
            if r_sid not in seen_sessions:
                seen_sessions.add(r_sid)
                r_rfq = rs.get("rfq_id")
                if r_rfq:
                    active_rfq_id = r_rfq
                r_type = rs.get("user_type") or "unknown"
                if r_type and r_type != "unknown":
                    detected_role = r_type
                if rs.get("outcome"):
                    latest_outcome = str(rs.get("outcome"))
                if rs.get("last_activity_at"):
                    latest_active = rs.get("last_activity_at")
                if not started_time and rs.get("started_at"):
                    started_time = rs.get("started_at")

                r_history = rs.get("conversation_history") if isinstance(rs.get("conversation_history"), dict) else {}
                raw_messages.extend(r_history.get("messages", []))

        def _extract_str(val: Any) -> Optional[str]:
            if val is None:
                return None
            if isinstance(val, str):
                return val.strip()
            if isinstance(val, dict):
                if "text" in val and isinstance(val["text"], (str, int, float)):
                    return str(val["text"]).strip()
                if "body" in val:
                    return _extract_str(val["body"])
                if "header" in val:
                    return _extract_str(val["header"])
                if "title" in val:
                    return str(val["title"]).strip()
                if "message" in val:
                    return _extract_str(val["message"])
                return None
            return str(val).strip()

        def _extract_buttons_list(content_dict: Dict[str, Any]) -> List[Dict[str, Any]]:
            raw_buttons = content_dict.get("buttons")
            if not raw_buttons and isinstance(content_dict.get("action"), dict):
                raw_buttons = content_dict["action"].get("buttons")

            buttons_out = []
            if isinstance(raw_buttons, list):
                for b in raw_buttons:
                    if isinstance(b, dict):
                        if "reply" in b and isinstance(b["reply"], dict):
                            b_id = str(b["reply"].get("id") or "")
                            b_title = str(b["reply"].get("title") or b_id)
                        else:
                            b_id = str(b.get("id") or "")
                            b_title = str(b.get("title") or b_id)
                        if b_title or b_id:
                            buttons_out.append({"id": b_id, "title": b_title})
            return buttons_out

        # Format and deduplicate messages
        formatted: List[Dict[str, Any]] = []
        for m in raw_messages:
            role = m.get("role") or m.get("sender") or "assistant"
            content = m.get("content")
            ts = m.get("timestamp")
            m_type = m.get("type") or "text"

            header = None
            body = None
            footer = None
            buttons = []

            if isinstance(content, dict):
                header = _extract_str(content.get("header"))
                body = _extract_str(content.get("body"))
                footer = _extract_str(content.get("footer"))
                buttons = _extract_buttons_list(content)
                text_content = body or header or _extract_str(content) or ""
            else:
                text_content = str(content or "")

            formatted.append({
                "role": role,
                "sender": role,
                "content": text_content,
                "header": header,
                "body": body,
                "footer": footer,
                "buttons": buttons,
                "timestamp": ts,
                "type": m_type,
            })

        return {
            "status": "success",
            "session_info": {
                "phone": phone or (db_sessions[0].external_user_id if db_sessions else (redis_sessions[0].get("external_user_id") if redis_sessions else "")),
                "user_type": detected_role,
                "outcome": latest_outcome,
                "started_at": started_time,
                "last_activity_at": latest_active,
                "rfq_id": active_rfq_id,
                "total_sessions": len(seen_sessions),
            },
            "messages": formatted
        }


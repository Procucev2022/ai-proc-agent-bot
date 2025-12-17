"""
Conversation Analytics Service - With Batch Processing and DataFrame Creation
"""

import logging
import json
import pandas as pd
from typing import Dict, Any, List, Tuple
from datetime import datetime, date, timezone
from sqlalchemy import func

from app.database import get_db_session, get_remote_db_session
from app.models import ConversationSession
from app.services.openai_service import OpenAIService
from app.config import get_settings
from app.utils.logging_utils import log_service_method
from sqlalchemy import text

logger = logging.getLogger(__name__)


class ConversationAnalyticsService:
    """Service for AI-powered conversation analytics and metrics extraction."""

    def __init__(self):
        self.settings = get_settings()
        self.openai_service = OpenAIService()
        self.batch_size = 5  # Process 5 sessions per AI call

    def _create_dataframes_from_sessions(self, sessions_data: List[Dict[str, Any]], analysis_date: str) -> Tuple[
        pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """
        Create buyer, seller, and unknown DataFrames from analyzed sessions.
        STRICT SEPARATION: Only buyers in buyer_df, only sellers in seller_df, only unknowns in unknown_df

        Args:
            sessions_data: List of analyzed session dictionaries
            analysis_date: Date string for the analysis

        Returns:
            Tuple of (buyer_df, seller_df, unknown_df)
        """
        buyer_records = []
        seller_records = []
        unknown_records = []

        for session in sessions_data:
            session_id = session.get('session_id', '')
            user_type = session.get('user_type', 'unknown')
            email = session.get('email', '')
            confidence_score = session.get('confidence_score', 0)

            # Extract metrics and emails
            buyer_metrics = session.get('buyer_metrics', {})
            seller_metrics = session.get('seller_metrics', {})
            registration_metrics = session.get('registration_metrics', {})
            buyer_email = session.get('buyer_email', '')
            seller_email = session.get('seller_email', '')

            # Check if session has buyer activity (any non-zero buyer metric)
            has_buyer_activity = (
                    buyer_metrics.get('successful_rfqs', 0) > 0 or
                    buyer_metrics.get('incomplete_rfqs', 0) > 0 or
                    buyer_metrics.get('buyers_started_but_not_raised_rfq', 0) > 0 or
                    buyer_metrics.get('number_of_buyer_chats', 0) > 0
            )

            # Check if session has seller activity (any non-zero seller metric)
            has_seller_activity = (
                    seller_metrics.get('subscription_plans_requested', 0) > 0 or
                    seller_metrics.get('zero_credit_rfq_attempt', 0) > 0 or
                    seller_metrics.get('number_of_seller_chats', 0) > 0
            )

            # Add to buyer_df if has buyer metrics
            if has_buyer_activity:
                buyer_record = {
                    'date': analysis_date,
                    'session_id': session_id,
                    'phone_number': session.get('phone_number', ''),
                    'user_type': user_type,
                    'buyer_email': buyer_email or email,
                    'successful_rfq': buyer_metrics.get('successful_rfqs', 0),
                    'incomplete_rfq': buyer_metrics.get('incomplete_rfqs', 0),
                    'buyers_started_but_not_raised_rfq': buyer_metrics.get('buyers_started_but_not_raised_rfq', 0),
                    'number_of_buyer_chats': buyer_metrics.get('number_of_buyer_chats', 0),
                    'successful_registration': registration_metrics.get('successful_registration', 0),
                    'failed_registration': registration_metrics.get('failed_registration', 0),
                    'confidence_score': confidence_score
                }
                buyer_records.append(buyer_record)

            # Add to seller_df if has seller metrics
            if has_seller_activity:
                seller_record = {
                    'date': analysis_date,
                    'session_id': session_id,
                    'phone_number': session.get('phone_number', ''),
                    'user_type': user_type,
                    'seller_email': seller_email or email,
                    'subscription_plans_requested': seller_metrics.get('subscription_plans_requested', 0),
                    'zero_credit_rfq_attempt': seller_metrics.get('zero_credit_rfq_attempt', 0),
                    'number_of_seller_chats': seller_metrics.get('number_of_seller_chats', 0),
                    'successful_registration': registration_metrics.get('successful_registration', 0),
                    'failed_registration': registration_metrics.get('failed_registration', 0),
                    'confidence_score': confidence_score
                }
                seller_records.append(seller_record)

            # Add to unknown_df if has NO buyer or seller activity
            if not has_buyer_activity and not has_seller_activity:
                unknown_record = {
                    'date': analysis_date,
                    'session_id': session_id,
                    'phone_number': session.get('external_user_id', ''),
                    'user_type': user_type,
                    'email': email,
                    'confidence_score': confidence_score,
                    'analysis_reasoning': session.get('analysis_reasoning', '')
                }
                unknown_records.append(unknown_record)

        # Create DataFrames
        buyer_df = pd.DataFrame(buyer_records)
        seller_df = pd.DataFrame(seller_records)
        unknown_df = pd.DataFrame(unknown_records)

        # Reorder columns for better readability
        if not buyer_df.empty:
            buyer_cols = [
                'date', 'session_id', 'phone_number', 'user_type', 'buyer_email',
                'successful_rfq', 'incomplete_rfq', 'buyers_started_but_not_raised_rfq',
                'number_of_buyer_chats', 'successful_registration', 'failed_registration',
                'confidence_score'
            ]
            buyer_df = buyer_df[buyer_cols]

        if not seller_df.empty:
            seller_cols = [
                'date', 'session_id', 'phone_number', 'user_type', 'seller_email',
                'subscription_plans_requested', 'zero_credit_rfq_attempt',
                'number_of_seller_chats', 'successful_registration', 'failed_registration',
                'confidence_score'
            ]
            seller_df = seller_df[seller_cols]

        if not unknown_df.empty:
            unknown_cols = [
                'date', 'session_id', 'phone_number', 'user_type', 'email',
                'confidence_score', 'analysis_reasoning'
            ]
            unknown_df = unknown_df[unknown_cols]

        logger.info(
            f"[CONVERSATION-ANALYTICS] Created DataFrames: "
            f"{len(buyer_df)} buyer rows, {len(seller_df)} seller rows, {len(unknown_df)} unknown rows"
        )

        return buyer_df, seller_df, unknown_df

    async def _process_sessions_in_batches(self, sessions: List[ConversationSession], target_date: date) -> Dict[
        str, Any]:
        """
        Process sessions in batches through AI analysis.
        Each batch processes multiple sessions in ONE AI call.
        """
        all_sessions = []

        # Process sessions in batches
        for batch_idx in range(0, len(sessions), self.batch_size):
            batch_sessions = sessions[batch_idx:batch_idx + self.batch_size]
            batch_num = (batch_idx // self.batch_size) + 1
            total_batches = (len(sessions) + self.batch_size - 1) // self.batch_size

            logger.info(
                f"[CONVERSATION-ANALYTICS] Processing batch {batch_num}/{total_batches} "
                f"({len(batch_sessions)} sessions)"
            )

            try:
                # Prepare data for this batch (multiple sessions)
                batch_data = await self._prepare_batch_session_data(batch_sessions)

                # Analyze this batch with AI (ONE call for multiple sessions)
                ai_response = await self._analyze_batch_with_ai(
                    batch_data,
                    batch_num,
                    target_date
                )
                print("ai", ai_response)

                if ai_response and 'sessions' in ai_response:
                    # AI returns flat format with all sessions in this batch
                    batch_session_list = ai_response.get('sessions', [])
                    all_sessions.extend(batch_session_list)

                    logger.info(
                        f"[CONVERSATION-ANALYTICS] Successfully analyzed batch {batch_num} "
                        f"({len(batch_session_list)} sessions)"
                    )
                else:
                    logger.warning(
                        f"[CONVERSATION-ANALYTICS] No metrics returned for batch {batch_num}"
                    )

            except Exception as e:
                logger.error(
                    f"[CONVERSATION-ANALYTICS] Failed to process batch {batch_num}: {e}"
                )
                continue

        result = {
            "date": str(target_date),
            "total_sessions": len(all_sessions),
            "sessions": all_sessions
        }

        logger.info(
            f"[CONVERSATION-ANALYTICS] Processed {len(all_sessions)}/{len(sessions)} "
            f"sessions successfully"
        )
        return result

    async def _prepare_batch_session_data(self, sessions: List[ConversationSession]) -> List[Dict[str, Any]]:
        """
        Prepare multiple conversation sessions data for AI analysis.

        Args:
            sessions: List of conversation sessions (up to batch_size)

        Returns:
            List of prepared session data dictionaries
        """
        batch_data = []

        for session in sessions:
            # Extract conversation history
            conversation_history = []
            if session.conversation_history:
                if isinstance(session.conversation_history, dict):
                    messages = session.conversation_history.get('messages', [])
                    for msg in messages:
                        if isinstance(msg, dict):
                            conversation_history.append({
                                'role': msg.get('role', 'unknown'),
                                'content': str(msg.get('content', ''))[:500],
                                'timestamp': msg.get('timestamp', '')
                            })
                elif isinstance(session.conversation_history, list):
                    for msg in session.conversation_history:
                        if isinstance(msg, dict):
                            conversation_history.append({
                                'role': msg.get('role', 'unknown'),
                                'content': str(msg.get('content', ''))[:500],
                                'timestamp': msg.get('timestamp', '')
                            })

            session_data = {
                'session_id': session.session_id,
                'external_user_id': session.external_user_id,
                'conversation_history': conversation_history,
                'created_at': str(session.created_at) if session.created_at else None,
            }

            batch_data.append(session_data)

        return batch_data

    async def _analyze_batch_with_ai(
            self,
            batch_data: List[Dict[str, Any]],
            batch_num: int,
            target_date: date
    ) -> Dict[str, Any]:
        """
        Use AI to analyze multiple conversation sessions in ONE call.

        Args:
            batch_data: List of prepared session data (up to batch_size sessions)
            batch_num: Batch number for logging
            target_date: Date being analyzed

        Returns:
            Dict with flat sessions array containing all analyzed sessions
        """
        try:
            # Create analysis prompt for batch
            analysis_prompt = self._build_batch_prompt(batch_data, batch_num, target_date)

            # Load conversation analytics tool
            tool_file = self.openai_service.tools_dir / "conversation_analytics.json"
            with open(tool_file, 'r') as f:
                analytics_tool = json.load(f)

            # Load system prompt
            system_prompt = self.openai_service._load_prompt(
                "conversation_analytics",
                "conversation_analytics_system_prompt"
            )

            # Get AI analysis
            response = await self.openai_service.client.responses.create(
                model=self.openai_service.default_model,
                input=[{"role": "user", "content": analysis_prompt}],
                instructions=system_prompt,
                tools=[analytics_tool],
                tool_choice={"type": "function", "name": "analyze_conversations"}
            )

            # Parse AI response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)

                    logger.info(
                        f"[CONVERSATION-ANALYTICS] AI analysis completed for batch {batch_num}"
                    )
                    return args

            logger.warning(
                f"[CONVERSATION-ANALYTICS] No function call in AI response for batch {batch_num}"
            )
            return None

        except Exception as e:
            logger.error(
                f"[CONVERSATION-ANALYTICS] AI analysis failed for batch {batch_num}: {e}"
            )
            return None

    def _build_batch_prompt(
            self,
            batch_data: List[Dict[str, Any]],
            batch_num: int,
            target_date: date
    ) -> str:
        """
        Build analysis prompt for a batch of sessions.

        Args:
            batch_data: List of session data to analyze
            batch_num: Batch number
            target_date: Target date for analysis

        Returns:
            Formatted analysis prompt
        """
        session_ids = [session['session_id'] for session in batch_data]

        prompt = f"""
Analyze the following {len(batch_data)} conversation sessions from {target_date} and extract business metrics.

DATE: {target_date}
BATCH NUMBER: {batch_num}
SESSION IDs TO ANALYZE: {', '.join(session_ids)}

BATCH DATA ({len(batch_data)} Sessions):
{json.dumps(batch_data, indent=2)}

INSTRUCTIONS:
Analyze ALL {len(batch_data)} conversation sessions in this batch and return metrics for EACH session:

1. For EACH session independently:
   - Determine user type (buyer/seller/unknown)
   - Extract buyer_email and seller_email separately from conversation history
   - Calculate all metrics based on conversation patterns
   - Provide confidence score (0-100)
   - Explain reasoning

2. Metric Guidelines:
   - BUYERS: Count successful_rfqs, incomplete_rfqs, buyers_started_but_not_raised_rfq, registrations
   - SELLERS: Count subscription_plans_requested, zero_credit_rfq_attempt, registrations
   - UNKNOWN: Set all metrics to 0
   - Always include ALL metric fields (set to 0 if not applicable)

3. Return Format:
   - Set total_sessions to {len(batch_data)}
   - Include ALL {len(batch_data)} sessions in the sessions array
   - Each session must have: session_id, user_type, email, buyer_email, seller_email, confidence_score, buyer_metrics, seller_metrics, registration_metrics, analysis_reasoning

CRITICAL: You must analyze and return data for ALL {len(batch_data)} sessions.
Session IDs to process: {', '.join(session_ids)}
"""
        return prompt

    async def analyze_daily_conversations(self, target_date: date = None) -> Dict[str, Any]:
        """
        Analyze conversations for a specific date using AI with batch processing.
        Returns result with DataFrames included.

        Args:
            target_date: Date to analyze (defaults to today)

        Returns:
            Dict with sessions array and buyer/seller DataFrames
        """
        if target_date is None:
            target_date = datetime.now(timezone.utc).date()

        try:
            logger.info(
                f"[CONVERSATION-ANALYTICS] Starting conversation analytics for {target_date}"
            )

            with get_db_session() as db:
                # Get all conversation sessions for the target date
                sessions = db.query(ConversationSession) \
                    .filter(func.date(ConversationSession.created_at) == target_date) \
                    .all()

                if not sessions:
                    logger.info(
                        f"[CONVERSATION-ANALYTICS] No conversation sessions found for {target_date}"
                    )
                    return {
                        "success": True,
                        "date": str(target_date),
                        "total_sessions": 0,
                        "sessions": [],
                        "buyer_df": pd.DataFrame(),
                        "seller_df": pd.DataFrame(),
                        "unknown_df": pd.DataFrame(),
                        "message": "No conversations found for analysis"
                    }

                logger.info(
                    f"[CONVERSATION-ANALYTICS] Found {len(sessions)} conversation sessions "
                    f"for analysis (batch size: {self.batch_size})"
                )

                # Process sessions in batches - AI returns flat format
                result = await self._process_sessions_in_batches(sessions, target_date)

                # Create DataFrames from analyzed sessions
                buyer_df, seller_df, unknown_df = self._create_dataframes_from_sessions(
                    result.get('sessions', []),
                    result.get('date', str(target_date))
                )

                # Query remote database after creating DataFrames
                remote_rfq_df = self._query_remote_users()
                
                # Join buyer_df with remote_rfq_df
                if not buyer_df.empty and not remote_rfq_df.empty:
                    buyer_df['phone_clean'] = buyer_df['phone_number'].str.replace('+', '', regex=False)
                    remote_rfq_df['phone_clean'] = remote_rfq_df['phone'].str.replace('+', '', regex=False)
                    joined_buyer_df = buyer_df.merge(remote_rfq_df, on=['phone_clean'], how='outer')
                    
                    # Keep only relevant columns
                    relevant_cols = [
                        'date', 'phone_number', 'email', 'session_id', 'confidence_score',
                        'successful_rfqs', 'incomplete_rfqs', 'buyers_started_but_not_raised_rfq',
                        'username', 'org_uuid', 'user_uuid', 'total_rfqs_raised', 
                        'total_items_in_rfqs', 'total_distinct_rfq_category', 'org_id'
                    ]
                    joined_buyer_df = joined_buyer_df[[col for col in relevant_cols if col in joined_buyer_df.columns]]
                else:
                    joined_buyer_df = buyer_df
                
                # Add DataFrames to result
                result["success"] = True
                result["analysis_timestamp"] = datetime.now(timezone.utc).isoformat()
                result["buyer_df"] = joined_buyer_df
                result["seller_df"] = seller_df
                result["unknown_df"] = unknown_df
                result["remote_rfq_df"] = remote_rfq_df
                result["joined_buyer_df"]=joined_buyer_df

                logger.info(
                    f"[CONVERSATION-ANALYTICS] Analysis completed for {target_date}. "
                    f"Total sessions analyzed: {result['total_sessions']}, "
                    f"Buyer rows: {len(buyer_df)}, Seller rows: {len(seller_df)}, Unknown rows: {len(unknown_df)}"
                )
                return result

        except Exception as e:
            logger.error(
                f"[CONVERSATION-ANALYTICS] Analysis failed for {target_date}: {e}"
            )
            return {
                "success": False,
                "date": str(target_date),
                "total_sessions": 0,
                "sessions": [],
                "buyer_df": pd.DataFrame(),
                "seller_df": pd.DataFrame(),
                "unknown_df": pd.DataFrame(),
                "error": str(e)
            }

    async def __aenter__(self):
        return self

    def _query_remote_users(self):
        """Query remote database for RFQ analytics data."""
        try:
            remote_db = get_remote_db_session()
            query = """
            SELECT
                DATE(rfh.created_ts) AS date,
                u.username,
                u.phone,
                u.org_uuid,
                u.uuid AS user_uuid,
                COUNT(DISTINCT rfh.rfq_id) AS total_rfqs_raised,
                COUNT(ri.uuid) AS total_items_in_rfqs,
                COUNT(DISTINCT CONCAT(rfh.rfq_id, '_', ri.category)) AS total_distinct_rfq_category,
                u.org_uuid AS org_id
            FROM user u
            JOIN rfq_header rfh 
                ON u.uuid = rfh.user
            LEFT JOIN rfq_items ri 
                ON ri.rfq_uuid = rfh.uuid
            WHERE 
                DATE(rfh.created_ts) = '2025-12-09'
                AND u.self_client = 1
                AND rfh.source_type = 'W'
            GROUP BY 
                DATE(rfh.created_ts),
                u.username,
                u.phone,
                u.org_uuid,
                u.uuid
            ORDER BY 
                u.username
            """
            result = remote_db.execute(text(query))
            rfq_data = [dict(row._mapping) for row in result]
            remote_db.close()
            rfq_df = pd.DataFrame(rfq_data)
            logger.info(f"[CONVERSATION-ANALYTICS] Retrieved {len(rfq_df)} RFQ records from remote database")
            return rfq_df
        except Exception as e:
            logger.error(f"[CONVERSATION-ANALYTICS] Remote database query failed: {e}")
            return []

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


if __name__ == "__main__":
    import asyncio
    from datetime import date, datetime


    async def main():
        service = ConversationAnalyticsService()
        today = datetime(2025, 12, 9).date()
        print(f"Analyzing conversations for {today}...")

        result = await service.analyze_daily_conversations(today)
        print(f"\nResults:")
        print(f"Success: {result.get('success', False)}")
        print(f"Total sessions: {result.get('total_sessions', 0)}")

        # Access DataFrames
        buyer_df = result.get('buyer_df')
        seller_df = result.get('seller_df')
        unknown_df = result.get('unknown_df')
        joined_df=result.get("joined_buyer_df")

        if buyer_df is not None and not buyer_df.empty:
            print(f"\n=== Buyer DataFrame ({len(buyer_df)} rows) ===")
            print(buyer_df.head())
            # Optionally save to CSV
            buyer_df.to_csv(f'buyer_analytics_{today}.csv', index=False)

        if seller_df is not None and not seller_df.empty:
            print(f"\n=== Seller DataFrame ({len(seller_df)} rows) ===")
            print(seller_df.head())
            # Optionally save to CSV
            seller_df.to_csv(f'seller_analytics_{today}.csv', index=False)

        if unknown_df is not None and not unknown_df.empty:
            print(f"\n=== Unknown DataFrame ({len(unknown_df)} rows) ===")
            print(unknown_df.head())
            # Optionally save to CSV
            unknown_df.to_csv(f'unknown_analytics_{today}.csv', index=False)

        if joined_df is not None and not joined_df.empty:
            print(f"\n=== Unknown DataFrame ({len(joined_df)} rows) ===")
            print(joined_df.head())
            # Optionally save to CSV
            joined_df.to_csv(f'joined_analytics_{today}.csv', index=False)


    asyncio.run(main())
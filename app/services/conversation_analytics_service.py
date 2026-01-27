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
from app.models import ConversationSession, BuyerDailyMetrics, SellerDailyMetrics, DailyAggregates, MetricMaster, CategoryAggregates, UnknownDailyMetrics, RFQNotificationFact
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
            confidence_score = session.get('confidence_score', 0)
            analysis_reasoning=session.get('analysis_reasoning',"")

            # Extract buyer and seller identities
            buyer_identities = session.get('buyer_identities', [])
            seller_identities = session.get('seller_identities', [])
            unknown_user_metrics = session.get('unknown_user_metrics', {})
            registration_metrics = session.get('registration_metrics', {})

            # Check if session has buyer activity
            has_buyer_activity = len(buyer_identities) > 0

            # Check if session has seller activity
            has_seller_activity = len(seller_identities) > 0

            # Add to buyer_df if has buyer metrics
            if has_buyer_activity:
                for buyer_identity in buyer_identities:
                    buyer_email = buyer_identity.get('buyer_email', '')
                    buyer_metrics = buyer_identity.get('buyer_metrics', {})
                    
                    buyer_record = {
                        'date': analysis_date,
                        'session_id': session_id,
                        'phone_number': session.get('phone_number', ''),
                        'user_type': user_type,
                        'buyer_email': buyer_email,
                        'successful_rfqs_ai': buyer_metrics.get('successful_rfqs_ai', 0),
                        'incomplete_rfq': buyer_metrics.get('incomplete_rfqs', 0),
                        'buyers_started_but_not_raised_rfq': buyer_metrics.get('buyers_started_but_not_raised_rfq', 0),
                        'number_of_buyer_chats': buyer_metrics.get('number_of_buyer_chats', 0),
                        'successful_registration': registration_metrics.get('buyer_successful_registration', 0),
                        'failed_registration': registration_metrics.get('buyer_failed_registration', 0),
                        'bfs_searches': buyer_metrics.get('bfs_searches', 0),
                        'products_bid_for': buyer_metrics.get('products_bid_for', 0),
                        'no_of_products_searched': buyer_metrics.get('no_of_products_searched', 0),
                        'bfs_stock_products_bid_placed_count': buyer_metrics.get('bfs_stock_products_bid_placed_count', 0),
                        'bfs_products_searched_list': buyer_metrics.get('bfs_products_searched_list', []),
                        'rfq_ids_created': buyer_metrics.get('rfq_ids_created', []),
                        'confidence_score': confidence_score,
                        'analysis_reasoning': analysis_reasoning
                    }
                    buyer_records.append(buyer_record)

            # Add to seller_df if has seller metrics
            if has_seller_activity:
                for seller_identity in seller_identities:
                    seller_email = seller_identity.get('seller_email', '')
                    seller_metrics = seller_identity.get('seller_metrics', {})
                    
                    seller_record = {
                        'date': analysis_date,
                        'session_id': session_id,
                        'phone_number': session.get('phone_number', ''),
                        'user_type': user_type,
                        'seller_email': seller_email,
                        'requested_rfq_ai': seller_metrics.get('rfq_requested_ai', 0),
                        'rfq_response_ai': seller_metrics.get('rfq_response_ai', 0),
                        'subscription_plans_requested': seller_metrics.get('subscription_plans_requested', 0),
                        'zero_credit_rfq_attempt': seller_metrics.get('zero_credit_rfq_attempt', 0),
                        'number_of_seller_chats': seller_metrics.get('number_of_seller_chats', 0),
                        'successful_registration': registration_metrics.get('seller_successful_registration', 0),
                        'failed_registration': registration_metrics.get('seller_failed_registration', 0),
                        'bids_accepted_ai': seller_metrics.get('bids_accepted_ai', 0),
                        'confidence_score': confidence_score,
                        'analysis_reasoning': analysis_reasoning
                    }
                    seller_records.append(seller_record)

            # Add to unknown_df if has NO buyer or seller activity
            if not has_buyer_activity and not has_seller_activity:
                unknown_record = {
                    'date': analysis_date,
                    'session_id': session_id,
                    'phone_number': session.get('external_user_id', ''),
                    'user_type': user_type,
                    'email': '',
                    'confidence_score': confidence_score,
                    'analysis_reasoning': analysis_reasoning,
                    'unregistered_seller_initiated_chat': unknown_user_metrics.get('unregistered_seller_initiated_chat', 0),
                    'unregistered_seller_requested_rfq': unknown_user_metrics.get('unregistered_seller_requested_rfq', 0),
                    'unregistered_buyer_bfs_only': unknown_user_metrics.get('unregistered_buyer_bfs_only', 0),
                    'number_of_faq_or_general_queries': unknown_user_metrics.get('number_of_faq_or_general_queries', 0),
                    'bfs_products_searched_by_unregistered': unknown_user_metrics.get('bfs_products_searched_by_unregistered', [])
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
                'successful_rfqs_ai', 'incomplete_rfq', 'buyers_started_but_not_raised_rfq',
                'number_of_buyer_chats', 'successful_registration', 'failed_registration','bfs_searches','products_bid_for','no_of_products_searched',
                'bfs_products_searched_list', 'rfq_ids_created','bfs_stock_products_bid_placed_count',
                'confidence_score', 'analysis_reasoning'
            ]
            buyer_df = buyer_df[buyer_cols]

        if not seller_df.empty:
            seller_cols = [
                'date', 'session_id', 'phone_number', 'user_type', 'seller_email','requested_rfq_ai','rfq_response_ai',
                'subscription_plans_requested', 'zero_credit_rfq_attempt',
                'number_of_seller_chats', 'successful_registration', 'failed_registration','bids_accepted_ai',
                'confidence_score','analysis_reasoning'
            ]
            seller_df = seller_df[seller_cols]

        if not unknown_df.empty:
            unknown_cols = [
                'date', 'session_id', 'phone_number', 'user_type', 'email',
                'confidence_score', 'analysis_reasoning', 'unregistered_seller_initiated_chat',
                'unregistered_seller_requested_rfq', 'unregistered_buyer_bfs_only',
                'number_of_faq_or_general_queries','bfs_products_searched_by_unregistered'
            ]
            unknown_df = unknown_df[unknown_cols]

        logger.info(
            f"[CONVERSATION-ANALYTICS] Created DataFrames: "
            f"{len(buyer_df)} buyer rows, {len(seller_df)} seller rows, {len(unknown_df)} unknown rows"
        )

        return buyer_df, seller_df, unknown_df
    
    def _create_seller_rfq_interest_event_df(self, sessions_data: List[Dict[str, Any]], analysis_date: str) -> pd.DataFrame:
        """Create seller RFQ interest event DataFrame from AI metrics."""
        interest_records = []
        for session in sessions_data:
            seller_interest_events = session.get('seller_rfq_interest_event', [])
            if seller_interest_events:
                for event in seller_interest_events:
                    record = {
                        'rfq_id': event.get('rfq_id', ''),
                        'seller_id': event.get('seller_id', ''),
                        'response_date': event.get('response_date', analysis_date),
                        'session_id': session.get('session_id', ''),
                        'phone_number':session.get('phone_number',''),
                        'rfq_notified_at': event.get('rfq_notified_at'),
                        'seller_response_at': event.get('seller_response_at')
                    }
                    interest_records.append(record)
        
        return pd.DataFrame(interest_records)

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
                print("ai",ai_response)
                logger.info(f"ai response:{ai_response}")

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
   - BUYERS: Count successful_rfqs_ai, incomplete_rfqs, buyers_started_but_not_raised_rfq, registrations
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
                        "joined_buyer_df": pd.DataFrame(),
                        "joined_seller_df": pd.DataFrame(),
                        "message": f"No conversations found for analysis on {target_date}"
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
                # Create seller_rfq_interest_event_df from AI metrics
                seller_rfq_interest_event_df = self._create_seller_rfq_interest_event_df(
                    result.get('sessions', []),
                    result.get('date', str(target_date))
                )


                # Query remote database after creating DataFrames
                remote_rfq_df = self._query_remote_users(target_date)
                remote_seller_rfq_df = self._query_remote_seller_rfqs(target_date)
                
                # Get RFQ IDs from seller interest events for filtering
                rfq_ids_filter = seller_rfq_interest_event_df['rfq_id'].unique().tolist() if not seller_rfq_interest_event_df.empty else []
                remote_rfq_category_df = self._query_remote_rfq_categories(target_date, rfq_ids_filter)
                
                # Join seller_rfq_interest_event_df with remote RFQ categories
                if not seller_rfq_interest_event_df.empty and not remote_rfq_category_df.empty:
                    joined_seller_interest_df = seller_rfq_interest_event_df.merge(
                        remote_rfq_category_df, 
                        left_on=['rfq_id'],
                        right_on=['rfq_id'],
                        how='left'
                    )
                else:
                    joined_seller_interest_df = seller_rfq_interest_event_df
                
                # Join buyer_df with remote_rfq_df
                if not buyer_df.empty and not remote_rfq_df.empty:
                    buyer_df['phone_clean'] = buyer_df['phone_number'].str.replace('+', '', regex=False)
                    remote_rfq_df['phone_clean'] = remote_rfq_df['phone'].str.replace('+', '', regex=False)
                    joined_buyer_df= buyer_df.merge(remote_rfq_df, left_on=['phone_clean', 'buyer_email'],
                                   right_on=['phone_clean', 'username'], how='outer')
                    
                    # Coalesce date columns
                    joined_buyer_df['date'] = joined_buyer_df['date_x'].fillna(joined_buyer_df['date_y'])
                    
                    # Keep only relevant columns
                    relevant_cols = [
                        'date', 'phone_number', 'phone_clean','buyer_email','session_id', 'confidence_score',
                        'successful_rfqs_ai', 'incomplete_rfqs', 'buyers_started_but_not_raised_rfq',
                        'username', 'org_uuid', 'user_uuid', 'total_rfqs_raised', 'rfqs_with_seller_responses',
                        'total_items_in_rfqs', 'total_distinct_rfq_category','number_of_buyer_chats',
                        'successful_registration','failed_registration','bfs_searches','products_bid_for','no_of_products_searched',
                        'bfs_stock_products_bid_placed_count', 'bfs_products_searched_list',
                        'analysis_reasoning'
                    ]
                    joined_buyer_df = joined_buyer_df[[col for col in relevant_cols if col in joined_buyer_df.columns]]
                    
                    # Dump joined buyer DataFrame to database
                    self._dump_joined_buyer_df_to_db(joined_buyer_df, db)
                    
                    # Calculate and store daily aggregates
                    self.calculate_and_store_daily_aggregates(target_date, db)
                    
                    # Calculate and store category aggregates
                    self.calculate_and_store_category_aggregates(target_date, db)
                elif not buyer_df.empty and remote_rfq_df.empty:
                    joined_buyer_df = buyer_df
                    self._dump_joined_buyer_df_to_db(joined_buyer_df, db)
                    self.calculate_and_store_daily_aggregates(target_date, db)
                elif buyer_df.empty and not remote_rfq_df.empty:
                    joined_buyer_df = remote_rfq_df.rename(columns={'phone': 'phone_number'})
                    self._dump_joined_buyer_df_to_db(joined_buyer_df, db)
                    self.calculate_and_store_daily_aggregates(target_date, db)
                else:
                    joined_buyer_df = buyer_df
                
                # Join seller_df with remote_seller_rfq_df
                if not seller_df.empty and not remote_seller_rfq_df.empty:
                    seller_df['phone_clean'] = seller_df['phone_number'].str.replace('+', '', regex=False)
                    remote_seller_rfq_df['phone_clean'] = remote_seller_rfq_df['phone'].str.replace('+', '', regex=False)
                    joined_seller_df = seller_df.merge(remote_seller_rfq_df, left_on=['phone_clean','seller_email'],right_on=['phone_clean', 'username'], how='outer')

                    # Join with seller interest data to fill rfq_response_ai
                    if not joined_seller_interest_df.empty:
                        seller_response_counts = joined_seller_interest_df.groupby(['seller_id','session_id','phone_number'])['rfq_id'].nunique().reset_index()
                        seller_response_counts.columns = ['seller_id','session_id','phone_number','rfq_response_count']
                        joined_seller_df = joined_seller_df.merge(seller_response_counts, left_on=['phone_clean','session_id'], right_on=['phone_number','session_id'], how='left')
                        joined_seller_df['rfq_response_ai'] = joined_seller_df['rfq_response_count'].fillna(joined_seller_df['rfq_response_ai']).fillna(0)
                        joined_seller_df.drop(['seller_id', 'rfq_response_count'], axis=1, inplace=True, errors='ignore')

                    # Coalesce date columns - use rfq_date when date is empty
                    joined_seller_df['date'] = joined_seller_df['date'].fillna(joined_seller_df['rfq_date'])
                    
                    # Keep only relevant columns
                    seller_relevant_cols = [
                        'date', 'phone_number','phone_clean', 'seller_email','session_id', 'confidence_score',
                        'requested_rfq_ai', 'rfq_response_ai','subscription_plans_requested', 'zero_credit_rfq_attempt',
                        'number_of_seller_chats', 'successful_registration', 'failed_registration','bids_accepted_ai',
                        'username', 'org_uuid', 'user_uuid', 'total_rfq_responsed', 'analysis_reasoning'
                    ]
                    joined_seller_df = joined_seller_df[[col for col in seller_relevant_cols if col in joined_seller_df.columns]]

                    # Dump joined seller DataFrame to database
                    self._dump_joined_seller_df_to_db(joined_seller_df, db)
                    
                    # Calculate and store seller daily aggregates
                    self.calculate_and_store_seller_daily_aggregates(target_date, db)
                elif not seller_df.empty and remote_seller_rfq_df.empty:
                    joined_seller_df = seller_df
                    self._dump_joined_seller_df_to_db(joined_seller_df, db)
                    self.calculate_and_store_seller_daily_aggregates(target_date, db)
                elif seller_df.empty and not remote_seller_rfq_df.empty:
                    joined_seller_df = remote_seller_rfq_df.rename(columns={'phone': 'phone_number', 'rfq_date': 'date'})
                    self._dump_joined_seller_df_to_db(joined_seller_df, db)
                    self.calculate_and_store_seller_daily_aggregates(target_date, db)
                else:
                    joined_seller_df = seller_df
                
                # Dump unknown DataFrame to database
                if not unknown_df.empty:
                    self._dump_unknown_df_to_db(unknown_df, db)
                    
                    # Calculate and store unknown daily aggregates
                    self.calculate_and_store_unknown_daily_aggregates(target_date, db)
                
                # Dump joined seller interest data to RFQ notification fact table
                if not joined_seller_interest_df.empty:
                    self._dump_joined_seller_interest_to_fact_table(joined_seller_interest_df, db)
                

                
                # Add DataFrames to result
                result["success"] = True
                result["analysis_timestamp"] = datetime.now(timezone.utc).isoformat()
                result["buyer_df"] = joined_buyer_df
                result["seller_df"] = joined_seller_df
                result["unknown_df"] = unknown_df
                result["remote_rfq_df"] = remote_rfq_df
                result["remote_seller_rfq_df"] = remote_seller_rfq_df
                result["joined_buyer_df"] = joined_buyer_df
                result["joined_seller_df"] = joined_seller_df
                result["seller_rfq_interest_event_df"] = seller_rfq_interest_event_df
                result["joined_seller_interest_df"] = joined_seller_interest_df

                logger.info(
                    f"[CONVERSATION-ANALYTICS] Analysis completed for {target_date}. "
                    f"Total sessions analyzed: {result['total_sessions']}, "
                    f"Buyer rows: {len(joined_buyer_df)}, Seller rows: {len(joined_seller_df)}, Unknown rows: {len(unknown_df)}"
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
                "joined_buyer_df": pd.DataFrame(),
                "joined_seller_df": pd.DataFrame(),
                "error": str(e)
            }

    async def __aenter__(self):
        return self

    def _safe_json_value(self, value):
        """Safely convert value to JSON-compatible format or None."""
        try:
            if pd.isna(value):
                return None
            if isinstance(value, str) and value.lower() == 'nan':
                return None
            if hasattr(value, '__len__') and len(value) == 0:
                return None
            return value
        except (ValueError, TypeError):
            return None

    def _dump_joined_buyer_df_to_db(self, joined_buyer_df: pd.DataFrame, db_session) -> None:
        """Dump joined buyer DataFrame to BuyerDailyMetrics table."""
        records_inserted = 0
        skipped = 0
        failed = 0
        
        for _, row in joined_buyer_df.iterrows():
            try:
                # Get date from either date_x or date_y column (from merge)
                date_val = row.get('date_x') if pd.notna(row.get('date_x')) else row.get('date_y')
                if not pd.notna(date_val):
                    date_val = row.get('date')
                
                # Skip rows without date
                if not pd.notna(date_val):
                    skipped += 1
                    continue

                relevant_cols = [
                    'date', 'phone_number', 'email', 'session_id', 'confidence_score',
                    'successful_rfqs_ai', 'incomplete_rfqs', 'buyers_started_but_not_raised_rfq',
                    'username', 'org_uuid', 'user_uuid', 'total_rfqs_raised',
                    'total_items_in_rfqs', 'total_distinct_rfq_category', 'number_of_buyer_chats',
                    'successful_registration', 'failed_registration', 'analysis_reasoning'
                ]
                
                buyer_metric = BuyerDailyMetrics(
                    date=pd.to_datetime(date_val).date(),
                    session_id=str(row.get('session_id', '')),
                    email=str(row.get('buyer_email', '')) if pd.notna(row.get('buyer_email')) and row.get('buyer_email', '') != '' else str(row.get('username', '')) if pd.notna(row.get('username')) else None,
                    phone_number=str(row.get('phone_number', '')) if pd.notna(row.get('phone_number')) else str(row.get('phone_clean', '')) if pd.notna(row.get('phone_clean')) else None,
                    total_rfq_raised=int(row.get('total_rfqs_raised', 0)) if pd.notna(row.get('total_rfqs_raised')) else 0,
                    total_items_in_rfqs=int(row.get('total_items_in_rfqs', 0)) if pd.notna(row.get('total_items_in_rfqs')) else 0,
                    total_distinct_categories_in_rfq=int(row.get('total_distinct_rfq_category', 0)) if pd.notna(row.get('total_distinct_rfq_category')) else 0,
                    total_incomplete_rfq=int(row.get('incomplete_rfq', 0)) if pd.notna(row.get('incomplete_rfq')) else 0,
                    buyers_started_but_not_raised_rfq=int(row.get('buyers_started_but_not_raised_rfq', 0)) if pd.notna(row.get('buyers_started_but_not_raised_rfq')) else 0,
                    failed_registration=int(row.get('failed_registration', 0)) if pd.notna(row.get('failed_registration')) else 0,
                    successfully_registered=int(row.get('successful_registration', 0)) if pd.notna(row.get('successful_registration')) else 0,
                    number_of_chats=int(row.get('number_of_buyer_chats', 0)) if pd.notna(row.get('number_of_buyer_chats')) else 0,
                    successful_rfqs_ai=int(row.get('successful_rfqs_ai', 0)) if pd.notna(row.get('successful_rfqs_ai')) else 0,
                    avg_products_per_rfq=float(row.get('total_items_in_rfqs', 0) / row.get('total_rfqs_raised', 1)) if pd.notna(row.get('total_rfqs_raised')) and row.get('total_rfqs_raised', 0) > 0 else None,
                    avg_categories_per_rfq=float(row.get('total_distinct_rfq_category', 0) / row.get('total_rfqs_raised', 1)) if pd.notna(row.get('total_rfqs_raised')) and row.get('total_rfqs_raised', 0) > 0 else None,
                    bfs_searches=int(row.get('bfs_searches', 0)) if pd.notna(
                        row.get('bfs_searches')) else 0,
                    products_bid_for=int(row.get('products_bid_for', 0)) if pd.notna(
                        row.get('products_bid_for')) else 0,
                    rfq_response_count=int(row.get('rfqs_with_seller_responses', 0)) if pd.notna(
                        row.get('rfqs_with_seller_responses')) else 0,
                    no_of_products_searched=int(row.get('no_of_products_searched', 0)) if pd.notna(
                        row.get('no_of_products_searched')) else 0,
                    bfs_stock_products_bid_placed_count=int(row.get('bfs_stock_products_bid_placed_count', 0)) if pd.notna(
                        row.get('bfs_stock_products_bid_placed_count')) else 0,
                    bfs_products_searched_list=self._safe_json_value(row.get('bfs_products_searched_list')),
                    org_id=str(row.get('org_uuid', '')) if pd.notna(row.get('org_uuid')) else None,
                    uuid=str(row.get('user_uuid', '')) if pd.notna(row.get('user_uuid')) else None,
                    ai_reasoning=str(row.get('analysis_reasoning', '')) if pd.notna(row.get('analysis_reasoning')) else None
                )
                
                # Upsert logic - update if exists, insert if not
                existing = db_session.query(BuyerDailyMetrics).filter(
                    BuyerDailyMetrics.date == buyer_metric.date,
                    BuyerDailyMetrics.email == buyer_metric.email,
                    BuyerDailyMetrics.phone_number == buyer_metric.phone_number
                ).first()
                
                if existing:
                    # Update existing record
                    existing.session_id = buyer_metric.session_id
                    existing.total_rfq_raised = buyer_metric.total_rfq_raised
                    existing.total_items_in_rfqs = buyer_metric.total_items_in_rfqs
                    existing.total_distinct_categories_in_rfq = buyer_metric.total_distinct_categories_in_rfq
                    existing.total_incomplete_rfq = buyer_metric.total_incomplete_rfq
                    existing.buyers_started_but_not_raised_rfq = buyer_metric.buyers_started_but_not_raised_rfq
                    existing.failed_registration = buyer_metric.failed_registration
                    existing.successfully_registered = buyer_metric.successfully_registered
                    existing.number_of_chats = buyer_metric.number_of_chats
                    existing.successful_rfqs_ai = buyer_metric.successful_rfqs_ai
                    existing.avg_products_per_rfq = buyer_metric.avg_products_per_rfq
                    existing.avg_categories_per_rfq = buyer_metric.avg_categories_per_rfq
                    existing.bfs_searches = buyer_metric.bfs_searches
                    existing.products_bid_for = buyer_metric.products_bid_for
                    existing.rfq_response_count = buyer_metric.rfq_response_count
                    existing.no_of_products_searched = buyer_metric.no_of_products_searched
                    existing.bfs_stock_products_bid_placed_count = buyer_metric.bfs_stock_products_bid_placed_count
                    existing.bfs_products_searched_list = buyer_metric.bfs_products_searched_list

                    existing.org_id = buyer_metric.org_id
                    existing.uuid = buyer_metric.uuid
                    existing.ai_reasoning = buyer_metric.ai_reasoning
                else:
                    db_session.add(buyer_metric)
                records_inserted += 1
                
            except Exception as row_error:
                failed += 1
                logger.error(f"[CONVERSATION-ANALYTICS] Failed to process buyer row {row.get('session_id', 'unknown')}: {row_error}")
                continue
        
        try:
            db_session.commit()
            logger.info(f"[CONVERSATION-ANALYTICS] Buyer metrics: {records_inserted} inserted, {skipped} skipped (no date), {failed} failed")
        except Exception as commit_error:
            logger.error(f"[CONVERSATION-ANALYTICS] Failed to commit buyer metrics: {commit_error}")
            db_session.rollback()
    
    def calculate_and_store_daily_aggregates(self, target_date, db_session) -> None:
        """Calculate aggregates from buyer_daily_metrics and store in daily_aggregates table."""
        inserted = 0
        updated = 0
        failed = 0
        
        try:
            from sqlalchemy import func, distinct
            
            # Query buyer_daily_metrics for the target date
            buyer_metrics = db_session.query(BuyerDailyMetrics).filter(
                BuyerDailyMetrics.date == target_date
            ).all()
            
            if not buyer_metrics:
                logger.info(f"[CONVERSATION-ANALYTICS] No buyer metrics found for {target_date}")
                return
            
            # Calculate aggregates with metric S.No
            aggregates = [
                ('buyer', 1, 'Number of Chats Initiated By Buyers', sum(m.number_of_chats for m in buyer_metrics)),
                ('buyer', 2, 'Unique Buyers', len(set((m.email, m.phone_number) for m in buyer_metrics if m.email or m.phone_number))),
                ('buyer', 3, 'Total RFQs Submitted', sum(m.total_rfq_raised for m in buyer_metrics)),
                ('buyer', 4, 'Unique Buyers Submitted RFQ', len(set((m.email, m.phone_number) for m in buyer_metrics if m.total_rfq_raised > 0 and (m.email or m.phone_number)))),
                ('buyer', 5, 'Avg Products per RFQ', 
                 sum(m.total_items_in_rfqs for m in buyer_metrics) / sum(m.total_rfq_raised for m in buyer_metrics) 
                 if sum(m.total_rfq_raised for m in buyer_metrics) > 0 else 0),
                ('buyer', 6, 'Avg Categories per RFQ', 
                 sum(m.total_distinct_categories_in_rfq for m in buyer_metrics) / sum(m.total_rfq_raised for m in buyer_metrics) 
                 if sum(m.total_rfq_raised for m in buyer_metrics) > 0 else 0),
                ('buyer', 7, 'Incomplete RFQs', sum(getattr(m, 'total_incomplete_rfq', 0) for m in buyer_metrics)),
                ('buyer', 8, 'No. of Registrations failed', sum(m.failed_registration for m in buyer_metrics)),
                ('buyer', 9, 'Total Items in All RFQs', sum(m.total_items_in_rfqs for m in buyer_metrics)),
                ('buyer', 10, 'Total Distinct RFQ Category Combinations', sum(m.total_distinct_categories_in_rfq for m in buyer_metrics)),
                ('buyer', 25, 'RFQs with At Least One Response', self._calculate_rfqs_with_response(target_date, db_session)),
                ('buyer', 26, 'Total RFQ Responses', self._calculate_total_rfq_responses(target_date, db_session)),
                ('buyer', 27, 'No of Buyers Started But Not Raised RFQ', sum(m.buyers_started_but_not_raised_rfq for m in buyer_metrics)),
                ('buyer', 28, 'No of Products Searched by Buyers', sum(m.no_of_products_searched for m in buyer_metrics)),
                ('buyer', 29, 'No of Unique Buyers searched for BFS Items', len(set((m.email, m.phone_number) for m in buyer_metrics if m.bfs_searches > 0 and (m.email or m.phone_number)))),
                ('buyer', 30, 'No of Unique buyers participated in Bidding', len(set((m.email, m.phone_number) for m in buyer_metrics if m.products_bid_for > 0 and (m.email or m.phone_number)))),
                ('buyer', 31, 'BFS Products Bids Count', sum(m.bfs_stock_products_bid_placed_count for m in buyer_metrics))
            ]
            
            # Insert or update aggregates
            for role, metric_s_no, metric_name, value in aggregates:
                try:
                    existing = db_session.query(DailyAggregates).filter(
                        DailyAggregates.date == target_date,
                        DailyAggregates.role == role,
                        DailyAggregates.metric_s_no == metric_s_no
                    ).first()
                    
                    if existing:
                        existing.value = value
                        updated += 1
                    else:
                        aggregate = DailyAggregates(
                            date=target_date,
                            role=role,
                            metric_s_no=metric_s_no,
                            metric_name=metric_name,
                            value=value
                        )
                        db_session.add(aggregate)
                        inserted += 1
                except Exception as agg_error:
                    failed += 1
                    logger.error(f"[CONVERSATION-ANALYTICS] Failed to process daily aggregate {metric_name}: {agg_error}")
                    continue
            
            db_session.commit()
            logger.info(f"[CONVERSATION-ANALYTICS] Daily aggregates: {inserted} inserted, {updated} updated, {failed} failed")
            
        except Exception as e:
            logger.error(f"[CONVERSATION-ANALYTICS] Failed to calculate daily aggregates: {e}")
            db_session.rollback()
    
    def _query_remote_users(self, target_date: date):
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
    COUNT(DISTINCT grv.rfq_uuid) AS rfqs_with_seller_responses,
    u.org_uuid AS org_id
FROM user u
JOIN rfq_header rfh 
    ON u.uuid = rfh.user
LEFT JOIN rfq_items ri 
    ON ri.rfq_uuid = rfh.uuid
LEFT JOIN development_gmtbfs.gmt_rfq_vendors grv
    ON grv.rfq_uuid = rfh.uuid
WHERE 
    DATE(rfh.created_ts) = :target_date
    AND u.is_active = 1
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
            result = remote_db.execute(text(query), {'target_date': str(target_date)})
            rfq_data = [dict(row._mapping) for row in result]
            remote_db.close()
            rfq_df = pd.DataFrame(rfq_data)
            logger.info(f"[CONVERSATION-ANALYTICS] Retrieved {len(rfq_df)} RFQ records from remote database")
            return rfq_df
        except Exception as e:
            logger.error(f"[CONVERSATION-ANALYTICS] Remote database query failed: {e}")
            return []
    
    def _query_remote_seller_rfqs(self, target_date: date):
        """Query remote database for seller RFQ data with quotations."""
        try:
            remote_db = get_remote_db_session()
            query = """
            SELECT 
                DATE(rfqv.created_ts) AS rfq_date,
                u.uuid,
                u.org_uuid,
                u.username,
                u.phone ,
                COUNT(DISTINCT rfqv.rfq_uuid) AS total_rfq_responsed
            FROM development_gmtbfs.gmt_rfq_vendors rfqv
            LEFT JOIN user u 
                ON u.org_uuid = rfqv.vendor_uuid
            WHERE u.self_client=0 AND u.is_active=1 and u.source_type='W'
              AND DATE(rfqv.created_ts) = :target_date
            GROUP BY DATE(rfqv.created_ts), u.uuid
            """
            result = remote_db.execute(text(query), {'target_date': str(target_date)})
            seller_rfq_data = [dict(row._mapping) for row in result]
            remote_db.close()
            seller_rfq_df = pd.DataFrame(seller_rfq_data)
            logger.info(f"[CONVERSATION-ANALYTICS] Retrieved {len(seller_rfq_df)} seller RFQ records from remote database")
            return seller_rfq_df
        except Exception as e:
            logger.error(f"[CONVERSATION-ANALYTICS] Remote seller RFQ query failed: {e}")
            return pd.DataFrame()
    
    def _query_remote_rfq_categories(self, target_date: date, rfq_ids_filter: list = None):
        """Query remote database for RFQ categories data."""
        try:
            remote_db = get_remote_db_session()
            
            # Base query
            query = """
            SELECT
                DATE(rh.created_ts) AS date,
                rh.rfq_id,
                rh.uuid AS rfq_uuid,
                ri.category as category
            FROM rfq_header rh
            JOIN rfq_items ri
                ON rh.uuid = ri.rfq_uuid
            WHERE rh.source_type="W"
            """
            
            # Add RFQ ID filter if provided
            params = {'target_date': str(target_date)}
            if rfq_ids_filter:
                placeholders = ','.join([f':rfq_id_{i}' for i in range(len(rfq_ids_filter))])
                query += f" AND rh.rfq_id IN ({placeholders})"
                for i, rfq_id in enumerate(rfq_ids_filter):
                    params[f'rfq_id_{i}'] = rfq_id
            
            query += """
            GROUP BY
                rh.rfq_id,
                rh.uuid,
                ri.category
            """
            
            result = remote_db.execute(text(query), params)
            rfq_category_data = [dict(row._mapping) for row in result]
            remote_db.close()
            rfq_category_df = pd.DataFrame(rfq_category_data)
            logger.info(f"[CONVERSATION-ANALYTICS] Retrieved {len(rfq_category_df)} RFQ category records from remote database")
            return rfq_category_df
        except Exception as e:
            logger.error(f"[CONVERSATION-ANALYTICS] Remote RFQ category query failed: {e}")
            return pd.DataFrame()
    
    def _dump_joined_seller_df_to_db(self, joined_seller_df: pd.DataFrame, db_session) -> None:
        """Dump joined seller DataFrame to SellerDailyMetrics table."""
        records_inserted = 0
        skipped = 0
        failed = 0
        
        for _, row in joined_seller_df.iterrows():
            try:
                # Get date from either date or rfq_date column (from merge)
                date_val = row.get('date') if pd.notna(row.get('date')) else row.get('rfq_date')
                
                # Skip rows without date
                if not pd.notna(date_val):
                    skipped += 1
                    continue
                
                seller_metric = SellerDailyMetrics(
                    date=pd.to_datetime(date_val).date(),
                    session_id=str(row.get('session_id', '')),
                    email=str(row.get('seller_email', '')) if pd.notna(row.get('seller_email')) and row.get('seller_email', '') != '' else str(row.get('username', '')) if pd.notna(row.get('username')) else None,
                    phone_number=str(row.get('phone_number', '')) if pd.notna(row.get('phone_number')) else str(row.get('phone_clean', '')) if pd.notna(row.get('phone_clean')) else None,
                    number_of_chats=int(row.get('number_of_seller_chats', 0)) if pd.notna(row.get('number_of_seller_chats')) else 0,
                    seller_failed_registration=int(row.get('failed_registration', 0)) if pd.notna(row.get('failed_registration')) else 0,
                    seller_successful_registration=int(row.get('successful_registration', 0)) if pd.notna(row.get('successful_registration')) else 0,
                    ai_reasoning=str(row.get('analysis_reasoning', '')) if pd.notna(row.get('analysis_reasoning')) else None,
                    rfq_requested_ai=int(row.get('requested_rfq_ai', 0)) if pd.notna(row.get('requested_rfq_ai')) else 0,
                    rfq_response_ai=int(row.get('rfq_response_ai', 0)) if pd.notna(row.get('rfq_response_ai')) else 0,
                    subscription_plans_requested=int(row.get('subscription_plans_requested', 0)) if pd.notna(row.get('subscription_plans_requested')) else 0,
                    zero_credit_rfq_attempt=int(row.get('zero_credit_rfq_attempt', 0)) if pd.notna(row.get('zero_credit_rfq_attempt')) else 0,
                    org_id=str(row.get('org_uuid', '')) if pd.notna(row.get('org_uuid')) else None,
                    uuid=str(row.get('user_uuid', '')) if pd.notna(row.get('user_uuid')) else None,
                    total_rfqs_requested=int(row.get('total_rfq_responsed', 0)) if pd.notna(row.get('total_rfq_responsed')) else 0,
                    bids_accepted_ai=int(row.get('bids_accepted_ai', 0)) if pd.notna(row.get('bids_accepted_ai')) else 0
                )
                
                # Upsert logic - update if exists, insert if not bids_accepted_ai
                existing = db_session.query(SellerDailyMetrics).filter(
                    SellerDailyMetrics.date == seller_metric.date,
                    SellerDailyMetrics.email == seller_metric.email,
                    SellerDailyMetrics.phone_number == seller_metric.phone_number
                ).first()
                
                if existing:
                    # Update existing record
                    existing.session_id = seller_metric.session_id
                    existing.number_of_chats = seller_metric.number_of_chats
                    existing.seller_failed_registration = seller_metric.seller_failed_registration
                    existing.seller_successful_registration = seller_metric.seller_successful_registration
                    existing.ai_reasoning = seller_metric.ai_reasoning
                    existing.rfq_requested_ai = seller_metric.rfq_requested_ai
                    existing.rfq_response_ai = seller_metric.rfq_response_ai
                    existing.subscription_plans_requested = seller_metric.subscription_plans_requested
                    existing.zero_credit_rfq_attempt = seller_metric.zero_credit_rfq_attempt
                    existing.org_id = seller_metric.org_id
                    existing.uuid = seller_metric.uuid
                    existing.total_rfqs_requested = seller_metric.total_rfqs_requested
                    existing.bids_accepted_ai = seller_metric.bids_accepted_ai
                else:
                    db_session.add(seller_metric)
                records_inserted += 1
                
            except Exception as row_error:
                failed += 1
                logger.error(f"[CONVERSATION-ANALYTICS] Failed to process seller row {row.get('session_id', 'unknown')}: {row_error}")
                continue
        
        try:
            db_session.commit()
            logger.info(f"[CONVERSATION-ANALYTICS] Seller metrics: {records_inserted} inserted, {skipped} skipped (no date), {failed} failed")
        except Exception as commit_error:
            logger.error(f"[CONVERSATION-ANALYTICS] Failed to commit seller metrics: {commit_error}")
            db_session.rollback()
    
    def calculate_and_store_seller_daily_aggregates(self, target_date, db_session) -> None:
        """Calculate seller aggregates from seller_daily_metrics and store in daily_aggregates table."""
        inserted = 0
        updated = 0
        failed = 0
        
        try:
            seller_metrics = db_session.query(SellerDailyMetrics).filter(
                SellerDailyMetrics.date == target_date
            ).all()
            
            if not seller_metrics:
                logger.info(f"[CONVERSATION-ANALYTICS] No seller metrics found for {target_date}")
                return
            
            seller_aggregates = [
                ('seller', 11, 'Seller Chats Initiated', sum(m.number_of_chats for m in seller_metrics)),
                ('seller', 12, 'Unique Sellers', len(set((m.email, m.phone_number) for m in seller_metrics if m.email or m.phone_number))),
                ('seller', 13, 'Total RFQs Requested', sum(m.total_rfqs_requested for m in seller_metrics)),
                ('seller', 14, 'Subscription Plans Requested', sum(m.subscription_plans_requested for m in seller_metrics)),
                ('seller', 15, 'Seller Registration Failed', sum(m.seller_failed_registration for m in seller_metrics)),
                ('seller', 16, 'Seller Successful Registration', sum(m.seller_successful_registration for m in seller_metrics)),
                ('seller', 17, 'Zero Credit RFQ Attempt', sum(m.zero_credit_rfq_attempt for m in seller_metrics)),
                ('seller', 24, 'Total RFQ Responded', sum(getattr(m, 'rfq_response_ai', 0) for m in seller_metrics))
            ]
            
            for role, metric_s_no, metric_name, value in seller_aggregates:
                try:
                    existing = db_session.query(DailyAggregates).filter(
                        DailyAggregates.date == target_date,
                        DailyAggregates.role == role,
                        DailyAggregates.metric_s_no == metric_s_no
                    ).first()
                    
                    if existing:
                        existing.value = value
                        updated += 1
                    else:
                        aggregate = DailyAggregates(
                            date=target_date,
                            role=role,
                            metric_s_no=metric_s_no,
                            metric_name=metric_name,
                            value=value
                        )
                        db_session.add(aggregate)
                        inserted += 1
                except Exception as agg_error:
                    failed += 1
                    logger.error(f"[CONVERSATION-ANALYTICS] Failed to process seller aggregate {metric_name}: {agg_error}")
                    continue
            
            db_session.commit()
            logger.info(f"[CONVERSATION-ANALYTICS] Seller aggregates: {inserted} inserted, {updated} updated, {failed} failed")
            
        except Exception as e:
            logger.error(f"[CONVERSATION-ANALYTICS] Failed to calculate seller daily aggregates: {e}")
            db_session.rollback()
    
    def _calculate_rfqs_with_response(self, target_date, db_session) -> int:
        """Calculate RFQs with at least one response from rfq_notification_fact table."""
        try:
            count = db_session.query(RFQNotificationFact.rfq_id).filter(
                RFQNotificationFact.date == target_date,
                RFQNotificationFact.seller_response_at.isnot(None)
            ).distinct().count()
            return count
        except Exception as e:
            logger.error(f"Error calculating RFQs with response: {e}")
            return 0
    
    def _calculate_total_rfq_responses(self, target_date, db_session) -> int:
        """Calculate total RFQ responses from rfq_notification_fact table."""
        try:
            count = db_session.query(RFQNotificationFact).filter(
                RFQNotificationFact.date == target_date,
                RFQNotificationFact.seller_response_at.isnot(None)
            ).count()
            return count
        except Exception as e:
            logger.error(f"Error calculating total RFQ responses: {e}")
            return 0
    
    def calculate_and_store_category_aggregates(self, target_date, db_session) -> None:
        """Calculate category aggregates from remote database and store in category_aggregates table."""
        inserted = 0
        updated = 0
        failed = 0
        
        try:
            remote_db = get_remote_db_session()
            query = """
            SELECT
                t.rfq_date,
                t.category,
                t.total_rfq_raised,
                q.rfqs_with_quotations AS rfqs_with_quotations,
                b.bids_requested AS bids_requested,
                b.bids_accepted AS bids_accepted
            FROM (
                /* RFQs Raised */
                SELECT
                    DATE(ri.created_ts) AS rfq_date,
                    ri.category,
                    COUNT(DISTINCT ri.rfq_uuid) AS total_rfq_raised
                FROM rfq_items ri
                JOIN rfq_header rh
                    ON ri.rfq_uuid = rh.uuid
                WHERE DATE(ri.created_ts) = :target_date
                  AND rh.source_type = 'W'
                GROUP BY DATE(ri.created_ts), ri.category
            ) t
            LEFT JOIN (
                /* RFQs With Quotations */
                SELECT
                    DATE(rfqv.created_ts) AS rfq_date,
                    ri.category,
                    COUNT(DISTINCT rfqv.rfq_uuid) AS rfqs_with_quotations
                FROM development_gmtbfs.gmt_rfq_vendors rfqv
                JOIN rfq_items ri
                    ON rfqv.rfq_uuid = ri.rfq_uuid
                JOIN rfq_header rh
                    ON ri.rfq_uuid = rh.uuid
                JOIN user u
                    ON u.org_uuid = rfqv.vendor_uuid
                WHERE DATE(rfqv.created_ts) = :target_date
                  AND rh.source_type = 'W'
                  AND u.self_client = 0
                  AND u.is_active = 1
                  AND u.source_type = 'W'
                GROUP BY DATE(rfqv.created_ts), ri.category
            ) q
                ON t.rfq_date = q.rfq_date
               AND t.category = q.category
            LEFT JOIN (
                /* Bids Requested & Accepted */
                SELECT
                    DATE(bu.created_ts) AS rfq_date,
                    bi.category,
                    COUNT(CASE WHEN bu.status_uuid = 115 THEN 1 END) AS bids_requested,
                    COUNT(CASE WHEN bu.status_uuid = 118 THEN 1 END) AS bids_accepted
                FROM development_gmtbfs.bfs_users bu
                JOIN development_gmtbfs.bfs_items bi
                    ON bu.items_uuid = bi.uuid
                WHERE DATE(bu.created_ts) = :target_date
                GROUP BY DATE(bu.created_ts), bi.category
            ) b
                ON t.rfq_date = b.rfq_date
               AND t.category = b.category;
            """

            result = remote_db.execute(text(query), {'target_date': str(target_date)})
            rows = result.fetchall()

            remote_db.close()

            if not rows:
                logger.info(f"[CONVERSATION-ANALYTICS] No category data found for {target_date}")
                return

            # Get total_rfqs_intimated from rfq_notification_fact table
            intimated_query = text("""
                SELECT
                    category,
                    COUNT(DISTINCT rfq_id) AS total_rfqs_intimated
                FROM procurement_db.rfq_notification_fact
                WHERE date = :target_date
                GROUP BY category
            """)
            intimated_result = db_session.execute(intimated_query, {'target_date': target_date})
            intimated_dict = {row[0]: int(row[1]) for row in intimated_result}

            # Get BFS product categories from buyer daily metrics
            bfs_category_counts, unregistered_bfs_category_counts = self._get_bfs_category_counts_combined(target_date, db_session)

            # Process RFQ categories
            for row in rows:
                try:
                    category_name = row[1] if row[1] is not None else 'Unknown'
                    total_rfq_raised = int(row[2]) if row[2] is not None else 0
                    rfqs_with_quotations = int(row[3]) if row[3] is not None else 0
                    bids_requested = int(row[4]) if row[4] is not None else 0
                    bids_accepted = int(row[5]) if row[5] is not None else 0
                    total_rfqs_intimated = intimated_dict.get(category_name, 0)
                    
                    existing = db_session.query(CategoryAggregates).filter(
                        CategoryAggregates.date == target_date,
                        CategoryAggregates.category_name == category_name
                    ).first()

                    if existing:
                        existing.total_rfq_raised_category = total_rfq_raised
                        existing.total_rfqs_with_quotations = rfqs_with_quotations
                        existing.total_rfqs_intimated = total_rfqs_intimated
                        existing.bids_requested = bids_requested
                        existing.bids_accepted = bids_accepted
                        existing.bfs_products_searched_count = bfs_category_counts.get(category_name, 0)
                        existing.bfs_products_searched_by_unregistered_count = unregistered_bfs_category_counts.get(category_name, 0)
                        updated += 1
                    else:
                        aggregate = CategoryAggregates(
                            date=target_date,
                            category_name=category_name,
                            total_rfq_raised_category=total_rfq_raised,
                            total_rfqs_with_quotations=rfqs_with_quotations,
                            total_rfqs_intimated=total_rfqs_intimated,
                            bids_requested=bids_requested,
                            bids_accepted=bids_accepted,
                            bfs_products_searched_count=bfs_category_counts.get(category_name, 0),
                            bfs_products_searched_by_unregistered_count=unregistered_bfs_category_counts.get(category_name, 0)
                        )
                        db_session.add(aggregate)
                        inserted += 1
                except Exception as cat_error:
                    failed += 1
                    logger.error(f"[CONVERSATION-ANALYTICS] Failed to process category {row[1] if len(row) > 1 else 'unknown'}: {cat_error}")
                    continue

            # Process BFS-only categories
            all_bfs_categories = set(bfs_category_counts.keys()) | set(unregistered_bfs_category_counts.keys())
            for category_name in all_bfs_categories:
                try:
                    if not any(row[1] == category_name for row in rows if row[1]):
                        existing = db_session.query(CategoryAggregates).filter(
                            CategoryAggregates.date == target_date,
                            CategoryAggregates.category_name == category_name
                        ).first()
                        
                        if not existing:
                            aggregate = CategoryAggregates(
                                date=target_date,
                                category_name=category_name,
                                total_rfq_raised_category=0,
                                total_rfqs_with_quotations=0,
                                total_rfqs_intimated=intimated_dict.get(category_name, 0),
                                bids_requested=0,
                                bids_accepted=0,
                                bfs_products_searched_count=bfs_category_counts.get(category_name, 0),
                                bfs_products_searched_by_unregistered_count=unregistered_bfs_category_counts.get(category_name, 0)
                            )
                            db_session.add(aggregate)
                            inserted += 1
                except Exception as bfs_error:
                    failed += 1
                    logger.error(f"[CONVERSATION-ANALYTICS] Failed to process BFS category {category_name}: {bfs_error}")
                    continue

            db_session.commit()
            logger.info(f"[CONVERSATION-ANALYTICS] Category aggregates: {inserted} inserted, {updated} updated, {failed} failed")

        except Exception as e:
            logger.error(f"[CONVERSATION-ANALYTICS] Failed to calculate category aggregates: {e}")
            db_session.rollback()

    def _get_bfs_category_counts_combined(self, target_date, db_session) -> tuple:
        """Get BFS product category counts from both buyer and unknown daily metrics using pandas."""
        try:
            # Get buyer metrics data
            buyer_query = db_session.query(BuyerDailyMetrics.bfs_products_searched_list).filter(
                BuyerDailyMetrics.date == target_date,
                BuyerDailyMetrics.bfs_products_searched_list.isnot(None)
            )
            buyer_df = pd.read_sql(buyer_query.statement, db_session.bind)
            
            # Get unknown metrics data
            unknown_query = db_session.query(UnknownDailyMetrics.bfs_products_searched_by_unregistered).filter(
                UnknownDailyMetrics.date == target_date,
                UnknownDailyMetrics.bfs_products_searched_by_unregistered.isnot(None)
            )
            unknown_df = pd.read_sql(unknown_query.statement, db_session.bind)
            
            # Extract all unique products
            all_products = set()
            
            # From buyer data
            if not buyer_df.empty:
                buyer_products = buyer_df['bfs_products_searched_list'].dropna().apply(
                    lambda x: x if isinstance(x, list) else []
                ).explode().dropna().unique()
                all_products.update(buyer_products)
            
            # From unknown data
            if not unknown_df.empty:
                unknown_products = unknown_df['bfs_products_searched_by_unregistered'].dropna().apply(
                    lambda x: x if isinstance(x, list) else []
                ).explode().dropna().unique()
                all_products.update(unknown_products)
            
            if not all_products:
                return {}, {}
            
            # Get product-category mapping from BFS database
            product_category_map = self._get_product_category_mapping(list(all_products))
            
            # Count categories for buyer data
            buyer_category_counts = self._count_categories_from_df(
                buyer_df, 'bfs_products_searched_list', product_category_map
            )
            
            # Count categories for unknown data
            unknown_category_counts = self._count_categories_from_df(
                unknown_df, 'bfs_products_searched_by_unregistered', product_category_map
            )
            
            return buyer_category_counts, unknown_category_counts
            
        except Exception as e:
            logger.error(f"Failed to get BFS category counts: {e}")
            return {}, {}
    
    def _get_product_category_mapping(self, products: list) -> dict:
        """Get product to category mapping from BFS database."""
        try:
            remote_db = get_remote_db_session()
            placeholders = ','.join([f':product{i}' for i in range(len(products))])
            like_conditions = ' OR '.join([f'description LIKE :like{i}' for i in range(len(products))])
            
            query = text(f"""
                SELECT DISTINCT category, description 
                FROM development_gmtbfs.bfs_items 
                WHERE description IN ({placeholders}) OR {like_conditions}
            """)
            
            params = {}
            for i, product in enumerate(products):
                params[f'product{i}'] = product
                params[f'like{i}'] = f"{product}%"
            
            result = remote_db.execute(query, params)
            mapping = {row[1]: row[0] for row in result.fetchall()}
            remote_db.close()
            
            return mapping
            
        except Exception as e:
            logger.error(f"Failed to get product category mapping: {e}")
            return {}
    
    def _count_categories_from_df(self, df: pd.DataFrame, column_name: str, product_category_map: dict) -> dict:
        """Count categories from DataFrame using pandas operations."""
        if df.empty:
            return {}
        
        # Explode products and map to categories
        products_series = df[column_name].dropna().apply(
            lambda x: x if isinstance(x, list) else []
        ).explode().dropna()
        
        # Map products to categories
        category_series = products_series.map(
            lambda product: next(
                (cat for desc, cat in product_category_map.items() 
                 if desc == product or desc.startswith(product)), None
            )
        ).dropna()
        
        # Count categories
        return category_series.value_counts().to_dict()
    
   

    def calculate_and_store_unknown_daily_aggregates(self, target_date, db_session) -> None:
        """Calculate unknown user aggregates from unknown_daily_metrics and store in daily_aggregates table."""
        inserted = 0
        updated = 0
        failed = 0
        
        try:
            unknown_metrics = db_session.query(UnknownDailyMetrics).filter(
                UnknownDailyMetrics.date == target_date
            ).all()
            
            if not unknown_metrics:
                logger.info(f"[CONVERSATION-ANALYTICS] No unknown metrics found for {target_date}")
                return
            
            unknown_aggregates = [
                ('unknown', 18, 'Unknown User Sessions', len(unknown_metrics)),
                ('unknown', 19, 'Unique Unknown Users', len(set((m.email, m.phone_number) for m in unknown_metrics if m.email or m.phone_number))),
                ('unknown', 20, 'Unregistered Sellers Initiated Chat But Not Registered', sum(m.unregistered_seller_initiated_chat for m in unknown_metrics)),
                ('unknown', 21, 'Unregistered Sellers Requested for RFQ', sum(m.unregistered_seller_requested_rfq for m in unknown_metrics)),
                ('unknown', 22, 'Unregistered Buyers Initiated Chat But Not Continued', sum(m.unregistered_buyer_bfs_only for m in unknown_metrics)),
                ('unknown', 23, 'Number of FAQ or General Queries', sum(m.number_of_faq_or_general_queries for m in unknown_metrics))
            ]
            
            for role, metric_s_no, metric_name, value in unknown_aggregates:
                try:
                    existing = db_session.query(DailyAggregates).filter(
                        DailyAggregates.date == target_date,
                        DailyAggregates.role == role,
                        DailyAggregates.metric_s_no == metric_s_no
                    ).first()
                    
                    if existing:
                        existing.value = value
                        updated += 1
                    else:
                        aggregate = DailyAggregates(
                            date=target_date,
                            role=role,
                            metric_s_no=metric_s_no,
                            metric_name=metric_name,
                            value=value
                        )
                        db_session.add(aggregate)
                        inserted += 1
                except Exception as agg_error:
                    failed += 1
                    logger.error(f"[CONVERSATION-ANALYTICS] Failed to process unknown aggregate {metric_name}: {agg_error}")
                    continue
            
            db_session.commit()
            logger.info(f"[CONVERSATION-ANALYTICS] Unknown aggregates: {inserted} inserted, {updated} updated, {failed} failed")
            
        except Exception as e:
            logger.error(f"[CONVERSATION-ANALYTICS] Failed to calculate unknown daily aggregates: {e}")
            db_session.rollback()
    
    def _dump_unknown_df_to_db(self, unknown_df: pd.DataFrame, db_session) -> None:
        """Dump unknown DataFrame to UnknownDailyMetrics table."""
        records_inserted = 0
        skipped = 0
        failed = 0
        
        for _, row in unknown_df.iterrows():
            try:
                date_val = row.get('date')
                
                if not pd.notna(date_val):
                    skipped += 1
                    continue
                
                unknown_metric = UnknownDailyMetrics(
                    date=pd.to_datetime(date_val).date(),
                    session_id=str(row.get('session_id', '')),
                    phone_number=str(row.get('phone_number', '')) if pd.notna(row.get('phone_number')) else None,
                    user_type=str(row.get('user_type', 'unknown')),
                    email=str(row.get('email', '')) if pd.notna(row.get('email')) else None,
                    confidence_score=float(row.get('confidence_score', 0)) if pd.notna(row.get('confidence_score')) else None,
                    ai_reasoning=str(row.get('analysis_reasoning', '')) if pd.notna(row.get('analysis_reasoning')) else None,
                    unregistered_seller_initiated_chat=int(row.get('unregistered_seller_initiated_chat', 0)) if pd.notna(row.get('unregistered_seller_initiated_chat')) else 0,
                    unregistered_seller_requested_rfq=int(row.get('unregistered_seller_requested_rfq', 0)) if pd.notna(row.get('unregistered_seller_requested_rfq')) else 0,
                    unregistered_buyer_bfs_only=int(row.get('unregistered_buyer_bfs_only', 0)) if pd.notna(row.get('unregistered_buyer_bfs_only')) else 0,
                    number_of_faq_or_general_queries=int(row.get('number_of_faq_or_general_queries', 0)) if pd.notna(row.get('number_of_faq_or_general_queries')) else 0
                )
                
                # Upsert logic - update if exists, insert if not
                existing = db_session.query(UnknownDailyMetrics).filter(
                    UnknownDailyMetrics.date == unknown_metric.date,
                    UnknownDailyMetrics.session_id == unknown_metric.session_id,
                    UnknownDailyMetrics.phone_number == unknown_metric.phone_number
                ).first()
                
                if existing:
                    # Update existing record
                    existing.user_type = unknown_metric.user_type
                    existing.email = unknown_metric.email
                    existing.confidence_score = unknown_metric.confidence_score
                    existing.ai_reasoning = unknown_metric.ai_reasoning
                    existing.unregistered_seller_initiated_chat = unknown_metric.unregistered_seller_initiated_chat
                    existing.unregistered_seller_requested_rfq = unknown_metric.unregistered_seller_requested_rfq
                    existing.unregistered_buyer_bfs_only = unknown_metric.unregistered_buyer_bfs_only
                    existing.number_of_faq_or_general_queries = unknown_metric.number_of_faq_or_general_queries
                else:
                    db_session.add(unknown_metric)
                records_inserted += 1
                
            except Exception as row_error:
                failed += 1
                logger.error(f"[CONVERSATION-ANALYTICS] Failed to process unknown row {row.get('session_id', 'unknown')}: {row_error}")
                continue
        
        try:
            db_session.commit()
            logger.info(f"[CONVERSATION-ANALYTICS] Unknown metrics: {records_inserted} inserted, {skipped} skipped (no date), {failed} failed")
        except Exception as commit_error:
            logger.error(f"[CONVERSATION-ANALYTICS] Failed to commit unknown metrics: {commit_error}")
            db_session.rollback()

    def _dump_joined_seller_interest_to_fact_table(self, joined_seller_interest_df: pd.DataFrame, db_session) -> None:
        """Dump joined seller interest DataFrame to RFQNotificationFact table."""
        records_inserted = 0
        records_updated = 0
        skipped = 0
        failed = 0
        
        for _, row in joined_seller_interest_df.iterrows():
            try:
                # Extract date from response_date or use current date
                date_val = row.get('response_date') or row.get('date')
                if not pd.notna(date_val):
                    date_val = datetime.now().date()
                else:
                    date_val = pd.to_datetime(date_val).date()
                
                # Validate required fields
                session_id = str(row.get('session_id', ''))
                rfq_id = str(row.get('rfq_id', ''))
                seller_id = str(row.get('seller_id', ''))
                
                if not all([session_id, rfq_id, seller_id]):
                    skipped += 1
                    continue
                
                # Parse timestamps
                rfq_notified_at = None
                seller_response_at = None
                
                if pd.notna(row.get('rfq_notified_at')):
                    try:
                        dt_val = pd.to_datetime(row.get('rfq_notified_at'))
                        if not pd.isna(dt_val):
                            rfq_notified_at = dt_val.to_pydatetime()
                    except:
                        pass
                
                if pd.notna(row.get('seller_response_at')):
                    try:
                        dt_val = pd.to_datetime(row.get('seller_response_at'))
                        if not pd.isna(dt_val):
                            seller_response_at = dt_val.to_pydatetime()
                    except:
                        pass
                
                # Check if record exists
                existing = db_session.query(RFQNotificationFact).filter(
                    RFQNotificationFact.date == date_val,
                    RFQNotificationFact.session_id == session_id,
                    RFQNotificationFact.rfq_id == rfq_id,
                    RFQNotificationFact.seller_id == seller_id,
                    RFQNotificationFact.category == str(row.get('category', '')) if pd.notna(row.get('category')) else None
                ).first()
                
                if existing:
                    # Update existing record
                    existing.category = str(row.get('category', '')) if pd.notna(row.get('category')) else None
                    existing.rfq_notified_at = rfq_notified_at
                    existing.seller_response_at = seller_response_at
                    existing.ai_reasoning = str(row.get('ai_reasoning', '')) if pd.notna(row.get('ai_reasoning')) else None
                    records_updated += 1
                else:
                    # Create new record
                    fact_record = RFQNotificationFact(
                        date=date_val,
                        session_id=session_id,
                        rfq_id=rfq_id,
                        seller_id=seller_id,
                        category=str(row.get('category', '')) if pd.notna(row.get('category')) else None,
                        rfq_notified_at=rfq_notified_at,
                        seller_response_at=seller_response_at,
                        ai_reasoning=str(row.get('ai_reasoning', '')) if pd.notna(row.get('ai_reasoning')) else None
                    )
                    db_session.add(fact_record)
                    records_inserted += 1
                    
            except Exception as row_error:
                failed += 1
                logger.error(f"[CONVERSATION-ANALYTICS] Failed to process seller interest row {row.get('rfq_id', 'unknown')}: {row_error}")
                continue
        
        try:
            db_session.commit()
            logger.info(
                f"[CONVERSATION-ANALYTICS] RFQ notification fact: "
                f"{records_inserted} inserted, {records_updated} updated, {skipped} skipped, {failed} failed"
            )
        except Exception as commit_error:
            logger.error(f"[CONVERSATION-ANALYTICS] Failed to commit RFQ notification fact: {commit_error}")
            db_session.rollback()

    async def analyze_date_range(self, start_date: date, end_date: date) -> Dict[str, Any]:
        """Analyze conversations for a date range."""
        from datetime import timedelta
        results = []
        current_date = start_date
        
        while current_date <= end_date:
            result = await self.analyze_daily_conversations(current_date)
            results.append({"date": str(current_date), "result": result})
            current_date += timedelta(days=1)
        
        return {"processed_dates": len(results), "results": results}

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


if __name__ == "__main__":
    import asyncio
    from datetime import date, datetime


    async def main():
        service = ConversationAnalyticsService()
        from datetime import timedelta
        

        start_date = datetime(2026, 1, 21).date()
        end_date = datetime(2026, 1, 23).date()
        
        current_date = start_date
        while current_date <= end_date:
            print(f"Analyzing conversations for {current_date}")
            result = await service.analyze_daily_conversations(current_date)
            print(f"Success: {result.get('success', False)}, Sessions: {result.get('total_sessions', 0)}")
            current_date += timedelta(days=1)





    asyncio.run(main())



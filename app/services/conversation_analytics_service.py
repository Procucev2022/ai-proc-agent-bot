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
from app.models import ConversationSession, BuyerDailyMetrics, SellerDailyMetrics, DailyAggregates, MetricMaster, CategoryAggregates, UnknownDailyMetrics
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
            analysis_reasoning=session.get('analysis_reasoning',"")

            # Extract metrics and emails
            buyer_metrics = session.get('buyer_metrics', {})
            seller_metrics = session.get('seller_metrics', {})
            registration_metrics = session.get('registration_metrics', {})
            buyer_email = session.get('buyer_email', '')
            seller_email = session.get('seller_email', '')

            # Check if session has buyer activity (any non-zero buyer metric)
            has_buyer_activity = (
                    buyer_metrics.get('successful_rfqs_ai', 0) > 0 or
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
                    'successful_rfqs_ai': buyer_metrics.get('successful_rfqs_ai', 0),
                    'incomplete_rfq': buyer_metrics.get('incomplete_rfqs', 0),
                    'buyers_started_but_not_raised_rfq': buyer_metrics.get('buyers_started_but_not_raised_rfq', 0),
                    'number_of_buyer_chats': buyer_metrics.get('number_of_buyer_chats', 0),
                    'successful_registration': registration_metrics.get('successful_registration', 0),
                    'failed_registration': registration_metrics.get('failed_registration', 0),
                    'confidence_score': confidence_score,
                    'analysis_reasoning':analysis_reasoning
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
                    'requested_rfq_ai':seller_metrics.get('rfq_requested_ai',0),
                    'rfq_response_ai':seller_metrics.get('rfq_response_ai',0),
                    'subscription_plans_requested': seller_metrics.get('subscription_plans_requested', 0),
                    'zero_credit_rfq_attempt': seller_metrics.get('zero_credit_rfq_attempt', 0),
                    'number_of_seller_chats': seller_metrics.get('number_of_seller_chats', 0),
                    'successful_registration': registration_metrics.get('successful_registration', 0),
                    'failed_registration': registration_metrics.get('failed_registration', 0),
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
                'successful_rfqs_ai', 'incomplete_rfq', 'buyers_started_but_not_raised_rfq',
                'number_of_buyer_chats', 'successful_registration', 'failed_registration',
                'confidence_score','analysis_reasoning'
            ]
            buyer_df = buyer_df[buyer_cols]

        if not seller_df.empty:
            seller_cols = [
                'date', 'session_id', 'phone_number', 'user_type', 'seller_email','requested_rfq_ai','rfq_response_ai',
                'subscription_plans_requested', 'zero_credit_rfq_attempt',
                'number_of_seller_chats', 'successful_registration', 'failed_registration',
                'confidence_score','analysis_reasoning'
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
                print("buyer df", buyer_df)
                print("seller df", seller_df)
                print("unkonwdf",unknown_df)

                # Query remote database after creating DataFrames
                remote_rfq_df = self._query_remote_users(target_date)
                remote_seller_rfq_df = self._query_remote_seller_rfqs(target_date)
                
                # Join buyer_df with remote_rfq_df
                if not buyer_df.empty and not remote_rfq_df.empty:
                    buyer_df['phone_clean'] = buyer_df['phone_number'].str.replace('+', '', regex=False)
                    remote_rfq_df['phone_clean'] = remote_rfq_df['phone'].str.replace('+', '', regex=False)
                    joined_buyer_df = buyer_df.merge(remote_rfq_df, on=['phone_clean'], how='outer')
                    
                    # Coalesce date columns
                    joined_buyer_df['date'] = joined_buyer_df['date_x'].fillna(joined_buyer_df['date_y'])
                    
                    # Keep only relevant columns
                    relevant_cols = [
                        'date', 'phone_number', 'buyer_email','session_id', 'confidence_score',
                        'successful_rfqs_ai', 'incomplete_rfqs', 'buyers_started_but_not_raised_rfq',
                        'username', 'org_uuid', 'user_uuid', 'total_rfqs_raised', 
                        'total_items_in_rfqs', 'total_distinct_rfq_category','number_of_buyer_chats',
                        'successful_registration','failed_registration','analysis_reasoning'
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
                    joined_seller_df = seller_df.merge(remote_seller_rfq_df, on=['phone_clean'], how='outer')


                    
                    # Coalesce date columns - use rfq_date when date is empty
                    joined_seller_df['date'] = joined_seller_df['date'].fillna(joined_seller_df['rfq_date'])
                    
                    # Keep only relevant columns
                    seller_relevant_cols = [
                        'date', 'phone_number', 'seller_email','session_id', 'confidence_score',
                        'requested_rfq_ai', 'rfq_response_ai','subscription_plans_requested', 'zero_credit_rfq_attempt',
                        'number_of_seller_chats', 'successful_registration', 'failed_registration',
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

    def _dump_joined_buyer_df_to_db(self, joined_buyer_df: pd.DataFrame, db_session) -> None:
        """Dump joined buyer DataFrame to BuyerDailyMetrics table."""
        try:
            records_inserted = 0
            skipped = 0
            for _, row in joined_buyer_df.iterrows():
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
                    phone_number=str(row.get('phone_number', '')) if pd.notna(row.get('phone_number')) else None,
                    total_rfq_raised=int(row.get('total_rfqs_raised', 0)) if pd.notna(row.get('total_rfqs_raised')) else 0,
                    total_items_in_rfqs=int(row.get('total_items_in_rfqs', 0)) if pd.notna(row.get('total_items_in_rfqs')) else 0,
                    total_distinct_categories_in_rfq=int(row.get('total_distinct_rfq_category', 0)) if pd.notna(row.get('total_distinct_rfq_category')) else 0,
                    buyers_started_but_not_raised_rfq=int(row.get('buyers_started_but_not_raised_rfq', 0)) if pd.notna(row.get('buyers_started_but_not_raised_rfq')) else 0,
                    failed_registration=int(row.get('failed_registration', 0)) if pd.notna(row.get('failed_registration')) else 0,
                    successfully_registered=int(row.get('successful_registration', 0)) if pd.notna(row.get('successful_registration')) else 0,
                    number_of_chats=int(row.get('number_of_buyer_chats', 0)) if pd.notna(row.get('number_of_buyer_chats')) else 0,
                    successful_rfqs_ai=int(row.get('successful_rfqs_ai', 0)) if pd.notna(row.get('successful_rfqs_ai')) else 0,
                    avg_products_per_rfq=float(row.get('total_items_in_rfqs', 0) / row.get('total_rfqs_raised', 1)) if pd.notna(row.get('total_rfqs_raised')) and row.get('total_rfqs_raised', 0) > 0 else None,
                    avg_categories_per_rfq=float(row.get('total_distinct_rfq_category', 0) / row.get('total_rfqs_raised', 1)) if pd.notna(row.get('total_rfqs_raised')) and row.get('total_rfqs_raised', 0) > 0 else None,
                    org_id=str(row.get('org_uuid', '')) if pd.notna(row.get('org_uuid')) else None,
                    uuid=str(row.get('user_uuid', '')) if pd.notna(row.get('user_uuid')) else None,
                    ai_reasoning=str(row.get('analysis_reasoning', '')) if pd.notna(row.get('analysis_reasoning')) else None
                )
                
                # Use merge to handle unique constraint
                existing = db_session.query(BuyerDailyMetrics).filter(
                    BuyerDailyMetrics.date == buyer_metric.date,
                    BuyerDailyMetrics.email == buyer_metric.email,
                    BuyerDailyMetrics.phone_number == buyer_metric.phone_number
                ).first()
                
                if not existing:
                    db_session.add(buyer_metric)
                    records_inserted += 1
            
            db_session.commit()
            logger.info(f"[CONVERSATION-ANALYTICS] Inserted {records_inserted} records into BuyerDailyMetrics table (skipped {skipped} rows without date)")
            
        except Exception as e:
            logger.error(f"[CONVERSATION-ANALYTICS] Failed to dump joined buyer DataFrame to database: {e}")
            db_session.rollback()
    
    def calculate_and_store_daily_aggregates(self, target_date, db_session) -> None:
        """Calculate aggregates from buyer_daily_metrics and store in daily_aggregates table."""
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
                ('buyer', 7, 'Incomplete RFQs', sum(m.buyers_started_but_not_raised_rfq for m in buyer_metrics)),
                ('buyer', 8, 'No. of Registrations failed', sum(m.failed_registration for m in buyer_metrics)),
                ('buyer', 9, 'Total Items in All RFQs', sum(m.total_items_in_rfqs for m in buyer_metrics)),
                ('buyer', 10, 'Total Distinct RFQ Category Combinations', sum(m.total_distinct_categories_in_rfq for m in buyer_metrics))
            ]
            
            # Insert or update aggregates
            for role, metric_s_no, metric_name, value in aggregates:
                existing = db_session.query(DailyAggregates).filter(
                    DailyAggregates.date == target_date,
                    DailyAggregates.role == role,
                    DailyAggregates.metric_s_no == metric_s_no
                ).first()
                
                if existing:
                    existing.value = value
                else:
                    aggregate = DailyAggregates(
                        date=target_date,
                        role=role,
                        metric_s_no=metric_s_no,
                        metric_name=metric_name,
                        value=value
                    )
                    db_session.add(aggregate)
            
            db_session.commit()
            logger.info(f"[CONVERSATION-ANALYTICS] Stored {len(aggregates)} daily aggregates for {target_date}")
            
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
                u.org_uuid AS org_id
            FROM user u
            JOIN rfq_header rfh 
                ON u.uuid = rfh.user
            LEFT JOIN rfq_items ri 
                ON ri.rfq_uuid = rfh.uuid
            WHERE 
                DATE(rfh.created_ts) = :target_date
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
                u.phone,
                COUNT(DISTINCT rfqv.rfq_id) AS total_rfq_responsed
            FROM development_gmtbfs.rfq_vendors rfqv
            JOIN user u 
                ON u.org_uuid = rfqv.organization_uuid
            WHERE rfqv.quotation_received > 0
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
    
    def _dump_joined_seller_df_to_db(self, joined_seller_df: pd.DataFrame, db_session) -> None:
        """Dump joined seller DataFrame to SellerDailyMetrics table."""
        try:
            records_inserted = 0
            skipped = 0
            for _, row in joined_seller_df.iterrows():
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
                    subscription_plans_requested=int(row.get('subscription_plans_requested', 0)) if pd.notna(row.get('subscription_plans_requested')) else 0,
                    zero_credit_rfq_attempt=int(row.get('zero_credit_rfq_attempt', 0)) if pd.notna(row.get('zero_credit_rfq_attempt')) else 0,
                    org_id=str(row.get('org_uuid', '')) if pd.notna(row.get('org_uuid')) else None,
                    uuid=str(row.get('user_uuid', '')) if pd.notna(row.get('user_uuid')) else None,
                    total_rfqs_requested_with_quotation=int(row.get('total_rfqs_requested', 0)) if pd.notna(row.get('total_rfqs_requested')) else 0
                )
                
                # Use merge to handle unique constraint
                existing = db_session.query(SellerDailyMetrics).filter(
                    SellerDailyMetrics.date == seller_metric.date,
                    SellerDailyMetrics.email == seller_metric.email,
                    SellerDailyMetrics.phone_number == seller_metric.phone_number
                ).first()
                
                if not existing:
                    db_session.add(seller_metric)
                    records_inserted += 1
            
            db_session.commit()
            logger.info(f"[CONVERSATION-ANALYTICS] Inserted {records_inserted} records into SellerDailyMetrics table (skipped {skipped} rows without date)")
            
        except Exception as e:
            logger.error(f"[CONVERSATION-ANALYTICS] Failed to dump joined seller DataFrame to database: {e}")
            db_session.rollback()
    
    def calculate_and_store_seller_daily_aggregates(self, target_date, db_session) -> None:
        """Calculate seller aggregates from seller_daily_metrics and store in daily_aggregates table."""
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
                ('seller', 13, 'Total RFQs Requested', sum(m.total_rfqs_requested_with_quotation for m in seller_metrics)),
                ('seller', 14, 'Subscription Plans Requested', sum(m.subscription_plans_requested for m in seller_metrics)),
                ('seller', 15, 'Seller Registration Failed', sum(m.seller_failed_registration for m in seller_metrics)),
                ('seller', 16, 'Seller Successful Registration', sum(m.seller_successful_registration for m in seller_metrics)),
                ('seller', 17, 'Zero Credit RFQ Attempt', sum(m.zero_credit_rfq_attempt for m in seller_metrics)),
                ('seller', 24, 'Total RFQ Responded', sum(getattr(m, 'rfq_response_ai', 0) for m in seller_metrics))
            ]
            
            for role, metric_s_no, metric_name, value in seller_aggregates:
                existing = db_session.query(DailyAggregates).filter(
                    DailyAggregates.date == target_date,
                    DailyAggregates.role == role,
                    DailyAggregates.metric_s_no == metric_s_no
                ).first()
                
                if existing:
                    existing.value = value
                else:
                    aggregate = DailyAggregates(
                        date=target_date,
                        role=role,
                        metric_s_no=metric_s_no,
                        metric_name=metric_name,
                        value=value
                    )
                    db_session.add(aggregate)
            
            db_session.commit()
            logger.info(f"[CONVERSATION-ANALYTICS] Stored {len(seller_aggregates)} seller daily aggregates for {target_date}")
            
        except Exception as e:
            logger.error(f"[CONVERSATION-ANALYTICS] Failed to calculate seller daily aggregates: {e}")
            db_session.rollback()
    
    def calculate_and_store_category_aggregates(self, target_date, db_session) -> None:
        """Calculate category aggregates from remote database and store in category_aggregates table."""
        try:
            remote_db = get_remote_db_session()
            query = """
            SELECT
                t.rfq_date,
                t.category,
                t.total_rfq_raised,
                q.rfqs_with_quotations
            FROM (
                SELECT
                    DATE(created_ts) AS rfq_date,
                    category,
                    COUNT(DISTINCT rfq_uuid) AS total_rfq_raised
                FROM rfq_items
                WHERE DATE(created_ts) = :target_date
                GROUP BY
                    DATE(created_ts),
                    category
            ) t
            LEFT JOIN (
                SELECT
                    DATE(ri.created_ts) AS rfq_date,
                    ri.category,
                    COUNT(DISTINCT ri.rfq_uuid) AS rfqs_with_quotations
                FROM rfq_items ri
                JOIN rfq_vendors rv
                    ON ri.rfq_uuid = rv.rfq_uuid
                WHERE DATE(ri.created_ts) = :target_date
                GROUP BY
                    DATE(ri.created_ts),
                    ri.category
            ) q
            ON t.rfq_date = q.rfq_date
            AND t.category = q.category
            """
            result = remote_db.execute(text(query), {'target_date': str(target_date)})
            rows = result.fetchall()

            remote_db.close()

            if not rows:
                logger.info(f"[CONVERSATION-ANALYTICS] No category data found for {target_date}")
                return

            for row in rows:
                category_name = row[1] if row[1] is not None else 'Unknown'
                total_rfq_raised = int(row[2]) if row[2] is not None else 0
                rfqs_with_quotations = int(row[3]) if row[3] is not None else 0
                
                existing = db_session.query(CategoryAggregates).filter(
                    CategoryAggregates.date == target_date,
                    CategoryAggregates.category_name == category_name
                ).first()

                if existing:
                    existing.total_rfq_raised_category = total_rfq_raised
                    existing.total_rfqs_with_quotations = rfqs_with_quotations
                else:
                    aggregate = CategoryAggregates(
                        date=target_date,
                        category_name=category_name,
                        total_rfq_raised_category=total_rfq_raised,
                        total_rfqs_with_quotations=rfqs_with_quotations
                    )
                    db_session.add(aggregate)

            db_session.commit()
            logger.info(f"[CONVERSATION-ANALYTICS] Stored {len(rows)} category aggregates for {target_date}")

        except Exception as e:
            logger.error(f"[CONVERSATION-ANALYTICS] Failed to calculate category aggregates: {e}")
            db_session.rollback()
    
    def _dump_unknown_df_to_db(self, unknown_df: pd.DataFrame, db_session) -> None:
        """Dump unknown DataFrame to UnknownDailyMetrics table."""
        try:
            records_inserted = 0
            skipped = 0
            for _, row in unknown_df.iterrows():
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
                    ai_reasoning=str(row.get('analysis_reasoning', '')) if pd.notna(row.get('analysis_reasoning')) else None
                )
                
                existing = db_session.query(UnknownDailyMetrics).filter(
                    UnknownDailyMetrics.date == unknown_metric.date,
                    UnknownDailyMetrics.session_id == unknown_metric.session_id,
                    UnknownDailyMetrics.phone_number == unknown_metric.phone_number
                ).first()
                
                if not existing:
                    db_session.add(unknown_metric)
                    records_inserted += 1
            
            db_session.commit()
            logger.info(f"[CONVERSATION-ANALYTICS] Inserted {records_inserted} records into UnknownDailyMetrics table (skipped {skipped} rows without date)")
            
        except Exception as e:
            logger.error(f"[CONVERSATION-ANALYTICS] Failed to dump unknown DataFrame to database: {e}")
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
        
        end_date = datetime(2025, 12, 19).date()
        start_date = datetime(2025, 12, 9).date()
        
        current_date = start_date
        while current_date <= end_date:
            print(f"Analyzing conversations for {current_date}...")
            result = await service.analyze_daily_conversations(current_date)
            print(f"Success: {result.get('success', False)}, Sessions: {result.get('total_sessions', 0)}")
            current_date += timedelta(days=1)





    asyncio.run(main())



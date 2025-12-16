"""
Conversation Analytics Service - With Batch Processing
"""

import logging
import json
from typing import Dict, Any, List
from datetime import datetime, date, timezone
from sqlalchemy import func

from app.database import get_db_session
from app.models import ConversationSession
from app.services.openai_service import OpenAIService
from app.config import get_settings
from app.utils.logging_utils import log_service_method

logger = logging.getLogger(__name__)


class ConversationAnalyticsService:
    """Service for AI-powered conversation analytics and metrics extraction."""

    def __init__(self):
        self.settings = get_settings()
        self.openai_service = OpenAIService()
        self.batch_size = 5  # Process 5 sessions per AI call


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
   - Extract email address from conversation history
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
   - Each session must have: session_id, user_type, email, confidence_score, metrics, analysis_reasoning

CRITICAL: You must analyze and return data for ALL {len(batch_data)} sessions.
Session IDs to process: {', '.join(session_ids)}

Example output structure for 2 sessions:
{{
  "date": "{target_date}",
  "total_sessions": 2,
  "sessions": [
    {{
      "session_id": "session_1",
      "user_type": "buyer",
      "email": "user1@example.com",
      "confidence_score": 90,
      "metrics": {{
        "successful_rfqs": 1,
        "incomplete_rfqs": 0,
        "buyers_started_but_not_raised_rfq": 0,
        "subscription_plans_requested": 0,
        "zero_credit_rfq_attempt": 0,
        "successful_registration": 1,
        "failed_registration": 0
      }},
      "analysis_reasoning": "User completed buyer registration and created one RFQ..."
    }},
    {{
      "session_id": "session_2",
      "user_type": "seller",
      "email": "user2@example.com",
      "confidence_score": 85,
      "metrics": {{
        "successful_rfqs": 0,
        "incomplete_rfqs": 0,
        "buyers_started_but_not_raised_rfq": 0,
        "subscription_plans_requested": 1,
        "zero_credit_rfq_attempt": 0,
        "successful_registration": 0,
        "failed_registration": 1
      }},
      "analysis_reasoning": "User attempted seller registration but failed..."
    }}
  ]
}}
"""
        return prompt

    async def analyze_daily_conversations(self, target_date: date = None) -> Dict[str, Any]:
        """
        Analyze conversations for a specific date using AI with batch processing.

        Args:
            target_date: Date to analyze (defaults to today)

        Returns:
            Dict with flat sessions array containing all analyzed sessions
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
                        "message": "No conversations found for analysis"
                    }

                logger.info(
                    f"[CONVERSATION-ANALYTICS] Found {len(sessions)} conversation sessions "
                    f"for analysis (batch size: {self.batch_size})"
                )

                # Process sessions in batches - AI returns flat format
                result = await self._process_sessions_in_batches(sessions, target_date)

                result["success"] = True
                result["analysis_timestamp"] = datetime.now(timezone.utc).isoformat()

                logger.info(
                    f"[CONVERSATION-ANALYTICS] Analysis completed for {target_date}. "
                    f"Total sessions analyzed: {result['total_sessions']}"
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
                "error": str(e)
            }

    async def __aenter__(self):
        return self

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
        
        if result.get('sessions'):
            print(f"Sessions analyzed: {len(result['sessions'])}")
            for session in result['sessions'][:3]:  # Show first 3
                print(f"  - {session.get('session_id')}: {session.get('user_type')}")
        else:
            print("No sessions found for analysis")
    
    asyncio.run(main())
"""
Enhanced Domain Check Service with AI-based matching and fallback logic.

Provides domain validation for user authentication with:
- AI-based domain matching analysis
- Fallback fuzzy matching using SequenceMatcher
- User approval API integration
- Support redirection for failed cases
"""

import logging
import json
from typing import Dict, Any, Optional
from difflib import SequenceMatcher
from app.services.openai_service import OpenAIService
from app.procucev_apis.register_apis import RegisterAPIService
from app.services.whatsapp_service import WhatsAppService
from app.services.support_notification_service import SupportNotificationService
from pathlib import Path

logger = logging.getLogger(__name__)


class DomainCheckService:
    """Enhanced domain check service with AI and fallback logic."""
    
    def __init__(self, openai_service: OpenAIService = None, 
                 whatsapp_service: WhatsAppService = None,
                 session_manager=None):
        self.openai_service = openai_service or OpenAIService()
        self.whatsapp_service = whatsapp_service or WhatsAppService()
        self.register_api_service = RegisterAPIService()
        self.support_notification_service = SupportNotificationService()
        self.session_manager = session_manager
        self.tools_dir = Path(__file__).parent.parent / "tools"
    
    def normalize(self, text: str) -> str:
        """Normalize text for domain matching."""
        return ''.join(e.lower() for e in text if e.isalnum())
    
    def fallback_domain_match_score(self, email: str, company_name: str) -> int:
        """Fallback fuzzy matching logic using SequenceMatcher."""
        logger.info(f"DOMAIN_CHECK_SERVICE: Starting fallback domain matching for email='{email}', company='{company_name}'")
        
        try:
            # Handle empty inputs
            if not email or not company_name or not email.strip() or not company_name.strip():
                logger.warning(f"DOMAIN_CHECK_SERVICE: Empty inputs in fallback matching")
                return 0
                
            domain = email.split('@')[-1].split('.')[0]  # "mohap"
            comp = self.normalize(company_name)           # "mohapai"
            logger.info(f"DOMAIN_CHECK_SERVICE: Extracted domain='{domain}', normalized_company='{comp}'")

            # Skip fuzzy matching for generic domains
            generic_domains = {'gmail', 'yahoo', 'hotmail', 'outlook', 'rediff', 'live'}
            if domain.lower() in generic_domains:
                logger.info(f"DOMAIN_CHECK_SERVICE: Generic domain '{domain}' detected, score=0")
                return 0

            score = 0
            # Exact match gets high score
            if domain.lower() == comp.lower():
                score = 95
                logger.info(f"DOMAIN_CHECK_SERVICE: Exact match found, score=95")
            # Substring match
            elif domain.lower() in comp.lower() or comp.lower() in domain.lower():
                score = 85
                logger.info(f"DOMAIN_CHECK_SERVICE: Substring match found, score=85")
            else:
                # Fuzzy matching only for non-generic domains
                ratio = SequenceMatcher(None, domain.lower(), comp.lower()).ratio()
                if ratio >= 0.8:  # High similarity threshold
                    score = int(ratio * 80)  # Max 64 points from fuzzy
                    logger.info(f"DOMAIN_CHECK_SERVICE: High fuzzy ratio={ratio:.2f}, score={score}")
                else:
                    score = 0
                    logger.info(f"DOMAIN_CHECK_SERVICE: Low fuzzy ratio={ratio:.2f}, score=0")
            
            logger.info(f"DOMAIN_CHECK_SERVICE: Final fallback score: {score}")
            return score
        except Exception as e:
            logger.error(f"DOMAIN_CHECK_SERVICE: Fallback domain matching error: {e}")
            return 0
    
    async def ai_domain_match_analysis(self, email: str, company_name: str) -> Dict[str, Any]:
        """AI-based domain matching analysis using proper function calling."""
        logger.info(f"DOMAIN_CHECK_SERVICE: Starting AI domain analysis for email='{email}', company='{company_name}'")
        
        try:
            # Load domain matching tool
            with open(self.tools_dir / "domain_matching.json", 'r') as f:
                domain_tool = json.load(f)
            
            # Build analysis prompt
            prompt = f"""
Analyze domain matching for user verification:

EMAIL: "{email}"
COMPANY NAME: "{company_name}"

Determine if the email domain matches or is related to the company name.
"""
            
            logger.info(f"DOMAIN_CHECK_SERVICE: Calling OpenAI function calling for domain analysis")
            
            response = await self.openai_service.client.responses.create(
                model=self.openai_service.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions=self.openai_service._load_prompt("domain_validation", "domain_match_analysis_system"),
                tools=[domain_tool],
                tool_choice={"type": "function", "name": "analyze_domain_match"}
            )
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    analysis = json.loads(function_call.arguments)
                    logger.info(f"DOMAIN_CHECK_SERVICE: AI analysis successful: {analysis}")
                    
                    result = {
                        "success": True,
                        "analysis": analysis,
                        "method": "ai"
                    }
                    return result
            
            logger.warning(f"DOMAIN_CHECK_SERVICE: No function call in AI response")
            return {"success": False, "error": "No function call in response"}
                
        except Exception as e:
            logger.error(f"DOMAIN_CHECK_SERVICE: AI domain analysis error: {e}")
            return {"success": False, "error": str(e)}
    
    async def check_domain_match(self, email: str, company_name: str) -> Dict[str, Any]:
        """
        Check domain match using AI-first approach with fallback.
        
        Returns:
            Dict with match results and recommendation
        """
        logger.info(f"DOMAIN_CHECK_SERVICE: Starting domain match check for email='{email}', company='{company_name}'")
        
        try:
            # Handle empty/null inputs
            if not email or not email.strip() or not company_name or not company_name.strip():
                logger.warning(f"DOMAIN_CHECK_SERVICE: Empty input detected - email='{email}', company='{company_name}'")
                return {
                    "approved": False,
                    "score": 0,
                    "match_type": "empty_input",
                    "confidence": "high",
                    "reasoning": "Empty or null email/company name",
                    "method": "validation"
                }
            
            logger.info(f"DOMAIN_CHECK_SERVICE: Processing domain match: {email} vs {company_name}")
            
            # Try AI-based analysis first
            logger.info(f"DOMAIN_CHECK_SERVICE: Attempting AI-based domain analysis")
            ai_result = await self.ai_domain_match_analysis(email, company_name)
            
            if ai_result.get("success"):
                analysis = ai_result["analysis"]
                score = analysis["score"]
                match_type = analysis["match_type"]
                approved = score >= 70
                
                logger.info(f"DOMAIN_CHECK_SERVICE: AI analysis successful - score={score}, type={match_type}, approved={approved}")
                
                result = {
                    "approved": approved,
                    "score": score,
                    "match_type": match_type,
                    "confidence": analysis["confidence"],
                    "reasoning": analysis["reasoning"],
                    "method": "ai"
                }
                logger.info(f"DOMAIN_CHECK_SERVICE: AI result: {result}")
                return result
            else:
                # Fallback to fuzzy matching
                logger.warning(f"DOMAIN_CHECK_SERVICE: AI analysis failed ({ai_result.get('error')}), using fallback fuzzy matching")
                fallback_score = self.fallback_domain_match_score(email, company_name)
                approved = fallback_score >= 70
                
                logger.info(f"DOMAIN_CHECK_SERVICE: Fallback analysis - score={fallback_score}, approved={approved}")
                
                result = {
                    "approved": approved,
                    "score": fallback_score,
                    "match_type": "fuzzy",
                    "confidence": "medium",
                    "reasoning": f"Fuzzy match score: {fallback_score}",
                    "method": "fallback"
                }
                logger.info(f"DOMAIN_CHECK_SERVICE: Fallback result: {result}")
                return result
                
        except Exception as e:
            logger.error(f"DOMAIN_CHECK_SERVICE: Domain check error: {e}")
            result = {
                "approved": False,
                "score": 0,
                "match_type": "error",
                "confidence": "low",
                "reasoning": f"Error during analysis: {str(e)}",
                "method": "error"
            }
            logger.error(f"DOMAIN_CHECK_SERVICE: Error result: {result}")
            return result
    
    async def user_approval_api_call(self, user_id: str) -> Dict[str, Any]:
        """Call /rest/gmt/acceptSelfRegisterClient API for user approval."""
        logger.info(f"DOMAIN_CHECK_SERVICE: Calling /rest/gmt/acceptSelfRegisterClient API for user_id: {user_id}")
        
        try:
            approval_response = await self.register_api_service.user_approval(user_id)
            logger.info(f"DOMAIN_CHECK_SERVICE: /rest/gmt/acceptSelfRegisterClient API response: {approval_response}")
            
            if (approval_response.get("statusCode") == "200" and 
                approval_response.get("status") == "Success"):
                result = {
                    "approved": True,
                    "status": "success",
                    "message": approval_response.get("message", "User approved successfully"),
                    "api_response": approval_response
                }
                logger.info(f"DOMAIN_CHECK_SERVICE: /rest/gmt/acceptSelfRegisterClient API call successful: {result}")
                return result
            else:
                result = {
                    "approved": False,
                    "status": "failed",
                    "message": approval_response.get("message", "Domain approval failed"),
                    "api_response": approval_response
                }
                logger.warning(f"DOMAIN_CHECK_SERVICE: /rest/gmt/acceptSelfRegisterClient API call failed: {result}")
                return result
        except Exception as e:
            logger.error(f"DOMAIN_CHECK_SERVICE: /rest/gmt/acceptSelfRegisterClient API error for user {user_id}: {e}")
            result = {
                "approved": False,
                "status": "error",
                "message": f"API error: {str(e)}",
                "error": str(e)
            }
            logger.error(f"DOMAIN_CHECK_SERVICE: Error result: {result}")
            return result
    
    async def process_user_approval(self, user_phone: str, user_id: str, 
                                  session, domain_check_result: Dict[str, Any]) -> Dict[str, Any]:
        """Process user approval flow based on AI domain check results."""
        logger.info(f"DOMAIN_CHECK_SERVICE: Processing user approval for phone={user_phone}, user_id={user_id}")
        logger.info(f"DOMAIN_CHECK_SERVICE: Domain check result: {domain_check_result}")
        
        try:
            # Only call API if AI-based domain matching says approved=True
            if domain_check_result.get("approved") and domain_check_result.get("method") == "ai":
                logger.info(f"DOMAIN_CHECK_SERVICE: AI approved domain for user {user_id}, calling approval API")
                
                approval_result = await self.user_approval_api_call(user_id)
                
                if approval_result.get("approved"):
                    logger.info(f"DOMAIN_CHECK_SERVICE: API approval successful for user {user_id}")
                    
                    # Refresh user data to get updated flag
                    from app.procucev_apis.auth_apis import AuthAPIService
                    auth_api_service = AuthAPIService()
                    refresh_response = await auth_api_service.authenticate_user(user_phone)
                    
                    if refresh_response.get("success") and refresh_response.get("data"):
                        fresh_users = refresh_response["data"]
                        selected_user = fresh_users[0] if isinstance(fresh_users, list) else fresh_users
                        logger.info(f"DOMAIN_CHECK_SERVICE: Fresh user data retrieved: approved={selected_user.get('approved')}")
                        
                        if selected_user.get("approved", False):
                            logger.info(f"DOMAIN_CHECK_SERVICE: User {user_id} approval flag updated successfully")
                            
                            # Store updated user session
                            from app.services.authentication_service import AuthenticationService
                            auth_service = AuthenticationService()
                            session_stored = await auth_service.store_user_session_with_email(
                                user_phone, [selected_user], selected_user.get("email") or selected_user.get("username")
                            )
                            
                            if session_stored:
                                logger.info(f"DOMAIN_CHECK_SERVICE: Updated session stored for approved user {user_phone}")
                            
                            session.workflow_type = None
                            session.workflow_state = {}
                            
                            final_result = {
                                "status": "approved_and_updated",
                                "approved": True,
                                "redirect_to_main_flow": True,
                                "message": "Domain verification successful. You can now proceed."
                            }
                            logger.info(f"DOMAIN_CHECK_SERVICE: Process completed successfully: {final_result}")
                            return final_result
                        else:
                            return await self._redirect_to_support(
                                user_phone, "approval_flag_not_updated", 
                                "API approval succeeded but flag not updated", session
                            )
                    else:
                        return await self._redirect_to_support(
                            user_phone, "refresh_failed_after_approval", 
                            "Could not refresh user data after approval", session
                        )
                else:
                    return await self._redirect_to_support(
                        user_phone, "api_approval_failed", 
                        f"API approval failed: {approval_result.get('message', 'Unknown error')}", session
                    )
            else:
                # AI says not approved OR fallback method was used - redirect to support
                reason = "ai_domain_mismatch" if domain_check_result.get("method") == "ai" else "fallback_method_used"
                logger.warning(f"DOMAIN_CHECK_SERVICE: {reason}, not calling API, redirecting to support")
                return await self._redirect_to_support(
                    user_phone, reason, 
                    f"Domain check failed: {domain_check_result.get('reasoning', 'No match found')}", session
                )
                
        except Exception as e:
            logger.error(f"DOMAIN_CHECK_SERVICE: User approval process error: {e}")
            return await self._redirect_to_support(
                user_phone, "approval_process_error", str(e), session
            )
    
    async def _redirect_to_support(self, user_phone: str, reason: str, 
                                 details: str, session) -> Dict[str, Any]:
        """Redirect user to support and exit flow."""
        logger.info(f"DOMAIN_CHECK_SERVICE: Redirecting user {user_phone} to support - reason: {reason}, details: {details}")
        
        try:
            support_message = (
                "We need to verify your account details. "
                "Our support team will contact you shortly to complete the verification process."
            )
            
            if self.session_manager:
                await self.session_manager.send_and_track_message(user_phone, support_message, session)
            else:
                await self.whatsapp_service.send_message(user_phone, support_message)
            
            # Clear workflow to exit flow
            session.workflow_type = None
            session.workflow_state = {}
            
            logger.info(f"DOMAIN_CHECK_SERVICE: User {user_phone} redirected to support: {reason} - {details}")
            
            result = {
                "status": "redirected_to_support",
                "approved": False,
                "reason": reason,
                "exit_flow": True,
                "support_message_sent": True
            }
            logger.info(f"DOMAIN_CHECK_SERVICE: Support redirect result: {result}")
            return result
            
        except Exception as e:
            logger.error(f"DOMAIN_CHECK_SERVICE: Support redirect error: {e}")
            result = {
                "status": "error",
                "approved": False,
                "error": str(e)
            }
            logger.error(f"DOMAIN_CHECK_SERVICE: Support redirect error result: {result}")
            return result
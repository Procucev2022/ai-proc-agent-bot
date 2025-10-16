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

logger = logging.getLogger(__name__)


class DomainCheckService:
    """Enhanced domain check service with AI and fallback logic."""
    
    def __init__(self, openai_service: OpenAIService = None, 
                 whatsapp_service = None,
                 session_manager = None):
        """
        Initialize DomainCheckService.
        
        Args:
            openai_service: OpenAI service instance for AI-based domain matching
            whatsapp_service: Either MessageQueueService (batched) or WhatsAppService (direct).
                If None, creates direct WhatsAppService for backward compatibility.
            session_manager: Session management service instance
        """
        self.openai_service = openai_service or OpenAIService()
        
        if whatsapp_service:
            self.whatsapp_service = whatsapp_service
        else:
            self.whatsapp_service = WhatsAppService()
            logger.warning("DomainCheckService initialized without whatsapp_service - using direct WhatsAppService")
        
        self.register_api_service = RegisterAPIService()
        self.support_notification_service = SupportNotificationService()
        self.session_manager = session_manager
    
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

            score = 0
            if domain in comp or comp in domain:
                score += 50
                logger.info(f"DOMAIN_CHECK_SERVICE: Substring match found, added 50 points")
            
            # Fuzzy matching
            ratio = SequenceMatcher(None, domain, comp).ratio()
            fuzzy_points = int(ratio * 50)
            score += fuzzy_points
            logger.info(f"DOMAIN_CHECK_SERVICE: Fuzzy ratio={ratio:.2f}, added {fuzzy_points} points")
            
            logger.info(f"DOMAIN_CHECK_SERVICE: Final fallback score: {score}")
            return score
        except Exception as e:
            logger.error(f"DOMAIN_CHECK_SERVICE: Fallback domain matching error: {e}")
            return 0
    
    async def ai_domain_match_analysis(self, email: str, company_name: str) -> Dict[str, Any]:
        """AI-based domain matching analysis."""
        logger.info(f"DOMAIN_CHECK_SERVICE: Starting AI domain analysis for email='{email}', company='{company_name}'")
        
        try:
            logger.info(f"DOMAIN_CHECK_SERVICE: Calling OpenAI for domain analysis")
            response = self.openai_service.generate_response(
                context={"email": email, "company_name": company_name},
                query_results=[],
                prompt_file="domain_validation/domain_match_analysis"
            )
            logger.info(f"DOMAIN_CHECK_SERVICE: OpenAI response received: {response}")
            
            # Parse JSON response
            analysis = json.loads(response.strip())
            logger.info(f"DOMAIN_CHECK_SERVICE: Parsed AI analysis: {analysis}")
            
            # Validate response structure
            required_keys = ["score", "match_type", "confidence", "reasoning"]
            if all(key in analysis for key in required_keys):
                result = {
                    "success": True,
                    "analysis": analysis,
                    "method": "ai"
                }
                logger.info(f"DOMAIN_CHECK_SERVICE: AI analysis successful: {result}")
                return result
            else:
                logger.warning(f"DOMAIN_CHECK_SERVICE: Invalid AI response structure: {analysis}")
                return {"success": False, "error": "Invalid AI response"}
                
        except json.JSONDecodeError as e:
            logger.error(f"DOMAIN_CHECK_SERVICE: AI domain analysis JSON parsing error: {e}")
            return {"success": False, "error": "JSON parsing failed"}
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
        """Call user approval API."""
        logger.info(f"DOMAIN_CHECK_SERVICE: Calling user approval API for user_id: {user_id}")
        
        try:
            approval_response = await self.register_api_service.user_approval(user_id)
            logger.info(f"DOMAIN_CHECK_SERVICE: API response received: {approval_response}")
            
            if (approval_response.get("statusCode") == "200" and 
                approval_response.get("status") == "Success"):
                result = {
                    "approved": True,
                    "status": "success",
                    "message": approval_response.get("message", "User approved successfully"),
                    "api_response": approval_response
                }
                logger.info(f"DOMAIN_CHECK_SERVICE: User approval successful: {result}")
                return result
            else:
                result = {
                    "approved": False,
                    "status": "failed",
                    "message": approval_response.get("message", "Domain approval failed"),
                    "api_response": approval_response
                }
                logger.warning(f"DOMAIN_CHECK_SERVICE: User approval failed: {result}")
                return result
        except Exception as e:
            logger.error(f"DOMAIN_CHECK_SERVICE: API approval error for user {user_id}: {e}")
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
        """Process user approval flow based on domain check results."""
        logger.info(f"DOMAIN_CHECK_SERVICE: Processing user approval for phone={user_phone}, user_id={user_id}")
        logger.info(f"DOMAIN_CHECK_SERVICE: Domain check result: {domain_check_result}")
        
        try:
            if domain_check_result.get("approved"):
                logger.info(f"DOMAIN_CHECK_SERVICE: Domain approved for user {user_id}, calling approval API")
                
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
                logger.warning(f"DOMAIN_CHECK_SERVICE: Domain not approved, redirecting to support")
                return await self._redirect_to_support(
                    user_phone, "domain_mismatch", 
                    f"Domain mismatch: {domain_check_result.get('reasoning', 'No match found')}", session
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
"""
User Selection Analysis Tool for Profile Selection.

Handles intelligent parsing of user responses when selecting from profile options.
Uses both rule-based matching and AI analysis for comprehensive coverage.
"""

import logging
import json
import re
from pathlib import Path
from typing import Dict, Any, List, Optional
from app.services.openai_service import OpenAIService

logger = logging.getLogger(__name__)


class UserSelectionTool:
    """Tool for analyzing user selection responses with multiple input formats."""
    
    def __init__(self, openai_service: OpenAIService):
        self.openai_service = openai_service
        self.prompts_dir = Path(__file__).parent.parent / "prompts"
        self.tools_dir = Path(__file__).parent
    
    async def analyze_user_selection(self, user_input: str, profile_options: List[Dict]) -> Dict[str, Any]:
        """
        Analyze user input to determine which profile option they selected.
        
        Args:
            user_input: The user's response message
            profile_options: List of available profile options with numbers
            
        Returns:
            Analysis result with selected option, confidence, reasoning, and registration detection
        """
        try:
            # Handle case where user_input might be a dict (button reply)
            if isinstance(user_input, dict):
                # Extract text from button reply or convert to string
                if 'button_reply' in user_input:
                    user_input = user_input['button_reply'].get('title', str(user_input))
                else:
                    user_input = str(user_input)
            
            # First try rule-based matching for common patterns
            rule_result = self._rule_based_analysis(user_input, profile_options)
            
            if rule_result['confidence'] >= 0.7:
                return rule_result
            
            # If rule-based matching is not confident enough, use AI analysis
            ai_result = await self._ai_based_analysis(user_input, profile_options)
            
            # Combine results, preferring higher confidence
            if ai_result['confidence'] > rule_result['confidence']:
                return ai_result
            else:
                return rule_result
                
        except Exception as e:
            logger.error(f"Error analyzing user selection: {e}")
            return {
                "selected_option": None,
                "confidence": 0.0,
                "reasoning": f"Analysis error: {str(e)}",
                "alternative_matches": [],
                "requires_clarification": True,
                "register": {"type": None}
            }
    
    def _fuzzy_email_match(self, user_input: str, target_email: str) -> Dict[str, Any]:
        """Fuzzy match user input against target email for typos/incomplete entries."""
        try:
            user_input = user_input.strip().lower()
            target_email = target_email.lower()
            
            if len(user_input) < 3:
                return {'confidence': 0.0}
            
            # Check if user input is substring of email (incomplete email)
            if user_input in target_email:
                confidence = min(0.8, len(user_input) / len(target_email) + 0.3)
                return {'confidence': confidence}
            
            # Check username part similarity
            if '@' in target_email:
                username = target_email.split('@')[0]
                domain = target_email.split('@')[1]
                
                # Username fuzzy match
                username_similarity = self._string_similarity(user_input, username)
                if username_similarity > 0.7:
                    return {'confidence': username_similarity * 0.8}
                
                # Domain fuzzy match
                domain_similarity = self._string_similarity(user_input, domain)
                if domain_similarity > 0.7:
                    return {'confidence': domain_similarity * 0.7}
                
                # Full email fuzzy match
                email_similarity = self._string_similarity(user_input, target_email)
                if email_similarity > 0.6:
                    return {'confidence': email_similarity * 0.6}
            
            return {'confidence': 0.0}
            
        except Exception:
            return {'confidence': 0.0}
    
    def _string_similarity(self, s1: str, s2: str) -> float:
        """Calculate string similarity using simple character-based approach."""
        try:
            if not s1 or not s2:
                return 0.0
            
            longer = s2 if len(s2) > len(s1) else s1
            shorter = s1 if len(s1) < len(s2) else s2
            
            if len(longer) == 0:
                return 1.0
            
            # Count matching characters in order
            matches = 0
            j = 0
            for char in shorter:
                while j < len(longer) and longer[j] != char:
                    j += 1
                if j < len(longer):
                    matches += 1
                    j += 1
            
            return matches / len(longer)
            
        except Exception:
            return 0.0
    
    def _rule_based_analysis(self, user_input: str, profile_options: List[Dict]) -> Dict[str, Any]:
        """Rule-based analysis for common selection patterns."""
        # Ensure user_input is a string
        if not isinstance(user_input, str):
            user_input = str(user_input)
        user_input = user_input.strip().lower()
        
        # Direct number matching
        number_match = re.search(r'(?:^|\s|#)(\d+)(?:\s|$|️⃣)', user_input)
        if number_match:
            option_num = int(number_match.group(1))
            if any(opt.get('number') == option_num for opt in profile_options):
                register_type = self._detect_registration_intent(user_input)
                return {
                    "selected_option": option_num,
                    "confidence": 0.95,
                    "reasoning": f"Direct number match: {option_num}",
                    "alternative_matches": [],
                    "requires_clarification": False,
                    "register": {"type": register_type}
                }
        
        # Word number matching
        word_numbers = {
            'one': 1, 'first': 1, '1st': 1,
            'two': 2, 'second': 2, '2nd': 2,
            'three': 3, 'third': 3, '3rd': 3,
            'four': 4, 'fourth': 4, '4th': 4,
            'five': 5, 'fifth': 5, '5th': 5
        }
        
        # Whole-token match. Substring or `\b`-bounded matches falsely pick "first"
        # inside emails like "bhavin@firstmarketingservices.in" or
        # "john.first@firstaid.org" — only equality against a stripped token is safe.
        tokens = {re.sub(r'^\W+|\W+$', '', t) for t in re.split(r'\s+', user_input) if t}
        for word, num in word_numbers.items():
            if word in tokens:
                if any(opt.get('number') == num for opt in profile_options):
                    register_type = self._detect_registration_intent(user_input)
                    return {
                        "selected_option": num,
                        "confidence": 0.8,
                        "reasoning": f"Word number match: {word} -> {num}",
                        "alternative_matches": [],
                        "requires_clarification": False,
                        "register": {"type": register_type}
                    }
        
        # Email and role matching with fuzzy correction
        matches = []
        for option in profile_options:
            if 'profile' in option:
                profile = option['profile']
                email = profile.get('email', '').lower()
                role = profile.get('role', '').lower()
                
                # Email matching (full, partial, domain)
                if email in user_input:
                    matches.append((option['number'], 0.9, f"Full email match: {email}"))
                elif '@' in email and email.split('@')[0] in user_input:
                    matches.append((option['number'], 0.8, f"Email username match: {email.split('@')[0]}"))
                elif '@' in email and email.split('@')[1] in user_input:
                    matches.append((option['number'], 0.7, f"Email domain match: {email.split('@')[1]}"))
                else:
                    # Fuzzy email matching for typos/incomplete emails
                    fuzzy_match = self._fuzzy_email_match(user_input, email)
                    if fuzzy_match['confidence'] > 0.5:
                        matches.append((option['number'], fuzzy_match['confidence'], f"Fuzzy email match: {email} (corrected from '{user_input}')"))
                
                # Role matching
                if role in user_input or (role == 'buyer' and 'buy' in user_input) or (role == 'seller' and 'sell' in user_input):
                    matches.append((option['number'], 0.7, f"Role match: {role}"))
            
            elif 'action' in option:
                action = option['action'].lower()
                if 'register' in user_input and 'register' in action:
                    matches.append((option['number'], 0.8, f"Action match: register"))
                elif 'new' in user_input and 'new' in action:
                    matches.append((option['number'], 0.7, f"Action match: new"))
        
        if matches:
            # Sort by confidence and return best match
            matches.sort(key=lambda x: x[1], reverse=True)
            best_match = matches[0]
            alternatives = [m[0] for m in matches[1:3]]  # Top 2 alternatives
            
            # Check for registration intent
            register_type = self._detect_registration_intent(user_input)
            
            return {
                "selected_option": best_match[0],
                "confidence": best_match[1],
                "reasoning": best_match[2],
                "alternative_matches": alternatives,
                "requires_clarification": best_match[1] < 0.4,
                "register": {"type": register_type}
            }
        
        # Check for registration intent
        register_type = self._detect_registration_intent(user_input)
        
        return {
            "selected_option": None,
            "confidence": 0.1,
            "reasoning": "No rule-based matches found",
            "alternative_matches": [],
            "requires_clarification": True,
            "register": {"type": register_type}
        }
    
    async def _ai_based_analysis(self, user_input: str, profile_options: List[Dict]) -> Dict[str, Any]:
        """AI-based analysis for complex or ambiguous inputs."""
        try:
            # Load prompt
            prompt_path = self.prompts_dir / "profile_selection" / "user_selection_analysis.txt"
            with open(prompt_path, 'r', encoding='utf-8') as f:
                system_prompt = f.read()
            
            # Build context about available options
            options_context = "Available options:\n"
            for option in profile_options:
                if 'profile' in option:
                    profile = option['profile']
                    options_context += f"{option['number']}️⃣ {profile['email']} — {profile['role'].title()}\n"
                elif 'action' in option:
                    options_context += f"{option['number']}️⃣ {option['display']}\n"
            
            user_message = f"{options_context}\nUser input: \"{user_input}\""
            
            # Load tool definition
            tool_path = self.tools_dir / "user_selection_analysis.json"
            with open(tool_path, 'r', encoding='utf-8') as f:
                tool_def = json.load(f)
            
            # Call OpenAI with function calling
            response = await self.openai_service.client.responses.create(
                model=self.openai_service.default_model,
                input=[{"role": "user", "content": user_message}],
                instructions=system_prompt,
                tools=[tool_def],
                tool_choice={"type": "function", "name": "analyze_user_selection"}
            )
            
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    result = json.loads(function_call.arguments)
                    result['reasoning'] = f"AI analysis: {result['reasoning']}"
                    return result
            
            return {
                "selected_option": None,
                "confidence": 0.2,
                "reasoning": "AI analysis failed",
                "alternative_matches": [],
                "requires_clarification": True,
                "register": {"type": None}
            }
            
        except Exception as e:
            logger.error(f"AI analysis error: {e}")
            return {
                "selected_option": None,
                "confidence": 0.1,
                "reasoning": f"AI analysis error: {str(e)}",
                "alternative_matches": [],
                "requires_clarification": True,
                "register": {"type": None}
            }
    
    def _detect_registration_intent(self, user_input: str) -> Optional[str]:
        """Detect registration intent from user message."""
        try:
            message_lower = user_input.lower().strip()
            
            # Check for explicit buyer registration phrases
            buyer_phrases = [
                'register me as buyer', 'register as buyer', 'register me as a buyer',
                'sign me up as buyer', 'sign up as buyer', 'create buyer account',
                'i want to register as buyer', 'register buyer account', 'add me as buyer'
            ]
            
            # Check for explicit seller registration phrases
            seller_phrases = [
                'register me as seller', 'register as seller', 'register me as a seller',
                'sign me up as seller', 'sign up as seller', 'create seller account',
                'i want to register as seller', 'register seller account', 'add me as seller'
            ]
            
            for phrase in buyer_phrases:
                if phrase in message_lower:
                    return 'buyer'
            
            for phrase in seller_phrases:
                if phrase in message_lower:
                    return 'seller'
            
            return None
            
        except Exception as e:
            logger.error(f"Error detecting registration intent: {e}")
            return None
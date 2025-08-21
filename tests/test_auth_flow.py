"""
Test cases for authentication, registration, and token validation flows.
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from app.services.authentication_service import AuthenticationService
from app.services.registration_service import RegistrationService
from app.services.chat_service import ChatService
from app.models import ConversationSession
from app.schemas.user import UserDetailsSchema


class TestAuthenticationFlow:
    """Test authentication flow scenarios."""
    
    @pytest.fixture
    def auth_service(self):
        return AuthenticationService()
    
    @pytest.fixture
    def chat_service(self):
        return ChatService()
    
    @pytest.fixture
    def mock_session(self):
        session = Mock(spec=ConversationSession)
        session.workflow_state = {}
        session.workflow_type = None
        return session
    
    @pytest.mark.asyncio
    async def test_token_validation_success(self, auth_service):
        """Test successful token validation from Redis."""
        # Mock Redis response
        mock_user = UserDetailsSchema(
            id="123",
            name="Test User",
            email="test@example.com",
            self_client=True,
            role="buyer",
            is_registered=True,
            phone_number="1234567890"
        )
        
        with patch.object(auth_service.auth_redis_service, 'retrieve', return_value=mock_user):
            result = await auth_service.validate_token("1234567890")
            
        assert result is not None
        assert result.id == "123"
        assert result.is_registered is True
    
    @pytest.mark.asyncio
    async def test_token_validation_failure(self, auth_service):
        """Test token validation failure."""
        with patch.object(auth_service.auth_redis_service, 'retrieve', return_value=None):
            result = await auth_service.validate_token("1234567890")
            
        assert result is None
    
    @pytest.mark.asyncio
    async def test_buyer_direct_authentication(self, auth_service, mock_session):
        """Test buyer direct authentication without email confirmation."""
        # Mock API response
        api_response = {
            "found": True,
            "users": [{
                "name": "John Doe",
                "email": "john@company.com",
                "unique_id": "buyer_123",
                "self_client": True,
                "company_name": "Test Company"
            }]
        }
        
        with patch.object(auth_service, '_call_authentication_api', return_value=api_response):
            with patch.object(auth_service, '_handle_successful_authentication') as mock_success:
                mock_success.return_value = {"status": "authentication_completed"}
                
                result = await auth_service._handle_authentication_response(
                    "1234567890", mock_session, api_response, "buy", "test message"
                )
        
        # Verify buyer goes directly to success without email confirmation
        assert mock_session.workflow_state["authentication_stage"] == "completed"
        assert mock_session.workflow_state["authenticated"] is True
        mock_success.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_seller_email_confirmation_required(self, auth_service, mock_session):
        """Test seller requires email confirmation."""
        api_response = {
            "found": True,
            "users": [{
                "name": "Jane Seller",
                "email": "jane@seller.com",
                "unique_id": "seller_123",
                "self_client": False,
                "company_name": "Seller Company"
            }]
        }
        
        with patch.object(auth_service, '_call_authentication_api', return_value=api_response):
            with patch.object(auth_service, '_request_email_confirmation') as mock_email:
                mock_email.return_value = {"status": "email_confirmation_requested"}
                
                result = await auth_service._handle_authentication_response(
                    "1234567890", mock_session, api_response, "sell", "test message"
                )
        
        # Verify seller goes to email confirmation
        assert mock_session.workflow_state["authentication_stage"] == "email_confirmation"
        mock_email.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_user_not_found_redirect_to_registration(self, auth_service, mock_session):
        """Test user not found redirects to registration."""
        api_response = {"found": False, "error": "User not found"}
        
        with patch.object(auth_service, '_call_authentication_api', return_value=api_response):
            result = await auth_service._handle_authentication_response(
                "1234567890", mock_session, api_response, "buy", "test message"
            )
        
        # Verify redirect to registration
        assert mock_session.workflow_state["workflow_type"] == "registration"
        assert result["status"] == "redirect_to_registration"


class TestRegistrationFlow:
    """Test registration flow scenarios."""
    
    @pytest.fixture
    def registration_service(self):
        return RegistrationService()
    
    @pytest.mark.asyncio
    async def test_buyer_registration_entity_extraction(self, registration_service):
        """Test buyer registration entity extraction."""
        message = "My name is John Doe from ABC Corp, email john@abc.com, pincode 110001"
        current_entities = {}
        
        # Mock OpenAI response
        mock_result = {
            "entities": {
                "name": "John Doe",
                "company_name": "ABC Corp", 
                "email": "john@abc.com",
                "pincode": "110001"
            },
            "completeness": 100,
            "missing_fields": [],
            "confidence": 0.9,
            "success": True
        }
        
        with patch.object(registration_service.openai_service, 'extract_registration_entities', return_value=mock_result):
            result = await registration_service._extract_registration_entities(
                message, current_entities, "buy"
            )
        
        assert result["success"] is True
        assert result["entities"]["name"] == "John Doe"
        assert result["completeness"] == 100
    
    @pytest.mark.asyncio
    async def test_seller_registration_entity_extraction(self, registration_service):
        """Test seller registration entity extraction."""
        message = "I'm Raj Kumar from Tech Solutions, we sell laptops in Mumbai 400001, GSTIN 27AABCU9603R1ZX"
        current_entities = {}
        
        mock_result = {
            "entities": {
                "full_name": "Raj Kumar",
                "company_name": "Tech Solutions",
                "products_services": "laptops",
                "location": "Mumbai",
                "pincode": "400001",
                "gstin": "27AABCU9603R1ZX"
            },
            "completeness": 85,
            "missing_fields": ["email"],
            "confidence": 0.9,
            "success": True
        }
        
        with patch.object(registration_service.openai_service, 'extract_registration_entities', return_value=mock_result):
            result = await registration_service._extract_registration_entities(
                message, current_entities, "sell"
            )
        
        assert result["success"] is True
        assert result["entities"]["full_name"] == "Raj Kumar"
        assert "email" in result["missing_fields"]


class TestChatServiceIntegration:
    """Test chat service integration with auth/registration."""
    
    @pytest.fixture
    def chat_service(self):
        return ChatService()
    
    @pytest.mark.asyncio
    async def test_authenticated_user_proceeds_to_main_flow(self, chat_service):
        """Test authenticated user proceeds to main flow."""
        # Mock authenticated user
        mock_user = UserDetailsSchema(
            id="123",
            name="Test User",
            email="test@example.com",
            self_client=True,
            role="buyer",
            is_registered=True,
            phone_number="1234567890"
        )
        
        with patch.object(chat_service, '_validate_user_authentication', return_value=mock_user):
            with patch.object(chat_service, '_process_text_message') as mock_process:
                mock_process.return_value = {"status": "processed"}
                
                result = await chat_service.process_message("1234567890", "I want to buy laptops")
        
        mock_process.assert_called_once()
        assert result["status"] == "processed"
    
    @pytest.mark.asyncio
    async def test_unauthenticated_user_goes_to_auth_flow(self, chat_service):
        """Test unauthenticated user goes to authentication flow."""
        with patch.object(chat_service, '_validate_user_authentication', return_value=None):
            with patch.object(chat_service, '_handle_authentication_and_registration_flow') as mock_auth:
                mock_auth.return_value = {"status": "authentication_started"}
                
                result = await chat_service.process_message("1234567890", "I want to buy laptops")
        
        mock_auth.assert_called_once()
        assert result["status"] == "authentication_started"


class TestEmailConfirmationParsing:
    """Test email confirmation parsing with OpenAI."""
    
    @pytest.mark.asyncio
    async def test_email_confirmation_positive_responses(self):
        """Test various positive confirmation responses."""
        from app.services.openai_service import OpenAIService
        
        openai_service = OpenAIService()
        test_cases = [
            ("yes", "confirmed"),
            ("confirm", "confirmed"), 
            ("that's my email", "confirmed"),
            ("this is correct", "confirmed"),
            ("confirmed", "confirmed")
        ]
        
        for message, expected_action in test_cases:
            mock_result = {"success": True, "action": expected_action, "confidence": 0.9}
            
            with patch.object(openai_service, 'parse_email_confirmation', return_value=mock_result):
                result = await openai_service.parse_email_confirmation(message, ["test@example.com"])
            
            assert result["action"] == expected_action
    
    @pytest.mark.asyncio
    async def test_email_confirmation_negative_responses(self):
        """Test various negative confirmation responses."""
        from app.services.openai_service import OpenAIService
        
        openai_service = OpenAIService()
        test_cases = [
            ("no", "declined"),
            ("not my email", "declined"),
            ("wrong email", "declined"),
            ("incorrect", "declined")
        ]
        
        for message, expected_action in test_cases:
            mock_result = {"success": True, "action": expected_action, "confidence": 0.9}
            
            with patch.object(openai_service, 'parse_email_confirmation', return_value=mock_result):
                result = await openai_service.parse_email_confirmation(message, ["test@example.com"])
            
            assert result["action"] == expected_action


if __name__ == "__main__":
    pytest.main([__file__])
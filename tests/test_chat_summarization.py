"""
Test chat summarization functionality.
"""

import pytest
import asyncio
from datetime import datetime, date
from unittest.mock import Mock, patch

from app.services.chat_summary_service import ChatSummaryService
from app.services.daily_summary_service import DailySummaryService
from app.models import ConversationSession, ChatSummary, DailySummary
from app.config import get_settings

class TestChatSummarization:
    """Test chat summarization services."""
    
    def setup_method(self):
        """Setup test data."""
        self.chat_summary_service = ChatSummaryService()
        self.daily_summary_service = DailySummaryService()
        
        # Mock session data
        self.mock_session = Mock(spec=ConversationSession)
        self.mock_session.session_id = "test_session_123"
        self.mock_session.external_user_id = "+918147745001"
        self.mock_session.workflow_type = "rfq_creation"
        self.mock_session.outcome = "completed"
        self.mock_session.rfq_id = "rfq_456"
        self.mock_session.extracted_entities = {
            "description": "Office chairs",
            "quantity": 50,
            "delivery_location": "Bangalore"
        }
        self.mock_session.created_at = datetime.now()
        self.mock_session.completed_at = datetime.now()
    
    @patch('app.services.chat_summary_service.get_db_session')
    @patch.object(ChatSummaryService, '_calculate_duration')
    async def test_generate_session_summary_success(self, mock_duration, mock_db_session):
        """Test successful session summary generation."""
        # Mock duration calculation
        mock_duration.return_value = 15
        
        # Mock database session
        mock_db = Mock()
        mock_db_session.return_value.__enter__.return_value = mock_db
        
        # Mock OpenAI service response
        with patch.object(self.chat_summary_service.openai_service, 'generate_session_summary') as mock_openai:
            mock_openai.return_value = "User requested 50 office chairs for Bangalore delivery. RFQ created successfully with focus on ergonomic requirements."
            
            # Test summary generation
            result = await self.chat_summary_service.generate_session_summary(self.mock_session)
            
            # Verify OpenAI was called with correct data
            mock_openai.assert_called_once()
            call_args = mock_openai.call_args[0][0]
            assert call_args['user_id'] == "+918147745001"
            assert call_args['workflow_type'] == "rfq_creation"
            assert call_args['outcome'] == "completed"
            assert call_args['extracted_entities']['description'] == "Office chairs"
            assert call_args['rfq_ids'] == ["rfq_456"]
            
            # Verify database operations
            mock_db.add.assert_called_once()
            mock_db.commit.assert_called_once()
    
    @patch('app.services.chat_summary_service.get_db_session')
    async def test_generate_session_summary_disabled(self, mock_db_session):
        """Test summary generation when disabled in config."""
        # Mock disabled setting
        with patch.object(self.chat_summary_service.settings, 'enable_session_summarization', False):
            result = await self.chat_summary_service.generate_session_summary(self.mock_session)
            
            # Should return None when disabled
            assert result is None
            mock_db_session.assert_not_called()
    
    @patch('app.services.chat_summary_service.get_db_session')
    async def test_load_user_context(self, mock_db_session):
        """Test loading user context from summaries."""
        # Mock database response
        mock_db = Mock()
        mock_db_session.return_value.__enter__.return_value = mock_db
        
        mock_summary = Mock(spec=ChatSummary)
        mock_summary.ai_generated_summary = "User created RFQ for office furniture"
        mock_summary.extracted_entities = {"description": "Office chairs"}
        mock_summary.rfq_ids = ["rfq_123"]
        mock_summary.session_outcome = "completed"
        mock_summary.created_at = datetime.now()
        
        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [mock_summary]
        
        # Test context loading
        result = await self.chat_summary_service.load_user_context("+918147745001")
        
        # Verify result
        assert len(result) == 1
        assert result[0]['summary'] == "User created RFQ for office furniture"
        assert result[0]['entities'] == {"description": "Office chairs"}
        assert result[0]['rfq_ids'] == ["rfq_123"]
        assert result[0]['outcome'] == "completed"
    
    @patch('app.services.daily_summary_service.get_db_session')
    async def test_generate_daily_summary(self, mock_db_session):
        """Test daily summary generation."""
        # Mock database
        mock_db = Mock()
        mock_db_session.return_value.__enter__.return_value = mock_db
        
        # Mock session count query
        mock_db.query.return_value.filter.return_value.filter.return_value.count.return_value = 3
        
        # Mock RFQ count query  
        mock_db.query.return_value.filter.return_value.filter.return_value.filter.return_value.count.return_value = 2
        
        # Mock summaries query for categories
        mock_summary = Mock()
        mock_summary.extracted_entities = {"description": "Office chairs"}
        mock_db.query.return_value.filter.return_value.filter.return_value.all.return_value = [mock_summary]
        
        # Mock no existing summary
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = None
        
        # Test daily summary generation
        result = await self.daily_summary_service.generate_daily_summary("+918147745001")
        
        # Verify database operations
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()
    
    @patch('app.services.daily_summary_service.get_db_session')
    async def test_get_user_daily_summary(self, mock_db_session):
        """Test getting user daily summary."""
        # Mock database
        mock_db = Mock()
        mock_db_session.return_value.__enter__.return_value = mock_db
        
        mock_summary = Mock(spec=DailySummary)
        mock_summary.date = date.today()
        mock_summary.sessions_count = 3
        mock_summary.rfqs_created_count = 2
        mock_summary.primary_product_categories = ["Office chairs", "Laptops"]
        mock_summary.total_rfq_value_estimate = None
        
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = mock_summary
        
        # Test getting daily summary
        result = await self.daily_summary_service.get_user_daily_summary("+918147745001")
        
        # Verify result
        assert result['sessions_count'] == 3
        assert result['rfqs_created_count'] == 2
        assert result['primary_product_categories'] == ["Office chairs", "Laptops"]
        assert result['total_rfq_value_estimate'] is None
    
    def test_calculate_duration(self):
        """Test session duration calculation."""
        # Create session with known duration
        session = Mock()
        session.created_at = datetime(2024, 1, 1, 10, 0, 0)
        session.completed_at = datetime(2024, 1, 1, 10, 15, 0)  # 15 minutes later
        
        duration = self.chat_summary_service._calculate_duration(session)
        assert duration == 15
        
        # Test with no completion time
        session.completed_at = None
        duration = self.chat_summary_service._calculate_duration(session)
        assert duration is None

def test_config_values():
    """Test that summarization config values are properly set."""
    settings = get_settings()
    
    # Test that new config values exist
    assert hasattr(settings, 'enable_session_summarization')
    assert hasattr(settings, 'enable_daily_summarization')
    assert hasattr(settings, 'max_chat_summaries_for_context')
    assert hasattr(settings, 'summarization_model')
    assert hasattr(settings, 'session_timeout_hours')
    
    # Test that session timeout was reduced
    assert settings.session_timeout_hours == 3

if __name__ == "__main__":
    # Run a simple test
    async def run_simple_test():
        """Run a simple integration test."""
        print("Testing Chat Summarization System...")
        
        # Test config
        settings = get_settings()
        print(f"✓ Session timeout: {settings.session_timeout_hours} hours")
        print(f"✓ Session summarization enabled: {settings.enable_session_summarization}")
        print(f"✓ Daily summarization enabled: {settings.enable_daily_summarization}")
        print(f"✓ Max summaries for context: {settings.max_chat_summaries_for_context}")
        print(f"✓ Summarization model: {settings.summarization_model}")
        
        # Test services can be instantiated
        try:
            chat_service = ChatSummaryService()
            daily_service = DailySummaryService()
            print("✓ Services instantiated successfully")
        except Exception as e:
            print(f"✗ Error instantiating services: {e}")
        
        print("Chat Summarization System test completed!")
    
    asyncio.run(run_simple_test())
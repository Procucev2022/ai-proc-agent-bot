"""
Test suite for single Excel file upload enforcement.

Tests verify that:
1. Only one Excel file can be processed per workflow
2. Subsequent uploads are rejected with clear messaging
3. Lock mechanism prevents simultaneous uploads
4. Cancel/timeout properly resets the flag
5. Flag is set immediately after validation (race condition protection)
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.processors.excel_message_processor import ExcelMessageProcessor
from app.models import User, ConversationSession, WorkflowType


@pytest.fixture
def mock_user():
    """Create a mock registered user."""
    user = MagicMock(spec=User)
    user.phone_number = "+971501234567"
    user.is_registered = True
    return user


@pytest.fixture
def mock_session():
    """Create a mock conversation session."""
    session = MagicMock(spec=ConversationSession)
    session.session_id = "971501234567_20251209"
    session.workflow_type = WorkflowType.rfq_creation
    session.workflow_state = {}
    return session


@pytest.fixture
def mock_whatsapp_service():
    """Create a mock WhatsApp service."""
    service = AsyncMock()
    service.send_message = AsyncMock()
    return service


@pytest.fixture
def mock_response_helpers():
    """Create a mock response helpers."""
    helpers = AsyncMock()
    helpers.generate_contextual_response = AsyncMock(return_value="Test response")
    helpers.generate_clarification_response = AsyncMock(return_value="Test clarification")
    return helpers


@pytest.fixture
def mock_openai_service():
    """Create a mock OpenAI service."""
    return MagicMock()


@pytest.fixture
def mock_redis():
    """Create a mock Redis client with lock support."""
    redis = AsyncMock()
    
    # Mock lock
    lock = AsyncMock()
    lock.acquire = AsyncMock(return_value=True)
    lock.release = AsyncMock()
    redis.lock = MagicMock(return_value=lock)
    
    return redis


@pytest.fixture
def excel_processor(mock_whatsapp_service, mock_response_helpers, mock_openai_service, mock_redis):
    """Create Excel message processor with mocked dependencies."""
    with patch('app.services.processors.excel_message_processor.get_settings') as mock_settings:
        mock_settings.return_value.redis_url = "redis://localhost:6379"
        
        processor = ExcelMessageProcessor(
            whatsapp_service=mock_whatsapp_service,
            response_helpers=mock_response_helpers,
            openai_service=mock_openai_service
        )
        # Replace Redis with our mock
        processor.redis = mock_redis
        
        return processor


@pytest.fixture
def mock_excel_content():
    """Create mock Excel upload content."""
    return {
        "document": {
            "link": "https://example.com/file.xlsx",
            "filename": "test_products.xlsx"
        }
    }


# ==============================================================================
# Test 1: First upload should be accepted
# ==============================================================================

@pytest.mark.asyncio
async def test_first_excel_upload_accepted(
    excel_processor, mock_user, mock_session, mock_excel_content
):
    """Test that the first Excel upload is accepted and processed."""
    # Mock validation and processing services
    with patch('app.services.processors.excel_message_processor.ExcelValidationService') as MockValidation, \
         patch('app.services.processors.excel_message_processor.ExcelProcessingService') as MockProcessing, \
         patch('app.services.processors.excel_message_processor.WorkflowManager'):
        
        # Mock validation success
        mock_validation_instance = MockValidation.return_value
        mock_validation_instance.validate_excel_file_from_url = AsyncMock(return_value={
            'valid': True,
            'content': b'mock_excel_content'
        })
        
        # Mock processing success
        mock_processing_instance = MockProcessing.return_value
        mock_processing_instance.process_excel_file = AsyncMock(return_value={
            'success': True,
            'items': [
                {'ItemDescription': 'Product 1', 'Quantity': 10, 'Uom': 'pcs'}
            ],
            'total_items': 1,
            'rfqs': [],
            'filename': 'test_products.xlsx'
        })
        
        # Mock ExcelHelpers
        with patch('app.services.processors.excel_message_processor.ExcelHelpers') as MockHelpers:
            MockHelpers.prepare_excel_context.return_value = {
                'excel_data': {
                    'success': True,
                    'items': [{'ItemDescription': 'Product 1', 'Quantity': 10}],
                    'filename': 'test_products.xlsx'
                },
                'completeness': 50
            }
            MockHelpers.should_complete_immediately.return_value = False
            
            # Execute
            result = await excel_processor.process_excel_upload(
                user=mock_user,
                session=mock_session,
                content=mock_excel_content
            )
            
            # Verify flag was set (it's set immediately after validation)
            assert mock_session.workflow_state.get('excel_file_processed') is True
            # The filename gets set immediately after validation with the filename from content
            assert mock_session.workflow_state.get('excel_filename') == 'test_products.xlsx'
            assert 'excel_processed_at' in mock_session.workflow_state
            
            # Verify lock was acquired and released
            excel_processor.redis.lock.assert_called_once()
            lock = excel_processor.redis.lock.return_value
            lock.acquire.assert_called_once()
            lock.release.assert_called()


# ==============================================================================
# Test 2: Second upload should be rejected
# ==============================================================================

@pytest.mark.asyncio
async def test_second_excel_upload_rejected(
    excel_processor, mock_user, mock_session, mock_excel_content, mock_whatsapp_service
):
    """Test that a second Excel upload is rejected when flag is set."""
    # Set flag to simulate first upload already processed
    mock_session.workflow_state = {
        'excel_file_processed': True,
        'excel_filename': 'first_file.xlsx',
        'excel_processed_at': '2025-12-09T10:00:00'
    }
    
    # Execute
    result = await excel_processor.process_excel_upload(
        user=mock_user,
        session=mock_session,
        content=mock_excel_content
    )
    
    # Verify rejection
    assert result['status'] == 'handled'
    assert result['response'] == 'excel_already_processed'
    
    # Verify user was notified
    mock_whatsapp_service.send_message.assert_called_once()
    message = mock_whatsapp_service.send_message.call_args[0][1]
    assert 'first_file.xlsx' in message
    assert 'already been processed' in message
    assert 'cancel' in message.lower()


# ==============================================================================
# Test 3: Simultaneous uploads - only first processes
# ==============================================================================

@pytest.mark.asyncio
async def test_simultaneous_uploads_blocked(
    excel_processor, mock_user, mock_session, mock_excel_content, mock_whatsapp_service
):
    """Test that simultaneous uploads are blocked by lock mechanism."""
    # Mock lock to simulate already acquired
    lock = excel_processor.redis.lock.return_value
    lock.acquire = AsyncMock(return_value=False)  # Lock already held
    
    # Execute
    result = await excel_processor.process_excel_upload(
        user=mock_user,
        session=mock_session,
        content=mock_excel_content
    )
    
    # Verify blocked
    assert result['status'] == 'handled'
    assert result['response'] == 'upload_in_progress'
    
    # Verify user was notified
    mock_whatsapp_service.send_message.assert_called_once()
    message = mock_whatsapp_service.send_message.call_args[0][1]
    assert 'being processed' in message.lower()
    
    # Verify lock was attempted but not released (since not acquired)
    lock.acquire.assert_called_once()
    lock.release.assert_not_called()


# ==============================================================================
# Test 4: Validation failure releases lock and clears flag
# ==============================================================================

@pytest.mark.asyncio
async def test_validation_failure_releases_lock(
    excel_processor, mock_user, mock_session, mock_excel_content
):
    """Test that validation failure releases lock and doesn't set flag."""
    with patch('app.services.processors.excel_message_processor.ExcelValidationService') as MockValidation:
        # Mock validation failure
        mock_validation_instance = MockValidation.return_value
        mock_validation_instance.validate_excel_file_from_url = AsyncMock(return_value={
            'valid': False,
            'error': 'Invalid Excel format'
        })
        
        # Execute
        result = await excel_processor.process_excel_upload(
            user=mock_user,
            session=mock_session,
            content=mock_excel_content
        )
        
        # Verify flag was NOT set
        assert not mock_session.workflow_state.get('excel_file_processed')
        
        # Verify lock was released
        lock = excel_processor.redis.lock.return_value
        lock.release.assert_called_once()
        
        # Verify response
        assert result['status'] == 'handled'
        assert result['response'] == 'validation_failed'


# ==============================================================================
# Test 5: Processing failure releases lock
# ==============================================================================

@pytest.mark.asyncio
async def test_processing_failure_releases_lock(
    excel_processor, mock_user, mock_session, mock_excel_content
):
    """Test that processing failure releases lock."""
    with patch('app.services.processors.excel_message_processor.ExcelValidationService') as MockValidation, \
         patch('app.services.processors.excel_message_processor.ExcelProcessingService') as MockProcessing:
        
        # Mock validation success
        mock_validation_instance = MockValidation.return_value
        mock_validation_instance.validate_excel_file_from_url = AsyncMock(return_value={
            'valid': True,
            'content': b'mock_content'
        })
        
        # Mock processing failure
        mock_processing_instance = MockProcessing.return_value
        mock_processing_instance.process_excel_file = AsyncMock(return_value={
            'success': False,
            'error': 'Processing failed'
        })
        
        # Execute
        result = await excel_processor.process_excel_upload(
            user=mock_user,
            session=mock_session,
            content=mock_excel_content
        )
        
        # Verify flag WAS set (immediately after validation)
        assert mock_session.workflow_state.get('excel_file_processed') is True
        
        # Verify lock was released
        lock = excel_processor.redis.lock.return_value
        lock.release.assert_called()
        
        # Verify response
        assert result['status'] == 'handled'
        assert result['response'] == 'processing_failed'


# ==============================================================================
# Test 6: Missing quantities clears flag and releases lock
# ==============================================================================

@pytest.mark.asyncio
async def test_missing_quantities_clears_flag(
    excel_processor, mock_user, mock_session, mock_excel_content
):
    """Test that missing quantities error clears flag and releases lock."""
    with patch('app.services.processors.excel_message_processor.ExcelValidationService') as MockValidation, \
         patch('app.services.processors.excel_message_processor.ExcelProcessingService') as MockProcessing, \
         patch('app.services.processors.excel_message_processor.WorkflowManager'):
        
        # Mock validation success
        mock_validation_instance = MockValidation.return_value
        mock_validation_instance.validate_excel_file_from_url = AsyncMock(return_value={
            'valid': True,
            'content': b'mock_content'
        })
        
        # Mock processing with missing quantities (>3 items)
        mock_processing_instance = MockProcessing.return_value
        mock_processing_instance.process_excel_file = AsyncMock(return_value={
            'success': True,
            'items': [
                {'ItemDescription': 'Product 1', 'Quantity': None},  # Missing
                {'ItemDescription': 'Product 2', 'Quantity': ''},    # Missing
                {'ItemDescription': 'Product 3', 'Quantity': None},  # Missing
                {'ItemDescription': 'Product 4', 'Quantity': None},  # Missing
            ],
            'total_items': 4,
            'rfqs': []
        })
        
        # Execute
        result = await excel_processor.process_excel_upload(
            user=mock_user,
            session=mock_session,
            content=mock_excel_content
        )
        
        # Verify flag was cleared
        assert not mock_session.workflow_state.get('excel_file_processed')
        assert not mock_session.workflow_state.get('excel_filename')
        
        # Verify lock was released
        lock = excel_processor.redis.lock.return_value
        lock.release.assert_called()
        
        # Verify response
        assert result['status'] == 'handled'
        assert result['response'] == 'missing_quantities_reupload_required'


# ==============================================================================
# Test 7: Critical error clears flag and releases lock
# ==============================================================================

@pytest.mark.asyncio
async def test_critical_error_clears_flag(
    excel_processor, mock_user, mock_session, mock_excel_content
):
    """Test that critical errors clear flag and release lock."""
    with patch('app.services.processors.excel_message_processor.ExcelValidationService') as MockValidation:
        # Mock validation to pass
        mock_validation_instance = MockValidation.return_value
        mock_validation_instance.validate_excel_file_from_url = AsyncMock(return_value={
            'valid': True,
            'content': b'mock_content'
        })
        
        with patch('app.services.processors.excel_message_processor.ExcelProcessingService') as MockProcessing:
            # Mock processing to raise critical error
            mock_processing_instance = MockProcessing.return_value
            mock_processing_instance.process_excel_file = AsyncMock(
                side_effect=Exception("Critical processing error")
            )
            
            # Execute
            result = await excel_processor.process_excel_upload(
                user=mock_user,
                session=mock_session,
                content=mock_excel_content
            )
            
            # Verify flag was cleared (it gets set after validation, then cleared on error)
            assert 'excel_file_processed' not in mock_session.workflow_state
            assert 'excel_filename' not in mock_session.workflow_state
            assert 'excel_processed_at' not in mock_session.workflow_state
            
            # Verify lock was released
            lock = excel_processor.redis.lock.return_value
            lock.release.assert_called()
            
            # Verify error response
            assert result['status'] == 'error'


# ==============================================================================
# Test 8: Flag is set IMMEDIATELY after validation (not after processing)
# ==============================================================================

@pytest.mark.asyncio
async def test_flag_set_immediately_after_validation(
    excel_processor, mock_user, mock_session, mock_excel_content
):
    """Test that flag is set immediately after validation, not after processing."""
    flag_set_after_validation = False
    
    async def mock_process_file(*args, **kwargs):
        # Check if flag is already set when processing starts
        nonlocal flag_set_after_validation
        flag_set_after_validation = mock_session.workflow_state.get('excel_file_processed', False)
        
        # Simulate processing taking time
        await asyncio.sleep(0.1)
        
        return {
            'success': True,
            'items': [{'ItemDescription': 'Product', 'Quantity': 10}],
            'total_items': 1,
            'rfqs': []
        }
    
    with patch('app.services.processors.excel_message_processor.ExcelValidationService') as MockValidation, \
         patch('app.services.processors.excel_message_processor.ExcelProcessingService') as MockProcessing, \
         patch('app.services.processors.excel_message_processor.WorkflowManager'), \
         patch('app.services.processors.excel_message_processor.ExcelHelpers') as MockHelpers:
        
        # Mock validation success
        mock_validation_instance = MockValidation.return_value
        mock_validation_instance.validate_excel_file_from_url = AsyncMock(return_value={
            'valid': True,
            'content': b'mock_content'
        })
        
        # Mock processing with our custom handler
        mock_processing_instance = MockProcessing.return_value
        mock_processing_instance.process_excel_file = mock_process_file
        
        MockHelpers.prepare_excel_context.return_value = {'excel_data': {}, 'completeness': 50}
        MockHelpers.should_complete_immediately.return_value = False
        
        # Execute
        await excel_processor.process_excel_upload(
            user=mock_user,
            session=mock_session,
            content=mock_excel_content
        )
        
        # Verify flag was set BEFORE processing started
        assert flag_set_after_validation is True


# ==============================================================================
# Test 9: Lock timeout is 120 seconds
# ==============================================================================

@pytest.mark.asyncio
async def test_lock_timeout_is_120_seconds(
    excel_processor, mock_user, mock_session, mock_excel_content
):
    """Test that lock timeout is set to 120 seconds (2 minutes)."""
    with patch('app.services.processors.excel_message_processor.ExcelValidationService') as MockValidation:
        # Mock validation to trigger lock acquisition
        mock_validation_instance = MockValidation.return_value
        mock_validation_instance.validate_excel_file_from_url = AsyncMock(return_value={
            'valid': False,
            'error': 'Test error'
        })
        
        # Execute
        await excel_processor.process_excel_upload(
            user=mock_user,
            session=mock_session,
            content=mock_excel_content
        )
        
        # Verify lock was created with 120 second timeout
        excel_processor.redis.lock.assert_called_once()
        call_args = excel_processor.redis.lock.call_args
        assert call_args[1]['timeout'] == 120


# ==============================================================================
# Test 10: Unregistered user doesn't trigger lock or flag
# ==============================================================================

@pytest.mark.asyncio
async def test_unregistered_user_no_lock_or_flag(
    excel_processor, mock_user, mock_session, mock_excel_content
):
    """Test that unregistered users don't trigger lock or flag logic."""
    # Make user unregistered
    mock_user.is_registered = False
    
    # Execute
    result = await excel_processor.process_excel_upload(
        user=mock_user,
        session=mock_session,
        content=mock_excel_content
    )
    
    # Verify lock was not acquired
    lock = excel_processor.redis.lock.return_value
    lock.acquire.assert_not_called()
    
    # Verify flag was not set
    assert not mock_session.workflow_state.get('excel_file_processed')
    
    # Verify registration response
    assert result['status'] == 'handled'
    assert result['response'] == 'registration_required'


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

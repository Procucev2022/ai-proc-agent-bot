"""Exhaustive deterministic branch tests for the OpenAI and entity services.

Every external collaborator is replaced with an in-memory fake.  This module is
intentionally independent from the neighboring service tests so it can be run
as a focused coverage target.
"""

import asyncio
import builtins
import json
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.entity_service as entity_module
import app.services.openai_service as openai_module


class _File:
    def __init__(self, value='{}'):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.value


def response(args=None, *, output_type='function_call', output_text='', usage=True, raw=False):
    output = []
    if args is not None:
        arguments = args if raw else json.dumps(args)
        output = [SimpleNamespace(type=output_type, arguments=arguments)]
    return SimpleNamespace(
        output=output,
        output_text=output_text,
        usage=(SimpleNamespace(input_tokens=10, output_tokens=4,
                               input_tokens_details=SimpleNamespace(cached_tokens=2))
               if usage else None),
    )


def no_output(text=''):
    return response(None, output_text=text, usage=False)


@pytest.fixture
def service(monkeypatch):
    settings = SimpleNamespace(
        openai_model_default='model', openai_model_advanced='advanced',
        support_email='support@example.test', support_contact_info='help@example.test',
        PROCUCEV_PORTAL_URL='https://portal.test',
    )
    interaction = MagicMock()
    monkeypatch.setattr(openai_module, 'get_settings', lambda: settings)
    monkeypatch.setattr(openai_module, 'get_interaction_logger', lambda: interaction)
    obj = openai_module.OpenAIService()
    client = SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock()),
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())),
        close=AsyncMock(),
    )
    obj._client = client
    monkeypatch.setattr(obj, '_load_prompt', lambda *a, **k: 'PROMPT')
    monkeypatch.setattr(builtins, 'open', lambda *a, **k: _File('{"tool":"definition"}'))
    return obj, client, interaction, settings


@pytest.fixture
def entity():
    api = MagicMock()
    for name in ('extract_entities', 'extract_registration_entities',
                 'validate_delivery_date', 'extract_entities_with_summary_context',
                 'merge_resolved_references_with_entities'):
        setattr(api, name, AsyncMock())
    api.extract_historical_options = MagicMock()
    return entity_module.EntityService(api), api


@pytest.mark.asyncio
async def test_lifecycle_helpers_notifications_prompts_and_message_shapes(monkeypatch, service, tmp_path):
    obj, client, _, settings = service
    constructed = MagicMock(close=AsyncMock())
    monkeypatch.setattr(openai_module, 'AsyncOpenAI', lambda **kwargs: constructed)
    monkeypatch.setenv('AZURE_OPENAI_API_KEY', 'key')
    monkeypatch.setenv('AZURE_OPENAI_ENDPOINT', 'https://endpoint')
    obj._client = None
    assert obj.client is constructed
    assert obj.client is constructed
    obj._client_closed = True
    assert obj.client is constructed
    await obj.close()
    assert constructed.close.await_count == 1
    constructed.close.side_effect = RuntimeError('close')
    await obj.close()
    obj._client = None
    obj.close_sync()

    obj._client = client
    client.close.side_effect = None
    obj.close_sync()
    assert obj._client_closed
    await obj.__aexit__(None, None, None)
    assert await obj.__aenter__() is obj
    obj._client = None
    obj.close_sync()

    obj._track_openai_call('x', 'phone')
    obj._track_openai_call('x', 'phone')
    obj._track_openai_call('y')
    assert obj.get_call_summary('phone') == {'x': 2}
    assert obj.get_call_summary() == {'y': 1}
    assert obj.get_call_summary('missing') == {}

    notifier = MagicMock(notify_general_error=AsyncMock())
    monkeypatch.setattr(obj, '_get_error_notification_service', lambda: notifier)
    await obj._notify_openai_error('timeout', 'down', 'method')
    notifier.notify_general_error.assert_awaited_once()
    notifier.notify_general_error.side_effect = RuntimeError('notify')
    await obj._notify_openai_error('timeout', 'down', 'method')

    obj._load_prompt = openai_module.OpenAIService._load_prompt.__get__(obj)
    obj.prompts_dir = tmp_path
    folder = tmp_path / 'cat'
    folder.mkdir()
    monkeypatch.setattr(builtins, 'open', lambda *a, **k: _File('Hi {name} {support_email} {support_info_email} {portal_url}'))
    assert obj._load_prompt('cat', 'p', name='Ada') == 'Hi Ada support@example.test help@example.test https://portal.test'
    monkeypatch.setattr(builtins, 'open', lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert obj._load_prompt('cat', 'missing').startswith('Generate')

    history = [{'role': 'user', 'content': str(i)} for i in range(12)]
    assert len(obj._build_messages_with_history({'conversation_history': {'openai_messages': history}}, 'now')) == 11
    assert obj._build_messages_with_history({'conversation_history': {'openai_messages': 'bad'}}, '') == []
    assert obj._build_messages_with_history(None) == []
    assert obj._is_image_content('{"mime_type":"image/png"}')
    assert obj._is_image_content('text', {'user_message': {'mime_type': 'image/png'}})
    assert obj._is_image_content('{"mime_type":"image/jpeg","image":true}')
    assert not obj._is_image_content('plain', {'user_message': 'plain'})
    assert obj.extract_text_from_message({'content': {'type': 'button_reply', 'button_reply': {}}}) == '[button reply]'
    assert obj.extract_text_from_message({'content': {'type': 'button_reply', 'button_reply': {'id': 'yes'}}}) == 'yes'
    assert obj.extract_text_from_message({'content': 'text'}) == 'text'
    assert obj.extract_text_from_message({'content': {'text': 'dict'}}) == 'dict'
    assert obj.extract_text_from_message({'content': {'x': 1}}) == '[non-text content]'
    assert obj.extract_text_from_message({'content': [{'type': 'text', 'text': 'a'}, {'type': 'image'}]}) == 'a'
    assert obj.extract_text_from_message({'content': [{'type': 'image'}]}) == '[multimodal content]'
    assert obj.extract_text_from_message({'content': 4}) == '[unknown content]'
    assert 'support@example.test' in obj._get_fallback_response({}, [])
    assert obj._get_fallback_intent_response('bad')['success'] is False


@pytest.mark.asyncio
async def test_classify_intent_all_input_context_and_response_paths(monkeypatch, service):
    obj, client, log, _ = service
    client.responses.create.return_value = response({'intent': 'buy_something', 'confidence': 90, 'reasoning': 'r'})
    context = {
        'user_role': 'buyer', 'workflow_type': 'rfq', 'conversation_stage': 'collecting',
        'conversation_history': {'openai_messages': [{'role': 'user', 'content': 'old'}, {'role': 'assistant', 'content': {'text': 'reply'}}]},
        'workflow_state': {'sectioned_rfq': {'active': True, 'current_section': 'items', 'awaiting_delivery_modification': True, 'awaiting_items_modification': True},
                           'pending_rfq': {'x': 1}, 'pending_optional_rfq': {'x': 1}, 'pending_attachment_decision': True,
                           'extracted_entities': {'description': 'bolt'}},
    }
    result = await obj.classify_intent({'text': 'buy'}, context)
    assert result['success'] and result['intent'] == 'buy_something'
    assert log.log_intent_classification.called
    client.responses.create.return_value = response({'intent': 'x'})
    assert (await obj.classify_intent([{'text': 'a'}, {'text': 'b'}]))['success']
    client.responses.create.return_value = response({'intent': 'x'}, output_type='text')
    obj._get_fallback_classification = AsyncMock(return_value={'fallback': True})
    assert await obj.classify_intent('bad output') == {'fallback': True}
    client.responses.create.return_value = response('{bad', raw=True)
    assert await obj.classify_intent('bad json') == {'fallback': True}

    class Unexpected(Exception):
        pass
    obj._notify_openai_error = AsyncMock()
    for exc_name, exc in [('APITimeoutError', Unexpected('timeout')), ('APIConnectionError', Unexpected('connection')), ('APIError', Unexpected('api'))]:
        monkeypatch.setattr(openai_module, exc_name, Unexpected)
        client.responses.create.side_effect = exc
        assert await obj.classify_intent('error') == {'fallback': True}
    monkeypatch.setattr(openai_module, 'RateLimitError', Unexpected)
    monkeypatch.setattr(openai_module, 'get_user_phone_context', lambda: None)
    client.responses.create.side_effect = Unexpected('rate')
    assert await obj.classify_intent('rate') == {'fallback': True}
    obj._handle_rate_limit_timeout = AsyncMock()
    monkeypatch.setattr(openai_module, 'get_user_phone_context', lambda: '+1555')
    assert (await obj.classify_intent('rate'))['timeout_handled']
    obj._handle_rate_limit_timeout.assert_awaited_with('+1555')


@pytest.mark.asyncio
async def test_fallback_classification_and_entity_openai_response_matrix(monkeypatch, service):
    obj, client, log, settings = service
    cancel = MagicMock(_send_cancellation_message=AsyncMock())
    monkeypatch.setattr('app.services.cancel_service.CancelService', lambda: cancel)
    notified = await obj._get_fallback_classification('x', {'user_role': 'buyer'}, user_phone='1')
    assert notified['success'] is False and notified['intent'] == 'general_inquiry'

    # Classifying must not message the user. Sending here produced a second
    # outbound message on top of the caller's real reply.
    cancel._send_cancellation_message.assert_not_awaited()

    # Still a usable classification dict rather than None, which callers used to
    # dereference as a dict.
    quiet = await obj._get_fallback_classification('x', {})
    assert quiet['success'] is False and quiet['confidence'] == 30
    assert await obj._get_fallback_classification('x', None) is not None
    cancel._send_cancellation_message.assert_not_awaited()

    cases = [
        ('modification_request', {'modifications': [{'operation_type': 'modify'}], 'has_new_values': True}),
        ('buy_something', {'products': [{'description': 'bolt'}], 'deliveryDate': '2030-01-01', 'state': 'MH', 'city': 'Pune', 'pincode': '411005'}),
        ('rfq_status_check', {'rfq_id': 'R1'}),
        ('registration_buyer', {'entities': {'email': 'a@test'}, 'completeness': 50, 'missing_fields': ['phone'], 'validation_errors': []}),
        ('registration_seller', {'entities': {'name': 'Seller'}}),
        ('product_search', {'entities': {'description': 'nut'}, 'next_questions': ['qty']}),
        ('bfs', {'entities': {'description': 'stock'}}),
        ('unknown_workflow', {'entities': {'description': 'x'}}),
    ]
    for workflow, args in cases:
        client.responses.create.side_effect = None
        client.responses.create.return_value = response(args)
        result = await obj.extract_entities('request', workflow)
        assert result['success']
    client.responses.create.return_value = response({'x': 1}, output_type='text')
    assert not (await obj.extract_entities('x'))['success']
    client.responses.create.return_value = response('{bad', raw=True)
    obj._notify_openai_error = AsyncMock()
    assert not (await obj.extract_entities('x'))['success']
    class ApiFailure(Exception):
        pass
    monkeypatch.setattr(openai_module, 'APIError', ApiFailure)
    client.responses.create.side_effect = ApiFailure('api')
    assert not (await obj.extract_entities('x'))['success']
    client.responses.create.side_effect = RuntimeError('unexpected')
    assert not (await obj.extract_entities('x'))['success']
    obj.interaction_logger.log_entity_extraction.side_effect = RuntimeError('log')
    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'products': [{'description': 'x'}]})
    assert (await obj.extract_entities('x'))['success']


@pytest.mark.asyncio
async def test_summary_reference_switch_and_response_generation_matrix(monkeypatch, service):
    obj, client, log, _ = service
    client.responses.create.return_value = response({'products': [{'description': 'usual'}], 'resolved_references': [{'phrase': 'usual'}]})
    assert (await obj.extract_entities_with_summary_context('same', [{'summary': 'old'}, {}]))['success']
    client.responses.create.return_value = response({'x': 1}, output_type='text')
    assert not (await obj.extract_entities_with_summary_context('x', []))['success']
    client.responses.create.return_value = response('{bad', raw=True)
    assert not (await obj.extract_entities_with_summary_context('x', []))['success']
    client.responses.create.side_effect = RuntimeError('summary')
    assert not (await obj.extract_entities_with_summary_context('x', []))['success']

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'has_references': True, 'confidence': 80, 'reference_types': ['address'], 'detected_phrases': ['usual']})
    assert (await obj.analyze_reference_context('usual'))['has_references']
    client.responses.create.return_value = response({'x': 1}, output_type='text')
    assert not (await obj.analyze_reference_context('x'))['success']
    client.responses.create.side_effect = RuntimeError('reference')
    assert not (await obj.analyze_reference_context('x'))['success']

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'chosen_action': 'switch_to_new', 'confidence': 90})
    assert (await obj.analyze_intent_switch_response('switch', {}))['chosen_action'] == 'switch_to_new'
    client.responses.create.return_value = no_output()
    assert (await obj.analyze_intent_switch_response('?', {}))['chosen_action'] == 'continue_current'
    client.responses.create.side_effect = RuntimeError('switch')
    assert not (await obj.analyze_intent_switch_response('?', {}))['success']
    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'success': True, 'updated_products': [{'city': 'Pune'}]})
    assert (await obj.merge_resolved_references_with_entities([], [], 'x'))['success']
    client.responses.create.return_value = response({'x': 1}, output_type='text')
    assert not (await obj.merge_resolved_references_with_entities([{'x': 1}], [], 'x'))['success']
    client.responses.create.side_effect = RuntimeError('merge')
    assert 'merge' in (await obj.merge_resolved_references_with_entities([{'x': 1}], [], 'x'))['error']

    client.responses.create.side_effect = None
    client.responses.create.return_value = SimpleNamespace(output=[], output_text='generated')
    assert await obj.generate_response({'user_message': 'x'}, [{'x': 1}]) == 'generated'
    assert await obj.generate_response({}, prompt_file='response_generation/file') == 'generated'
    client.responses.create.return_value = SimpleNamespace(output=[], output_text='')
    assert 'apologize' in await obj.generate_response({})
    client.responses.create.side_effect = RuntimeError('generation')
    assert 'technical issue' in await obj.generate_response({})
    client.responses.create.side_effect = None
    client.responses.create.return_value = SimpleNamespace(output=[], output_text='status')
    assert await obj.generate_rfq_status_response({'user_role': 'BUYER'}) == 'status'
    assert await obj.generate_seller_rfq_overview_response({}) == 'status'
    client.responses.create.side_effect = RuntimeError('status')
    assert 'technical issue' in await obj.generate_rfq_status_response({})


@pytest.mark.asyncio
async def test_seller_validation_timeout_and_contextual_response_paths(monkeypatch, service):
    obj, client, _, _ = service
    client.responses.create.return_value = response({'intent': 'seller', 'confidence': 80})
    assert (await obj.generate_seller_intent({'workflow_state': {'seller_workflow_state': 'x'}}))['intent'] == 'seller'
    client.responses.create.return_value = response({'x': 1}, output_type='text')
    assert (await obj.generate_seller_intent({}))['intent'] == 'general_question'
    client.responses.create.side_effect = RuntimeError('seller')
    assert 'technical issue' in await obj.generate_seller_intent({})
    client.responses.create.side_effect = None
    client.responses.create.return_value = SimpleNamespace(output=[], output_text='overview')
    assert await obj.generate_seller_rfq_overview_response({}) == 'overview'
    client.responses.create.side_effect = RuntimeError('overview')
    assert 'technical issue' in await obj.generate_seller_rfq_overview_response({})

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'is_valid': False, 'validation_score': 50, 'severity': 'warning'})
    assert (await obj.validate_field_value('qty', 'x', {'a': 1}))['severity'] == 'warning'
    client.responses.create.return_value = response({'x': 1}, output_type='text')
    assert (await obj.validate_field_value('qty', 'x', {}))['is_valid']
    client.responses.create.return_value = response('{bad', raw=True)
    assert (await obj.validate_field_value('qty', 'x', {}))['severity'] == 'warning'

    # Patch every dependency imported by the timeout method; no Redis/DB/WhatsApp is real.
    import app.redis_db as redis_module
    import app.database as database_module
    import app.models as models_module
    import app.services.helpers.session_helpers as session_module
    import app.services.whatsapp_service as whatsapp_module
    session_store = AsyncMock(get_session=AsyncMock(return_value={'session_id': 'sid'}), delete_session=AsyncMock())
    direct_redis = SimpleNamespace(init_client=AsyncMock(), client=SimpleNamespace(delete=AsyncMock(return_value=9)))
    db = MagicMock(save_conversation_session=MagicMock(), close=MagicMock())
    whatsapp = MagicMock(send_message=AsyncMock())
    monkeypatch.setattr(session_module.SessionHelpers, 'generate_session_id', staticmethod(lambda phone, kind: 'sid'))
    monkeypatch.setattr(redis_module, 'get_session_redis_service', lambda: session_store)
    monkeypatch.setattr(redis_module, 'get_redis_service', lambda: direct_redis)
    monkeypatch.setattr(database_module, 'DatabaseManager', lambda: db)
    monkeypatch.setattr(models_module, 'ConversationOutcome', SimpleNamespace(abandoned=SimpleNamespace(value='abandoned')))
    monkeypatch.setattr(models_module, 'ConversationSession', lambda **kwargs: kwargs)
    monkeypatch.setattr(whatsapp_module, 'WhatsAppService', lambda: whatsapp)
    await obj._handle_rate_limit_timeout('+1555')
    session_store.delete_session.assert_awaited_once_with('sid')
    direct_redis.client.delete.assert_awaited_once()
    whatsapp.send_message.assert_awaited_once()
    session_store.get_session.side_effect = RuntimeError('redis get')
    await obj._handle_rate_limit_timeout('1')
    direct_redis.init_client.side_effect = RuntimeError('redis init')
    await obj._handle_rate_limit_timeout('1')

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'acknowledgment': 'A', 'progress_update': 'P', 'next_question': 'Q'})
    assert await obj.generate_contextual_response({'user_message': 'x'}, ['base']) == 'A\n\nP\n\nQ'
    client.responses.create.return_value = no_output()
    assert await obj.generate_contextual_response({}, ['first']) == 'Thank you for the information! first'
    assert 'details' in await obj.generate_contextual_response({}, [])
    client.responses.create.side_effect = RuntimeError('context')
    assert 'details' in await obj.generate_contextual_response({}, [])

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'confirmation': 'done', 'summary': 'sum', 'next_steps': 'next', 'reference_id': 'R'})
    completed = await obj.generate_completion_response({}, {'user_message': 'x'})
    assert 'Summary:' in completed and 'Reference: R' in completed
    client.responses.create.return_value = no_output()
    assert await obj.generate_completion_response({}, {}) == 'RFQ completed. Processing request.'
    client.responses.create.side_effect = RuntimeError('complete')
    assert await obj.generate_completion_response({}, {}) == 'RFQ completed. Processing request.'
    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'progress_acknowledgment': 'ok', 'questions': ['one', 'two']})
    assert '• one' in await obj.generate_clarification_response(['fallback'], 20, {})
    client.responses.create.return_value = no_output()
    assert '• fallback' in await obj.generate_clarification_response(['fallback'], 20, {})
    client.responses.create.side_effect = RuntimeError('clarify')
    assert '• fallback' in await obj.generate_clarification_response(['fallback'], 20, {})


@pytest.mark.asyncio
async def test_excel_seller_learning_and_selection_methods(monkeypatch, service):
    obj, client, _, _ = service
    client.responses.create.return_value = response({'header_row_index': 2, 'confidence': 90})
    assert (await obj.detect_excel_header_row([[None, 'x']]))['header_row_index'] == 2
    client.responses.create.return_value = response({'x': 1}, output_type='text')
    assert (await obj.detect_excel_header_row([]))['header_row_index'] == 0
    client.responses.create.side_effect = RuntimeError('header')
    assert not (await obj.detect_excel_header_row([]))['success']

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'selected_division': 'IT', 'confidence': 80})
    assert (await obj.select_division({'x': 1}))['selected_division'] == 'IT'
    client.responses.create.return_value = no_output()
    assert (await obj.select_division({}))['selected_division'] == 'Admin & IT'
    client.responses.create.side_effect = RuntimeError('division')
    assert (await obj.select_division({}))['confidence'] == 20

    client.responses.create.side_effect = None
    client.responses.create.return_value = SimpleNamespace(output=[], output_text='summary')
    assert await obj.generate_session_summary({'conversation_messages': [{'sender': 'u', 'content': 'hello'}]}) == 'summary'
    client.responses.create.side_effect = RuntimeError('session')
    assert await obj.generate_session_summary({}) == 'Session completed'

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'column_mapping': {'Qty': 'Quantity'}})
    assert (await obj.map_excel_columns(['Qty']))['column_mapping']['Qty'] == 'Quantity'
    client.responses.create.return_value = response({'mapping_details': [{'excel_header': 'Item', 'target_column': 'ItemDescription'}]})
    assert (await obj.map_excel_columns(['Item']))['column_mapping']['Item'] == 'ItemDescription'
    client.responses.create.return_value = no_output()
    assert not (await obj.map_excel_columns(['x']))['success']
    client.responses.create.side_effect = RuntimeError('columns')
    assert not (await obj.map_excel_columns(['x']))['success']

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'items': ['a', 'b'], 'confidence': 95})
    assert (await obj.parse_seller_product_items('a,b'))['items'] == ['a', 'b']
    client.responses.create.return_value = response({'x': 1}, output_type='text')
    assert (await obj.parse_seller_product_items('a'))['fallback_used']
    client.responses.create.side_effect = RuntimeError('parse')
    assert (await obj.parse_seller_product_items('a'))['fallback_used']

    similar = [{'item': 'bolt', 'category': 'Hardware', 'similarity_score': 0.9}]
    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'category': 'Hardware', 'confidence_score': .9})
    assert (await obj.categorize_with_similar_items('bolt', similar, ['Hardware']))['success']
    client.responses.create.return_value = no_output()
    assert (await obj.categorize_with_similar_items('bolt', similar))['category'] == 'Hardware'
    client.responses.create.side_effect = RuntimeError('category')
    assert (await obj.categorize_with_similar_items('bolt', []))['category'] is None

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'level_1_category': 'IT', 'level_2_category': 'Hardware', 'level_3_category': 'Laptop'})
    three = await obj.generate_3_level_categorization('laptop', similar, 'IT')
    assert three['categorization']['level_3'] == 'Laptop'
    client.responses.create.return_value = no_output()
    assert not (await obj.generate_3_level_categorization('x', []))['success']
    client.responses.create.side_effect = RuntimeError('three')
    assert not (await obj.generate_3_level_categorization('x', []))['success']
    obj._client = client
    obj._client_closed = False
    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'is_valid': True, 'confidence_score': .8})
    assert (await obj.validate_learning_category('a', 'b', 'c', 'x'))['is_valid']
    client.responses.create.return_value = no_output()
    assert (await obj.validate_learning_category('a', 'b', 'c', 'x'))['is_valid']
    client.responses.create.side_effect = RuntimeError('validate')
    assert not (await obj.validate_learning_category('a', 'b', 'c', 'x'))['is_valid']

    cats = [{'id': 1, 'level_1_category': 'IT', 'level_2_category': 'Hardware', 'level_3_category': 'Laptop'},
            {'id': 2, 'level_1_category': 'IT', 'level_2_category': 'Hardware', 'level_3_category': 'Desktop'}]
    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'selected_category_id': 1})
    assert (await obj.map_seller_category_to_existing_learning('Computers', cats, 'Acme', {'city': 'Pune'}))['success']
    client.responses.create.return_value = response({'selected_category_id': 99}, usage=False)
    assert not (await obj.map_seller_category_to_existing_learning('x', cats))['success']
    client.responses.create.return_value = response({'x': 1}, output_type='text')
    assert not (await obj.map_seller_category_to_existing_learning('x', cats))['success']
    class Rate(Exception):
        pass
    monkeypatch.setattr(openai_module, 'RateLimitError', Rate)
    sleeps = []
    async def fake_sleep(value): sleeps.append(value)
    monkeypatch.setattr(openai_module.asyncio, 'sleep', fake_sleep)
    client.responses.create.side_effect = [Rate('r'), response({'selected_category_id': 1})]
    assert (await obj.map_seller_category_to_existing_learning('x', cats))['success'] and sleeps == [1]
    client.responses.create.side_effect = RuntimeError('map')
    assert not (await obj.map_seller_category_to_existing_learning('x', cats))['success']

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'mappings': [{'seller_category': 'Computers', 'selected_category_id': 1}, {'seller_category': 'bad', 'selected_category_id': 99}]})
    batch = await obj.map_seller_categories_batch(['Computers', 'bad'], cats)
    assert batch['Computers']['success'] and not batch['bad']['success']
    client.responses.create.return_value = no_output()
    assert await obj.map_seller_categories_batch(['none'], cats) == {}
    client.responses.create.side_effect = RuntimeError('batch')
    assert not (await obj.map_seller_categories_batch(['x'], cats))['x']['success']

    candidate = [{'seller_id': 'S1', 'seller_name': 'Acme', 'ranking': 'gold', 'category_match': {'original_category': 'Hardware', 'similarity_score': .8}, 'distance_km': 2, 'phone_number': '1', 'location': {'city': 'Pune', 'state': 'MH'}}]
    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'selected_seller_ids': ['S1', 'missing']})
    assert (await obj.select_best_sellers('bolt', candidate))['total_selected'] == 1
    assert not (await obj.select_best_sellers('bolt', []))['success']
    client.responses.create.return_value = no_output()
    assert not (await obj.select_best_sellers('bolt', candidate))['success']
    client.responses.create.side_effect = RuntimeError('select')
    assert not (await obj.select_best_sellers('bolt', candidate))['success']


@pytest.mark.asyncio
async def test_openai_confirmation_dates_registration_misc_and_completion(monkeypatch, service):
    obj, client, _, _ = service
    rfq = {'items': [{'description': 'x', 'brand': 'b\x00' + 'z' * 100, 'remarks': 'r'}] * 6, 'delivery_date': '2030-01-01'}
    client.responses.create.return_value = response({'summary': 'line1\\nline2 ' * 250})
    assert '+1 more items' in await obj.generate_rfq_confirmation(rfq, {'user_message': 'buy'})
    client.responses.create.return_value = response({'x': 1}, output_type='text')
    assert await obj.generate_rfq_confirmation({}, {}) == "Here's a summary of your RFQ."
    client.responses.create.side_effect = RuntimeError('confirm')
    assert await obj.generate_rfq_confirmation({}, {}) == "Here's a summary of your RFQ."

    client.responses.create.side_effect = None
    client.responses.create.return_value = SimpleNamespace(output=[], output_text='generated')
    assert await obj.generate_opt_out_confirmation('Acme') == 'generated'
    assert await obj.generate_opt_in_confirmation('Acme', ['Tools']) == 'generated'
    assert await obj.generate_permission_request('Acme', []) == 'generated'
    client.responses.create.return_value = SimpleNamespace(output=[], output_text='')
    assert 'opted out' in await obj.generate_opt_out_confirmation('Acme')
    client.responses.create.side_effect = RuntimeError('message')
    assert 'welcome back' in await obj.generate_opt_in_confirmation('Acme', [])
    assert 'RFQ notifications' in await obj.generate_permission_request('Acme', [])

    future = (date.today() + timedelta(days=10)).isoformat()
    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'is_valid': True, 'normalized_date': future, 'validation_issues': []})
    assert (await obj.validate_delivery_date('next week', future))['is_valid']
    client.responses.create.return_value = response({'is_valid': True, 'normalized_date': 'not-a-date'})
    assert not (await obj.validate_delivery_date('bad'))['is_valid']
    client.responses.create.return_value = no_output()
    assert not (await obj.validate_delivery_date('bad'))['success']
    client.responses.create.side_effect = RuntimeError('date')
    assert (await obj.validate_delivery_date('bad'))['parsing_method'] == 'error'

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'intent': 'opt_out', 'confidence': 90})
    assert (await obj.detect_opt_out_intent('stop'))['intent'] == 'opt_out'
    client.responses.create.return_value = response({'x': 1}, output_type='text')
    assert (await obj.detect_opt_out_intent('?'))['intent'] == 'none'
    client.responses.create.side_effect = RuntimeError('opt')
    assert (await obj.detect_opt_out_intent('?'))['confidence'] == 20

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'entities': {'email': 'a@test'}, 'completeness': 80, 'confidence': 90})
    assert (await obj.extract_registration_entities('email', 'old', 'seller', {'name': 'A'}))['entities']['email'] == 'a@test'
    client.responses.create.return_value = no_output()
    assert not (await obj.extract_registration_entities('x'))['success']
    client.responses.create.side_effect = RuntimeError('registration')
    assert not (await obj.extract_registration_entities('x'))['success']

    obj.classify_intent = MagicMock(return_value={'intent': 'sell_something', 'confidence': 70, 'success': True})
    assert obj.classify_auth_intent('sell')['intent'] == 'sell'
    obj.classify_intent.side_effect = RuntimeError('auth')
    assert obj.classify_auth_intent('?')['intent'] == 'unclear'

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'status': 'confirmed', 'selected_email': 'a@test'})
    assert (await obj.parse_email_confirmation('yes', ['a@test']))['success']
    client.responses.create.return_value = no_output()
    assert not (await obj.parse_email_confirmation('?', []))['success']
    client.responses.create.side_effect = RuntimeError('email')
    assert not (await obj.parse_email_confirmation('?', []))['success']

    client.responses.create.side_effect = None
    client.responses.create.return_value = SimpleNamespace(output=[], output_text=' YES ')
    assert await obj.parse_confirmation_response('yes') == 'yes'
    client.responses.create.return_value = SimpleNamespace(output=[], output_text='maybe')
    assert await obj.parse_confirmation_response('maybe') == 'unclear'
    client.responses.create.side_effect = RuntimeError('confirmation')
    assert await obj.parse_confirmation_response('?') == 'unclear'

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'registration_type': 'buyer', 'confidence': 90})
    assert (await obj.detect_registration_type('buy'))['registration_type'] == 'buyer'
    client.responses.create.return_value = no_output()
    assert not (await obj.detect_registration_type('?'))['success']
    client.responses.create.side_effect = RuntimeError('type')
    assert not (await obj.detect_registration_type('?'))['success']

    client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='completion'))])
    assert await obj.get_completion('prompt') == 'completion'
    client.chat.completions.create.side_effect = RuntimeError('chat')
    with pytest.raises(RuntimeError, match='chat'):
        await obj.get_completion('prompt')

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'rfq_ids': ['R1'], 'confidence': 80})
    assert (await obj.extract_rfq_ids_from_message('R1', 'last'))['rfq_ids'] == ['R1']
    client.responses.create.return_value = response({'x': 1}, output_type='text')
    assert not (await obj.extract_rfq_ids_from_message('?', 'last'))['success']
    client.responses.create.side_effect = RuntimeError('ids')
    assert not (await obj.extract_rfq_ids_from_message('?', 'last'))['success']

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'response': 'done', 'actions': [{'type': 'modify'}], 'context_understanding': {'user_intent': 'modify', 'confidence': 90}})
    assert (await obj.handle_contextual_interaction('change', {'messages': []}, {}, []))['response'] == 'done'
    client.responses.create.return_value = no_output()
    assert not (await obj.handle_contextual_interaction('?', {}, {}, []))['success']
    client.responses.create.side_effect = RuntimeError('interaction')
    assert (await obj.handle_contextual_interaction('?', {}, {}, []))['context_understanding']['user_intent'] == 'error'

    prompt = obj._build_contextual_analysis_prompt('change', {'messages': [{'role': 'user', 'content': 'old'}]}, {'workflow_type': 'rfq', 'stage': 'collecting', 'pending_rfq': {'x': 1}}, [{'description': 'bolt', 'quantity': 2, 'specifications': 'steel'}])
    assert 'CURRENT EXTRACTED ENTITIES' in prompt and 'RECENT CONVERSATION' in prompt
    assert obj._build_contextual_analysis_prompt('x', {}, {}, [])
    assert obj._clean_for_json_serialization({'d': datetime(2024, 1, 2), 'f': 2.0, 'bad': object()})['f'] == 2
    assert obj._strip_base64_from_entities({'attachments': [{'file_content': 'secret'}]})['attachments'][0]['file_content'] == '[base64_data]'

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({'rfqs': [{'items': []}], 'processing_summary': {'count': 1}, 'confidence': 70})
    assert (await obj.process_excel_to_rfqs('rows', 'book.xlsx'))['rfqs']
    client.responses.create.return_value = response({'x': 1}, output_type='text')
    assert not (await obj.process_excel_to_rfqs('rows', 'book.xlsx'))['success']
    client.responses.create.side_effect = RuntimeError('excel')
    assert not (await obj.process_excel_to_rfqs('rows', 'book.xlsx'))['success']


@pytest.mark.asyncio
async def test_entity_routing_registration_standard_and_guards(monkeypatch, entity):
    obj, api = entity
    assert obj.openai_service is api
    api.extract_entities.return_value = {'entities': {'description': 'bolt'}, 'confidence': 80, 'success': True}
    assert (await obj.extract_entities('bolt'))['entities']['description'] == 'bolt'
    obj._handle_reference_extraction = AsyncMock(return_value={'reference': True})
    ctx = {'intent_result': {'intent': 'reference_request', 'confidence': 90, 'reasoning': 'r', 'context_analysis': {'reference_details': {'reference_type': 'address', 'has_history': True}}}}
    assert await obj.extract_entities('usual', ctx) == {'reference': True}
    api.extract_entities.side_effect = RuntimeError('extract')
    assert not (await obj.extract_entities('x'))['success']
    api.extract_entities.side_effect = None
    api.extract_registration_entities.return_value = {'entities': {'email': 'a@test'}, 'confidence': 70, 'success': True, 'extracted_fields': ['email'], 'reasoning': 'clear'}
    assert (await obj._handle_registration_extraction('x', {'conversation_history': 'old', 'registration_entities': {'name': 'A'}}, 'seller_registration'))['entities']['email'] == 'a@test'
    assert (await obj._handle_registration_extraction('x', None, 'buyer_registration'))['entities']['email'] == 'a@test'
    api.extract_registration_entities.side_effect = RuntimeError('reg')
    assert not (await obj._handle_registration_extraction('x'))['success']

    api.extract_registration_entities.side_effect = None
    future = (date.today() + timedelta(days=5)).isoformat()
    api.extract_entities.return_value = {'products': [{'description': 'bolt', 'quantity': 2, 'pincode': '411005'}], 'deliveryDate': future, 'state': 'old', 'city': 'old', 'pincode': '411005', 'confidence': 88, 'success': True}
    api.validate_delivery_date.return_value = {'is_valid': True, 'normalized_date': future}
    monkeypatch.setattr(entity_module, 'get_location_from_pincode_async', AsyncMock(return_value={'city': 'Pune', 'state': 'MH'}))
    context = {'workflow_state': {'global_supplementary_fields': {'state': 'old'}, 'incomplete_products': [{'entities': {'description': 'bolt'}}]}}
    result = await obj._handle_standard_extraction('more', context)
    assert result['city'] == 'Pune' and result['state'] == 'MH'
    api.extract_entities.return_value = {'products': [], 'deliveryDate': '2030-01-01', 'city': 'Pune', 'confidence': 40}
    supplementary = await obj._handle_standard_extraction('address', {'workflow_state': {'incomplete_products': [{'description': 'bolt'}]}})
    assert supplementary['products']
    api.extract_entities.return_value = {'entities': {'description': 'x', 'deliveryDate': future}, 'confidence': 50, 'success': True}
    assert (await obj._handle_standard_extraction('legacy'))['entities']['deliveryDate'] == future
    api.validate_delivery_date.return_value = {'is_valid': False, 'user_friendly_message': 'bad'}
    assert (await obj._handle_standard_extraction('bad'))['date_validation_error']
    api.extract_entities.return_value = {'products': [{'description': 'NON_PROCURABLE', 'remarks': 'weapon'}], 'confidence': 1}
    assert (await obj._handle_standard_extraction('weapon'))['error_type'] == 'non_procurable'
    api.extract_entities.return_value = {'products': [{'description': 'bulk', 'quantity': 10000000001}], 'confidence': 1}
    assert (await obj._handle_standard_extraction('bulk'))['error_type'] == 'quantity_limit'


@pytest.mark.asyncio
async def test_entity_modification_routes_operations_and_helpers(monkeypatch, entity):
    obj, api = entity
    base = [{'entities': {'description': 'bolt', 'quantity': 2, 'city': 'Pune', 'state': 'MH', 'unitofMeasures': 'kg', 'date_validation_error': 'old'}},
            {'entities': {'description': 'nut', 'quantity': 1, 'city': 'Pune', 'state': 'MH', 'unitofMeasures': 'kg'}}]
    original = json.loads(json.dumps(base))
    out = obj._apply_modifications_to_existing_products(base, [
        {'operation_type': 'modify', 'target_product_index': 0, 'new_quantity': 5, 'delivery_date': '2030-01-01', 'project_desc': 'p', 'unit_of_measures': 'box', 'city': None},
        {'operation_type': 'modify', 'target_product_index': None, 'new_quantity': 7},
        {'operation_type': 'modify', 'target_product_index': 99, 'new_quantity': 7},
        {'operation_type': 'add', 'target_product_description': 'washer', 'new_description': 'washer', 'new_quantity': 3},
        {'operation_type': 'add', 'target_product_description': 'empty'},
        {'operation_type': 'remove', 'target_product_index': 1},
        {'operation_type': 'remove', 'target_product_index': None},
        {'operation_type': 'remove', 'target_product_index': 99},
        {'operation_type': 'unknown', 'target_product_index': 0},
    ], 'change', {'state': 'new'})
    assert len(out) == 3 and out[0]['quantity'] == 5 and out[0]['state'] == 'new' and out[1]['description'] == 'washer'
    assert 'description' not in out[2]
    empty_add = obj._apply_modifications_to_existing_products([{}], [{'operation_type': 'add', 'target_product_description': 'empty'}], 'change')
    assert empty_add == [{}]
    assert base == original
    formatted = obj._format_existing_products_for_prompt(base)
    assert 'Index 0' in formatted and 'Location: Pune, MH' in formatted
    assert obj._extract_common_fields_from_products([]) == {}
    assert obj._extract_common_fields_from_products([{'city': 'Pune'}, {'city': 'Pune'}]) == {'city': 'Pune'}
    assert obj._extract_common_fields_from_products([{'city': 'Pune'}, {'city': 'Mumbai'}]) == {}
    assert not obj._has_meaningful_modification_values([{'remarks': 'change', 'quantity': None, 'x': ' null '}], 'x')
    assert obj._has_meaningful_modification_values([{'remarks': 'change', 'quantity': '4'}], 'x')

    api.extract_entities.return_value = {'is_modification_extraction': True, 'has_new_values': True, 'modifications': [{'operation_type': 'modify', 'target_product_index': 0, 'new_quantity': 4}], 'confidence': 90}
    api.validate_delivery_date.return_value = {'is_valid': True, 'normalized_date': None}
    obj._auto_fill_location_from_pincode = AsyncMock(side_effect=lambda p: p)
    assert (await obj._handle_modification_extraction('change', {'workflow_state': {'pending_rfq': base[0]}}))['is_modification']
    api.extract_entities.return_value = {'is_modification_extraction': True, 'has_new_values': False, 'modifications': [], 'modification_intent': 'city'}
    assert (await obj._handle_modification_extraction('city', {'workflow_state': {'pending_rfq': base[0]}}))['requires_clarification']
    api.extract_entities.return_value = {'is_modification_extraction': True, 'has_new_values': False, 'modifications': [{'operation_type': 'remove', 'target_product_index': 0}]}
    assert (await obj._handle_modification_extraction('remove', {'workflow_state': {'pending_rfq': base[0]}}))['is_modification']
    api.extract_entities.return_value = {'products': [{'quantity': 4}], 'confidence': 70}
    assert (await obj._handle_modification_extraction('qty', {'workflow_state': {'pending_rfq': base[0]}}))['is_modification']
    api.extract_entities.return_value = {'products': [{'remarks': 'just intent'}], 'confidence': 70}
    assert (await obj._handle_modification_extraction('none', {'workflow_state': {'pending_rfq': base[0]}}))['requires_clarification']
    api.extract_entities.return_value = {'products': [], 'confidence': 70}
    assert (await obj._handle_modification_extraction('none', {'workflow_state': {'pending_rfq': base[0]}}))['requires_clarification']
    api.extract_entities.return_value = {'entities': {'description': 'fallback'}}
    assert (await obj._handle_modification_extraction('none', {'workflow_state': {}}))['entities']['description'] == 'fallback'

    for state_key, state_value in [('pending_combined_rfq', {'products': base}), ('pending_optional_combined_rfq', {'products': base}), ('pending_optional_rfq', base[0]), ('incomplete_products', base), ('complete_products', base), ('extracted_entities', [{'description': 'bolt'}])]:
        api.extract_entities.return_value = {'is_modification_extraction': True, 'has_new_values': False, 'modifications': []}
        result = await obj._handle_modification_extraction('x', {'workflow_state': {state_key: state_value}})
        assert result['requires_clarification']

    assert obj._merge_global_fields_into_products([{'city': 'old'}, {'city': None}], {'city': 'new', 'state': 'MH'})[0]['city'] == 'old'
    merged = obj._merge_new_extraction_with_existing_products([{'description': 'bolt', 'unitofMeasures': 'kg'}], [{'description': 'bolt', 'quantity': 2, 'unitofMeasures': 'unit(s)'}], 'x')
    assert merged[0]['quantity'] == 2 and merged[0]['unitofMeasures'] == 'kg'
    assert len(obj._merge_new_extraction_with_existing_products([{}], [{'description': 'bolt'}], 'x')) == 1
    merged = obj._merge_new_extraction_with_existing_products([{'description': 'bolt'}], [{'description': None, 'city': 'Pune'}, {'description': 'NO_PRODUCTS_MENTIONED'}], 'x')
    assert merged[0]['city'] == 'Pune'
    assert obj._apply_supplementary_data_to_existing_products([{'description': 'x', 'city': 'old'}], {'city': 'new', 'state': 'MH'})[0]['state'] == 'MH'


@pytest.mark.asyncio
async def test_entity_dates_references_summary_schema_cleaning_and_location(monkeypatch, entity):
    obj, api = entity
    today = date.today()
    future = (today + timedelta(days=3)).isoformat()
    api.validate_delivery_date.return_value = {'is_valid': True, 'normalized_date': future}
    products, error = await obj._validate_dates_in_products([{'deliveryDate': future}, {'deliveryDate': future}], 'x')
    assert not error and api.validate_delivery_date.await_count == 1
    api.validate_delivery_date.return_value = {'is_valid': True, 'normalized_date': (today - timedelta(days=1)).isoformat()}
    products, error = await obj._validate_dates_in_products([{'deliveryDate': future}], 'x')
    assert error and products[0]['deliveryDate'] is None
    api.validate_delivery_date.return_value = {'is_valid': True, 'normalized_date': 'bad'}
    assert (await obj._validate_dates_in_products([{'deliveryDate': future}], 'x'))[1]
    api.validate_delivery_date.return_value = {'is_valid': True, 'normalized_date': future, 'validation_issues': ['warning']}
    assert (await obj._validate_date_in_entity({'deliveryDate': future}, 'x'))[1]
    api.validate_delivery_date.return_value = {'is_valid': False, 'user_friendly_message': 'bad'}
    assert (await obj._validate_date_in_entity({'deliveryDate': future}, 'x'))[0]['deliveryDate'] is None
    assert (await obj._validate_date_in_entity({'description': 'x'}, 'x'))[0]['description'] == 'x'
    assert obj._is_date_future_or_today(future) and not obj._is_date_future_or_today('bad') and not obj._is_date_future_or_today(None)

    obj._handle_standard_extraction = AsyncMock(return_value={'standard': True})
    assert await obj._handle_reference_extraction('usual', {'user_context': {}}, {'reference_type': 'address'}) == {'standard': True}
    api.extract_historical_options.return_value = {'success': True, 'options': [{'city': 'Pune'}], 'summary': 'found', 'recommendations': {}}
    result = await obj._handle_reference_extraction('usual', {'user_context': {'chat_history': [1]}, 'workflow_state': {}}, {'reference_type': 'address', 'confidence': 80})
    assert result['requires_user_selection']
    api.extract_historical_options.return_value = {'success': False}
    assert await obj._handle_reference_extraction('usual', {'user_context': {'chat_history': [1]}, 'workflow_state': {}}, {'reference_type': 'address'}) == {'standard': True}

    api.extract_entities_with_summary_context.return_value = {'products': [{'description': 'x'}], 'resolved_references': [{'phrase': 'usual'}]}
    api.merge_resolved_references_with_entities.return_value = {'success': True, 'updated_products': [{'description': 'x', 'city': 'Pune'}]}
    api.validate_delivery_date.return_value = {'is_valid': True, 'normalized_date': None}
    obj._auto_fill_location_from_pincode = AsyncMock(side_effect=lambda p: p)
    summary = await obj.extract_entities_with_summary_context('usual', {'chat_summaries': [{'summary': 'old'}]})
    assert summary['products'][0]['city'] == 'Pune'
    assert await obj.extract_entities_with_summary_context('x', {}) == {'standard': True}
    api.extract_entities_with_summary_context.side_effect = RuntimeError('summary')
    assert await obj.extract_entities_with_summary_context('x', {'chat_summaries': [1]}) == {'standard': True}
    assert await obj._apply_resolved_references_intelligently([], [], 'x') == []
    api.merge_resolved_references_with_entities.return_value = {'success': False}
    products = [{'description': 'x'}]
    assert await obj._apply_resolved_references_intelligently(products, [1], 'x') == products
    api.merge_resolved_references_with_entities.side_effect = RuntimeError('merge')
    assert await obj._apply_resolved_references_intelligently(products, [1], 'x') == products

    schema = {'type': 'object'}
    monkeypatch.setattr(builtins, 'open', lambda *a, **k: _File(json.dumps(schema)))
    assert obj._get_schema('buy_something') == schema and obj._get_schema('other') == {}
    monkeypatch.setattr(builtins, 'open', lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert obj._get_schema('buy_something') == {}
    assert obj._detect_non_procurable_items([{'description': ' NON_PROCURABLE ', 'remarks': 'weapon'}, {'description': 'bolt'}]) == ['weapon']
    assert obj._check_quantity_limits([{'description': 'x', 'quantity': 'bad'}, {'description': 'x', 'quantity': '2'}]) == []
    assert obj._check_quantity_limits([{'description': 'x', 'quantity': 10000000001}])
    source = [{'description': term} for term in ['kg', 'item', '123', 'bolt', None]]
    clean = obj._clean_invalid_descriptions(source)
    assert clean[0]['description'] is None and clean[1]['description'] is None and clean[2]['description'] is None and clean[3]['description'] == 'bolt'

    obj._auto_fill_location_from_pincode = entity_module.EntityService._auto_fill_location_from_pincode.__get__(obj)
    lookup = AsyncMock(return_value={'city': 'Pune', 'state': 'MH'})
    monkeypatch.setattr(entity_module, 'get_location_from_pincode_async', lookup)
    located = await obj._auto_fill_location_from_pincode([{'pincode': '411005'}, {'pincode': '411005'}])
    assert all(x['city'] == 'Pune' for x in located) and lookup.await_count == 1
    invalid = await obj._auto_fill_location_from_pincode([{'pincode': 'bad'}])
    assert invalid[0]['pincode'] is None
    lookup.return_value = None
    assert (await obj._auto_fill_location_from_pincode([{'pincode': '411005'}]))[0]['pincode'] is None
    lookup.side_effect = RuntimeError('lookup')
    assert (await obj._auto_fill_location_from_pincode([{'pincode': '411005'}]))[0]['pincode'] == '411005'
    assert await obj._auto_fill_location_from_pincode([]) == []


@pytest.mark.asyncio
async def test_remaining_conditional_branches_and_invalid_payloads(monkeypatch, service, entity):
    obj, client, _, _ = service
    ent, api = entity

    # Exercise prompt formatting failure and all context input fallbacks.
    obj._load_prompt = openai_module.OpenAIService._load_prompt.__get__(obj)
    monkeypatch.setattr(builtins, 'open', lambda *a, **k: _File('{missing}'))
    assert obj._load_prompt('x', 'y') .startswith('Generate')
    monkeypatch.setattr(builtins, 'open', lambda *a, **k: _File('{"tool": 1}'))
    obj._load_prompt = lambda *a, **k: 'P'
    client.responses.create.return_value = response({'intent': 'general_inquiry'}, usage=False)
    await obj.classify_intent('x', {'conversation_history': {'openai_messages': []}, 'workflow_state': {'extracted_entities': []}})
    await obj.classify_intent('x', {'user_message': 'mime_type image/png'})
    client.responses.create.return_value = response({'intent': 'x'}, usage=False)
    await obj.classify_intent('x', {'workflow_state': {'extracted_entities': [{'description': 'x'}]}})

    # Empty/malformed data branches in lightweight response methods.
    client.responses.create.return_value = no_output()
    assert await obj.generate_opt_in_confirmation('A', [])
    assert await obj.generate_permission_request('A', [])
    client.responses.create.return_value = response('{bad', raw=True)
    assert not (await obj.extract_registration_entities('x'))['success']
    assert await obj.parse_confirmation_response('x') == 'unclear'

    # Usage-less success, malformed arguments, and no-choice completions.
    client.responses.create.return_value = response({'level_1_category': 'a'}, usage=False)
    assert (await obj.generate_3_level_categorization('x', []))['token_usage'] is None
    client.responses.create.return_value = response('{bad', raw=True)
    assert not (await obj.process_excel_to_rfqs('x', 'f'))['success']
    client.chat.completions.create.return_value = SimpleNamespace(choices=[])
    with pytest.raises(IndexError):
        await obj.get_completion('x')

    # Seller mapping retries all five times without sleeping in real time.
    class RateError(Exception):
        pass
    monkeypatch.setattr(openai_module, 'RateLimitError', RateError)
    monkeypatch.setattr(openai_module.asyncio, 'sleep', AsyncMock())
    client.responses.create.side_effect = RateError('rate')
    cat = [{'id': 1, 'level_1_category': 'A', 'level_2_category': 'B', 'level_3_category': 'C'}]
    assert not (await obj.map_seller_category_to_existing_learning('x', cat))['success']
    client.responses.create.side_effect = RateError('rate')
    assert not (await obj.map_seller_categories_batch(['x'], cat))['x']['success']

    # Entity routing with incomplete/empty context, all optional format fields,
    # positional merge, partial pincode response, and preserved existing errors.
    api.extract_entities.return_value = {'products': [{'description': 'bolt'}], 'confidence': 1}
    api.validate_delivery_date.return_value = {'is_valid': True, 'normalized_date': None}
    monkeypatch.setattr(entity_module, 'get_location_from_pincode_async', AsyncMock(return_value={'city': 'Pune'}))
    assert (await ent._handle_standard_extraction('x', {'workflow_state': {'incomplete_products': []}, 'extracted_entities': [{'description': 'old'}]}))['products']
    assert (await ent._handle_standard_extraction('x', {'extracted_entities': {'description': 'old'}}))['products']
    assert ent._format_existing_products_for_prompt([{'entities': {'description': 'x', 'division': 'D', 'deliveryDate': 'tomorrow', 'projectDesc': 'P', 'brand': 'B', 'city': 'C', 'state': 'S', 'pincode': '1'}}])
    merged = ent._merge_new_extraction_with_existing_products([{}, {}], [{'description': 'a'}, {'description': 'b'}], 'x')
    assert [p['description'] for p in merged] == ['a', 'b']
    merged = ent._merge_new_extraction_with_existing_products([{'description': 'a'}, {'description': 'b'}], [{'description': 'a'}, {'description': 'b'}], 'x')
    assert len(merged) == 2
    products, has_error = await ent._validate_dates_in_products([{'description': 'x', 'date_validation_error': 'old'}], 'x')
    assert not has_error and products[0]['date_validation_error'] == 'old'
    assert ent._merge_global_fields_into_products([], {'city': 'Pune'}) == []
    assert ent._apply_supplementary_data_to_existing_products([], {'city': 'Pune'}) == []
    monkeypatch.setattr(entity_module, 'get_location_from_pincode_async', AsyncMock(return_value={'state': 'MH'}))
    assert (await ent._auto_fill_location_from_pincode([{'pincode': '411005'}]))[0]['state'] == 'MH'

    # Explicit route with a reference intent lacking usable details, and
    # modification globals with all fields blank (invalid response shape).
    api.extract_entities.return_value = {'entities': {'description': 'fallback'}}
    assert (await ent.extract_entities('x', {'intent_result': {'intent': 'reference_request'}}, 'buy_something'))['entities']
    pending = {'workflow_state': {'pending_rfq': {'entities': {'description': 'x'}}}}
    api.extract_entities.return_value = {'is_modification_extraction': True, 'has_new_values': False, 'modifications': [], 'deliveryDate': '', 'state': '', 'city': '', 'pincode': ''}
    assert (await ent.extract_entities('x', pending, 'modification_request'))['requires_clarification']

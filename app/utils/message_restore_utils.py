async def restore_last_bot_message(session, whatsapp_service, user_phone, fallback_message, state_key="last_bot_message_before_cancel"):
    """Restore and send the last bot message from session state"""
    last_bot_message = session.workflow_state.get(state_key)
    
    if last_bot_message:
        if isinstance(last_bot_message, dict):
            body = last_bot_message.get("body")
            buttons = last_bot_message.get("buttons") or []
            header = last_bot_message.get("header")
            footer = last_bot_message.get("footer")

            if buttons or header or footer:
                await whatsapp_service.send_configurable_buttons(
                    recipient_id=user_phone,
                    body=body,
                    buttons_config=buttons,
                    header=header,
                    footer=footer,
                )
            else:
                await whatsapp_service.send_message(user_phone, body)
        elif isinstance(last_bot_message, str):
            await whatsapp_service.send_message(user_phone, last_bot_message)
        
        # Clear saved message after sending
        session.workflow_state.pop(state_key, None)
    else:
        await whatsapp_service.send_message(user_phone, fallback_message)
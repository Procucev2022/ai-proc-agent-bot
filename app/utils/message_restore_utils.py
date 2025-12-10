async def restore_last_bot_message(
    session,
    whatsapp_service,
    user_phone,
    fallback_message,
    state_key="last_bot_message_before_cancel"
):
    """Restore and send the last bot message from session state"""

    last_bot_message = session.workflow_state.get(state_key)
    print("last bot message:", last_bot_message)

    if last_bot_message:

        # ---- CASE 1: Saved message is a structured dict ----
        if isinstance(last_bot_message, dict):

            body_obj = last_bot_message.get("body") or {}
            body = body_obj.get("text") if isinstance(body_obj, dict) else body_obj
            
            header_obj = last_bot_message.get("header") or {}
            header = header_obj.get("text") if isinstance(header_obj, dict) else header_obj
            
            footer_obj = last_bot_message.get("footer") or {}
            footer = footer_obj.get("text") if isinstance(footer_obj, dict) else footer_obj

            # FIX: Read buttons from action.buttons if present
            action = last_bot_message.get("action") or {}
            buttons = action.get("buttons") or []

            # Convert WhatsApp API format to send_configurable_buttons format
            buttons_config = []
            for btn in buttons:
                if isinstance(btn, dict):
                    # Handle nested reply format: {"type": "reply", "reply": {"id": "...", "title": "..."}}
                    if "reply" in btn:
                        buttons_config.append({
                            "id": btn["reply"].get("id"),
                            "title": btn["reply"].get("title")
                        })
                    # Handle direct format: {"id": "...", "title": "..."}
                    elif "title" in btn:
                        buttons_config.append(btn)

            # WhatsApp requires at least ONE button if action exists
            if buttons_config:
                await whatsapp_service.send_configurable_buttons(
                    recipient_id=user_phone,
                    body=body,
                    buttons_config=buttons_config,
                    header=header,
                    footer=footer,
                )
            else:
                # No buttons → send simple text
                await whatsapp_service.send_message(user_phone, body)

        # ---- CASE 2: Saved message is plain text ----
        elif isinstance(last_bot_message, str):
            await whatsapp_service.send_message(user_phone, last_bot_message)

        # Clear saved state
        session.workflow_state.pop(state_key, None)

    else:
        await whatsapp_service.send_message(user_phone, fallback_message)

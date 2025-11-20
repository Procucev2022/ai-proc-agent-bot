"""
User Cache Service.

Centralized service for caching user data retrieved by phone number.
This cache is shared across authentication, intent switching, and other services.
Cache expires with the auth token to maintain data consistency.
"""

import logging
from typing import Dict, Any, Optional, List
from datetime import datetime
from app.redis_db import get_redis_service
from app.schemas.user import APIUserSchema

logger = logging.getLogger(__name__)


class UserCacheService:
    """Centralized service for caching user data."""

    def __init__(self):
        self.redis_service = get_redis_service()

    async def store_user_data(self, phone_number: str, user_data: List[Dict], expiry_seconds: int = 43200) -> bool:
        """
        Store user data in Redis cache.
        Preserves meaningful message if it exists.

        Args:
            phone_number: User's phone number (cache key)
            user_data: List of user dictionaries from API response
            expiry_seconds: Cache expiry time (defaults to 12 hours)
        """
        try:
            cache_key = self._get_cache_key(phone_number)

            # Get existing cache to preserve meaningful message
            existing_cache = await self.redis_service.get(cache_key, as_json=True)

            # Prepare cache data with metadata
            cache_data = {
                "user_data": user_data,
                "cached_at": datetime.now().isoformat(),
                "phone_number": phone_number,
                "count": len(user_data)
            }

            # Preserve meaningful message from existing cache if present
            if existing_cache and isinstance(existing_cache, dict):
                if "meaningful_message" in existing_cache:
                    cache_data["meaningful_message"] = existing_cache["meaningful_message"]
                    logger.info(f"Preserving meaningful message in store_user_data: '{existing_cache['meaningful_message'][:50]}...'")
                if "meaningful_intent_result" in existing_cache:
                    cache_data["meaningful_intent_result"] = existing_cache["meaningful_intent_result"]
                if "meaningful_message_cached_at" in existing_cache:
                    cache_data["meaningful_message_cached_at"] = existing_cache["meaningful_message_cached_at"]
            else:
                logger.info(f"No existing cache found or no meaningful message to preserve in store_user_data")

            success = await self.redis_service.set(cache_key, cache_data, ex=expiry_seconds)

            if success:
                logger.info(f"Cached user data for {phone_number} with {len(user_data)} records (expires in {expiry_seconds}s)")
            else:
                logger.error(f"Failed to cache user data for {phone_number}")

            return success

        except Exception as e:
            logger.error(f"Error storing user data cache for {phone_number}: {e}")
            return False

    async def get_user_data(self, phone_number: str) -> Optional[List[Dict]]:
        """
        Retrieve cached user data by phone number.

        Args:
            phone_number: User's phone number

        Returns:
            List of user dictionaries if found, None if not cached
        """
        try:
            cache_key = self._get_cache_key(phone_number)
            cache_data = await self.redis_service.get(cache_key, as_json=True)

            if cache_data and isinstance(cache_data, dict):
                user_data = cache_data.get("user_data")
                if user_data:
                    logger.info(f"Retrieved cached user data for {phone_number} with {len(user_data)} records")
                    return user_data

            logger.info(f"No cached user data found for {phone_number}")
            return None

        except Exception as e:
            logger.error(f"Error retrieving user data cache for {phone_number}: {e}")
            return None

    async def get_filtered_user_data(self, phone_number: str, intent: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve cached user data and filter by intent.

        Args:
            phone_number: User's phone number
            intent: Intent to filter by (buy_something, sell_something)

        Returns:
            Filtered user data dict with users and emails, or None if not cached
        """
        try:
            cached_data = await self.get_user_data(phone_number)
            if not cached_data:
                return None

            # Apply intent filtering
            filtered_result = self._filter_users_by_intent(cached_data, intent)

            if filtered_result.get("success"):
                logger.info(f"Filtered cached data for {phone_number} by intent '{intent}': {filtered_result['count']} users")
                return filtered_result
            else:
                logger.warning(f"Intent filtering failed for cached data: {filtered_result.get('message')}")
                return None

        except Exception as e:
            logger.error(f"Error filtering cached user data for {phone_number}: {e}")
            return None

    async def clear_user_data(self, phone_number: str, preserve_meaningful_message: bool = True) -> bool:
        """
        Clear cached user data for a phone number.

        Args:
            phone_number: User's phone number
            preserve_meaningful_message: If True, preserves meaningful message for post-auth processing.
                                        If False (e.g., on exit), completely clears everything.
        """
        try:
            cache_key = self._get_cache_key(phone_number)

            # Get existing cache to preserve meaningful message if requested
            cache_data = await self.redis_service.get(cache_key, as_json=True)

            if cache_data and isinstance(cache_data, dict):
                # Preserve meaningful message fields only if requested
                meaningful_msg = cache_data.get("meaningful_message") if preserve_meaningful_message else None
                meaningful_intent = cache_data.get("meaningful_intent_result") if preserve_meaningful_message else None
                meaningful_cached_at = cache_data.get("meaningful_message_cached_at") if preserve_meaningful_message else None

                # Delete the cache
                await self.redis_service.delete(cache_key)
                logger.info(f"Cleared cached user data for {phone_number}")

                # Restore meaningful message if it existed and preservation is requested
                if preserve_meaningful_message and meaningful_msg and meaningful_intent:
                    new_cache = {
                        "phone_number": phone_number,
                        "meaningful_message": meaningful_msg,
                        "meaningful_intent_result": meaningful_intent,
                        "meaningful_message_cached_at": meaningful_cached_at,
                        "cached_at": datetime.now().isoformat()
                    }
                    await self.redis_service.set(cache_key, new_cache, ex=43200)
                    logger.info(f"Preserved meaningful message after clearing user data for {phone_number}")
                else:
                    if not preserve_meaningful_message:
                        logger.info(f"Completely cleared all user data including meaningful message for {phone_number}")

                return True
            else:
                logger.warning(f"No cached user data found to clear for {phone_number}")
                return False

        except Exception as e:
            logger.error(f"Error clearing user data cache for {phone_number}: {e}")
            return False

    async def is_data_cached(self, phone_number: str) -> bool:
        """
        Check if user data is cached for a phone number.

        Args:
            phone_number: User's phone number
        """
        try:
            cache_key = self._get_cache_key(phone_number)
            return await self.redis_service.exists(cache_key)
        except Exception as e:
            logger.error(f"Error checking cache existence for {phone_number}: {e}")
            return False

    async def refresh_cache_expiry(self, phone_number: str, expiry_seconds: int = 43200) -> bool:
        """
        Refresh cache expiry time (extend TTL).

        Args:
            phone_number: User's phone number
            expiry_seconds: New expiry time in seconds
        """
        try:
            cache_key = self._get_cache_key(phone_number)
            success = await self.redis_service.expire(cache_key, expiry_seconds)

            if success:
                logger.info(f"Refreshed cache expiry for {phone_number} to {expiry_seconds}s")

            return success

        except Exception as e:
            logger.error(f"Error refreshing cache expiry for {phone_number}: {e}")
            return False

    def _get_cache_key(self, phone_number: str) -> str:
        """Generate Redis cache key for user data."""
        # Normalize phone number for consistent key generation
        normalized_phone = phone_number.lstrip('+').replace(' ', '').replace('-', '')
        logger.info(f"normalie phone:{normalized_phone}")
        return f"user_cache:{normalized_phone}"

    async def store_meaningful_message(self, phone_number: str, message: str, intent_result: Dict[str, Any]) -> bool:
        """
        Store meaningful message in cache for post-auth/registration processing.

        Args:
            phone_number: User's phone number
            message: The meaningful message to preserve
            intent_result: The intent classification result
        """
        try:
            cache_key = self._get_cache_key(phone_number)

            # Get existing cache data or create new
            cache_data = await self.redis_service.get(cache_key, as_json=True)
            if not cache_data:
                cache_data = {
                    "phone_number": phone_number,
                    "cached_at": datetime.now().isoformat()
                }

            # Add meaningful message fields
            cache_data["meaningful_message"] = message
            cache_data["meaningful_intent_result"] = intent_result
            cache_data["meaningful_message_cached_at"] = datetime.now().isoformat()

            # Store with same expiry as user data (12 hours)
            success = await self.redis_service.set(cache_key, cache_data, ex=43200)

            if success:
                logger.info(f"Cached meaningful message for {phone_number}: '{str(message)[:50]}...'")
            else:
                logger.error(f"Failed to cache meaningful message for {phone_number}")

            return success

        except Exception as e:
            logger.error(f"Error storing meaningful message for {phone_number}: {e}")
            return False

    async def get_meaningful_message(self, phone_number: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve cached meaningful message.

        Args:
            phone_number: User's phone number

        Returns:
            Dict with 'message' and 'intent_result' if found, None otherwise
        """
        try:
            cache_key = self._get_cache_key(phone_number)
            cache_data = await self.redis_service.get(cache_key, as_json=True)

            if cache_data and isinstance(cache_data, dict):
                message = cache_data.get("meaningful_message")
                intent_result = cache_data.get("meaningful_intent_result")

                if message and intent_result:
                    logger.info(f"Retrieved cached meaningful message for {phone_number}: '{str(message)[:50]}...'")
                    return {
                        "message": message,
                        "intent_result": intent_result
                    }

            logger.info(f"No cached meaningful message found for {phone_number}")
            return None

        except Exception as e:
            logger.error(f"Error retrieving meaningful message for {phone_number}: {e}")
            return None

    async def clear_meaningful_message(self, phone_number: str) -> bool:
        """
        Clear meaningful message from cache (keeps other cache data intact).

        Args:
            phone_number: User's phone number
        """
        try:
            cache_key = self._get_cache_key(phone_number)
            cache_data = await self.redis_service.get(cache_key, as_json=True)

            if cache_data and isinstance(cache_data, dict):
                # Remove meaningful message fields
                cache_data.pop("meaningful_message", None)
                cache_data.pop("meaningful_intent_result", None)
                cache_data.pop("meaningful_message_cached_at", None)

                # Update cache
                success = await self.redis_service.set(cache_key, cache_data, ex=43200)

                if success:
                    logger.info(f"Cleared meaningful message from cache for {phone_number}")

                return success

            return True  # Nothing to clear

        except Exception as e:
            logger.error(f"Error clearing meaningful message for {phone_number}: {e}")
            return False

    async def get_account_options_for_intent_switch(self, phone_number: str, target_intent: str,
                                                   current_user_email: str = None) -> Optional[Dict[str, Any]]:
        """
        Get formatted account options for intent switching with actual account details.

        Args:
            phone_number: User's phone number
            target_intent: Target intent (buy_something, sell_something)
            current_user_email: Current user's email to exclude from options

        Returns:
            Dict with account options, formatted message, and metadata
        """
        try:
            cached_data = await self.get_user_data(phone_number)
            if not cached_data:
                logger.info(f"No cached data for intent switch: {phone_number}")
                return None

            # Filter by target intent
            filtered_result = self._filter_users_by_intent(cached_data, target_intent)

            if not filtered_result.get("success"):
                logger.info(f"No accounts found for target intent '{target_intent}'")
                # Still provide register and continue options even with no target accounts
                target_role = "seller" if target_intent == "sell_something" else "buyer"
                formatted_options = [
                    {
                        "number": 1,
                        "text": f"1. Register new {target_role} account",
                        "action": "register_new",
                        "target_role": target_role
                    },
                    {
                        "number": 2,
                        "text": f"2. Continue with current account",
                        "action": "continue_current"
                    }
                ]

                return {
                    "success": True,
                    "has_target_accounts": False,
                    "target_accounts": [],
                    "formatted_options": formatted_options,
                    "message_type": "no_target_accounts",
                    "target_intent": target_intent,
                    "target_role": target_role
                }

            target_accounts = filtered_result["filtered_users"]

            # Exclude current user account if provided
            if current_user_email:
                target_accounts = [
                    account for account in target_accounts
                    if account.get("email") != current_user_email and
                       account.get("username") != current_user_email
                ]

            # Format account options
            formatted_options = []
            for i, account in enumerate(target_accounts, 1):
                email = account.get("email") or account.get("username", "Unknown")
                company = account.get("companyName", "")
                self_client_value = account.get("selfClient")
                user_type = "Buyer" if self_client_value else "Seller"

                logger.info(f"Account formatting debug: email={email}, selfClient={self_client_value}, user_type={user_type}")

                option_text = f"{i}. {email}"
                if company:
                    option_text += f" ({user_type} - {company})"
                else:
                    option_text += f" ({user_type})"

                formatted_options.append({
                    "number": i,
                    "text": option_text,
                    "email": email,
                    "account_data": account
                })

            # Add standard options
            register_option_num = len(formatted_options) + 1
            continue_option_num = register_option_num + 1

            target_role = "seller" if target_intent == "sell_something" else "buyer"

            formatted_options.append({
                "number": register_option_num,
                "text": f"{register_option_num}. Register new {target_role} account",
                "action": "register_new",
                "target_role": target_role
            })

            formatted_options.append({
                "number": continue_option_num,
                "text": f"{continue_option_num}. Continue with current account",
                "action": "continue_current"
            })

            return {
                "success": True,
                "has_target_accounts": len(target_accounts) > 0,
                "target_accounts": target_accounts,
                "formatted_options": formatted_options,
                "message_type": "with_accounts" if target_accounts else "no_target_accounts",
                "target_intent": target_intent,
                "target_role": target_role
            }

        except Exception as e:
            logger.error(f"Error getting account options for intent switch: {e}")
            return None

    def _filter_users_by_intent(self, raw_users: List[Dict], intent: str) -> Dict[str, Any]:
        """Filter users based on intent and return structured data."""
        try:
            from app.schemas.user import APIUserSchema, User

            logger.info(f"Filtering {len(raw_users)} cached users by intent '{intent}'")

            # Normalize using schema
            users: List[APIUserSchema] = [APIUserSchema(**user) for user in raw_users]

            # Apply intent filtering
            if intent == "buy_something":
                filtered_users = [user for user in users if user.selfClient is True]
            elif intent == "sell_something":
                filtered_users = [user for user in users if user.selfClient is False]
            else:
                filtered_users = users

            if not filtered_users:
                logger.info(f"No matching users found for intent '{intent}'")
                return {"success": False, "message": "No matching users found"}

            # Extract unique emails
            unique_emails = []
            for user in filtered_users:
                if user.username and user.username not in unique_emails:
                    unique_emails.append(user.username)

            # Convert to dict format (keep raw dict to preserve selfClient field)
            user_details_list = []
            for user in filtered_users:
                user_dict = user.dict()
                user_details_list.append(user_dict)

            logger.info(f"Intent filtering success: {len(filtered_users)} users, {len(unique_emails)} unique emails")

            return {
                "success": True,
                "filtered_users": user_details_list,
                "unique_emails": unique_emails,
                "count": len(filtered_users)
            }

        except Exception as e:
            logger.error(f"User filtering error: {e}")
            return {"success": False, "message": str(e)}


# Singleton instance
_user_cache_service: Optional[UserCacheService] = None


def get_user_cache_service() -> UserCacheService:
    """Get UserCacheService singleton instance."""
    global _user_cache_service
    if _user_cache_service is None:
        _user_cache_service = UserCacheService()
    return _user_cache_service
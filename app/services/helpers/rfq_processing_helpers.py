from typing import Dict, Any, List
import logging

logger = logging.getLogger(__name__)


async def run_auto_categorization_for_rfqs(rfq_results: List[Dict[str, Any]], auto_categorization_service, enhanced_auto_categorization_service=None) -> str:
    """
    Run auto-categorization for successfully submitted RFQs.
    
    This is an offline process that runs after RFQ submission to categorize
    items using vector search and AI, without impacting user experience.
    
    Args:
        rfq_results: List of RFQ submission results with success status and RFQ IDs
        auto_categorization_service: Instance of the auto categorization service
        
    Returns:
        User-friendly message about categorization results
    """
    try:
        logger.info(f"Starting auto-categorization for {len(rfq_results)} RFQ results")
        
        categorization_summary = []
        total_items_processed = 0
        successful_categorizations = 0
        
        for rfq_result in rfq_results:
            if not rfq_result.get("success"):
                continue
                
            rfq_id = rfq_result.get("rfq_id")
            if not rfq_id:
                logger.warning("RFQ result missing rfq_id, skipping auto-categorization")
                continue
            
            # Get RFQ data to extract item descriptions
            rfq_data = rfq_result.get("rfq_data", {})
            items = rfq_data.get("items", [])
            
            if not items:
                logger.warning(f"No items found in RFQ {rfq_id}, skipping auto-categorization")
                continue
            
            # Process each item in the RFQ
            for item in items:
                item_description = item.get("description", "")
                if not item_description:
                    logger.warning(f"Empty item description in RFQ {rfq_id}, skipping")
                    continue
                
                total_items_processed += 1
                logger.info(f"Auto-categorizing item: '{item_description}' for RFQ {rfq_id}")
                
                # Try enhanced categorization first, then fallback
                categorization_result = None
                
                if enhanced_auto_categorization_service:
                    try:
                        logger.info(f"Trying enhanced auto-categorization for '{item_description}'")
                        categorization_result = await enhanced_auto_categorization_service.categorize_item(
                            item_description=item_description,
                            user_id="system_auto_categorization",
                            session_id=f"rfq_{rfq_id}",
                            rfq_id=rfq_id
                        )
                        
                        if categorization_result.get("success"):
                            logger.info(f"Enhanced categorization successful: {categorization_result.get('method', 'unknown')}")
                        else:
                            logger.warning(f"Enhanced categorization failed: {categorization_result.get('error', 'Unknown error')}")
                            categorization_result = None
                    except Exception as e:
                        logger.error(f"Enhanced categorization service error: {str(e)}")
                        categorization_result = None
                
                # Fallback to traditional categorization if enhanced failed or unavailable
                if not categorization_result or not categorization_result.get("success"):
                    logger.info(f"Using fallback auto-categorization for '{item_description}'")
                    categorization_result = auto_categorization_service.categorize_item(
                        item_description=item_description,
                        user_id="system_auto_categorization",
                        session_id=f"rfq_{rfq_id}",
                        rfq_id=rfq_id
                    )
                
                if categorization_result.get("success"):
                    # Handle different categorization result formats
                    category = None
                    confidence = 0
                    
                    # Enhanced vector format (primary path)
                    if categorization_result.get("client_category"):
                        category = categorization_result.get("client_category")
                        confidence = categorization_result.get("confidence_score", 0)
                    
                    # Regular auto-categorization format
                    elif categorization_result.get("category"):
                        category = categorization_result.get("category")
                        confidence = categorization_result.get("confidence_score", 0)
                    
                    # Enhanced fallback format (categorize_with_learning)
                    elif categorization_result.get("auto_categorization"):
                        auto_cat = categorization_result.get("auto_categorization", {})
                        category = auto_cat.get("category")
                        confidence = auto_cat.get("confidence_score", 0)
                    successful_categorizations += 1
                    
                    # Add to summary for user
                    categorization_summary.append(f"• {item_description[:50]}{'...' if len(item_description) > 50 else ''} → {category}")
                    
                    logger.info(f"Successfully categorized '{item_description}' as {category} (confidence: {confidence:.2f})")
                else:
                    error_msg = categorization_result.get("error", "Unknown error")
                    logger.error(f"Failed to categorize '{item_description}': {error_msg}")
            
            logger.info(f"Completed auto-categorization for RFQ {rfq_id}")
        
        logger.info("Auto-categorization process completed for all RFQs")
        
        # Generate user-friendly summary message
        if total_items_processed == 0:
            return "Auto-categorization completed, but no items were found to categorize."
        elif successful_categorizations == 0:
            return f"Auto-categorization completed for {total_items_processed} items, but categorization failed. Manual review may be needed."
        elif successful_categorizations == total_items_processed:
            summary_text = "\n".join(categorization_summary)
            return f"Auto-categorization completed successfully!\n\nCategorized items:\n{summary_text}"
        else:
            summary_text = "\n".join(categorization_summary)
            return f"Auto-categorization completed: {successful_categorizations}/{total_items_processed} items categorized successfully.\n\nSuccessfully categorized:\n{summary_text}"
        
    except Exception as e:
        logger.error(f"Error in auto-categorization process: {str(e)}")
        return "Auto-categorization encountered an error. Your RFQs have been created successfully, but manual categorization may be needed."


async def run_seller_recommendation_for_rfqs(rfq_results: List[Dict[str, Any]], seller_recommendation_service, enhanced_seller_matching_service=None) -> str:
    """
    Run seller recommendation for successfully submitted RFQs.
    
    This matches sellers to RFQs and sends WhatsApp notifications to selected sellers,
    providing the user with a summary of which sellers were notified.
    
    Args:
        rfq_results: List of RFQ submission results with success status and RFQ IDs
        seller_recommendation_service: Instance of the seller recommendation service
        
    Returns:
        User-friendly message about seller matching and notification results
    """
    try:
        logger.info(f"Starting seller recommendation for {len(rfq_results)} RFQ results")
        
        total_rfqs_processed = 0
        total_sellers_selected = 0
        notification_summary = []
        
        for rfq_result in rfq_results:
            if not rfq_result.get("success"):
                continue
                
            rfq_id = rfq_result.get("rfq_id")
            if not rfq_id:
                logger.warning("RFQ result missing rfq_id, skipping seller recommendation")
                continue
            
            # Get RFQ data for seller matching
            rfq_data = rfq_result.get("rfq_data", {})
            if not rfq_data:
                logger.warning(f"No RFQ data found for {rfq_id}, skipping seller recommendation")
                continue
            
            # Extract categories from RFQ items
            items = rfq_data.get("items", [])
            categories = []
            for item in items:
                if item.get("division"):
                    categories.append(item.get("division"))
                if item.get("description"):
                    # Simple category extraction from description
                    desc = item.get("description", "").lower()
                    if "medical" in desc or "healthcare" in desc:
                        categories.append("Medical Equipment")
                    elif "it" in desc or "computer" in desc or "laptop" in desc:
                        categories.append("IT Hardware")
                    elif "office" in desc:
                        categories.append("Office Equipment")
            
            # Fallback to general category if none found
            if not categories:
                categories = ["General Equipment"]
            
            # Create RFQ matching data
            rfq_matching_data = {
                "rfq_id": rfq_id,
                "rfq_title": f"RFQ {rfq_id}",
                "categories": list(set(categories)),  # Remove duplicates
                "delivery_location": {
                    "state": "Karnataka",  # Default fallback
                    "city": "Bangalore",
                    "pincode": "560001"
                },
                "quantity_info": f"{len(items)} items",
                "division": categories[0] if categories else "General"
            }
            
            total_rfqs_processed += 1
            logger.info(f"Matching sellers for RFQ {rfq_id} with categories: {categories}")
            
            # Try enhanced seller matching first, then fallback
            seller_result = None
            
            if enhanced_seller_matching_service:
                try:
                    logger.info(f"Trying enhanced seller matching for RFQ {rfq_id}")
                    # Use enhanced service for each item description
                    enhanced_sellers = []
                    for item in items:
                        item_description = item.get("description", "")
                        if item_description:
                            enhanced_result = await enhanced_seller_matching_service.find_sellers_for_item(
                                item_description=item_description,
                                delivery_location=rfq_matching_data.get("delivery_location"),
                                max_distance_km=200,
                                max_sellers=5
                            )
                            
                            if enhanced_result.get("success") and enhanced_result.get("sellers"):
                                enhanced_sellers.extend(enhanced_result["sellers"])
                    
                    if enhanced_sellers:
                        # Convert enhanced seller format to traditional format
                        seller_result = {
                            "total_selected": len(enhanced_sellers),
                            "subscribed_sellers": [s for s in enhanced_sellers if s.get("subscription_credits", 0) > 0],
                            "unsubscribed_sellers": [s for s in enhanced_sellers if s.get("subscription_credits", 0) == 0]
                        }
                        logger.info(f"Enhanced seller matching found {len(enhanced_sellers)} sellers")
                    else:
                        logger.warning("Enhanced seller matching found no sellers, will use fallback")
                        seller_result = None
                        
                except Exception as e:
                    logger.error(f"Enhanced seller matching service error: {str(e)}")
                    seller_result = None
            
            # Fallback to traditional seller recommendation if enhanced failed or unavailable
            if not seller_result:
                logger.info(f"Using fallback seller recommendation for RFQ {rfq_id}")
                seller_result = await seller_recommendation_service.select_sellers_for_rfq(rfq_matching_data)
            
            # TODO: Integrate with RFQBackgroundService to send actual WhatsApp notifications
            # For testing phase, just show selection results
            
            if seller_result.get("total_selected", 0) > 0:
                selected_count = seller_result["total_selected"]
                subscribed_count = len(seller_result.get("subscribed_sellers", []))
                unsubscribed_count = len(seller_result.get("unsubscribed_sellers", []))
                
                total_sellers_selected += selected_count
                
                # Get top seller names for summary
                all_sellers = seller_result.get("subscribed_sellers", []) + seller_result.get("unsubscribed_sellers", [])
                top_sellers = [seller["seller_name"] for seller in all_sellers[:3]]
                
                notification_summary.append(
                    f"📋 {rfq_id}: {selected_count} sellers notified ({subscribed_count} subscribed, {unsubscribed_count} unsubscribed)"
                )
                
                if top_sellers:
                    notification_summary.append(f"   Top sellers: {', '.join(top_sellers)}")
                
                logger.info(f"Selected {selected_count} sellers for RFQ {rfq_id}")
            else:
                notification_summary.append(f"📋 {rfq_id}: No matching sellers found")
                logger.warning(f"No sellers found for RFQ {rfq_id}")
        
        logger.info(f"Seller recommendation completed: {total_sellers_selected} sellers selected for {total_rfqs_processed} RFQs")
        
        # Generate user-friendly summary message
        if total_rfqs_processed == 0:
            return "Seller matching completed, but no RFQs were found to process."
        elif total_sellers_selected == 0:
            return f"Seller matching completed for {total_rfqs_processed} RFQ(s), but no matching sellers were found."
        else:
            summary_text = "\n".join(notification_summary)
            return f"🎯 Seller Matching Results:\n\n{summary_text}\n\nTotal: {total_sellers_selected} sellers have been notified about your RFQ(s) via WhatsApp!"
            
    except Exception as e:
        logger.error(f"Error in seller recommendation process: {str(e)}")
        return "Seller matching encountered an error. Your RFQs have been created successfully, but sellers may need to be notified manually."
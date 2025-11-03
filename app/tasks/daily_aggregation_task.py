"""
Daily aggregation background task.

This task runs the daily aggregation service to calculate B2B WhatsApp metrics
including buyer summary, seller summary, and category summary metrics.
"""

import logging
import os
import pandas as pd
from typing import Dict, Any
from celery import shared_task
from datetime import datetime, date, timedelta

from app.services.daily_aggregation_service import DailyAggregationService
from app.database import get_db_session
from app.models import DailyAggregatedMetrics, RollingWindowMetrics
from app.config import get_settings

logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 300})
def run_daily_aggregation(self, target_date: str = None):
    """
    Run daily aggregation for B2B WhatsApp metrics.
    
    Args:
        target_date: Date string in YYYY-MM-DD format. If None, uses yesterday.
        
    Returns:
        Dict with task execution results
    """
    try:
        settings = get_settings()
        logger.info("Starting daily aggregation task")
        
        # Parse target date
        if target_date:
            try:
                parsed_date = datetime.strptime(target_date, '%Y-%m-%d').date()
            except ValueError as e:
                logger.error(f"Invalid date format '{target_date}': {e}")
                return {
                    "status": "failed",
                    "error": f"Invalid date format '{target_date}'. Use YYYY-MM-DD",
                    "timestamp": datetime.utcnow().isoformat()
                }
        else:
            # Default to yesterday
            parsed_date = date.today() - timedelta(days=1)

        logger.info(f"Running daily aggregation for date: {parsed_date}")
        
        # Initialize and run daily aggregation service
        aggregation_service = DailyAggregationService()
        success = aggregation_service.run_daily_aggregation(parsed_date)
        
        if success:
            # Export metrics to Excel file
            excel_file = _export_daily_metrics_to_excel(parsed_date)
            
            logger.info(f"Daily aggregation completed successfully for {parsed_date}")
            return {
                "status": "completed",
                "target_date": str(parsed_date),
                "timestamp": datetime.utcnow().isoformat(),
                "message": f"Daily aggregation completed for {parsed_date}",
                "excel_file": excel_file
            }
        else:
            logger.error(f"Daily aggregation failed for {parsed_date}")
            return {
                "status": "failed",
                "target_date": str(parsed_date),
                "error": "Daily aggregation service returned False",
                "timestamp": datetime.utcnow().isoformat()
            }

    except Exception as e:
        logger.error(f"Daily aggregation task failed: {e}")
        # Retry with exponential backoff
        raise self.retry(countdown=300)


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 2, 'countdown': 180})
def run_daily_aggregation_range(self, start_date: str, end_date: str):
    """
    Run daily aggregation for a range of dates.
    
    Args:
        start_date: Start date string in YYYY-MM-DD format
        end_date: End date string in YYYY-MM-DD format
        
    Returns:
        Dict with task execution results
    """
    try:
        logger.info(f"Starting daily aggregation for date range: {start_date} to {end_date}")
        
        # Parse dates
        try:
            start_parsed = datetime.strptime(start_date, '%Y-%m-%d').date()
            end_parsed = datetime.strptime(end_date, '%Y-%m-%d').date()
        except ValueError as e:
            logger.error(f"Invalid date format: {e}")
            return {
                "status": "failed",
                "error": f"Invalid date format. Use YYYY-MM-DD",
                "timestamp": datetime.utcnow().isoformat()
            }
        
        if start_parsed > end_parsed:
            logger.error("Start date must be before or equal to end date")
            return {
                "status": "failed",
                "error": "Start date must be before or equal to end date",
                "timestamp": datetime.utcnow().isoformat()
            }
        
        # Initialize aggregation service
        aggregation_service = DailyAggregationService()
        
        # Process each date
        current_date = start_parsed
        processed_dates = []
        failed_dates = []
        
        while current_date <= end_parsed:
            try:
                logger.info(f"Processing daily aggregation for {current_date}")
                success = aggregation_service.run_daily_aggregation(current_date)
                
                if success:
                    processed_dates.append(str(current_date))
                    logger.info(f"Daily aggregation completed for {current_date}")
                else:
                    failed_dates.append(str(current_date))
                    logger.error(f"Daily aggregation failed for {current_date}")
                    
            except Exception as e:
                logger.error(f"Error processing date {current_date}: {e}")
                failed_dates.append(str(current_date))
            
            current_date += timedelta(days=1)
        
        logger.info(f"Date range aggregation completed. Processed: {len(processed_dates)}, Failed: {len(failed_dates)}")
        
        return {
            "status": "completed" if not failed_dates else "partial_failure",
            "start_date": start_date,
            "end_date": end_date,
            "processed_dates": processed_dates,
            "failed_dates": failed_dates,
            "total_processed": len(processed_dates),
            "total_failed": len(failed_dates),
            "timestamp": datetime.utcnow().isoformat()
        }

    except Exception as e:
        logger.error(f"Daily aggregation range task failed: {e}")
        raise self.retry(countdown=180)


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 2, 'countdown': 120})
def update_rolling_windows(self, target_date: str = None):
    """
    Update rolling window metrics (7/30/90 day windows).
    
    Args:
        target_date: End date string in YYYY-MM-DD format. If None, uses today.
        
    Returns:
        Dict with task execution results
    """
    try:
        logger.info("Starting rolling windows update task")
        
        # Parse target date
        if target_date:
            try:
                parsed_date = datetime.strptime(target_date, '%Y-%m-%d').date()
            except ValueError as e:
                logger.error(f"Invalid date format '{target_date}': {e}")
                return {
                    "status": "failed",
                    "error": f"Invalid date format '{target_date}'. Use YYYY-MM-DD",
                    "timestamp": datetime.utcnow().isoformat()
                }
        else:
            # Default to today
            parsed_date = date.today()

        logger.info(f"Updating rolling windows for end date: {parsed_date}")
        
        # Initialize aggregation service
        aggregation_service = DailyAggregationService()
        
        # Update rolling windows
        aggregation_service._update_rolling_windows(parsed_date)
        
        logger.info(f"Rolling windows updated successfully for {parsed_date}")
        return {
            "status": "completed",
            "target_date": str(parsed_date),
            "timestamp": datetime.utcnow().isoformat(),
            "message": f"Rolling windows updated for {parsed_date}"
        }

    except Exception as e:
        logger.error(f"Rolling windows update task failed: {e}")
        raise self.retry(countdown=120)


def _export_daily_metrics_to_excel(target_date: date) -> str:
    """
    Export daily metrics to Excel file in daily_metrics folder.
    
    Args:
        target_date: Date to export metrics for
        
    Returns:
        Path to the created Excel file, or None if failed
    """
    try:
        # Create daily_metrics folder if it doesn't exist
        metrics_dir = os.path.join(os.getcwd(), "daily_metrics")
        os.makedirs(metrics_dir, exist_ok=True)
        
        # Generate filename
        filename = f"daily_metrics_{target_date.strftime('%Y-%m-%d')}.xlsx"
        output_file = os.path.join(metrics_dir, filename)
        
        logger.info(f"Exporting daily metrics to: {output_file}")
        
        with get_db_session() as db:
            # Query metrics for the specific date
            metrics = db.query(DailyAggregatedMetrics)\
                .filter(DailyAggregatedMetrics.date == target_date)\
                .all()
            
            # Query rolling window metrics for context
            rolling_metrics = db.query(RollingWindowMetrics)\
                .filter(RollingWindowMetrics.end_date == target_date)\
                .all()
            
            if not metrics and not rolling_metrics:
                logger.warning(f"No metrics found for date {target_date}")
                return None
            
            # Organize data by metric type
            buyer_data = []
            seller_data = []
            category_data = []
            
            for metric in metrics:
                base_row = {
                    'date': metric.date,
                    'created_at': metric.created_at,
                    'is_complete': metric.is_complete
                }
                
                if metric.metric_type == 'buyer_summary':
                    row = {**base_row, **metric.metric_data}
                    buyer_data.append(row)
                elif metric.metric_type == 'seller_summary':
                    row = {**base_row, **metric.metric_data}
                    seller_data.append(row)
                elif metric.metric_type == 'category_summary':
                    # Category data is nested, so flatten it
                    for category, cat_metrics in metric.metric_data.items():
                        cat_row = {
                            **base_row,
                            'category': category,
                            **cat_metrics
                        }
                        category_data.append(cat_row)
            
            # Create Excel file with multiple sheets
            with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
                
                # Buyer Metrics Sheet
                if buyer_data:
                    buyer_df = pd.DataFrame(buyer_data)
                    buyer_df.to_excel(writer, sheet_name='Buyer Metrics', index=False)
                    logger.info(f"Exported {len(buyer_data)} buyer metric records")
                
                # Seller Metrics Sheet
                if seller_data:
                    seller_df = pd.DataFrame(seller_data)
                    seller_df.to_excel(writer, sheet_name='Seller Metrics', index=False)
                    logger.info(f"Exported {len(seller_data)} seller metric records")
                
                # Category Metrics Sheet
                if category_data:
                    category_df = pd.DataFrame(category_data)
                    category_df.to_excel(writer, sheet_name='Category Metrics', index=False)
                    logger.info(f"Exported {len(category_data)} category metric records")
                
                # Rolling Window Metrics Sheet
                if rolling_metrics:
                    rolling_data = []
                    for metric in rolling_metrics:
                        # Flatten buyer metrics
                        if metric.buyer_metrics:
                            for key, value in metric.buyer_metrics.items():
                                rolling_data.append({
                                    'window_type': metric.window_type,
                                    'end_date': metric.end_date,
                                    'metric_category': 'buyer',
                                    'metric_name': key,
                                    'metric_value': value,
                                    'last_updated': metric.last_updated
                                })
                        
                        # Flatten seller metrics
                        if metric.seller_metrics:
                            for key, value in metric.seller_metrics.items():
                                rolling_data.append({
                                    'window_type': metric.window_type,
                                    'end_date': metric.end_date,
                                    'metric_category': 'seller',
                                    'metric_name': key,
                                    'metric_value': value,
                                    'last_updated': metric.last_updated
                                })
                        
                        # Flatten category metrics
                        if metric.category_metrics:
                            for category, cat_metrics in metric.category_metrics.items():
                                for key, value in cat_metrics.items():
                                    rolling_data.append({
                                        'window_type': metric.window_type,
                                        'end_date': metric.end_date,
                                        'metric_category': 'category',
                                        'category_name': category,
                                        'metric_name': key,
                                        'metric_value': value,
                                        'last_updated': metric.last_updated
                                    })
                    
                    if rolling_data:
                        rolling_df = pd.DataFrame(rolling_data)
                        rolling_df.to_excel(writer, sheet_name='Rolling Windows', index=False)
                        logger.info(f"Exported {len(rolling_data)} rolling window metric records")
                
                # Summary Sheet
                summary_data = {
                    'Sheet Name': ['Buyer Metrics', 'Seller Metrics', 'Category Metrics', 'Rolling Windows'],
                    'Record Count': [len(buyer_data), len(seller_data), len(category_data), len(rolling_data) if rolling_metrics else 0],
                    'Export Date': [datetime.now().strftime('%Y-%m-%d %H:%M:%S')] * 4,
                    'Target Date': [str(target_date)] * 4
                }
                summary_df = pd.DataFrame(summary_data)
                summary_df.to_excel(writer, sheet_name='Summary', index=False)
            
            logger.info(f"Excel export completed: {output_file}")
            return output_file
            
    except Exception as e:
        logger.error(f"Excel export failed for {target_date}: {e}")
        return None
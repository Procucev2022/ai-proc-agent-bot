#!/usr/bin/env python3
"""
Simple Excel export script for daily aggregated metrics.

Usage:
    python tests/export_aggregated_metrics.py --start-date 2024-01-01 --end-date 2024-01-31
"""

import pandas as pd
from datetime import date, datetime, timedelta
import argparse
import sys
import os

# Add the app directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.database import get_db_session
from app.models import DailyAggregatedMetrics, DailySummary, RollingWindowMetrics

def export_all_aggregations_to_excel(start_date: date, end_date: date, output_file: str = None):
    """Export all aggregation data to Excel file with multiple sheets."""
    
    if output_file is None:
        output_file = f"all_aggregations_{start_date}_{end_date}.xlsx"
    
    try:
        with get_db_session() as db:
            # Create Excel file with multiple sheets
            with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
                
                # 1. Daily Summaries (User-Level)
                summaries = db.query(DailySummary)\
                    .filter(DailySummary.date.between(start_date, end_date))\
                    .order_by(DailySummary.date.desc())\
                    .all()
                
                if summaries:
                    summary_data = []
                    for summary in summaries:
                        row = {
                            'date': summary.date,
                            'user_id': summary.external_user_id,
                            'user_type': summary.user_type.value if summary.user_type else None,
                            'sessions_count': summary.sessions_count,
                            'rfqs_created_count': summary.rfqs_created_count,
                            'seller_interaction_count': summary.seller_interaction_count,
                            'avg_session_duration': summary.avg_session_duration,
                            'product_items_count': summary.product_items_count,
                            'unique_categories_count': summary.unique_categories_count,
                            'session_continuation_count': summary.session_continuation_count,
                            'primary_product_categories': str(summary.primary_product_categories),
                            'session_states_breakdown': str(summary.session_states_breakdown),
                            'created_at': summary.created_at
                        }
                        summary_data.append(row)
                    
                    df = pd.DataFrame(summary_data)
                    df.to_excel(writer, sheet_name='User Daily Summaries', index=False)
                    print(f"Exported {len(summary_data)} user daily summaries")
                
                # 2. Business Aggregated Metrics
                metrics = db.query(DailyAggregatedMetrics)\
                    .filter(DailyAggregatedMetrics.date.between(start_date, end_date))\
                    .order_by(DailyAggregatedMetrics.date.desc())\
                    .all()
                
                # Separate by metric type for different sheets
                buyer_data = []
                seller_data = []
                system_data = []
                
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
                    elif metric.metric_type == 'system_summary':
                        row = {**base_row, **metric.metric_data}
                        system_data.append(row)
                
                if buyer_data:
                    buyer_df = pd.DataFrame(buyer_data)
                    buyer_df.to_excel(writer, sheet_name='Business Buyer Metrics', index=False)
                    print(f"Exported {len(buyer_data)} business buyer metrics")
                
                if seller_data:
                    seller_df = pd.DataFrame(seller_data)
                    seller_df.to_excel(writer, sheet_name='Business Seller Metrics', index=False)
                    print(f"Exported {len(seller_data)} business seller metrics")
                
                if system_data:
                    system_df = pd.DataFrame(system_data)
                    system_df.to_excel(writer, sheet_name='Business System Metrics', index=False)
                    print(f"Exported {len(system_data)} business system metrics")
                
                # 3. Rolling Window Metrics
                rolling_metrics = db.query(RollingWindowMetrics)\
                    .filter(RollingWindowMetrics.end_date >= start_date)\
                    .order_by(RollingWindowMetrics.end_date.desc())\
                    .all()
                
                if rolling_metrics:
                    rolling_data = []
                    for metric in rolling_metrics:
                        row = {
                            'window_type': metric.window_type,
                            'end_date': metric.end_date,
                            'last_updated': metric.last_updated,
                            'buyer_metrics': str(metric.buyer_metrics) if metric.buyer_metrics else None,
                            'seller_metrics': str(metric.seller_metrics) if metric.seller_metrics else None,
                            'category_metrics': str(metric.category_metrics) if metric.category_metrics else None
                        }
                        rolling_data.append(row)
                    
                    rolling_df = pd.DataFrame(rolling_data)
                    rolling_df.to_excel(writer, sheet_name='Rolling Window Metrics', index=False)
                    print(f"Exported {len(rolling_data)} rolling window metrics")
                
                # 4. Summary Overview
                summary_overview = {
                    'Metric Type': ['User Daily Summaries', 'Business Buyer Metrics', 'Business Seller Metrics', 'Business System Metrics', 'Rolling Window Metrics'],
                    'Record Count': [len(summaries), len(buyer_data), len(seller_data), len(system_data), len(rolling_metrics) if rolling_metrics else 0],
                    'Date Range': [f"{start_date} to {end_date}"] * 5,
                    'Export Date': [datetime.now().strftime('%Y-%m-%d %H:%M:%S')] * 5
                }
                overview_df = pd.DataFrame(summary_overview)
                overview_df.to_excel(writer, sheet_name='Export Summary', index=False)
            
            print(f"Complete export saved to: {output_file}")
            return output_file
            
    except Exception as e:
        print(f"Export failed: {e}")
        return None

def export_daily_metrics_to_excel(start_date: date, end_date: date, output_file: str = None):
    """Export daily aggregated metrics to Excel file."""
    
    if output_file is None:
        output_file = f"daily_metrics_{start_date}_{end_date}.xlsx"
    
    try:
        with get_db_session() as db:
            # Query all metrics for the date range
            metrics = db.query(DailyAggregatedMetrics)\
                .filter(DailyAggregatedMetrics.date.between(start_date, end_date))\
                .order_by(DailyAggregatedMetrics.date)\
                .all()
            
            if not metrics:
                print(f"No metrics found for date range {start_date} to {end_date}")
                return
            
            # Organize data by metric type
            buyer_data = []
            seller_data = []
            system_data = []
            
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
                elif metric.metric_type == 'system_summary':
                    row = {**base_row, **metric.metric_data}
                    system_data.append(row)
            
            # Create Excel file with multiple sheets
            with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
                
                if buyer_data:
                    buyer_df = pd.DataFrame(buyer_data)
                    buyer_df.to_excel(writer, sheet_name='Buyer Metrics', index=False)
                    print(f"Exported {len(buyer_data)} buyer metric records")
                
                if seller_data:
                    seller_df = pd.DataFrame(seller_data)
                    seller_df.to_excel(writer, sheet_name='Seller Metrics', index=False)
                    print(f"Exported {len(seller_data)} seller metric records")
                
                if system_data:
                    system_df = pd.DataFrame(system_data)
                    system_df.to_excel(writer, sheet_name='System Metrics', index=False)
                    print(f"Exported {len(system_data)} system metric records")
                
                # Summary sheet
                summary_data = {
                    'Metric Type': ['Buyer Metrics', 'Seller Metrics', 'System Metrics'],
                    'Record Count': [len(buyer_data), len(seller_data), len(system_data)],
                    'Date Range': [f"{start_date} to {end_date}"] * 3,
                    'Export Date': [datetime.now().strftime('%Y-%m-%d %H:%M:%S')] * 3
                }
                summary_df = pd.DataFrame(summary_data)
                summary_df.to_excel(writer, sheet_name='Summary', index=False)
            
            print(f"✅ Export completed: {output_file}")
            return output_file
            
    except Exception as e:
        print(f"❌ Export failed: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(description='Export all aggregation data to Excel')
    
    # Set default dates - today and yesterday to capture recent activity
    today = date.today()
    yesterday = today - timedelta(days=1)
    
    parser.add_argument('--start-date', type=str, default=yesterday.strftime('%Y-%m-%d'),
                       help=f'Start date in YYYY-MM-DD format (default: {yesterday})')
    parser.add_argument('--end-date', type=str, default=today.strftime('%Y-%m-%d'),
                       help=f'End date in YYYY-MM-DD format (default: {today})') 
    parser.add_argument('--output', type=str, 
                       help='Output Excel file name')
    parser.add_argument('--legacy', action='store_true',
                       help='Use legacy export (business metrics only)')
    
    args = parser.parse_args()
    
    try:
        start_date = datetime.strptime(args.start_date, '%Y-%m-%d').date()
        end_date = datetime.strptime(args.end_date, '%Y-%m-%d').date()
        print(f"Exporting data from {start_date} to {end_date}")
    except ValueError as e:
        print(f"Invalid date format: {e}")
        print("Please use YYYY-MM-DD format")
        return 1
    
    if start_date > end_date:
        print("Start date must be before or equal to end date")
        return 1
    
    # Run export
    if args.legacy:
        print("Using legacy export (business metrics only)")
        result = export_daily_metrics_to_excel(start_date, end_date, args.output)
    else:
        print("Using comprehensive export (all aggregation data)")
        result = export_all_aggregations_to_excel(start_date, end_date, args.output)
    
    return 0 if result else 1

if __name__ == "__main__":
    exit(main())
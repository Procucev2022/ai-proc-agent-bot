#!/usr/bin/env python3
"""
Enhanced Excel Report Generator Service for Procurement Agent Analytics

Generates comprehensive Excel reports using actual database tables:
- buyer_daily_metrics
- seller_daily_metrics  
- category_aggregates

Creates multiple sheets:
1. Buyer Details (Last 30D)
2. Seller Details (Last 30D) 
3. Category Details (Last 30D)
4. Buyer Summary (1/7/30/90 day metrics)

Usage:
    python app/services/enhanced_excel_report_service.py [--date YYYY-MM-DD] [--output filename.xlsx]
    
    Or import as service:
    from app.services.enhanced_excel_report_service import EnhancedExcelReportService
    service = EnhancedExcelReportService()
    service.generate_report()
"""

import os
import sys
import logging
import base64
import asyncio
from datetime import datetime, date, timedelta
from typing import Dict, List, Any, Optional
import pandas as pd
from sqlalchemy import text, func

# Add parent directories to path for standalone execution
if __name__ == "__main__":
    current_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(os.path.dirname(current_dir))
    sys.path.insert(0, parent_dir)
    
    from app.database import get_db_session_context, get_db_session
    from app.config import get_settings
    from app.models import ConversationSession, DailyAggregatedMetrics, RollingWindowMetrics, UserType
    from app.services.email_service import EmailService
else:
    from ..database import get_db_session_context, get_db_session
    from ..config import get_settings
    from ..models import ConversationSession, DailyAggregatedMetrics, RollingWindowMetrics, UserType
    from .email_service import EmailService

logger = logging.getLogger(__name__)

class EnhancedExcelReportService:
    """Generate comprehensive Excel reports using actual database tables."""
    
    def __init__(self):
        self.settings = get_settings()
        
    def generate_report(self, target_date: date = None, output_file: str = None, send_email: bool = False) -> str:
        """
        Generate complete Excel report with all sheets.
        
        Args:
            target_date: Date for the report (defaults to yesterday)
            output_file: Output filename (defaults to auto-generated)
            send_email: Whether to email the report (defaults to False)
            
        Returns:
            Path to generated Excel file
        """
        if target_date is None:
            target_date = date.today() - timedelta(days=1)
            
        if output_file is None:
            output_file = f"procurement_analytics_{target_date.strftime('%Y-%m-%d')}.xlsx"
            
        logger.info(f"Generating enhanced Excel report for {target_date}")
        
        # Create Excel writer with formatting
        with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
            # Generate each sheet
            self._generate_buyer_details_sheet(writer, target_date)
            self._generate_seller_details_sheet(writer, target_date)
            self._generate_category_details_sheet(writer, target_date)
            self._generate_buyer_summary_sheet(writer, target_date)
            self._generate_seller_summary_sheet(writer, target_date)
            self._generate_category_summary_sheet(writer, target_date)
            
            # Format the Excel file
            self._format_excel_sheets(writer)
            
        logger.info(f"Enhanced Excel report generated: {output_file}")
        
        # Send email if requested
        if send_email:
            asyncio.run(self._send_report_email(output_file, target_date))
            
        return output_file
    
    def _generate_buyer_details_sheet(self, writer: pd.ExcelWriter, target_date: date):
        """Generate Buyer Details (Last 30D) sheet using buyer_daily_metrics table."""
        logger.info("Generating Buyer Details sheet from buyer_daily_metrics")
        
        # Calculate date range (last 30 days)
        start_date = target_date - timedelta(days=29)
        
        with get_db_session_context() as db:
            # First try to use buyer_daily_metrics table if it exists
            try:
                query = text("""
                    SELECT 
                        date,
                        email,
                        phone_number,
                        number_of_chats as "Chat Initiated",
                        total_rfq_raised as "RFQ Raised",
                        avg_products_per_rfq as "Avg Products per RFQ", 
                        avg_categories_per_rfq as "Avg Categories per RFQ"
                    FROM buyer_daily_metrics 
                    WHERE date BETWEEN :start_date AND :target_date
                    ORDER BY date DESC, email
                """)
                
                result = db.execute(query, {
                    'start_date': start_date,
                    'target_date': target_date
                })
                
                # Convert to DataFrame
                columns = ["date", "email", "phone_number", "Chat Initiated", "RFQ Raised", "Avg Products per RFQ", "Avg Categories per RFQ"]
                data = []
                
                for row in result:
                    data.append([
                        row[0],  # date
                        row[1],  # email
                        row[2],  # phone_number
                        row[3],  # Chat Initiated
                        row[4],  # RFQ Raised
                        round(float(row[5]) if row[5] else 0, 2),  # Avg Products per RFQ
                        round(float(row[6]) if row[6] else 0, 2)   # Avg Categories per RFQ
                    ])
                
                df = pd.DataFrame(data, columns=columns)
                
            except Exception as e:
                logger.warning(f"buyer_daily_metrics table not found or error: {e}. Using conversation_sessions fallback.")
                df = self._generate_buyer_details_fallback(db, start_date, target_date)
            
            # Write to Excel
            df.to_excel(writer, sheet_name='Buyer Details (Last 30D)', index=False)
            
            logger.info(f"Buyer Details sheet created with {len(df)} rows")
    
    def _generate_buyer_details_fallback(self, db, start_date: date, target_date: date) -> pd.DataFrame:
        """Fallback method to generate buyer details from conversation_sessions."""
        query = text("""
            SELECT 
                DATE(created_at) as date,
                external_user_id as email,
                external_user_id as phone_number,
                COUNT(DISTINCT session_id) as "Chat Initiated",
                COALESCE(SUM(CASE WHEN rfq_ids IS NOT NULL AND JSON_LENGTH(rfq_ids) > 0 THEN JSON_LENGTH(rfq_ids) 
                                 WHEN rfq_id IS NOT NULL THEN 1 ELSE 0 END), 0) as "RFQ Raised",
                COALESCE(AVG(CASE WHEN product_items IS NOT NULL AND JSON_LENGTH(product_items) > 0 
                                 THEN JSON_LENGTH(product_items) ELSE 0 END), 0) as "Avg Products per RFQ",
                1.0 as "Avg Categories per RFQ"
            FROM conversation_sessions 
            WHERE user_type = 'buyer' 
                AND DATE(created_at) BETWEEN :start_date AND :target_date
            GROUP BY DATE(created_at), external_user_id
            ORDER BY date DESC, external_user_id
        """)
        
        result = db.execute(query, {
            'start_date': start_date,
            'target_date': target_date
        })
        
        columns = ["date", "email", "phone_number", "Chat Initiated", "RFQ Raised", "Avg Products per RFQ", "Avg Categories per RFQ"]
        data = []
        
        for row in result:
            data.append([
                row[0],  # date
                row[1],  # email
                row[2],  # phone_number
                row[3],  # Chat Initiated
                row[4],  # RFQ Raised
                round(float(row[5]), 2),  # Avg Products per RFQ
                round(float(row[6]), 2)   # Avg Categories per RFQ
            ])
        
        return pd.DataFrame(data, columns=columns)
    
    def _generate_seller_details_sheet(self, writer: pd.ExcelWriter, target_date: date):
        """Generate Seller Details (Last 30D) sheet using seller_daily_metrics table."""
        logger.info("Generating Seller Details sheet from seller_daily_metrics")
        
        # Calculate date range (last 30 days)
        start_date = target_date - timedelta(days=29)
        
        with get_db_session_context() as db:
            # First try to use seller_daily_metrics table if it exists
            try:
                query = text("""
                    SELECT 
                        date,
                        email,
                        phone_number,
                        number_of_chats as "Chats Initiated",
                        total_rfqs_requested_with_quotation as "Requested RFQs",
                        rfq_response_ai as "RFQs Responded"
                    FROM seller_daily_metrics 
                    WHERE date BETWEEN :start_date AND :target_date
                    ORDER BY date DESC, email
                """)
                
                result = db.execute(query, {
                    'start_date': start_date,
                    'target_date': target_date
                })
                
                # Convert to DataFrame
                columns = ["date", "email", "phone_number", "Chats Initiated", "Requested RFQs", "RFQs Responded"]
                data = []
                
                for row in result:
                    data.append([
                        row[0],  # date
                        row[1],  # email
                        row[2],  # phone_number
                        row[3],  # Chats Initiated
                        row[4],  # Requested RFQs
                        row[5]   # RFQs Responded
                    ])
                
                df = pd.DataFrame(data, columns=columns)
                
            except Exception as e:
                logger.warning(f"seller_daily_metrics table not found or error: {e}. Using conversation_sessions fallback.")
                df = self._generate_seller_details_fallback(db, start_date, target_date)
            
            # Write to Excel
            df.to_excel(writer, sheet_name='Seller Details (Last 30D)', index=False)
            
            logger.info(f"Seller Details sheet created with {len(df)} rows")
    
    def _generate_seller_details_fallback(self, db, start_date: date, target_date: date) -> pd.DataFrame:
        """Fallback method to generate seller details from conversation_sessions."""
        query = text("""
            SELECT 
                DATE(created_at) as date,
                external_user_id as email,
                external_user_id as phone_number,
                COUNT(DISTINCT session_id) as "Chats Initiated",
                COALESCE(SUM(CASE WHEN rfq_ids IS NOT NULL AND JSON_LENGTH(rfq_ids) > 0 THEN JSON_LENGTH(rfq_ids) 
                                 WHEN rfq_id IS NOT NULL THEN 1 ELSE 0 END), 0) as "Requested RFQs",
                COALESCE(SUM(CASE WHEN seller_responses IS NOT NULL AND JSON_LENGTH(seller_responses) > 0 
                                 THEN JSON_LENGTH(seller_responses) ELSE 0 END), 0) as "RFQs Responded"
            FROM conversation_sessions 
            WHERE user_type = 'seller' 
                AND DATE(created_at) BETWEEN :start_date AND :target_date
            GROUP BY DATE(created_at), external_user_id
            ORDER BY date DESC, external_user_id
        """)
        
        result = db.execute(query, {
            'start_date': start_date,
            'target_date': target_date
        })
        
        columns = ["date", "email", "phone_number", "Chats Initiated", "Requested RFQs", "RFQs Responded"]
        data = []
        
        for row in result:
            data.append([
                row[0],  # date
                row[1],  # email
                row[2],  # phone_number
                row[3],  # Chats Initiated
                row[4],  # Requested RFQs
                row[5]   # RFQs Responded
            ])
        
        return pd.DataFrame(data, columns=columns)
    
    def _generate_category_details_sheet(self, writer: pd.ExcelWriter, target_date: date):
        """Generate Category Details (Last 30D) sheet using category_aggregates table."""
        logger.info("Generating Category Details sheet from category_aggregates")
        
        # Calculate date range (last 30 days)
        start_date = target_date - timedelta(days=29)
        
        with get_db_session_context() as db:
            # First try to use category_aggregates table if it exists
            try:
                query = text("""
                    SELECT 
                        date,
                        category_name as "Category",
                        total_rfq_raised_category as "RFQs Raised",
                        total_rfqs_intimated as "RFQs w/ Response"
                    FROM category_aggregates 
                    WHERE date BETWEEN :start_date AND :target_date
                    ORDER BY date DESC, category_name
                """)
                
                result = db.execute(query, {
                    'start_date': start_date,
                    'target_date': target_date
                })
                
                # Convert to DataFrame
                columns = ["date", "Category", "RFQs Raised", "RFQs w/ Response"]
                data = []
                
                for row in result:
                    data.append([
                        row[0],  # date
                        row[1],  # Category
                        row[2],  # RFQs Raised
                        row[3]   # RFQs w/ Response
                    ])
                
                df = pd.DataFrame(data, columns=columns)
                
            except Exception as e:
                logger.warning(f"category_aggregates table not found or error: {e}. Using daily_aggregated_metrics fallback.")
                df = self._generate_category_details_fallback(db, start_date, target_date)
            
            # Write to Excel
            df.to_excel(writer, sheet_name='Category Details (Last 30D)', index=False)
            
            logger.info(f"Category Details sheet created with {len(df)} rows")
    
    def _generate_category_details_fallback(self, db, start_date: date, target_date: date) -> pd.DataFrame:
        """Fallback method to generate category details from daily_aggregated_metrics."""
        try:
            # Try to use daily_aggregated_metrics
            query = text("""
                SELECT 
                    date,
                    JSON_UNQUOTE(JSON_EXTRACT(metric_data, '$.category_name')) as "Category",
                    JSON_UNQUOTE(JSON_EXTRACT(metric_data, '$.rfqs_uploaded')) as "RFQs Raised"
                FROM daily_aggregated_metrics 
                WHERE metric_type = 'category_summary'
                    AND date BETWEEN :start_date AND :target_date
                    AND JSON_EXTRACT(metric_data, '$.category_name') IS NOT NULL
                ORDER BY date DESC
            """)
            
            result = db.execute(query, {
                'start_date': start_date,
                'target_date': target_date
            })
            
            columns = ["date", "Category", "RFQs Raised"]
            data = []
            
            for row in result:
                data.append([
                    row[0],  # date
                    row[1] or "General",  # Category
                    int(row[2]) if row[2] else 0   # RFQs Raised
                ])
            
            df = pd.DataFrame(data, columns=columns)
            
        except Exception as e:
            logger.warning(f"daily_aggregated_metrics fallback failed: {e}. Using basic fallback.")
            # Final fallback - create basic category data
            df = pd.DataFrame([
                [target_date, "General", 0],
                [target_date, "Electronics", 0],
                [target_date, "Medical Equipment", 0]
            ], columns=["date", "Category", "RFQs Raised"])
        
        return df
    
    def _generate_buyer_summary_sheet(self, writer: pd.ExcelWriter, target_date: date):
        """Generate Buyer Summary sheet with 1/7/30/90 day metrics."""
        logger.info("Generating Buyer Summary sheet")
        
        # Calculate metrics for different time periods
        metrics_data = []
        
        # Define time periods
        periods = [
            ("Last 1 Day", 1),
            ("Last 7 Days", 7),
            ("Last 30 Days", 30),
            ("Last 90 Days", 90)
        ]
        
        with get_db_session_context() as db:
            for period_name, days in periods:
                start_date = target_date - timedelta(days=days-1)
                
                # Try to use rolling window metrics first, then calculate
                metrics = self._get_rolling_window_metrics(db, target_date, days)
                if not metrics:
                    metrics = self._calculate_buyer_metrics(db, start_date, target_date)
                
                metrics_data.append({
                    'Period': period_name,
                    **metrics
                })
        
        # Create summary DataFrame
        summary_metrics = [
            "Number of Chats Initiated By Buyers",
            "Unique Buyers", 
            "Total RFQs Submitted",
            "Unique Buyers Submitted RFQ",
            "No of Buyers started, but not raised RFQ",
            "Avg Products per RFQ",
            "Avg Categories per RFQ",
            "RFQs with At Least One Response",
            "Total RFQ Responses",
            "Incomplete RFQs",
            "No. of Registrations failed",
            "Unregistered Buyers initiated chat but not continued along with details",
            "User Not Identified"
        ]
        
        # Create the summary table
        summary_data = []
        for metric in summary_metrics:
            row = [metric]
            for period_data in metrics_data:
                # Map metric names to data keys
                value = self._get_metric_value(metric, period_data)
                row.append(value)
            summary_data.append(row)
        
        # Create DataFrame
        columns = ['Metric'] + [data['Period'] for data in metrics_data]
        df = pd.DataFrame(summary_data, columns=columns)
        
        # Write to Excel
        df.to_excel(writer, sheet_name='Buyer Summary', index=False)
        
        logger.info(f"Buyer Summary sheet created with {len(df)} rows")
    
    def _generate_seller_summary_sheet(self, writer: pd.ExcelWriter, target_date: date):
        """Generate Seller Summary sheet with 1/7/30/90 day metrics."""
        logger.info("Generating Seller Summary sheet")
        
        # Calculate metrics for different time periods
        metrics_data = []
        
        # Define time periods
        periods = [
            ("Last 1 Day", 1),
            ("Last 7 Days", 7),
            ("Last 30 Days", 30),
            ("Last 90 Days", 90)
        ]
        
        with get_db_session_context() as db:
            for period_name, days in periods:
                start_date = target_date - timedelta(days=days-1)
                metrics = self._calculate_seller_metrics(db, start_date, target_date)
                
                metrics_data.append({
                    'Period': period_name,
                    **metrics
                })
        
        # Create summary DataFrame
        summary_metrics = [
            "Seller Chats Initiated",
            "Unique Sellers", 
            "RFQs Requested",
            "Subscription Plans Requested",
            "Seller Requested for RFQ but 0 credits",
            "Unregistered sellers initiated chat but not registered"
        ]
        
        # Create the summary table
        summary_data = []
        for metric in summary_metrics:
            row = [metric]
            for period_data in metrics_data:
                value = self._get_seller_metric_value(metric, period_data)
                row.append(value)
            summary_data.append(row)
        
        # Create DataFrame
        columns = ['Metric'] + [data['Period'] for data in metrics_data]
        df = pd.DataFrame(summary_data, columns=columns)
        
        # Write to Excel
        df.to_excel(writer, sheet_name='Seller Summary', index=False)
        
        logger.info(f"Seller Summary sheet created with {len(df)} rows")
    
    def _calculate_seller_metrics(self, db, start_date: date, end_date: date) -> Dict[str, Any]:
        """Calculate seller metrics using daily_aggregates table."""
        
        try:
            query = text("""
                SELECT 
                    metric_name,
                    SUM(value) as total_value
                FROM daily_aggregates 
                WHERE role = 'seller' 
                    AND date BETWEEN :start_date AND :end_date
                GROUP BY metric_name
            """)
            
            result = db.execute(query, {
                'start_date': start_date,
                'end_date': end_date
            }).fetchall()
            
            if result:
                metrics_dict = {row[0]: float(row[1]) for row in result}
                
                return {
                    'seller_chats_initiated': int(metrics_dict.get('Seller Chats Initiated', 0)),
                    'unique_sellers': int(metrics_dict.get('Unique Sellers', 0)),
                    'rfqs_requested': int(metrics_dict.get('Total RFQs Requested', 0)),
                    'subscription_plans_requested': int(metrics_dict.get('Subscription Plans Requested', 0)),
                    'zero_credit_rfq_attempt': int(metrics_dict.get('Zero Credit RFQ Attempt', 0)),
                    'unregistered_sellers_requested_rfq': int(metrics_dict.get('Unregistered Sellers Requested for RFQ', 0))
                }
                
        except Exception as e:
            logger.warning(f"daily_aggregates seller metrics error: {e}. Using fallback.")
        
        # Fallback - return zeros
        return {
            'seller_chats_initiated': 0,
            'unique_sellers': 0,
            'rfqs_requested': 0,
            'subscription_plans_requested': 0,
            'zero_credit_rfq_attempt': 0,
            'unregistered_sellers_requested_rfq': 0
        }
    
    def _get_seller_metric_value(self, metric_name: str, period_data: Dict) -> Any:
        """Map seller metric names to data values."""
        mapping = {
            "Seller Chats Initiated": period_data.get('seller_chats_initiated', 0),
            "Unique Sellers": period_data.get('unique_sellers', 0),
            "RFQs Requested": period_data.get('rfqs_requested', 0),
            "Subscription Plans Requested": period_data.get('subscription_plans_requested', 0),
            "Seller Requested for RFQ but 0 credits": period_data.get('zero_credit_rfq_attempt', 0),
            "Unregistered sellers initiated chat but not registered": period_data.get('unregistered_sellers_requested_rfq', 0)
        }
        return mapping.get(metric_name, 0)
    
    def _generate_category_summary_sheet(self, writer: pd.ExcelWriter, target_date: date):
        """Generate Category Summary (Last 30D) sheet."""
        logger.info("Generating Category Summary sheet")
        
        # Calculate date range (last 30 days)
        start_date = target_date - timedelta(days=29)
        
        with get_db_session_context() as db:
            try:
                query = text("""
                    SELECT 
                        category_name as "Category",
                        SUM(total_rfq_raised_category) as "RFQs Uploaded",
                        SUM(total_rfqs_with_quotations) as "total_rfqs_with_quotations",
                        SUM(total_rfqs_intimated) as "RFQs w/ Response"
                    FROM category_aggregates 
                    WHERE date BETWEEN :start_date AND :target_date
                    GROUP BY category_name
                    ORDER BY category_name
                """)
                
                result = db.execute(query, {
                    'start_date': start_date,
                    'target_date': target_date
                })
                
                # Convert to DataFrame
                columns = ["Category", "RFQs Uploaded", "total_rfqs_with_quotations", "RFQs w/ Response"]
                data = []
                
                for row in result:
                    data.append([
                        row[0] or "General",  # Category
                        int(row[1] or 0),     # RFQs Uploaded
                        int(row[2] or 0),     # total_rfqs_with_quotations
                        int(row[3] or 0)      # RFQs w/ Response
                    ])
                
                df = pd.DataFrame(data, columns=columns)
                
            except Exception as e:
                logger.warning(f"Category summary query failed: {e}. Using sample data.")
                # Fallback sample data
                df = pd.DataFrame([
                    ["Bearings & Accessories", 0, 0, 0],
                    ["Cables", 0, 0, 0],
                    ["Chemicals", 0, 0, 0],
                    ["Ferrous Material & Metals", 0, 0, 0]
                ], columns=["Category", "RFQs Uploaded", "total_rfqs_with_quotations", "RFQs w/ Response"])
            
            # Write to Excel
            df.to_excel(writer, sheet_name='Category Summary (Last 30D)', index=False)
            
            logger.info(f"Category Summary sheet created with {len(df)} rows")
    
    def _get_rolling_window_metrics(self, db, end_date: date, days: int) -> Optional[Dict[str, Any]]:
        """Try to get metrics from rolling_window_metrics table."""
        try:
            window_type_map = {1: "1day", 7: "7day", 30: "30day", 90: "90day"}
            window_type = window_type_map.get(days)
            
            if not window_type:
                return None
            
            query = text("""
                SELECT buyer_metrics 
                FROM rolling_window_metrics 
                WHERE window_type = :window_type 
                    AND end_date = :end_date
                ORDER BY last_updated DESC 
                LIMIT 1
            """)
            
            result = db.execute(query, {
                'window_type': window_type,
                'end_date': end_date
            }).fetchone()
            
            if result and result[0]:
                return result[0]  # JSON data
                
        except Exception as e:
            logger.debug(f"Could not get rolling window metrics: {e}")
            
        return None
    
    def _calculate_buyer_metrics(self, db, start_date: date, end_date: date) -> Dict[str, Any]:
        """Calculate buyer metrics using daily_aggregates table."""
        
        try:
            # Query daily_aggregates with pivot-like approach
            query = text("""
                SELECT 
                    metric_name,
                    SUM(value) as total_value
                FROM daily_aggregates 
                WHERE role = 'buyer' 
                    AND date BETWEEN :start_date AND :end_date
                GROUP BY metric_name
            """)
            
            result = db.execute(query, {
                'start_date': start_date,
                'end_date': end_date
            }).fetchall()
            
            if result:
                # Convert to dictionary
                metrics_dict = {row[0]: float(row[1]) for row in result}
                
                # Calculate averages for products and categories per RFQ
                total_rfqs = metrics_dict.get('Total RFQs Submitted', 0)
                total_items = metrics_dict.get('Total Items in All RFQs', 0)
                total_categories = metrics_dict.get('Total Distinct RFQ Category Combinations', 0)
                
                avg_products_per_rfq = round(total_items / total_rfqs, 2) if total_rfqs > 0 else 0
                avg_categories_per_rfq = round(total_categories / total_rfqs, 2) if total_rfqs > 0 else 0
                
                return {
                    'chats_initiated': int(metrics_dict.get('Number of Chats Initiated By Buyers', 0)),
                    'unique_buyers': int(metrics_dict.get('Unique Buyers', 0)),
                    'total_rfqs': int(metrics_dict.get('Total RFQs Submitted', 0)),
                    'unique_buyers_with_rfqs': int(metrics_dict.get('Unique Buyers Submitted RFQ', 0)),
                    'buyers_no_rfq': int(metrics_dict.get('No of Buyers Started But Not Raised RFQ', 0)),
                    'avg_products_per_rfq': avg_products_per_rfq,
                    'avg_categories_per_rfq': avg_categories_per_rfq,
                    'rfqs_with_response': int(metrics_dict.get('rfqs_with_response', 0)),
                    'total_rfq_responses': int(metrics_dict.get('total_rfq_responses', 0)),
                    'incomplete_rfqs': int(metrics_dict.get('Incomplete RFQs', 0)),
                    'registrations_failed': int(metrics_dict.get('No. of Registrations failed', 0)),
                    'unregistered_abandoned': int(metrics_dict.get('unregistered_abandoned', 0)),
                    'user_not_identified': int(metrics_dict.get('user_not_identified', 0))
                }
                
        except Exception as e:
            logger.warning(f"daily_aggregates table not found or error: {e}. Using conversation_sessions fallback.")
        
        # Fallback to conversation_sessions
        query = text("""
            SELECT 
                COUNT(DISTINCT session_id) as total_chats,
                COUNT(DISTINCT external_user_id) as unique_buyers,
                COUNT(DISTINCT CASE WHEN rfq_ids IS NOT NULL AND JSON_LENGTH(rfq_ids) > 0 THEN session_id
                                   WHEN rfq_id IS NOT NULL THEN session_id END) as sessions_with_rfqs,
                COUNT(DISTINCT CASE WHEN rfq_ids IS NOT NULL AND JSON_LENGTH(rfq_ids) > 0 THEN external_user_id
                                   WHEN rfq_id IS NOT NULL THEN external_user_id END) as unique_buyers_with_rfqs,
                COALESCE(SUM(CASE WHEN rfq_ids IS NOT NULL AND JSON_LENGTH(rfq_ids) > 0 THEN JSON_LENGTH(rfq_ids) 
                                 WHEN rfq_id IS NOT NULL THEN 1 ELSE 0 END), 0) as total_rfqs,
                COALESCE(SUM(CASE WHEN product_items IS NOT NULL AND JSON_LENGTH(product_items) > 0 
                                 THEN JSON_LENGTH(product_items) ELSE 0 END), 0) as total_products,
                COALESCE(SUM(CASE WHEN rfqs_with_response IS NOT NULL AND JSON_LENGTH(rfqs_with_response) > 0 
                                 THEN JSON_LENGTH(rfqs_with_response) ELSE 0 END), 0) as rfqs_with_response,
                COUNT(CASE WHEN outcome IS NULL OR outcome = 'abandoned' THEN 1 END) as incomplete_sessions,
                COUNT(CASE WHEN user_type = 'unknown' THEN 1 END) as unknown_users
            FROM conversation_sessions 
            WHERE user_type IN ('buyer', 'unknown')
                AND DATE(created_at) BETWEEN :start_date AND :end_date
        """)
        
        result = db.execute(query, {
            'start_date': start_date,
            'end_date': end_date
        }).fetchone()
        
        if not result:
            return self._get_empty_metrics()
        
        total_chats = result[0] or 0
        unique_buyers = result[1] or 0
        sessions_with_rfqs = result[2] or 0
        unique_buyers_with_rfqs = result[3] or 0
        total_rfqs = result[4] or 0
        total_products = result[5] or 0
        rfqs_with_response = result[6] or 0
        incomplete_sessions = result[7] or 0
        unknown_users = result[8] or 0
        
        # Calculate averages
        avg_products_per_rfq = round(total_products / total_rfqs, 2) if total_rfqs > 0 else 0
        avg_categories_per_rfq = 1.2  # Placeholder - would need category extraction logic
        
        return {
            'chats_initiated': total_chats,
            'unique_buyers': unique_buyers,
            'total_rfqs': total_rfqs,
            'unique_buyers_with_rfqs': unique_buyers_with_rfqs,
            'buyers_no_rfq': 0,  # Should use daily_aggregates column
            'avg_products_per_rfq': avg_products_per_rfq,
            'avg_categories_per_rfq': avg_categories_per_rfq,
            'rfqs_with_response': rfqs_with_response,
            'total_rfq_responses': rfqs_with_response * 2,  # Estimate
            'incomplete_rfqs': 0,  # Should use daily_aggregates column
            'registrations_failed': 0,  # Would need registration failure tracking
            'unregistered_abandoned': max(0, incomplete_sessions - unique_buyers),  # Estimate
            'user_not_identified': unknown_users
        }
    
    def _get_metric_value(self, metric_name: str, period_data: Dict) -> Any:
        """Map metric names to data values."""
        mapping = {
            "Number of Chats Initiated By Buyers": period_data.get('chats_initiated', 0),
            "Unique Buyers": period_data.get('unique_buyers', 0),
            "Total RFQs Submitted": period_data.get('total_rfqs', 0),
            "Unique Buyers Submitted RFQ": period_data.get('unique_buyers_with_rfqs', 0),
            "No of Buyers started, but not raised RFQ": period_data.get('buyers_no_rfq', 0),
            "Avg Products per RFQ": period_data.get('avg_products_per_rfq', 0),
            "Avg Categories per RFQ": period_data.get('avg_categories_per_rfq', 0),
            "RFQs with At Least One Response": period_data.get('rfqs_with_response', 0),
            "Total RFQ Responses": period_data.get('total_rfq_responses', 0),
            "Incomplete RFQs": period_data.get('incomplete_rfqs', 0),
            "No. of Registrations failed": period_data.get('registrations_failed', 0),
            "Unregistered Buyers initiated chat but not continued along with details": period_data.get('unregistered_abandoned', 0),
            "User Not Identified": period_data.get('user_not_identified', 0)
        }
        return mapping.get(metric_name, 0)
    
    def _get_empty_metrics(self) -> Dict[str, Any]:
        """Return empty metrics structure."""
        return {
            'chats_initiated': 0,
            'unique_buyers': 0,
            'total_rfqs': 0,
            'unique_buyers_with_rfqs': 0,
            'buyers_no_rfq': 0,
            'avg_products_per_rfq': 0,
            'avg_categories_per_rfq': 0,
            'rfqs_with_response': 0,
            'total_rfq_responses': 0,
            'incomplete_rfqs': 0,
            'registrations_failed': 0,
            'unregistered_abandoned': 0,
            'user_not_identified': 0
        }
    
    def _format_excel_sheets(self, writer: pd.ExcelWriter):
        """Apply formatting to Excel sheets."""
        try:
            from openpyxl.styles import Font, PatternFill, Alignment
            from openpyxl.utils.dataframe import dataframe_to_rows
            
            # Get the workbook and worksheets
            workbook = writer.book
            
            # Define styles
            header_font = Font(bold=True, color="FFFFFF")
            header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
            center_alignment = Alignment(horizontal="center", vertical="center")
            
            # Format each worksheet
            for sheet_name in workbook.sheetnames:
                worksheet = workbook[sheet_name]
                
                # Format headers (first row)
                for cell in worksheet[1]:
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = center_alignment
                
                # Auto-adjust column widths
                for column in worksheet.columns:
                    max_length = 0
                    column_letter = column[0].column_letter
                    
                    for cell in column:
                        try:
                            if len(str(cell.value)) > max_length:
                                max_length = len(str(cell.value))
                        except:
                            pass
                    
                    adjusted_width = min(max_length + 2, 50)  # Cap at 50 characters
                    worksheet.column_dimensions[column_letter].width = adjusted_width
                
                # Freeze the header row
                worksheet.freeze_panes = "A2"
            
            logger.info("Excel formatting applied successfully")
            
        except Exception as e:
            logger.warning(f"Could not apply Excel formatting: {e}")
    
    async def _send_report_email(self, excel_file: str, target_date: date):
        """Send Excel report via email using existing template."""
        try:
            # Initialize API client
            if __name__ == "__main__":
                from app.procucev_apis.procucev_api_client import init_procucev_api_client, close_procucev_api_client
            else:
                from ..procucev_apis.procucev_api_client import init_procucev_api_client, close_procucev_api_client
            
            await init_procucev_api_client()
            
            email_service = EmailService()
            
            # Read Excel file and prepare attachment
            filename = os.path.basename(excel_file)
            with open(excel_file, 'rb') as f:
                file_content = f.read()
                file_base64 = base64.b64encode(file_content).decode('utf-8')
            
            # Prepare attachment in correct format
            attachment_data = {
                "filename": filename,
                "content": file_base64,
                "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            }
            
            # Template variables
            template_variables = {
                "parsed_Date": target_date.strftime('%Y-%m-%d')
            }
            
            # Send email with attachment
            result = await email_service.send_email_by_template(
                template_name="B2B_whatsapp_insight_report",
                variables=template_variables,
                attachment=attachment_data
            )
            
            if result.get("status") == "Success":
                logger.info(f"Excel report emailed successfully")
            else:
                logger.error(f"Failed to email report: {result}")
                
        except Exception as e:
            logger.error(f"Error sending email: {e}")
        finally:
            try:
                await close_procucev_api_client()
            except:
                pass


def main():
    """Main function to run the enhanced Excel report generator."""
    import argparse
    import sys
    
    parser = argparse.ArgumentParser(description='Generate enhanced Excel report for procurement analytics')
    parser.add_argument('--date', type=str, help='Target date (YYYY-MM-DD), defaults to yesterday')
    parser.add_argument('--output', type=str, help='Output filename, defaults to auto-generated')
    parser.add_argument('--email', action='store_true', help='Email report to configured recipients')
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose logging')
    
    args = parser.parse_args()
    
    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Parse target date
    target_date = None
    if args.date:
        try:
            target_date = datetime.strptime(args.date, '%Y-%m-%d').date()
        except ValueError:
            logger.error(f"Invalid date format: {args.date}. Use YYYY-MM-DD")
            sys.exit(1)
    
    try:
        # Generate report
        service = EnhancedExcelReportService()
        output_file = service.generate_report(target_date, args.output, args.email)
        
        print(f"Enhanced Excel report generated successfully: {output_file}")
        print(f"Report contains 6 sheets:")
        print(f"  1. Buyer Details (Last 30D)")
        print(f"  2. Seller Details (Last 30D)")
        print(f"  3. Category Details (Last 30D)")
        print(f"  4. Buyer Summary (1/7/30/90 day metrics)")
        print(f"  5. Seller Summary (1/7/30/90 day metrics)")
        print(f"  6. Category Summary (Last 30D)")
        
    except Exception as e:
        logger.error(f"Failed to generate enhanced Excel report: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
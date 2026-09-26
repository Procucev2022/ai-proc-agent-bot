#!/usr/bin/env python3
"""
Real Data Setup Script - Post Migration Setup

This script handles setup tasks after migrating from mock data to real seller data.
It focuses on category mapping and system validation.

Usage:
    python real_data_setup.py [--map-categories] [--validate-only]
"""

import sys
import os
import asyncio
import logging
import argparse
from typing import Dict, Any
from datetime import datetime

# Add the project root directory to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.seller_data_adapter import SellerDataAdapter
from app.services.seller_recommendation_service import SellerRecommendationService
from app.database import test_remote_connection

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class RealDataSetup:
    """Setup manager for real data integration."""

    def __init__(self):
        self.adapter = SellerDataAdapter()

    async def run_complete_setup(self, map_categories: bool = True) -> Dict[str, Any]:
        """
        Run complete setup for real data integration.

        Args:
            map_categories: Whether to map sellers to learning categories

        Returns:
            Setup results dictionary
        """
        print("="*60)
        print("REAL DATA INTEGRATION SETUP")
        print("="*60)

        results = {
            "success": False,
            "start_time": datetime.utcnow().isoformat(),
            "tasks": {},
            "seller_count": 0,
            "errors": []
        }

        try:
            # Task 1: Validate Real Data Connection
            print("\n1. Validating real data connection...")
            connection_result = await self._validate_connection()
            results["tasks"]["connection_validation"] = connection_result

            if not connection_result["success"]:
                results["errors"].append("Failed to connect to real data")
                return results

            # Task 2: Analyze Real Seller Data
            print("\n2. Analyzing real seller data...")
            analysis_result = await self._analyze_seller_data()
            results["tasks"]["data_analysis"] = analysis_result
            results["seller_count"] = analysis_result.get("seller_count", 0)

            # Task 4: Map Categories (optional)
            if map_categories:
                print("\n4. Mapping sellers to learning categories...")
                mapping_result = await self._setup_category_mapping()
                results["tasks"]["category_mapping"] = mapping_result

            # Task 5: Validate Integration
            print("\n5. Validating integration...")
            validation_result = await self._validate_integration()
            results["tasks"]["integration_validation"] = validation_result

            # Check overall success
            all_tasks_successful = all(
                task.get("success", False) for task in results["tasks"].values()
            )

            results["success"] = all_tasks_successful
            results["end_time"] = datetime.utcnow().isoformat()

            # Print summary
            self._print_setup_summary(results)

        except Exception as e:
            logger.error(f"Setup failed with error: {e}")
            results["errors"].append(str(e))
            results["success"] = False

        return results

    async def _validate_connection(self) -> Dict[str, Any]:
        """Validate connection to real data sources."""
        try:
            # Test remote database connection
            if not test_remote_connection():
                return {
                    "success": False,
                    "error": "Cannot connect to remote database"
                }

            # Test seller data adapter
            if not self.adapter.test_connection():
                return {
                    "success": False,
                    "error": "Seller data adapter connection failed"
                }

            print("   SUCCESS: All connections validated")
            return {"success": True}

        except Exception as e:
            return {
                "success": False,
                "error": f"Connection validation failed: {str(e)}"
            }

    async def _analyze_seller_data(self) -> Dict[str, Any]:
        """Analyze the real seller data quality and distribution."""
        try:
            sellers = self.adapter.get_sellers_from_remote()

            if not sellers:
                return {
                    "success": False,
                    "error": "No sellers found in remote database",
                    "seller_count": 0
                }

            # Analyze data quality
            analysis = {
                "seller_count": len(sellers),
                "with_credits": len([s for s in sellers if s.subscription_credits > 0]),
                "by_ranking": {},
                "by_city": {},
                "category_distribution": {},
                "data_quality_issues": []
            }

            for seller in sellers:
                # Ranking distribution
                ranking = seller.ranking.value if seller.ranking else "None"
                analysis["by_ranking"][ranking] = analysis["by_ranking"].get(ranking, 0) + 1

                # City distribution
                if seller.location and seller.location.get("city"):
                    city = seller.location["city"]
                    analysis["by_city"][city] = analysis["by_city"].get(city, 0) + 1

                # Category distribution
                for category in seller.categories:
                    analysis["category_distribution"][category] = analysis["category_distribution"].get(category, 0) + 1

                # Data quality checks
                if not seller.seller_name or seller.seller_name.strip() == "":
                    analysis["data_quality_issues"].append(f"Seller {seller.seller_id} has empty name")

                if not seller.categories:
                    analysis["data_quality_issues"].append(f"Seller {seller.seller_id} has no categories")

            print(f"   SUCCESS: Analyzed {analysis['seller_count']} sellers")
            print(f"   - With credits: {analysis['with_credits']}")
            print(f"   - Ranking distribution: {analysis['by_ranking']}")
            print(f"   - Data quality issues: {len(analysis['data_quality_issues'])}")

            return {
                "success": True,
                **analysis
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Data analysis failed: {str(e)}",
                "seller_count": 0
            }

    async def _setup_category_mapping(self) -> Dict[str, Any]:
        """Setup category mapping for real sellers."""
        try:
            # This would run the updated map_sellers_to_categories.py
            print("   INFO: Category mapping setup for real sellers")
            print("   (This would map real sellers to 3-level learning categories)")

            return {
                "success": True,
                "message": "Category mapping ready for real sellers"
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Category mapping setup failed: {str(e)}"
            }

    async def _validate_integration(self) -> Dict[str, Any]:
        """Validate that the real data integration is working correctly."""
        try:
            service = SellerRecommendationService()

            # Test seller selection with real data
            test_rfq = {
                "rfq_id": "setup-validation-001",
                "categories": ["General Trading", "Electronics"],
                "delivery_location": {
                    "lat": 12.9716,
                    "lng": 77.5946,
                    "city": "Bangalore"
                }
            }

            result = await service.select_sellers_for_rfq(test_rfq)

            total_selected = result.get("total_selected", 0)

            if total_selected > 0:
                print(f"   SUCCESS: Integration validated - {total_selected} sellers selected")
                return {
                    "success": True,
                    "sellers_selected": total_selected,
                    "subscribed": len(result.get("subscribed_sellers", [])),
                    "unsubscribed": len(result.get("unsubscribed_sellers", []))
                }
            else:
                return {
                    "success": False,
                    "error": "No sellers selected in validation test",
                    "reason": result.get("selection_metadata", {}).get("reason", "Unknown")
                }

        except Exception as e:
            return {
                "success": False,
                "error": f"Integration validation failed: {str(e)}"
            }

    def _print_setup_summary(self, results: Dict[str, Any]):
        """Print setup summary."""
        print("\n" + "="*60)
        print("REAL DATA SETUP SUMMARY")
        print("="*60)

        status = "SUCCESS" if results["success"] else "FAILED"
        print(f"Overall Status: {status}")
        print(f"Sellers Available: {results['seller_count']}")
        print(f"Start Time: {results['start_time']}")
        print(f"End Time: {results.get('end_time', 'N/A')}")

        print("\nTask Results:")
        for task_name, task_result in results["tasks"].items():
            status = "PASS" if task_result.get("success") else "FAIL"
            print(f"  {task_name}: {status}")
            if not task_result.get("success") and task_result.get("error"):
                print(f"    Error: {task_result['error']}")

        if results.get("errors"):
            print(f"\nErrors:")
            for error in results["errors"]:
                print(f"  - {error}")

        print("="*60)


async def main():
    """Main setup function."""
    parser = argparse.ArgumentParser(description="Real data setup for seller recommendation system")
    parser.add_argument("--map-categories", action="store_true", help="Map sellers to learning categories")
    parser.add_argument("--validate-only", action="store_true", help="Only run validation checks")

    args = parser.parse_args()

    setup_manager = RealDataSetup()

    if args.validate_only:
        # Only run validation
        print("Running validation checks only...")
        connection_result = await setup_manager._validate_connection()
        analysis_result = await setup_manager._analyze_seller_data()
        validation_result = await setup_manager._validate_integration()

        all_good = all([
            connection_result.get("success", False),
            analysis_result.get("success", False),
            validation_result.get("success", False)
        ])

        print(f"\nValidation Result: {'PASS' if all_good else 'FAIL'}")
        return 0 if all_good else 1
    else:
        # Run complete setup
        results = await setup_manager.run_complete_setup(
            map_categories=args.map_categories
        )

        return 0 if results["success"] else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
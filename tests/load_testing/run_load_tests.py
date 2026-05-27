#!/usr/bin/env python3
"""
Main load testing runner script.

This script provides a unified interface to run all load tests with configuration
management and comprehensive reporting.
"""

import asyncio
import json
import argparse
import logging
import sys
from pathlib import Path
from typing import Dict, Any, List

# Add parent directories to path
sys.path.append(str(Path(__file__).parent.parent.parent))

from test_message_handling import MessageLoadTester
from test_dialogue_flow import DialogueFlowTester
from utils.load_test_utils import LoadTestConfig, LoadTestOrchestrator, create_test_config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class LoadTestRunner:
    """Main load test runner that orchestrates all tests."""
    
    def __init__(self, config_file: str = "config/load_test_config.json"):
        self.config_file = config_file
        self.config = self._load_config()
        self.results = {}
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from JSON file."""
        try:
            config_path = Path(__file__).parent / self.config_file
            with open(config_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            logger.warning(f"Config file {self.config_file} not found. Using default configuration.")
            return self._get_default_config()
        except json.JSONDecodeError as e:
            logger.error(f"Error parsing config file: {e}")
            return self._get_default_config()
    
    def _get_default_config(self) -> Dict[str, Any]:
        """Get default configuration when config file is not available."""
        return {
            "test_configurations": {
                "basic_message_test": {
                    "max_concurrent_requests": 10,
                    "total_requests": 50,
                    "request_timeout": 30.0,
                    "test_duration": 180.0
                },
                "dialogue_flow_test": {
                    "max_concurrent_conversations": 5,
                    "total_conversations": 10,
                    "test_duration": 300.0
                }
            },
            "system_thresholds": {
                "response_time": {"acceptable": 2.0, "warning": 5.0, "critical": 10.0},
                "error_rate": {"acceptable": 0.01, "warning": 0.05, "critical": 0.1}
            },
            "reporting": {
                "output_directory": "load_test_results",
                "include_charts": True
            }
        }
    
    def _create_load_test_config(self, test_name: str) -> LoadTestConfig:
        """Create LoadTestConfig from JSON configuration."""
        test_config = self.config["test_configurations"].get(test_name, {})
        
        return LoadTestConfig(
            max_concurrent_requests=test_config.get("max_concurrent_requests", 10),
            total_requests=test_config.get("total_requests", 50),
            request_timeout=test_config.get("request_timeout", 30.0),
            delay_between_requests=test_config.get("delay_between_requests", 0.1),
            warmup_requests=test_config.get("warmup_requests", 5),
            test_duration=test_config.get("test_duration", 180.0),
            collect_system_metrics=test_config.get("collect_system_metrics", True),
            metrics_interval=test_config.get("metrics_interval", 5.0),
            output_directory=self.config["reporting"].get("output_directory", "load_test_results"),
            save_individual_results=test_config.get("save_individual_results", True),
            generate_charts=test_config.get("generate_charts", True)
        )
    
    async def run_message_handling_test(self, test_name: str = "basic_message_test") -> Dict[str, Any]:
        """Run message handling load test."""
        logger.info(f"Starting message handling test: {test_name}")
        
        config = self._create_load_test_config(test_name)
        orchestrator = LoadTestOrchestrator(config)
        tester = MessageLoadTester()
        
        async def test_function():
            concurrent_results = await tester.run_concurrent_test(
                num_messages=config.total_requests,
                max_concurrent=config.max_concurrent_requests
            )
            return {
                "test_type": "message_handling",
                "test_name": test_name,
                "total_requests": concurrent_results.total_messages,
                "successful_requests": concurrent_results.successful_messages,
                "failed_requests": concurrent_results.failed_messages,
                "error_rate": concurrent_results.error_rate,
                "average_response_time": concurrent_results.average_response_time,
                "min_response_time": concurrent_results.min_response_time,
                "max_response_time": concurrent_results.max_response_time,
                "requests_per_second": concurrent_results.messages_per_second,
                "test_duration": concurrent_results.test_duration
            }
        
        return await orchestrator.run_comprehensive_test(test_function, f"message_handling_{test_name}")
    
    async def run_webhook_test(self, test_name: str = "webhook_test") -> Dict[str, Any]:
        """Run webhook processing load test."""
        logger.info(f"Starting webhook test: {test_name}")
        
        config = self._create_load_test_config(test_name)
        orchestrator = LoadTestOrchestrator(config)
        tester = MessageLoadTester()
        
        async def test_function():
            webhook_results = await tester.run_webhook_test(
                num_messages=config.total_requests
            )
            return {
                "test_type": "webhook_processing",
                "test_name": test_name,
                "total_requests": webhook_results.total_messages,
                "successful_requests": webhook_results.successful_messages,
                "failed_requests": webhook_results.failed_messages,
                "error_rate": webhook_results.error_rate,
                "average_response_time": webhook_results.average_response_time,
                "min_response_time": webhook_results.min_response_time,
                "max_response_time": webhook_results.max_response_time,
                "requests_per_second": webhook_results.messages_per_second,
                "test_duration": webhook_results.test_duration
            }
        
        return await orchestrator.run_comprehensive_test(test_function, f"webhook_{test_name}")
    
    async def run_dialogue_flow_test(self, test_name: str = "dialogue_flow_test") -> Dict[str, Any]:
        """Run dialogue flow load test."""
        logger.info(f"Starting dialogue flow test: {test_name}")
        
        config = self._create_load_test_config(test_name)
        orchestrator = LoadTestOrchestrator(config)
        tester = DialogueFlowTester()
        
        async def test_function():
            dialogue_config = self.config["test_configurations"].get(test_name, {})
            dialogue_results = await tester.run_concurrent_conversations(
                num_conversations=dialogue_config.get("total_conversations", 10),
                max_concurrent=dialogue_config.get("max_concurrent_conversations", 5)
            )
            return {
                "test_type": "dialogue_flow",
                "test_name": test_name,
                "total_conversations": dialogue_results.total_conversations,
                "successful_conversations": dialogue_results.successful_conversations,
                "failed_conversations": dialogue_results.failed_conversations,
                "success_rate": dialogue_results.success_rate,
                "average_conversation_duration": dialogue_results.average_conversation_duration,
                "min_conversation_duration": dialogue_results.min_conversation_duration,
                "max_conversation_duration": dialogue_results.max_conversation_duration,
                "conversations_per_minute": dialogue_results.conversations_per_minute,
                "test_duration": dialogue_results.test_duration,
                "stage_performance": dialogue_results.stage_performance
            }
        
        return await orchestrator.run_comprehensive_test(test_function, f"dialogue_flow_{test_name}")
    
    async def run_stress_test(self) -> Dict[str, Any]:
        """Run comprehensive stress test."""
        logger.info("Starting comprehensive stress test")
        
        stress_config = self._create_load_test_config("stress_test")
        orchestrator = LoadTestOrchestrator(stress_config)
        tester = MessageLoadTester()
        
        async def test_function():
            stress_results = await tester.run_concurrent_test(
                num_messages=stress_config.total_requests,
                max_concurrent=stress_config.max_concurrent_requests
            )
            return {
                "test_type": "stress_test",
                "total_requests": stress_results.total_messages,
                "successful_requests": stress_results.successful_messages,
                "failed_requests": stress_results.failed_messages,
                "error_rate": stress_results.error_rate,
                "average_response_time": stress_results.average_response_time,
                "requests_per_second": stress_results.messages_per_second,
                "test_duration": stress_results.test_duration
            }
        
        return await orchestrator.run_comprehensive_test(test_function, "stress_test")
    
    async def run_all_tests(self) -> Dict[str, Any]:
        """Run all available load tests."""
        logger.info("Starting comprehensive load test suite")
        
        all_results = {}
        
        # Run message handling tests
        try:
            all_results["message_handling"] = await self.run_message_handling_test()
        except Exception as e:
            logger.error(f"Message handling test failed: {e}")
            all_results["message_handling"] = {"error": str(e)}
        
        # Run webhook tests
        try:
            all_results["webhook"] = await self.run_webhook_test()
        except Exception as e:
            logger.error(f"Webhook test failed: {e}")
            all_results["webhook"] = {"error": str(e)}
        
        # Run dialogue flow tests
        try:
            all_results["dialogue_flow"] = await self.run_dialogue_flow_test()
        except Exception as e:
            logger.error(f"Dialogue flow test failed: {e}")
            all_results["dialogue_flow"] = {"error": str(e)}
        
        # Run stress test
        try:
            all_results["stress_test"] = await self.run_stress_test()
        except Exception as e:
            logger.error(f"Stress test failed: {e}")
            all_results["stress_test"] = {"error": str(e)}
        
        self.results = all_results
        self._generate_summary_report()
        
        return all_results
    
    def _generate_summary_report(self):
        """Generate a summary report of all test results."""
        logger.info("Generating summary report")
        
        summary = {
            "test_summary": {
                "total_tests": len(self.results),
                "successful_tests": len([r for r in self.results.values() if "error" not in r]),
                "failed_tests": len([r for r in self.results.values() if "error" in r])
            },
            "performance_summary": {},
            "recommendations": []
        }
        
        # Analyze performance across all tests
        for test_name, result in self.results.items():
            if "error" in result:
                continue
            
            test_summary = result.get("test_results", {})
            if test_summary:
                summary["performance_summary"][test_name] = {
                    "success_rate": 1.0 - test_summary.get("error_rate", 0),
                    "avg_response_time": test_summary.get("average_response_time", 0),
                    "throughput": test_summary.get("requests_per_second", 0)
                }
        
        # Generate recommendations
        thresholds = self.config.get("system_thresholds", {})
        
        for test_name, perf in summary["performance_summary"].items():
            if perf["success_rate"] < (1.0 - thresholds.get("error_rate", {}).get("acceptable", 0.01)):
                summary["recommendations"].append(f"{test_name}: High error rate detected")
            
            if perf["avg_response_time"] > thresholds.get("response_time", {}).get("acceptable", 2.0):
                summary["recommendations"].append(f"{test_name}: High response time detected")
        
        # Save summary report
        output_dir = Path(self.config["reporting"]["output_directory"])
        output_dir.mkdir(exist_ok=True)
        
        with open(output_dir / "test_summary.json", 'w') as f:
            json.dump(summary, f, indent=2, default=str)
        
        print("\n" + "="*60)
        print("LOAD TEST SUMMARY")
        print("="*60)
        print(f"Total Tests: {summary['test_summary']['total_tests']}")
        print(f"Successful: {summary['test_summary']['successful_tests']}")
        print(f"Failed: {summary['test_summary']['failed_tests']}")
        
        if summary["recommendations"]:
            print("\nRECOMMENDATIONS:")
            for rec in summary["recommendations"]:
                print(f"• {rec}")
        
        print("="*60)
        print(f"Detailed results saved to: {output_dir}")

def main():
    """Main entry point for load testing."""
    parser = argparse.ArgumentParser(description="Run load tests for message handling and dialogue flow")
    parser.add_argument("--test", choices=["message", "webhook", "dialogue", "stress", "all"], 
                       default="all", help="Type of test to run")
    parser.add_argument("--config", default="config/load_test_config.json", 
                       help="Path to configuration file")
    parser.add_argument("--verbose", "-v", action="store_true", 
                       help="Enable verbose logging")
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    runner = LoadTestRunner(args.config)
    
    async def run_selected_test():
        if args.test == "message":
            return await runner.run_message_handling_test()
        elif args.test == "webhook":
            return await runner.run_webhook_test()
        elif args.test == "dialogue":
            return await runner.run_dialogue_flow_test()
        elif args.test == "stress":
            return await runner.run_stress_test()
        elif args.test == "all":
            return await runner.run_all_tests()
        else:
            raise ValueError(f"Unknown test type: {args.test}")
    
    try:
        results = asyncio.run(run_selected_test())
        print(f"\nLoad testing completed successfully!")
        print(f"Results saved to: {runner.config['reporting']['output_directory']}")
        
    except KeyboardInterrupt:
        print("\nLoad testing interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nError running load tests: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
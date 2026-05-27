"""
Load testing utilities and helper functions.

This module provides common utilities for load testing including:
- Test data generation
- Result analysis
- Performance monitoring
- Report generation
"""

import time
import json
import csv
import statistics
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, asdict
import asyncio
import psutil
import threading
from pathlib import Path

@dataclass
class PerformanceMetrics:
    """System performance metrics during load testing."""
    timestamp: float
    cpu_percent: float
    memory_percent: float
    memory_used_mb: float
    network_sent_bytes: int
    network_recv_bytes: int
    active_connections: int

@dataclass
class LoadTestConfig:
    """Configuration for load tests."""
    max_concurrent_requests: int = 10
    total_requests: int = 100
    request_timeout: float = 30.0
    delay_between_requests: float = 0.1
    warmup_requests: int = 10
    ramp_up_duration: float = 60.0
    test_duration: float = 300.0
    collect_system_metrics: bool = True
    metrics_interval: float = 5.0
    output_directory: str = "load_test_results"
    save_individual_results: bool = True
    generate_charts: bool = True

class PerformanceMonitor:
    """Monitor system performance during load tests."""
    
    def __init__(self, interval: float = 5.0):
        self.interval = interval
        self.metrics: List[PerformanceMetrics] = []
        self.monitoring = False
        self.monitor_thread = None
        self.initial_network_stats = None
    
    def start_monitoring(self):
        """Start performance monitoring."""
        if self.monitoring:
            return
        
        self.monitoring = True
        self.metrics = []
        self.initial_network_stats = psutil.net_io_counters()
        self.monitor_thread = threading.Thread(target=self._monitor_loop)
        self.monitor_thread.daemon = True
        self.monitor_thread.start()
    
    def stop_monitoring(self):
        """Stop performance monitoring."""
        self.monitoring = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=1)
    
    def _monitor_loop(self):
        """Main monitoring loop."""
        while self.monitoring:
            try:
                # Get current metrics
                cpu_percent = psutil.cpu_percent(interval=1)
                memory = psutil.virtual_memory()
                network = psutil.net_io_counters()
                
                # Calculate network deltas
                if self.initial_network_stats:
                    network_sent = network.bytes_sent - self.initial_network_stats.bytes_sent
                    network_recv = network.bytes_recv - self.initial_network_stats.bytes_recv
                else:
                    network_sent = network.bytes_sent
                    network_recv = network.bytes_recv
                
                # Get active connections (estimate)
                try:
                    active_connections = len(psutil.net_connections())
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    active_connections = 0
                
                # Record metrics
                metrics = PerformanceMetrics(
                    timestamp=time.time(),
                    cpu_percent=cpu_percent,
                    memory_percent=memory.percent,
                    memory_used_mb=memory.used / (1024 * 1024),
                    network_sent_bytes=network_sent,
                    network_recv_bytes=network_recv,
                    active_connections=active_connections
                )
                
                self.metrics.append(metrics)
                
                # Sleep until next interval
                time.sleep(self.interval)
                
            except Exception as e:
                print(f"Error in monitoring loop: {e}")
                time.sleep(self.interval)
    
    def get_metrics(self) -> List[PerformanceMetrics]:
        """Get collected metrics."""
        return self.metrics.copy()
    
    def get_peak_metrics(self) -> Dict[str, float]:
        """Get peak system metrics."""
        if not self.metrics:
            return {}
        
        return {
            "peak_cpu_percent": max(m.cpu_percent for m in self.metrics),
            "peak_memory_percent": max(m.memory_percent for m in self.metrics),
            "peak_memory_used_mb": max(m.memory_used_mb for m in self.metrics),
            "peak_network_sent_bytes": max(m.network_sent_bytes for m in self.metrics),
            "peak_network_recv_bytes": max(m.network_recv_bytes for m in self.metrics),
            "peak_active_connections": max(m.active_connections for m in self.metrics)
        }

class TestDataGenerator:
    """Generate realistic test data for load testing."""
    
    @staticmethod
    def generate_phone_numbers(count: int, base: str = "+91987654") -> List[str]:
        """Generate unique phone numbers for testing."""
        return [f"{base}{i:04d}" for i in range(count)]
    
    @staticmethod
    def generate_procurement_messages() -> List[Dict[str, Any]]:
        """Generate realistic procurement messages."""
        messages = [
            {
                "type": "text",
                "content": "I need 50 laptops for my office setup",
                "category": "buy_something",
                "complexity": "medium"
            },
            {
                "type": "text",
                "content": "Looking for office furniture - desks, chairs, and cabinets",
                "category": "buy_something",
                "complexity": "high"
            },
            {
                "type": "text",
                "content": "Need printer cartridges urgently",
                "category": "buy_something",
                "complexity": "low"
            },
            {
                "type": "text",
                "content": "Hello, can you help me with procurement?",
                "category": "general_inquiry",
                "complexity": "low"
            },
            {
                "type": "text",
                "content": "What's the status of my RFQ GMT123456?",
                "category": "rfq_status_check",
                "complexity": "low"
            },
            {
                "type": "text",
                "content": "I want to buy 200 units of LED monitors with specific requirements",
                "category": "buy_something",
                "complexity": "medium"
            },
            {
                "type": "interactive",
                "content": '{"type": "button_reply", "button_reply": {"id": "confirm_rfq", "title": "Confirm"}}',
                "category": "confirmation_response",
                "complexity": "low"
            },
            {
                "type": "text",
                "content": "I need IT equipment for new branch office - computers, servers, networking",
                "category": "buy_something",
                "complexity": "high"
            },
            {
                "type": "text",
                "content": "Can you tell me about your services and pricing?",
                "category": "general_inquiry",
                "complexity": "medium"
            },
            {
                "type": "text",
                "content": "Need stationery supplies for 100 employees",
                "category": "buy_something",
                "complexity": "medium"
            }
        ]
        return messages
    
    @staticmethod
    def generate_conversation_sequences() -> List[List[str]]:
        """Generate realistic conversation sequences."""
        sequences = [
            [
                "I need office equipment",
                "Looking for 25 desks and 30 chairs",
                "Wooden desks with drawers, ergonomic chairs",
                "Deliver to Mumbai by March 15th",
                "Yes, create the RFQ"
            ],
            [
                "Hello, I want to buy laptops",
                "Need 50 Dell laptops with 16GB RAM",
                "For software development team",
                "Budget is around 30 lakhs",
                "Delivery to Bangalore office",
                "Yes, proceed with RFQ creation"
            ],
            [
                "I need printer cartridges",
                "For HP LaserJet printers",
                "Black and color both",
                "Urgent delivery needed",
                "Confirm the order"
            ],
            [
                "What's the status of my RFQ?",
                "RFQ ID is GMT123456",
                "It was for office supplies",
                "Thank you for the update"
            ],
            [
                "I want to modify my existing RFQ",
                "Change quantity from 100 to 150",
                "Also change delivery location",
                "Update to Chennai instead of Delhi",
                "Yes, save the changes"
            ]
        ]
        return sequences

class ResultAnalyzer:
    """Analyze load test results and generate insights."""
    
    @staticmethod
    def analyze_response_times(response_times: List[float]) -> Dict[str, float]:
        """Analyze response time distribution."""
        if not response_times:
            return {}
        
        return {
            "count": len(response_times),
            "mean": statistics.mean(response_times),
            "median": statistics.median(response_times),
            "mode": statistics.mode(response_times) if len(set(response_times)) < len(response_times) else None,
            "std_dev": statistics.stdev(response_times) if len(response_times) > 1 else 0,
            "min": min(response_times),
            "max": max(response_times),
            "p50": statistics.quantiles(response_times, n=2)[0] if len(response_times) >= 2 else response_times[0],
            "p95": statistics.quantiles(response_times, n=20)[18] if len(response_times) >= 20 else max(response_times),
            "p99": statistics.quantiles(response_times, n=100)[98] if len(response_times) >= 100 else max(response_times)
        }
    
    @staticmethod
    def calculate_throughput(successful_requests: int, total_duration: float) -> Dict[str, float]:
        """Calculate throughput metrics."""
        if total_duration <= 0:
            return {"requests_per_second": 0, "requests_per_minute": 0}
        
        return {
            "requests_per_second": successful_requests / total_duration,
            "requests_per_minute": (successful_requests / total_duration) * 60
        }
    
    @staticmethod
    def analyze_error_patterns(errors: List[str]) -> Dict[str, int]:
        """Analyze error patterns in test results."""
        error_counts = {}
        for error in errors:
            # Categorize errors
            if "timeout" in error.lower():
                category = "timeout"
            elif "connection" in error.lower():
                category = "connection"
            elif "validation" in error.lower():
                category = "validation"
            elif "processing" in error.lower():
                category = "processing"
            else:
                category = "other"
            
            error_counts[category] = error_counts.get(category, 0) + 1
        
        return error_counts
    
    @staticmethod
    def generate_performance_report(results: Dict[str, Any], system_metrics: List[PerformanceMetrics]) -> Dict[str, Any]:
        """Generate comprehensive performance report."""
        report = {
            "test_summary": results,
            "system_performance": {},
            "recommendations": []
        }
        
        # Analyze system metrics
        if system_metrics:
            report["system_performance"] = {
                "peak_cpu": max(m.cpu_percent for m in system_metrics),
                "avg_cpu": statistics.mean(m.cpu_percent for m in system_metrics),
                "peak_memory": max(m.memory_percent for m in system_metrics),
                "avg_memory": statistics.mean(m.memory_percent for m in system_metrics),
                "peak_connections": max(m.active_connections for m in system_metrics)
            }
        
        # Generate recommendations
        if results.get("error_rate", 0) > 0.05:
            report["recommendations"].append("High error rate detected. Consider reducing load or investigating failures.")
        
        if results.get("average_response_time", 0) > 5.0:
            report["recommendations"].append("High average response time. Consider optimizing message processing.")
        
        if system_metrics and max(m.cpu_percent for m in system_metrics) > 80:
            report["recommendations"].append("High CPU usage detected. Consider scaling resources.")
        
        return report

class ReportGenerator:
    """Generate detailed reports from load test results."""
    
    def __init__(self, output_dir: str = "load_test_results"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
    
    def generate_csv_report(self, results: List[Dict[str, Any]], filename: str = "load_test_results.csv"):
        """Generate CSV report from test results."""
        if not results:
            return
        
        filepath = self.output_dir / filename
        
        with open(filepath, 'w', newline='') as csvfile:
            if results:
                fieldnames = results[0].keys()
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(results)
    
    def generate_html_report(self, results: Dict[str, Any], filename: str = "load_test_report.html"):
        """Generate HTML report from test results."""
        filepath = self.output_dir / filename
        
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Load Test Report</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                .header {{ background-color: #f0f0f0; padding: 20px; }}
                .metric {{ margin: 10px 0; }}
                .section {{ margin: 20px 0; }}
                table {{ border-collapse: collapse; width: 100%; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                th {{ background-color: #f2f2f2; }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1>Load Test Report</h1>
                <p>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            </div>
            
            <div class="section">
                <h2>Test Summary</h2>
                <div class="metric">Total Requests: {results.get('total_requests', 0)}</div>
                <div class="metric">Successful: {results.get('successful_requests', 0)}</div>
                <div class="metric">Failed: {results.get('failed_requests', 0)}</div>
                <div class="metric">Error Rate: {results.get('error_rate', 0):.2%}</div>
                <div class="metric">Average Response Time: {results.get('average_response_time', 0):.3f}s</div>
                <div class="metric">Throughput: {results.get('requests_per_second', 0):.2f} req/s</div>
            </div>
            
            <div class="section">
                <h2>Performance Metrics</h2>
                <table>
                    <tr><th>Metric</th><th>Value</th></tr>
                    <tr><td>Min Response Time</td><td>{results.get('min_response_time', 0):.3f}s</td></tr>
                    <tr><td>Max Response Time</td><td>{results.get('max_response_time', 0):.3f}s</td></tr>
                    <tr><td>95th Percentile</td><td>{results.get('p95_response_time', 0):.3f}s</td></tr>
                    <tr><td>99th Percentile</td><td>{results.get('p99_response_time', 0):.3f}s</td></tr>
                </table>
            </div>
        </body>
        </html>
        """
        
        with open(filepath, 'w') as f:
            f.write(html_content)
    
    def generate_charts(self, results: Dict[str, Any], metrics: List[PerformanceMetrics]):
        """Generate performance charts."""
        if not metrics:
            return
        
        # Response time over time chart
        fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(15, 10))
        
        # CPU Usage
        timestamps = [datetime.fromtimestamp(m.timestamp) for m in metrics]
        cpu_values = [m.cpu_percent for m in metrics]
        ax1.plot(timestamps, cpu_values, 'b-', linewidth=2)
        ax1.set_title('CPU Usage Over Time')
        ax1.set_ylabel('CPU %')
        ax1.grid(True)
        
        # Memory Usage
        memory_values = [m.memory_percent for m in metrics]
        ax2.plot(timestamps, memory_values, 'r-', linewidth=2)
        ax2.set_title('Memory Usage Over Time')
        ax2.set_ylabel('Memory %')
        ax2.grid(True)
        
        # Network Traffic
        network_sent = [m.network_sent_bytes / 1024 / 1024 for m in metrics]  # Convert to MB
        network_recv = [m.network_recv_bytes / 1024 / 1024 for m in metrics]
        ax3.plot(timestamps, network_sent, 'g-', label='Sent', linewidth=2)
        ax3.plot(timestamps, network_recv, 'orange', label='Received', linewidth=2)
        ax3.set_title('Network Traffic Over Time')
        ax3.set_ylabel('MB')
        ax3.legend()
        ax3.grid(True)
        
        # Active Connections
        connections = [m.active_connections for m in metrics]
        ax4.plot(timestamps, connections, 'purple', linewidth=2)
        ax4.set_title('Active Connections Over Time')
        ax4.set_ylabel('Connections')
        ax4.grid(True)
        
        # Format x-axis
        for ax in [ax1, ax2, ax3, ax4]:
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
            ax.xaxis.set_major_locator(mdates.SecondLocator(interval=30))
            plt.setp(ax.xaxis.get_majorticklabels(), rotation=45)
        
        plt.tight_layout()
        plt.savefig(self.output_dir / "performance_charts.png", dpi=300, bbox_inches='tight')
        plt.close()

class LoadTestOrchestrator:
    """Orchestrate comprehensive load testing."""
    
    def __init__(self, config: LoadTestConfig):
        self.config = config
        self.performance_monitor = PerformanceMonitor(config.metrics_interval)
        self.report_generator = ReportGenerator(config.output_directory)
        self.data_generator = TestDataGenerator()
        
    async def run_comprehensive_test(self, test_function, test_name: str = "load_test") -> Dict[str, Any]:
        """Run a comprehensive load test with monitoring and reporting."""
        print(f"Starting comprehensive {test_name}...")
        
        # Start performance monitoring
        if self.config.collect_system_metrics:
            self.performance_monitor.start_monitoring()
        
        try:
            # Run the actual test
            results = await test_function()
            
            # Stop monitoring
            if self.config.collect_system_metrics:
                self.performance_monitor.stop_monitoring()
            
            # Get system metrics
            system_metrics = self.performance_monitor.get_metrics()
            
            # Generate comprehensive report
            report = ResultAnalyzer.generate_performance_report(results, system_metrics)
            
            # Save results
            if self.config.save_individual_results:
                self.save_detailed_results(results, system_metrics, test_name)
            
            # Generate charts
            if self.config.generate_charts and system_metrics:
                self.report_generator.generate_charts(results, system_metrics)
            
            # Generate HTML report
            self.report_generator.generate_html_report(results, f"{test_name}_report.html")
            
            return report
            
        except Exception as e:
            if self.config.collect_system_metrics:
                self.performance_monitor.stop_monitoring()
            raise e
    
    def save_detailed_results(self, results: Dict[str, Any], metrics: List[PerformanceMetrics], test_name: str):
        """Save detailed results to files."""
        # Save main results
        results_file = self.config.output_directory + f"/{test_name}_results.json"
        with open(results_file, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        
        # Save system metrics
        if metrics:
            metrics_file = self.config.output_directory + f"/{test_name}_metrics.json"
            with open(metrics_file, 'w') as f:
                json.dump([asdict(m) for m in metrics], f, indent=2, default=str)
        
        print(f"Detailed results saved to {self.config.output_directory}/")

# Utility functions for common operations
def create_test_config(
    concurrent_requests: int = 10,
    total_requests: int = 100,
    test_duration: float = 300.0
) -> LoadTestConfig:
    """Create a load test configuration with common settings."""
    return LoadTestConfig(
        max_concurrent_requests=concurrent_requests,
        total_requests=total_requests,
        test_duration=test_duration,
        collect_system_metrics=True,
        generate_charts=True,
        save_individual_results=True
    )

def format_duration(seconds: float) -> str:
    """Format duration in human-readable format."""
    if seconds < 60:
        return f"{seconds:.1f} seconds"
    elif seconds < 3600:
        return f"{seconds/60:.1f} minutes"
    else:
        return f"{seconds/3600:.1f} hours"

def format_size(bytes_size: int) -> str:
    """Format byte size in human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} TB"
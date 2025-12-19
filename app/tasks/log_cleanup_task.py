"""
Log Cleanup and Archival Task

Celery task for automated log management:
- Archives logs older than retention period
- Compresses archives to .tar.gz format
- Purges old archives based on retention policy
- Runs daily via Celery Beat

This task handles date-based log files for all services including:
- app_YYYY-MM-DD.log
- chromadb_YYYY-MM-DD.log
- api_health_monitor_YYYY-MM-DD.log
- openai_interactions/interactions_YYYY-MM-DD.jsonl
- procucev_api_interactions/procucev_api_calls_YYYY-MM-DD.jsonl
"""

import os
import logging
import tarfile
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from celery import shared_task

from app.config import get_settings

logger = logging.getLogger(__name__)


class LogCleanupManager:
    """Manages log file cleanup, archival, and compression."""

    # Patterns for date-based log files
    LOG_PATTERNS = [
        r'^app_\d{4}-\d{2}-\d{2}\.log$',
        r'^chromadb_\d{4}-\d{2}-\d{2}\.log$',
        r'^api_health_monitor_\d{4}-\d{2}-\d{2}\.log$',
        r'^whatsapp_media_\d{4}-\d{2}-\d{2}\.log$',
        r'^whatsapp_webhook_\d{4}-\d{2}-\d{2}\.log$',
    ]

    # Patterns for JSONL log files in subdirectories
    JSONL_PATTERNS = [
        r'^interactions_\d{4}-\d{2}-\d{2}\.jsonl$',  # openai_interactions/
        r'^procucev_api_calls_\d{4}-\d{2}-\d{2}\.jsonl$',  # procucev_api_interactions/
    ]

    # Subdirectories to check for JSONL files
    JSONL_SUBDIRS = [
        'openai_interactions',
        'procucev_api_interactions',
    ]

    def __init__(
        self,
        log_dir: str,
        log_retention_days: int,
        archive_retention_days: int,
        exclude_patterns: Optional[List[str]] = None
    ):
        """
        Initialize the log cleanup manager.

        Args:
            log_dir: Base directory containing log files
            log_retention_days: Days to keep unarchived logs
            archive_retention_days: Days to keep archived logs
            exclude_patterns: List of regex patterns to exclude from cleanup
        """
        self.log_dir = Path(log_dir).resolve()
        self.archive_dir = self.log_dir / 'archives'
        self.log_retention_days = log_retention_days
        self.archive_retention_days = archive_retention_days
        self.exclude_patterns = exclude_patterns or []

        # Statistics
        self.stats = {
            'logs_archived': 0,
            'logs_size': 0,
            'archives_created': 0,
            'archives_deleted': 0,
            'errors': 0,
        }

    def run(self) -> Dict:
        """
        Execute the full cleanup workflow.

        Returns:
            Dictionary containing cleanup statistics
        """
        logger.info("=" * 70)
        logger.info("Log Cleanup Task Started")
        logger.info("=" * 70)
        logger.info(f"Log directory: {self.log_dir}")
        logger.info(f"Archive directory: {self.archive_dir}")
        logger.info(f"Log retention: {self.log_retention_days} days")
        logger.info(f"Archive retention: {self.archive_retention_days} days")
        logger.info("=" * 70)

        if not self.log_dir.exists():
            logger.error(f"Log directory does not exist: {self.log_dir}")
            self.stats['errors'] += 1
            return self.stats

        # Create archive directory if it doesn't exist
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Archive directory ready: {self.archive_dir}")

        # Step 1: Find and archive old logs
        old_logs = self._find_old_logs()
        if old_logs:
            self._archive_logs(old_logs)
        else:
            logger.info("No old logs found to archive")

        # Step 2: Purge old archives
        old_archives = self._find_old_archives()
        if old_archives:
            self._purge_archives(old_archives)
        else:
            logger.info("No old archives found to purge")

        # Log summary
        self._log_summary()

        return self.stats

    def _find_old_logs(self) -> Dict[str, List[Path]]:
        """
        Find log files older than retention period.

        Returns:
            Dictionary mapping date strings to lists of log file paths
        """
        logger.info(f"Searching for logs older than {self.log_retention_days} days...")

        cutoff_date = datetime.now() - timedelta(days=self.log_retention_days)
        old_logs: Dict[str, List[Path]] = {}

        # Search main log directory
        for pattern in self.LOG_PATTERNS:
            for log_file in self.log_dir.glob('*.log'):
                if re.match(pattern, log_file.name):
                    if self._should_exclude(log_file.name):
                        logger.debug(f"Excluded (pattern match): {log_file.name}")
                        continue

                    log_date = self._extract_date_from_filename(log_file.name)
                    if log_date and log_date < cutoff_date:
                        date_key = log_date.strftime('%Y-%m-%d')
                        if date_key not in old_logs:
                            old_logs[date_key] = []
                        old_logs[date_key].append(log_file)

        # Search JSONL subdirectories
        for subdir_name in self.JSONL_SUBDIRS:
            subdir = self.log_dir / subdir_name
            if not subdir.exists():
                continue

            for pattern in self.JSONL_PATTERNS:
                for log_file in subdir.glob('*.jsonl'):
                    if re.match(pattern, log_file.name):
                        if self._should_exclude(log_file.name):
                            logger.debug(f"Excluded (pattern match): {log_file.name}")
                            continue

                        log_date = self._extract_date_from_filename(log_file.name)
                        if log_date and log_date < cutoff_date:
                            date_key = log_date.strftime('%Y-%m-%d')
                            if date_key not in old_logs:
                                old_logs[date_key] = []
                            old_logs[date_key].append(log_file)

        # Sort dates
        sorted_logs = {k: old_logs[k] for k in sorted(old_logs.keys())}

        logger.info(f"Found {sum(len(v) for v in sorted_logs.values())} log files to archive across {len(sorted_logs)} dates")

        return sorted_logs

    def _extract_date_from_filename(self, filename: str) -> Optional[datetime]:
        """
        Extract date from log filename.

        Args:
            filename: Log filename

        Returns:
            datetime object or None if date cannot be extracted
        """
        # Match YYYY-MM-DD pattern
        match = re.search(r'(\d{4})-(\d{2})-(\d{2})', filename)
        if match:
            try:
                year, month, day = match.groups()
                return datetime(int(year), int(month), int(day))
            except ValueError:
                return None
        return None

    def _should_exclude(self, filename: str) -> bool:
        """
        Check if filename matches any exclude pattern.

        Args:
            filename: Name of file to check

        Returns:
            True if file should be excluded
        """
        for pattern in self.exclude_patterns:
            if re.search(pattern, filename):
                return True
        return False

    def _archive_logs(self, logs_by_date: Dict[str, List[Path]]):
        """
        Archive logs grouped by date into compressed tar.gz files.

        Args:
            logs_by_date: Dictionary mapping date strings to lists of log files
        """
        logger.info(f"Archiving {sum(len(v) for v in logs_by_date.values())} log files...")

        for date_str, log_files in logs_by_date.items():
            archive_name = f"logs_{date_str}.tar.gz"
            archive_path = self.archive_dir / archive_name

            # Check if archive already exists
            if archive_path.exists():
                logger.warning(f"Archive already exists: {archive_name} - Skipping")
                continue

            logger.info(f"Creating archive: {archive_name}")

            # Calculate total size
            total_size = sum(f.stat().st_size for f in log_files)
            size_mb = total_size / (1024 * 1024)

            logger.info(f"  Files to archive: {len(log_files)}")
            logger.info(f"  Total size: {size_mb:.2f} MB")

            try:
                # Create compressed tar archive
                with tarfile.open(archive_path, 'w:gz') as tar:
                    for log_file in log_files:
                        # Store with relative path from log_dir
                        arcname = log_file.relative_to(self.log_dir)
                        tar.add(log_file, arcname=arcname)

                logger.info(f"  ✓ Archive created: {archive_path.name}")

                # Delete original log files after successful archival
                for log_file in log_files:
                    try:
                        log_file.unlink()
                        logger.debug(f"  ✓ Deleted: {log_file.name}")
                    except Exception as e:
                        logger.error(f"  ✗ Failed to delete {log_file.name}: {e}")
                        self.stats['errors'] += 1

                # Update statistics
                self.stats['logs_archived'] += len(log_files)
                self.stats['logs_size'] += total_size
                self.stats['archives_created'] += 1

            except Exception as e:
                logger.error(f"  ✗ Failed to create archive {archive_name}: {e}")
                self.stats['errors'] += 1

    def _find_old_archives(self) -> List[Path]:
        """
        Find archive files older than archive retention period.

        Returns:
            List of archive file paths to delete
        """
        logger.info(f"Searching for archives older than {self.archive_retention_days} days...")

        if not self.archive_dir.exists():
            return []

        cutoff_date = datetime.now() - timedelta(days=self.archive_retention_days)
        old_archives = []

        for archive_file in self.archive_dir.glob('logs_*.tar.gz'):
            # Extract date from filename (logs_YYYY-MM-DD.tar.gz)
            match = re.search(r'logs_(\d{4}-\d{2}-\d{2})', archive_file.name)
            if match:
                date_str = match.group(1)
                try:
                    archive_date = datetime.strptime(date_str, '%Y-%m-%d')
                    if archive_date < cutoff_date:
                        old_archives.append(archive_file)
                except ValueError:
                    logger.warning(f"Could not parse date from archive: {archive_file.name}")

        logger.info(f"Found {len(old_archives)} archives to purge")
        return old_archives

    def _purge_archives(self, archives: List[Path]):
        """
        Delete old archive files.

        Args:
            archives: List of archive file paths to delete
        """
        logger.info(f"Purging {len(archives)} old archives...")

        for archive_file in archives:
            size_mb = archive_file.stat().st_size / (1024 * 1024)
            logger.info(f"  Deleting: {archive_file.name} ({size_mb:.2f} MB)")

            try:
                archive_file.unlink()
                logger.info(f"  ✓ Deleted: {archive_file.name}")
                self.stats['archives_deleted'] += 1
            except Exception as e:
                logger.error(f"  ✗ Failed to delete {archive_file.name}: {e}")
                self.stats['errors'] += 1

    def _log_summary(self):
        """Log cleanup summary statistics."""
        logger.info("=" * 70)
        logger.info("CLEANUP SUMMARY")
        logger.info("=" * 70)
        logger.info(f"Logs archived:        {self.stats['logs_archived']} files")
        logger.info(f"Total size archived:  {self.stats['logs_size'] / (1024 * 1024):.2f} MB")
        logger.info(f"Archives created:     {self.stats['archives_created']}")
        logger.info(f"Archives purged:      {self.stats['archives_deleted']}")
        logger.info(f"Errors encountered:   {self.stats['errors']}")
        logger.info("=" * 70)


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def cleanup_logs(self):
    """
    Celery task to clean up and archive old log files.
    
    Runs daily to:
    1. Archive logs older than LOG_RETENTION_DAYS
    2. Compress archives to .tar.gz format
    3. Purge archives older than ARCHIVE_RETENTION_DAYS
    
    Returns:
        Dict containing cleanup statistics and status
    """
    try:
        settings = get_settings()
        
        # Get configuration
        log_dir = os.path.join(settings.PROJECT_ROOT, "logs")
        log_retention_days = settings.log_retention_days
        archive_retention_days = settings.archive_retention_days
        
        logger.info("Starting log cleanup task")
        logger.info(f"Configuration: log_dir={log_dir}, "
                   f"retention={log_retention_days}d, "
                   f"archive_retention={archive_retention_days}d")
        
        # Create and run cleanup manager
        manager = LogCleanupManager(
            log_dir=log_dir,
            log_retention_days=log_retention_days,
            archive_retention_days=archive_retention_days
        )
        
        stats = manager.run()
        
        # Check for errors
        if stats['errors'] > 0:
            logger.warning(f"Log cleanup completed with {stats['errors']} errors")
        else:
            logger.info("Log cleanup completed successfully")
        
        return {
            "status": "completed" if stats['errors'] == 0 else "completed_with_errors",
            "timestamp": datetime.utcnow().isoformat(),
            "statistics": stats
        }
        
    except Exception as e:
        logger.error(f"Log cleanup task failed: {e}", exc_info=True)
        
        # Retry on failure
        raise self.retry(exc=e)

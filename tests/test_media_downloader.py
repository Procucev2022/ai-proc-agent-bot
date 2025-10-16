"""
Test script for WhatsApp Media Downloader Service.

This script tests the media download functionality with the 3 media IDs
found in the logs and verifies the download flow.
"""

import asyncio
import logging
import sys
import os
from pathlib import Path

# Add the project root to the Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.services.media_downloader_service import MediaDownloaderService
from app.config import get_settings

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Media IDs from the logs
TEST_MEDIA_IDS = [
    "2305762589845664",  # RFQ_BOQ_SAMPLE_FILE.xlsx
    "1325112549257415",  # 1000294730.jpeg  
    "1545447230127737"   # RFQ_BOQ_SAMPLE_FILE (1).xlsx
]

async def test_media_download():
    """Test media download functionality."""
    logger.info("Starting media downloader test")
    
    # Initialize the media downloader service
    downloader = MediaDownloaderService()
    
    # Test each media ID
    results = []
    
    for i, media_id in enumerate(TEST_MEDIA_IDS, 1):
        logger.info(f"\n{'='*60}")
        logger.info(f"Testing media download {i}/3")
        logger.info(f"Media ID: {media_id}")
        logger.info(f"{'='*60}")
        
        # Download the media
        result = await downloader.download_media(media_id)
        results.append(result)
        
        # Log the result
        if result["success"]:
            file_info = result["file_info"]
            logger.info(f"✅ Download successful!")
            logger.info(f"   Filename: {file_info['filename']}")
            logger.info(f"   File path: {file_info['file_path']}")
            logger.info(f"   File size: {file_info['file_size']} bytes")
            logger.info(f"   Content type: {file_info['content_type']}")
        else:
            logger.error(f"❌ Download failed!")
            logger.error(f"   Error: {result['error']}")
    
    # Summary
    logger.info(f"\n{'='*60}")
    logger.info("DOWNLOAD SUMMARY")
    logger.info(f"{'='*60}")
    
    successful_downloads = sum(1 for r in results if r["success"])
    failed_downloads = len(results) - successful_downloads
    
    logger.info(f"Total downloads attempted: {len(results)}")
    logger.info(f"Successful downloads: {successful_downloads}")
    logger.info(f"Failed downloads: {failed_downloads}")
    
    # List all downloaded files
    logger.info(f"\n{'='*60}")
    logger.info("DOWNLOADED FILES")
    logger.info(f"{'='*60}")
    
    downloaded_files = downloader.list_downloaded_files()
    if downloaded_files:
        for file_info in downloaded_files:
            logger.info(f"📁 {file_info['filename']}")
            logger.info(f"   Size: {file_info['file_size']} bytes")
            logger.info(f"   Path: {file_info['file_path']}")
    else:
        logger.info("No files found in download directory")
    
    return results

async def test_individual_media_id(media_id: str):
    """Test downloading a single media ID."""
    logger.info(f"Testing individual media ID: {media_id}")
    
    downloader = MediaDownloaderService()
    result = await downloader.download_media(media_id)
    
    if result["success"]:
        logger.info(f"✅ Successfully downloaded media {media_id}")
        logger.info(f"File info: {result['file_info']}")
    else:
        logger.error(f"❌ Failed to download media {media_id}")
        logger.error(f"Error: {result['error']}")
    
    return result

def verify_download_directory():
    """Verify that the download directory exists and is accessible."""
    download_dir = Path("app/download")
    
    logger.info(f"Checking download directory: {download_dir.absolute()}")
    
    if not download_dir.exists():
        logger.info("Creating download directory...")
        download_dir.mkdir(parents=True, exist_ok=True)
        logger.info("✅ Download directory created")
    else:
        logger.info("✅ Download directory exists")
    
    # Check if directory is writable
    test_file = download_dir / "test_write.tmp"
    try:
        test_file.write_text("test")
        test_file.unlink()
        logger.info("✅ Download directory is writable")
    except Exception as e:
        logger.error(f"❌ Download directory is not writable: {e}")
        return False
    
    return True

def main():
    """Main test function."""
    logger.info("WhatsApp Media Downloader Test")
    logger.info("=" * 60)
    
    # Verify download directory
    if not verify_download_directory():
        logger.error("Cannot proceed - download directory issues")
        return
    
    # Load configuration
    try:
        settings = get_settings()
        logger.info(f"✅ Configuration loaded")
        logger.info(f"   Media download URL: {settings.WHATSAPP_MEDIA_DOWNLOAD_URL}")
        logger.info(f"   Username: {settings.WHATSAPP_USERNAME}")
    except Exception as e:
        logger.error(f"❌ Configuration error: {e}")
        return
    
    # Run the test
    try:
        results = asyncio.run(test_media_download())
        
        # Final status
        successful = sum(1 for r in results if r["success"])
        if successful == len(TEST_MEDIA_IDS):
            logger.info(f"\n🎉 All {len(TEST_MEDIA_IDS)} media files downloaded successfully!")
        elif successful > 0:
            logger.info(f"\n⚠️  {successful}/{len(TEST_MEDIA_IDS)} media files downloaded successfully")
        else:
            logger.error(f"\n💥 No media files were downloaded successfully")
            
    except Exception as e:
        logger.error(f"Test execution failed: {e}", exc_info=True)

if __name__ == "__main__":
    main()
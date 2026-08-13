"""
WhatsApp Media Downloader Service.

This service handles downloading media files from WhatsApp using the media ID.
It downloads files to the app/download directory and provides file information.
"""

import os
import requests
import logging
from typing import Dict, Any, Optional
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

class MediaDownloaderService:
    """Service for downloading WhatsApp media files."""
    
    def __init__(self):
        settings = get_settings()
        self.username = settings.WHATSAPP_USERNAME
        self.password = settings.WHATSAPP_PASSWORD
        self.media_download_url = settings.WHATSAPP_MEDIA_DOWNLOAD_URL
        self.download_dir = Path("app/download")
        
        # Create download directory if it doesn't exist
        self.download_dir.mkdir(parents=True, exist_ok=True)
        
    async def download_media(self, media_id: str, filename: Optional[str] = None) -> Dict[str, Any]:
        """
        Download media file using WhatsApp media ID.
        
        Args:
            media_id: WhatsApp media ID
            filename: Optional filename, if not provided will use media_id
            
        Returns:
            Dict with download status, file path, and file info
        """
        try:
            logger.info(f"Starting media download for ID: {media_id}")
            
            # Use direct URL format like the cURL example
            download_url = f"{self.media_download_url}/{media_id}"
            
            logger.info(f"Download URL: {download_url}")
            
            # Make download request using GET method
            response = requests.get(
                download_url,
                timeout=30
            )
            
            logger.info(f"Download response status: {response.status_code}")
            logger.info(f"Download response headers: {dict(response.headers)}")
            
            if response.status_code != 200:
                logger.error(f"Media download failed with status {response.status_code}")
                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}",
                    "media_id": media_id
                }
            
            # Determine filename
            if not filename:
                # Try to get filename from response headers
                content_disposition = response.headers.get('content-disposition', '')
                if 'filename=' in content_disposition:
                    filename = content_disposition.split('filename=')[1].strip('"')
                else:
                    # Use media_id as filename with appropriate extension
                    content_type = response.headers.get('content-type', '')
                    extension = self._get_extension_from_content_type(content_type)
                    filename = f"{media_id}{extension}"
            
            # Save file to download directory
            file_path = self.download_dir / filename
            
            with open(file_path, 'wb') as f:
                f.write(response.content)
            
            # Get file info
            file_info = {
                "filename": filename,
                "file_path": str(file_path),
                "file_size": len(response.content),
                "content_type": response.headers.get('content-type', 'unknown'),
                "media_id": media_id
            }
            
            logger.info(f"Media downloaded successfully: {filename} ({file_info['file_size']} bytes)")
            
            return {
                "success": True,
                "file_info": file_info,
                "media_id": media_id
            }
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error downloading media {media_id}: {e}")
            return {
                "success": False,
                "error": f"Network error: {str(e)}",
                "media_id": media_id
            }
        except Exception as e:
            logger.error(f"Unexpected error downloading media {media_id}: {e}")
            return {
                "success": False,
                "error": f"Unexpected error: {str(e)}",
                "media_id": media_id
            }
    
    def _get_extension_from_content_type(self, content_type: str) -> str:
        """Get file extension from content type."""
        content_type_map = {
            'image/jpeg': '.jpg',
            'image/png': '.png',
            'image/gif': '.gif',
            'image/webp': '.webp',
            'video/mp4': '.mp4',
            'video/avi': '.avi',
            'audio/mpeg': '.mp3',
            'audio/wav': '.wav',
            'application/pdf': '.pdf',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
            'application/vnd.ms-excel': '.xls',
            'application/msword': '.doc',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
            'text/plain': '.txt'
        }
        
        return content_type_map.get(content_type.lower(), '.bin')
    
    def list_downloaded_files(self) -> list:
        """List all files in the download directory."""
        try:
            files = []
            for file_path in self.download_dir.iterdir():
                if file_path.is_file():
                    files.append({
                        "filename": file_path.name,
                        "file_path": str(file_path),
                        "file_size": file_path.stat().st_size,
                        "modified_time": file_path.stat().st_mtime
                    })
            return files
        except Exception as e:
            logger.error(f"Error listing downloaded files: {e}")
            return []
    
    def get_file_info(self, filename: str) -> Optional[Dict[str, Any]]:
        """Get information about a downloaded file."""
        try:
            file_path = self.download_dir / filename
            if file_path.exists():
                return {
                    "filename": filename,
                    "file_path": str(file_path),
                    "file_size": file_path.stat().st_size,
                    "modified_time": file_path.stat().st_mtime,
                    "exists": True
                }
            return {"exists": False}
        except Exception as e:
            logger.error(f"Error getting file info for {filename}: {e}")
            return {"exists": False, "error": str(e)}
    
    async def download_from_webhook_content(self, content: Dict[str, Any]) -> Dict[str, Any]:
        """
        Download media from WhatsApp webhook content.
        
        Args:
            content: Webhook content containing media information
            
        Returns:
            Dict with download status, file path, and file info
        """
        try:
            # Extract media ID and filename from webhook content
            media_id = content.get('id')
            filename = content.get('filename')
            mime_type = content.get('mime_type')
            
            if not media_id:
                return {
                    "success": False,
                    "error": "No media ID found in content",
                    "content": content
                }
            
            logger.info(f"Downloading media from webhook - ID: {media_id}, filename: {filename}, mime_type: {mime_type}")
            
            # Download the media
            result = await self.download_media(media_id, filename)
            
            # Add webhook-specific info to result
            if result["success"] and "file_info" in result:
                result["file_info"]["original_mime_type"] = mime_type
                result["file_info"]["webhook_content"] = content
            
            return result
            
        except Exception as e:
            logger.error(f"Error downloading from webhook content: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Webhook download error: {str(e)}",
                "content": content
            }


# Global instance for easy access
_media_downloader = None

def get_media_downloader() -> 'MediaDownloaderService':
    """Get global media downloader instance."""
    pass
    global _media_downloader
    if _media_downloader is None:
        _media_downloader = MediaDownloaderService()
    return _media_downloader
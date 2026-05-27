"""
ChromaDB Client Utility

Provides a centralized way to create ChromaDB clients with proper configuration.
Supports both HttpClient (server mode) and PersistentClient (local mode) based on configuration.
"""

import logging
import chromadb
from chromadb.config import Settings
from typing import Optional, Union
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


def get_chroma_client(path: Optional[str] = None) -> Union[chromadb.HttpClient, chromadb.PersistentClient]:
    """
    Create a ChromaDB client (HttpClient or PersistentClient) based on configuration.
    
    If CHROMA_USE_SERVER=true (default), creates HttpClient connected to ChromaDB server.
    Otherwise, creates PersistentClient with Settings for local storage.
    
    HttpClient mode (server):
    - Connects to ChromaDB server (Docker container)
    - Better for multi-worker deployments
    - Server handles memory management
    
    PersistentClient mode (local):
    - Uses v2 API with Settings configuration for:
      - LRU cache policy for automatic collection unloading
      - Memory limit (8GB default) to prevent OOM issues
      - Automatic memory management
    
    Args:
        path: Optional path to ChromaDB database (only used for PersistentClient mode).
              If None, uses default from settings.
        
    Returns:
        Configured HttpClient or PersistentClient instance
    """
    settings = get_settings()
    
    # Check if server mode is enabled
    if settings.chroma_use_server:
        # Use HttpClient to connect to ChromaDB server
        # Note: ChromaDB client library automatically handles API versioning (v1/v2)
        # The heartbeat() method will use the appropriate API version supported by the server
        client = chromadb.HttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port
        )
        
        # Test connection - fail fast if server is not running
        # heartbeat() automatically uses v2 API if available, falls back to v1
        try:
            client.heartbeat()
            logger.info(f"Created ChromaDB HttpClient connected to server at {settings.chroma_host}:{settings.chroma_port} (using v2 API)")
        except Exception as e:
            raise RuntimeError(
                f"ChromaDB server not available at {settings.chroma_host}:{settings.chroma_port}. "
                f"Make sure the ChromaDB server container is running. "
                f"Error: {e}"
            ) from e
        
        return client
    else:
        # Use PersistentClient for local storage
        # Use provided path or default from settings
        if path is None:
            path = settings.chroma_persist_directory
        
        # Ensure path exists
        Path(path).mkdir(parents=True, exist_ok=True)
        
        # Create client with Settings for v2 API
        client = chromadb.PersistentClient(
            path=path,
            settings=Settings(
                chroma_segment_cache_policy="LRU",
                chroma_memory_limit_bytes=8_000_000_000  # 8GB limit
            )
        )
        
        logger.info(f"Created ChromaDB PersistentClient at {path} with LRU cache policy and 8GB memory limit")
        
        return client


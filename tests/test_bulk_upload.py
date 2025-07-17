#!/usr/bin/env python3
"""
Script to test bulk upload API with the sample Excel file.
"""

import sys
import asyncio
import base64
from pathlib import Path

# Add the app directory to the path
sys.path.insert(0, str(Path(__file__).parent / "app"))

from app.services.gmt_api_service import GMTAPIService

async def test_bulk_upload():
    """Test bulk upload API with the sample Excel file."""
    
    print("=== Testing Bulk Upload API ===")
    
    # Read the sample Excel file
    sample_file_path = Path(__file__).parent / "RFQ_BOQ_SAMPLE_FILE.xlsx"
    
    if not sample_file_path.exists():
        print(f"Error: Sample file not found at {sample_file_path}")
        return
    
    print(f"1. Reading sample file: {sample_file_path}")
    
    # Read and encode the Excel file
    with open(sample_file_path, 'rb') as f:
        file_bytes = f.read()
    
    # Base64 encode the file
    file_base64 = base64.b64encode(file_bytes).decode('utf-8')
    
    print(f"   - File size: {len(file_bytes)} bytes")
    print(f"   - Base64 encoded size: {len(file_base64)} characters")
    print(f"   - Base64 starts with: {file_base64[:50]}...")
    
    # Prepare API data
    api_data = {
        "boqFileName": "RFQ_BOQ_SAMPLE_FILE.xlsx",
        "boqfile": file_base64
    }
    
    print(f"\n2. Preparing API request...")
    print(f"   - boqFileName: {api_data['boqFileName']}")
    print(f"   - boqfile length: {len(api_data['boqfile'])} characters")
    
    # Initialize GMT API service
    gmt_service = GMTAPIService()
    
    print(f"\n3. Authenticating with GMT API...")
    auth_success = await gmt_service.authenticate()
    
    if not auth_success:
        print("   ❌ Authentication failed!")
        return
    
    print("   ✅ Authentication successful")
    
    print(f"\n4. Calling bulk upload API...")
    try:
        result = await gmt_service.bulk_upload_rfq(api_data)
        print("\n\n", result, "\n\n")
        print(f"   - Success: {result.get('success')}")
        
        if result.get('success'):
            print("   ✅ Bulk upload successful!")
            response = result.get('response', {})
            print(f"   - Response: {response}")
            # Check if it's a structured response
            if isinstance(response, dict):
                for key, value in response.items():
                    print(f"     {key}: {value}")
        else:
            print("   ❌ Bulk upload failed!")
            error = result.get('error', 'Unknown error')
            print(f"   - Error: {error}")
            
            # Try to parse error details
            if 'HTTP' in error:
                print(f"   - This might be a server error or format issue")
                print(f"   - Check the Excel file format and structure")
    
    except Exception as e:
        print(f"   ❌ Exception during bulk upload: {e}")
    
    print(f"\n=== Test Complete ===")

if __name__ == "__main__":
    asyncio.run(test_bulk_upload())
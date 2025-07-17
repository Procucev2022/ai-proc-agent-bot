#!/usr/bin/env python3
"""
Comprehensive test for the complete Excel processing and GMT API upload pipeline.
This test allows hardcoding an Excel file path and tests the entire flow.
"""

import asyncio
import sys
import os
from pathlib import Path
import json

# Add the app directory to the path
sys.path.insert(0, str(Path(__file__).parent))

from app.services.excel_validation_service import ExcelValidationService
from app.services.excel_processing_service import ExcelProcessingService
from app.services.gmt_api_service import GMTAPIService
from app.services.openai_service import OpenAIService

class CompletePipelineTest:
    """Test the complete Excel processing and GMT API upload pipeline."""
    
    def __init__(self, excel_file_path: str):
        self.excel_file_path = excel_file_path
        self.validation_service = ExcelValidationService()
        self.processing_service = ExcelProcessingService()
        self.gmt_service = GMTAPIService()
        
    def print_separator(self, title: str):
        """Print a formatted separator."""
        print(f"\n{'='*60}")
        print(f"  {title}")
        print(f"{'='*60}")
    
    def print_step(self, step_num: int, title: str):
        """Print a formatted step."""
        print(f"\n{step_num}. {title}")
        print("-" * 40)
    
    async def test_complete_pipeline(self):
        """Test the complete pipeline from Excel file to GMT API upload."""
        
        self.print_separator("COMPLETE EXCEL PROCESSING & GMT API UPLOAD TEST")
        print(f"Testing file: {self.excel_file_path}")
        
        # Step 1: File validation
        self.print_step(1, "FILE VALIDATION")
        
        if not os.path.exists(self.excel_file_path):
            print(f"❌ File not found: {self.excel_file_path}")
            return False
        
        # Read file content
        try:
            with open(self.excel_file_path, 'rb') as f:
                file_content = f.read()
                filename = os.path.basename(self.excel_file_path)
            
            print(f"✅ File loaded successfully")
            print(f"   - Filename: {filename}")
            print(f"   - Size: {len(file_content)} bytes")
            
        except Exception as e:
            print(f"❌ Error reading file: {e}")
            return False
        
        # Step 2: Excel processing
        self.print_step(2, "EXCEL PROCESSING WITH OPENAI")
        
        try:
            processing_result = await self.processing_service.process_excel_file(
                content=file_content,
                filename=filename
            )
            
            if processing_result.get('success'):
                print(f"✅ Excel processing successful")
                print(f"   - Headers found: {processing_result.get('headers', [])}")
                print(f"   - Column mapping: {processing_result.get('column_mapping', {})}")
                print(f"   - Items extracted: {processing_result.get('total_items', 0)}")
                print(f"   - Header row index: {processing_result.get('header_row_index', 'N/A')}")
                
                # Show validation results
                validation_result = processing_result.get('validation_result', {})
                if validation_result.get('valid'):
                    print(f"   - ✅ Validation passed")
                    if validation_result.get('warnings'):
                        print(f"   - ⚠️ Validation warnings:")
                        for warning in validation_result.get('warnings', []):
                            print(f"     • {warning}")
                else:
                    print(f"   - ❌ Validation FAILED - Cannot proceed to GMT API")
                    for error in validation_result.get('errors', []):
                        print(f"     • ❌ {error}")
                    for warning in validation_result.get('warnings', []):
                        print(f"     • ⚠️ {warning}")
                    
                    print(f"\n   📋 VALIDATION FAILURE ANALYSIS:")
                    print(f"   - Required fields: ItemDescription, Specification, Uom, Quantity")
                    print(f"   - Column mapping: {processing_result.get('column_mapping', {})}")
                    
                    if not processing_result.get('column_mapping'):
                        print(f"   - ⚠️ OpenAI column mapping failed - no columns were mapped")
                    
                    return False
                
                # Show first item if available
                if processing_result.get('items'):
                    print(f"\n   First item example:")
                    first_item = processing_result['items'][0]
                    for key, value in first_item.items():
                        print(f"     {key}: {value}")
                else:
                    print(f"   - ⚠️ No items extracted from Excel file")
                    return False
                
            else:
                print(f"❌ Excel processing failed: {processing_result.get('error', 'Unknown error')}")
                return False
                
        except Exception as e:
            print(f"❌ Excel processing error: {e}")
            return False
        
        # Step 3: Template creation
        self.print_step(3, "GMT API TEMPLATE CREATION")
        
        try:
            template_bytes = self.processing_service.create_standard_template(
                processing_result['items']
            )
            
            print(f"✅ Template created successfully")
            print(f"   - Template size: {len(template_bytes)} bytes")
            
            # Encode for API
            api_data = self.processing_service.encode_for_api(template_bytes, filename)
            print(f"   - API data prepared")
            print(f"   - Filename: {api_data['boqFileName']}")
            print(f"   - Base64 encoded size: {len(api_data['boqfile'])} characters")
            
        except Exception as e:
            print(f"❌ Template creation error: {e}")
            return False
        
        # Step 4: GMT API authentication
        self.print_step(4, "GMT API AUTHENTICATION")
        
        try:
            auth_success = await self.gmt_service.authenticate()
            
            if auth_success:
                print(f"✅ GMT API authentication successful")
            else:
                print(f"❌ GMT API authentication failed")
                return False
                
        except Exception as e:
            print(f"❌ GMT API authentication error: {e}")
            return False
        
        # Step 5: GMT API bulk upload
        self.print_step(5, "GMT API BULK UPLOAD")
        
        try:
            upload_result = await self.gmt_service.bulk_upload_rfq(api_data)
            
            print(f"Upload result: {json.dumps(upload_result, indent=2)}")
            
            if upload_result.get('success'):
                print(f"✅ GMT API bulk upload successful!")
                response = upload_result.get('response', {})
                print(f"   - Response: {response}")
                
            else:
                print(f"❌ GMT API bulk upload failed")
                error = upload_result.get('error', 'Unknown error')
                print(f"   - Error: {error}")
                
                # Analyze the error
                if 'File format is not correct' in error:
                    print(f"\n   📋 FORMAT ERROR ANALYSIS:")
                    print(f"   - GMT API expects: S.No,ItemDescription,Specification,Uom,Quantity,Remarks")
                    print(f"   - Our mapping: {processing_result.get('column_mapping', {})}")
                    print(f"   - Missing mappings might be causing format issues")
                
                return False
                
        except Exception as e:
            print(f"❌ GMT API bulk upload error: {e}")
            return False
        
        # Step 6: Summary
        self.print_step(6, "PIPELINE SUMMARY")
        
        print(f"✅ Complete pipeline test PASSED")
        print(f"   - File processed: {filename}")
        print(f"   - Items processed: {processing_result.get('total_items', 0)}")
        print(f"   - GMT API upload: SUCCESS")
        
        return True

async def main():
    """Main test function."""
    
    # ==========================================
    # HARDCODE YOUR EXCEL FILE PATH HERE
    # ==========================================
    
    EXCEL_FILE_PATH = "test_excel_files/large_rfq.xlsx"
    
    # You can also test with other files by changing the path:
    # EXCEL_FILE_PATH = "/path/to/your/test/file.xlsx"
    
    # ==========================================
    
    # Check environment variables
    required_vars = [
        "AZURE_OPENAI_API_KEY", 
        "AZURE_OPENAI_ENDPOINT",
        "GMT_BASE_URL",
        "GMT_CLIENT_ID", 
        "GMT_CLIENT_SECRET",
        "GMT_USERNAME",
        "GMT_PASSWORD"
    ]
    
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        print("❌ Missing required environment variables:")
        for var in missing_vars:
            print(f"   - {var}")
        print("\nPlease set these variables before running the test.")
        return False
    
    print("✅ Environment variables OK")
    
    # Run the test
    test = CompletePipelineTest(EXCEL_FILE_PATH)
    success = await test.test_complete_pipeline()
    
    if success:
        print(f"\n🎉 COMPLETE PIPELINE TEST PASSED! 🎉")
    else:
        print(f"\n💥 COMPLETE PIPELINE TEST FAILED! 💥")
    
    return success

if __name__ == "__main__":
    asyncio.run(main())
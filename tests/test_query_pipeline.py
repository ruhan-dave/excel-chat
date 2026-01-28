#!/usr/bin/env python3
"""
Test script for the query pipeline with real data
"""

import requests
import json
import pandas as pd
from pathlib import Path
import sys
import os

# Add the backend src directory to the path
sys.path.append(str(Path(__file__).parent.parent / "backend" / "src"))

from excelservices import ExcelService

def test_query_pipeline():
    """Test the query pipeline with sample queries"""
    
    # Base URL for the backend API
    base_url = "http://127.0.0.1:8000/api"
    
    # Test queries
    test_queries = [
        "How much did I spend on wages and salaries in 2022 and 2023?",
        "What percentage of grants are from public sources?"
    ]
    
    print("🧪 Testing Query Pipeline")
    print("=" * 50)
    
    # First, let's check what data is available
    try:
        excel_file = Path(__file__).parent.parent / "example_sheets" / "Detailed_Expense_Breakdown.xlsx"
        if excel_file.exists():
            df = pd.read_excel(excel_file)
            cleaned_df = ExcelService.clean_dataframe(df)
            print(f"📊 Available fields: {cleaned_df.index.tolist()}")
            print(f"📅 Available years: {cleaned_df.columns.tolist()}")
            print()
        else:
            print(f"❌ Excel file not found: {excel_file}")
            return
    except Exception as e:
        print(f"❌ Error loading Excel file: {e}")
        return
    
    # Test each query
    for i, query in enumerate(test_queries, 1):
        print(f"🔍 Test {i}: {query}")
        print("-" * 40)
        
        try:
            # Make the API call
            response = requests.get(f"{base_url}/query", params={"query": query})
            
            if response.status_code == 200:
                result = response.json()
                print("✅ Response received:")
                print(json.dumps(result, indent=2))
                
                # Check if we got a valid answer
                if "answer" in result:
                    answer = result["answer"]
                    if answer and any(v is not None for v in answer.values()):
                        print("✅ Query processed successfully")
                    else:
                        print("⚠️ Query returned null/empty values")
                elif "error" in result:
                    print(f"❌ Error in response: {result['error']}")
                    
            else:
                print(f"❌ HTTP Error {response.status_code}: {response.text}")
                
        except requests.exceptions.RequestException as e:
            print(f"❌ Request failed: {e}")
        except json.JSONDecodeError as e:
            print(f"❌ JSON decode error: {e}")
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
        
        print()

if __name__ == "__main__":
    test_query_pipeline()

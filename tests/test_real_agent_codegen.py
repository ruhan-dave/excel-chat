"""
Test the actual LLM agent generating Python code for vague prompts.
"""

import sys
from pathlib import Path

# Add the backend/src directory to the path
backend_src = Path(__file__).parent.parent / "backend" / "src"
sys.path.insert(0, str(backend_src))

import os
import pandas as pd
from dotenv import load_dotenv
from pipeline import build_query_pipeline

# Load environment variables
load_dotenv()

# Set the required environment variable for pydantic-ai
os.environ["OPENAI_API_KEY"] = os.environ.get("OPENROUTER_API_KEY")


def test_real_agent_code_generation():
    """
    Test if the actual LLM agent can generate Python code for vague prompts
    and execute it through the sandbox.
    """
    print("=" * 60)
    print("Testing real LLM agent code generation")
    print("=" * 60 + "\n")
    
    # Load the Excel file for testing
    excel_path = "/Users/ruhwang/Desktop/AI/spring2025_courses/capstone/excel-chat/example_sheets/Detailed_Expense_Breakdown.xlsx"
    df = pd.read_excel(excel_path, index_col=0)
    
    print("Test DataFrame:")
    print(df.head(10))
    print(f"\nShape: {df.shape}")
    print()
    
    # Build the pipeline
    pipeline = build_query_pipeline(None, df, None)
    
    # Test 1: Vague prompt for CAGR calculation
    print("Test 1: Vague prompt for CAGR calculation")
    print("-" * 60)
    prompt1 = "What's the compound annual growth rate of revenue from 2020 to 2023?"
    print(f"Prompt: {prompt1}\n")
    
    try:
        result1 = pipeline(prompt1)
        print(f"Result: {result1}")
        print()
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        print()
    
    # Test 2: Vague prompt for trend analysis
    print("Test 2: Vague prompt for trend analysis")
    print("-" * 60)
    prompt2 = "Analyze the profit margin trend across all years"
    print(f"Prompt: {prompt2}\n")
    
    try:
        result2 = pipeline(prompt2)
        print(f"Result: {result2}")
        print()
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        print()
    
    # Test 3: Vague prompt requiring custom calculation
    print("Test 3: Vague prompt for custom calculation")
    print("-" * 60)
    prompt3 = "Calculate the average revenue growth rate year over year"
    print(f"Prompt: {prompt3}\n")
    
    try:
        result3 = pipeline(prompt3)
        print(f"Result: {result3}")
        print()
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        print()
    
    # Test 4: Very vague prompt
    print("Test 4: Very vague prompt about efficiency")
    print("-" * 60)
    prompt4 = "How efficient is the business becoming over time?"
    print(f"Prompt: {prompt4}\n")
    
    try:
        result4 = pipeline(prompt4)
        print(f"Result: {result4}")
        print()
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        print()
    
    print("=" * 60)
    print("Real agent code generation test complete")
    print("=" * 60)


if __name__ == "__main__":
    test_real_agent_code_generation()

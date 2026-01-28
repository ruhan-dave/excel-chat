#!/usr/bin/env python3
"""
Test script for frontend-backend integration
"""

import requests
import json
import time

def test_frontend_backend_integration():
    """Test that the frontend can communicate with the backend"""
    
    # Base URLs
    backend_url = "http://127.0.0.1:8000/api"
    frontend_url = "http://localhost:5173"
    
    print("🔗 Testing Frontend-Backend Integration")
    print("=" * 50)
    
    # Test 1: Check if backend is running
    print("1. Testing backend health...")
    try:
        response = requests.get(f"{backend_url}/test-openai", timeout=5)
        if response.status_code == 200:
            print("✅ Backend is running and OpenAI is working")
        else:
            print(f"❌ Backend returned status {response.status_code}")
            return
    except requests.exceptions.RequestException as e:
        print(f"❌ Backend not accessible: {e}")
        return
    
    # Test 2: Check if frontend is running
    print("\n2. Testing frontend health...")
    try:
        response = requests.get(frontend_url, timeout=5)
        if response.status_code == 200:
            print("✅ Frontend is running")
        else:
            print(f"❌ Frontend returned status {response.status_code}")
            return
    except requests.exceptions.RequestException as e:
        print(f"❌ Frontend not accessible: {e}")
        return
    
    # Test 3: Test query endpoint with sample data
    print("\n3. Testing query endpoint...")
    test_query = "What were the wages and salaries in 2022?"
    try:
        response = requests.get(f"{backend_url}/query", params={"query": test_query}, timeout=30)
        if response.status_code == 200:
            result = response.json()
            if "answer" in result and result["answer"]:
                print(f"✅ Query endpoint working: {test_query}")
                print(f"   Response: {result['answer']}")
            else:
                print("❌ Query endpoint returned empty response")
        else:
            print(f"❌ Query endpoint error: {response.status_code} - {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"❌ Query endpoint request failed: {e}")
    
    # Test 4: Test upload endpoint
    print("\n4. Testing upload endpoint...")
    try:
        # Just check if the endpoint exists (we won't actually upload)
        response = requests.get(f"{backend_url}/upload")
        if response.status_code == 405:  # Method not allowed is expected for GET on upload
            print("✅ Upload endpoint is accessible")
        else:
            print(f"⚠️ Upload endpoint returned unexpected status: {response.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"❌ Upload endpoint not accessible: {e}")
    
    print("\n🎉 Integration test complete!")
    print("\nNext steps:")
    print("1. Open your browser and go to http://localhost:5173")
    print("2. Upload an Excel file using the frontend")
    print("3. Try querying the data with questions like:")
    print("   - 'What were the wages and salaries in 2022?'")
    print("   - 'What percentage of grants are from public sources?'")

if __name__ == "__main__":
    test_frontend_backend_integration()

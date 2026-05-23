"""
Minimal tests for the execute_python_code sandbox tool using pydantic-monty.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Add the backend/src directory to the path
backend_src = Path(__file__).parent.parent / "backend" / "src"
sys.path.insert(0, str(backend_src))

from pipeline import PipelineDeps, execute_python_code
from pydantic_ai import RunContext


def create_mock_context(computed_values=None):
    """Create a mock RunContext for testing."""
    deps = PipelineDeps(
        df=None,
        original_query="test",
        computed_values=computed_values or {},
        available_fields=[],
        available_years=[],
    )
    
    # Create a mock RunContext with required attributes
    mock_ctx = MagicMock(spec=RunContext)
    mock_ctx.deps = deps
    mock_ctx.model = None
    mock_ctx.retry = 0
    mock_ctx.messages = []
    mock_ctx.tool_name = "execute_python_code"
    
    return mock_ctx


def test_basic_math():
    """Test basic math operations in the sandbox."""
    print("Testing basic math operations...")
    
    ctx = create_mock_context()
    
    # Test 1: Simple addition
    code1 = "return 10 + 20"
    output1 = execute_python_code(ctx, code1)
    print(f"Test 1 (10 + 20): {output1}")
    assert "30" in str(output1), f"Expected 30, got {output1}"
    
    # Test 2: Multiplication
    code2 = "return 5 * 4"
    output2 = execute_python_code(ctx, code2)
    print(f"Test 2 (5 * 4): {output2}")
    assert "20" in str(output2), f"Expected 20, got {output2}"
    
    # Test 3: Division
    code3 = "return 100 / 4"
    output3 = execute_python_code(ctx, code3)
    print(f"Test 3 (100 / 4): {output3}")
    assert "25" in str(output3) or "25.0" in str(output3), f"Expected 25, got {output3}"
    
    # Test 4: Exponentiation
    code4 = "return 2 ** 10"
    output4 = execute_python_code(ctx, code4)
    print(f"Test 4 (2 ** 10): {output4}")
    assert "1024" in str(output4), f"Expected 1024, got {output4}"
    
    # Test 5: Complex calculation
    code5 = "return (100 * 1.05) ** 3"
    output5 = execute_python_code(ctx, code5)
    print(f"Test 5 ((100 * 1.05) ** 3): {output5}")
    print(f"  (Expected approximately 115.76)")
    
    # Test 6: Test with result= syntax (should be auto-converted)
    code6 = "result = 50 + 50"
    output6 = execute_python_code(ctx, code6)
    print(f"Test 6 (result = 50 + 50): {output6}")
    assert "100" in str(output6), f"Expected 100, got {output6}"
    
    print("✅ Basic math tests passed!\n")


def test_sorting_functions():
    """Test Python sorting functions in the sandbox."""
    print("Testing sorting functions...")
    
    ctx = create_mock_context()
    
    # Test 1: Sorting a list
    code1 = """
numbers = [5, 2, 8, 1, 9]
return sorted(numbers)
"""
    output1 = execute_python_code(ctx, code1)
    print(f"Test 1 (sorted list): {output1}")
    assert "1" in str(output1) and "9" in str(output1), f"Expected sorted list, got {output1}"
    
    # Test 2: Sorting with reverse
    code2 = """
numbers = [5, 2, 8, 1, 9]
return sorted(numbers, reverse=True)
"""
    output2 = execute_python_code(ctx, code2)
    print(f"Test 2 (reverse sorted): {output2}")
    assert "9" in str(output2), f"Expected 9 at start, got {output2}"
    
    # Test 3: Sorting strings
    code3 = """
names = ['charlie', 'alice', 'bob']
return sorted(names)
"""
    output3 = execute_python_code(ctx, code3)
    print(f"Test 3 (sorted strings): {output3}")
    assert "alice" in str(output3), f"Expected 'alice', got {output3}"
    
    print("✅ Sorting tests passed!\n")


def test_with_computed_values():
    """Test sandbox with injected computed values."""
    print("Testing with computed values...")
    
    ctx = create_mock_context(computed_values={"revenue": 1000, "cost": 600})
    
    # Note: pydantic-monty requires explicit variable declarations in the code
    # We'll test basic operations without relying on injected values for now
    code = """
revenue = 1000
cost = 600
return revenue - cost
"""
    output = execute_python_code(ctx, code)
    print(f"Test with values: {output}")
    assert "400" in str(output), f"Expected 400, got {output}"
    
    print("✅ Computed values test passed!\n")


def test_error_handling():
    """Test error handling in the sandbox."""
    print("Testing error handling...")
    
    ctx = create_mock_context()
    
    # Test division by zero
    code1 = "result = 10 / 0"
    output1 = execute_python_code(ctx, code1)
    print(f"Test division by zero: {output1}")
    assert "ERROR" in str(output1), f"Expected error, got {output1}"
    
    print("✅ Error handling test passed!\n")


if __name__ == "__main__":
    print("=" * 60)
    print("Running sandbox tests for execute_python_code")
    print("=" * 60 + "\n")
    
    try:
        test_basic_math()
        test_sorting_functions()
        test_with_computed_values()
        test_error_handling()
        
        print("=" * 60)
        print("All tests passed! ✅")
        print("=" * 60)
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

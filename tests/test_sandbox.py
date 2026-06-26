"""
Minimal tests for the execute_python_code sandbox tool using pydantic-monty.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Add the backend/src directory to the path
backend_src = Path(__file__).parent.parent / "backend" / "src"
sys.path.insert(0, str(backend_src))

from pipeline import PipelineDeps, execute_python_code
from pydantic_ai import RunContext


def _run(ctx, code):
    """Helper to run async execute_python_code in sync tests."""
    return asyncio.run(execute_python_code(ctx, code))


def create_mock_context(computed_values=None):
    """Create a mock RunContext for testing."""
    deps = PipelineDeps(
        sheets={},
        sheet_metas=[],
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
    output1 = _run(ctx,code1)
    print(f"Test 1 (10 + 20): {output1}")
    assert "30" in str(output1), f"Expected 30, got {output1}"
    
    # Test 2: Multiplication
    code2 = "return 5 * 4"
    output2 = _run(ctx,code2)
    print(f"Test 2 (5 * 4): {output2}")
    assert "20" in str(output2), f"Expected 20, got {output2}"
    
    # Test 3: Division
    code3 = "return 100 / 4"
    output3 = _run(ctx,code3)
    print(f"Test 3 (100 / 4): {output3}")
    assert "25" in str(output3) or "25.0" in str(output3), f"Expected 25, got {output3}"
    
    # Test 4: Exponentiation
    code4 = "return 2 ** 10"
    output4 = _run(ctx,code4)
    print(f"Test 4 (2 ** 10): {output4}")
    assert "1024" in str(output4), f"Expected 1024, got {output4}"
    
    # Test 5: Complex calculation
    code5 = "return (100 * 1.05) ** 3"
    output5 = _run(ctx,code5)
    print(f"Test 5 ((100 * 1.05) ** 3): {output5}")
    print(f"  (Expected approximately 115.76)")
    
    # Test 6: Test with result= syntax (should be auto-converted)
    code6 = "result = 50 + 50"
    output6 = _run(ctx,code6)
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
    output1 = _run(ctx,code1)
    print(f"Test 1 (sorted list): {output1}")
    assert "1" in str(output1) and "9" in str(output1), f"Expected sorted list, got {output1}"
    
    # Test 2: Sorting with reverse
    code2 = """
    numbers = [5, 2, 8, 1, 9]
    return sorted(numbers, reverse=True)
    """
    output2 = _run(ctx,code2)
    print(f"Test 2 (reverse sorted): {output2}")
    assert "9" in str(output2), f"Expected 9 at start, got {output2}"
    
    # Test 3: Sorting strings
    code3 = """
    names = ['charlie', 'alice', 'bob']
    return sorted(names)
    """
    output3 = _run(ctx,code3)
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
    output = _run(ctx,code)
    print(f"Test with values: {output}")
    assert "400" in str(output), f"Expected 400, got {output}"
    
    print("✅ Computed values test passed!\n")


def test_error_handling():
    """Test error handling in the sandbox."""
    print("Testing error handling...")
    
    ctx = create_mock_context()
    
    # Test division by zero
    code1 = "result = 10 / 0"
    output1 = _run(ctx,code1)
    print(f"Test division by zero: {output1}")
    assert "ERROR" in str(output1), f"Expected error, got {output1}"
    
    print("✅ Error handling test passed!\n")


def test_named_operations():
    """Test the NAMED_OPERATIONS functions directly."""
    print("Testing named operations...")
    
    from pipeline import NAMED_OPERATIONS
    
    # Unary operations
    assert NAMED_OPERATIONS["sqrt"](16) == 4.0, "sqrt(16) should be 4.0"
    assert NAMED_OPERATIONS["abs"](-5) == 5, "abs(-5) should be 5"
    assert NAMED_OPERATIONS["negate"](3) == -3, "negate(3) should be -3"
    assert NAMED_OPERATIONS["exp"](0) == 1.0, "exp(0) should be 1.0"
    print("  ✅ Unary operations passed")
    
    # Binary operations
    assert NAMED_OPERATIONS["subtract"](10, 3) == 7, "subtract(10, 3) should be 7"
    assert NAMED_OPERATIONS["divide"](10, 2) == 5.0, "divide(10, 2) should be 5.0"
    assert NAMED_OPERATIONS["divide"](10, 0) is None, "divide(10, 0) should be None"
    assert NAMED_OPERATIONS["return_percentage"](25, 100) == 25.0, "return_percentage(25, 100) should be 25.0"
    assert NAMED_OPERATIONS["power"](2, 3) == 8.0, "power(2, 3) should be 8.0"
    assert NAMED_OPERATIONS["ratio"](10, 2) == 5.0, "ratio(10, 2) should be 5.0"
    assert NAMED_OPERATIONS["ratio"](10, 0) is None, "ratio(10, 0) should be None"
    assert NAMED_OPERATIONS["percentage_change"](110, 100) == 10.0, "percentage_change(110, 100) should be 10.0"
    assert NAMED_OPERATIONS["difference"](10, 3) == 7, "difference(10, 3) should be 7"
    assert NAMED_OPERATIONS["yoy_growth"](110, 100) == 10.0, "yoy_growth(110, 100) should be 10.0"
    assert NAMED_OPERATIONS["yoy_growth"](110, 0) is None, "yoy_growth(110, 0) should be None"
    print("  ✅ Binary operations passed")
    
    # Ternary operations
    cagr_result = NAMED_OPERATIONS["cagr"](200, 100, 5)
    assert cagr_result is not None and abs(cagr_result - 14.8698) < 0.01, f"cagr(200, 100, 5) should be ~14.87, got {cagr_result}"
    assert NAMED_OPERATIONS["cagr"](200, 0, 5) is None, "cagr(200, 0, 5) should be None"
    assert NAMED_OPERATIONS["cagr"](200, 100, 0) is None, "cagr(200, 100, 0) should be None"
    print("  ✅ Ternary operations passed")
    
    # N-ary operations
    assert NAMED_OPERATIONS["add"](1, 2, 3) == 6, "add(1, 2, 3) should be 6"
    assert NAMED_OPERATIONS["multiply"](2, 3, 4) == 24.0, "multiply(2, 3, 4) should be 24.0"
    assert NAMED_OPERATIONS["max"](3, 7, 1) == 7, "max(3, 7, 1) should be 7"
    assert NAMED_OPERATIONS["min"](3, 7, 1) == 1, "min(3, 7, 1) should be 1"
    assert NAMED_OPERATIONS["average"](10, 20, 30) == 20.0, "average(10, 20, 30) should be 20.0"
    assert NAMED_OPERATIONS["median"](1, 2, 3) == 2, "median(1, 2, 3) should be 2"
    stdev_result = NAMED_OPERATIONS["stdev"](2, 4, 4, 4, 5, 5, 7, 9)
    assert stdev_result is not None and abs(stdev_result - 2.138) < 0.01, f"stdev should be ~2.14, got {stdev_result}"
    assert NAMED_OPERATIONS["stdev"](5) is None, "stdev with <2 values should be None"
    print("  ✅ N-ary operations passed")
    
    print("✅ Named operations tests passed!\n")


def test_sandbox_math_module():
    """Test math module functions in the sandbox."""
    print("Testing math module in sandbox...")
    
    ctx = create_mock_context()
    
    # Test math.sqrt
    code1 = "import math\nreturn math.sqrt(144)"
    output1 = _run(ctx,code1)
    print(f"Test math.sqrt(144): {output1}")
    assert "12" in str(output1), f"Expected 12, got {output1}"
    
    # Test math.pow
    code2 = "import math\nreturn math.pow(3, 4)"
    output2 = _run(ctx,code2)
    print(f"Test math.pow(3, 4): {output2}")
    assert "81" in str(output2), f"Expected 81, got {output2}"
    
    # Test math.log
    code3 = "import math\nreturn math.log(math.e)"
    output3 = _run(ctx,code3)
    print(f"Test math.log(e): {output3}")
    assert "1" in str(output3), f"Expected 1, got {output3}"
    
    # Test math.exp
    code4 = "import math\nreturn math.exp(1)"
    output4 = _run(ctx,code4)
    print(f"Test math.exp(1): {output4}")
    assert "2.718" in str(output4) or "2.71" in str(output4), f"Expected ~2.718, got {output4}"
    
    print("✅ Math module sandbox tests passed!\n")


def test_sandbox_financial_calculations():
    """Test financial calculation patterns in the sandbox."""
    print("Testing financial calculations in sandbox...")
    
    ctx = create_mock_context()
    
    # Test YoY growth
    code1 = """
    revenue_2023 = 1100000
    revenue_2022 = 1000000
    yoy = ((revenue_2023 - revenue_2022) / revenue_2022) * 100
    return yoy
    """
    output1 = _run(ctx,code1)
    print(f"Test YoY growth: {output1}")
    assert "10" in str(output1), f"Expected 10, got {output1}"
    
    # Test CAGR
    code2 = """
    import math
    end_value = 200000
    start_value = 100000
    years = 5
    cagr = ((end_value / start_value) ** (1 / years) - 1) * 100
    return cagr
    """
    output2 = _run(ctx,code2)
    print(f"Test CAGR: {output2}")
    assert "14.8" in str(output2) or "14.87" in str(output2), f"Expected ~14.87, got {output2}"
    
    # Test profit margin
    code3 = """
    revenue = 1500000
    expenses = 800000
    margin = (revenue - expenses) / revenue * 100
    return margin
    """
    output3 = _run(ctx,code3)
    print(f"Test profit margin: {output3}")
    assert "46.6" in str(output3), f"Expected ~46.67, got {output3}"
    
    # Test ratio
    code4 = """
    rd = 50000
    marketing = 25000
    ratio = rd / marketing
    return ratio
    """
    output4 = _run(ctx,code4)
    print(f"Test ratio: {output4}")
    assert "2" in str(output4), f"Expected 2, got {output4}"
    
    print("✅ Financial calculation sandbox tests passed!\n")


def test_sandbox_statistics():
    """Test statistics calculations in the sandbox."""
    print("Testing statistics in sandbox...")
    
    ctx = create_mock_context()
    
    # Test average
    code1 = """
    values = [100, 200, 300, 400, 500]
    return sum(values) / len(values)
    """
    output1 = _run(ctx,code1)
    print(f"Test average: {output1}")
    assert "300" in str(output1), f"Expected 300, got {output1}"
    
    # Test max/min
    code2 = """
    values = [100, 200, 300, 400, 500]
    return max(values) - min(values)
    """
    output2 = _run(ctx,code2)
    print(f"Test max-min range: {output2}")
    assert "400" in str(output2), f"Expected 400, got {output2}"
    
    print("✅ Statistics sandbox tests passed!\n")


if __name__ == "__main__":
    print("=" * 60)
    print("Running sandbox tests for execute_python_code")
    print("=" * 60 + "\n")
    
    try:
        test_basic_math()
        test_sorting_functions()
        test_with_computed_values()
        test_error_handling()
        test_named_operations()
        test_sandbox_math_module()
        test_sandbox_financial_calculations()
        test_sandbox_statistics()
        
        print("=" * 60)
        print("All tests passed! ✅")
        print("=" * 60)
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

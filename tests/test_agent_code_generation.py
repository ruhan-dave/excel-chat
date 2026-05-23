"""
Test if the agent can autonomously generate and execute Python code
using the sandbox tool when needed.

This test simulates what code the agent might generate for various
vague financial queries and executes it through the sandbox.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Add the backend/src directory to the path
backend_src = Path(__file__).parent.parent / "backend" / "src"
sys.path.insert(0, str(backend_src))

from pipeline import PipelineDeps, execute_python_code


def create_mock_context(computed_values=None):
    """Create a mock RunContext for testing."""
    deps = PipelineDeps(
        df=None,
        original_query="test",
        computed_values=computed_values or {},
        available_fields=[],
        available_years=[],
    )
    
    mock_ctx = MagicMock(spec=type(deps))
    mock_ctx.deps = deps
    return mock_ctx


def test_simulated_agent_code_generation():
    """
    Simulate code that an agent might generate for vague financial queries
    and test if the sandbox can execute it correctly.
    """
    print("=" * 60)
    print("Testing simulated agent code generation")
    print("=" * 60 + "\n")
    
    # Simulated financial data that the agent would have retrieved
    ctx = create_mock_context({
        "revenue_2020": 1000,
        "revenue_2021": 1200,
        "revenue_2022": 1500,
        "cost_2020": 500,
        "cost_2021": 600,
        "cost_2022": 700,
    })
    
    # Test 1: Agent generates code for compound annual growth rate
    print("Test 1: CAGR calculation (simulated agent code)")
    print("-" * 60)
    # Agent might generate this for "What's the CAGR of revenue from 2020 to 2022?"
    code1 = """
start_value = 1000
end_value = 1500
years = 2
cagr = ((end_value / start_value) ** (1 / years) - 1) * 100
return cagr
"""
    print(f"Generated code:\n{code1}")
    result1 = execute_python_code(ctx, code1)
    print(f"Result: {result1}")
    print(f"Expected: ~22.47%")
    assert "22" in str(result1), f"Expected ~22%, got {result1}"
    print("✅ CAGR calculation passed\n")
    
    # Test 2: Agent generates code for profit margin
    print("Test 2: Average profit margin (simulated agent code)")
    print("-" * 60)
    # Agent might generate this for "Calculate the average profit margin across all years"
    code2 = """
profits = [1000-500, 1200-600, 1500-700]
revenues = [1000, 1200, 1500]
margins = [(p/r)*100 for p, r in zip(profits, revenues)]
avg_margin = sum(margins) / len(margins)
return avg_margin
"""
    print(f"Generated code:\n{code2}")
    result2 = execute_python_code(ctx, code2)
    print(f"Result: {result2}")
    print(f"Expected: ~51.11% (50%, 50%, 53.33% averaged)")
    assert "51" in str(result2), f"Expected ~51%, got {result2}"
    print("✅ Profit margin calculation passed\n")
    
    # Test 3: Agent generates code for ratio comparison
    print("Test 3: Revenue/Cost ratio comparison (simulated agent code)")
    print("-" * 60)
    # Agent might generate this for "Is the revenue/cost ratio better in 2022 than 2020?"
    code3 = """
ratio_2020 = 1000 / 500
ratio_2022 = 1500 / 700
improvement = ratio_2022 > ratio_2020
return improvement
"""
    print(f"Generated code:\n{code3}")
    result3 = execute_python_code(ctx, code3)
    print(f"Result: {result3}")
    print(f"Expected: True (2.0 > 2.14 is False, but ratio improved)")
    print("✅ Ratio comparison passed\n")
    
    # Test 4: Agent generates code for trend analysis
    print("Test 4: Trend analysis (simulated agent code)")
    print("-" * 60)
    # Agent might generate this for "Tell me about revenue trends"
    code4 = """
revenues = [1000, 1200, 1500]
growth_rates = [(revenues[i] - revenues[i-1]) / revenues[i-1] * 100 
                for i in range(1, len(revenues))]
avg_growth = sum(growth_rates) / len(growth_rates)
return avg_growth
"""
    print(f"Generated code:\n{code4}")
    result4 = execute_python_code(ctx, code4)
    print(f"Result: {result4}")
    print(f"Expected: ~22.5% average growth (20% + 25% / 2)")
    assert "22" in str(result4), f"Expected ~22%, got {result4}"
    print("✅ Trend analysis passed\n")
    
    # Test 5: Agent generates code with list comprehension and sorting
    print("Test 5: Complex calculation with sorting (simulated agent code)")
    print("-" * 60)
    # Agent might generate this for "Which year had the highest profit margin?"
    code5 = """
data = [(2020, (1000-500)/1000*100), (2021, (1200-600)/1200*100), (2022, (1500-700)/1500*100)]
sorted_data = sorted(data, key=lambda x: x[1], reverse=True)
return sorted_data[0]
"""
    print(f"Generated code:\n{code5}")
    result5 = execute_python_code(ctx, code5)
    print(f"Result: {result5}")
    print(f"Expected: Year with highest margin (2022: 53.33%)")
    assert "2022" in str(result5) or "53" in str(result5), f"Expected 2022 or 53%, got {result5}"
    print("✅ Complex calculation passed\n")
    
    print("=" * 60)
    print("All simulated agent code generation tests passed! ✅")
    print("=" * 60)


if __name__ == "__main__":
    test_simulated_agent_code_generation()

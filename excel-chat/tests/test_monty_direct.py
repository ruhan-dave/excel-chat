"""
Direct test of pydantic-monty to understand its API.
"""

import asyncio
import pydantic_monty

# Test 1: Simple code with print
code1 = """
print(10 + 20)
"""

m1 = pydantic_monty.Monty(code1, script_name="test.py")

async def test1():
    output = await m1.run_async(inputs={})
    print(f"Test 1 output: {output}")

asyncio.run(test1())

# Test 2: Code with result variable
code2 = """
result = 10 + 20
"""

m2 = pydantic_monty.Monty(code2, script_name="test.py")

async def test2():
    output = await m2.run_async(inputs={})
    print(f"Test 2 output: {output}")

asyncio.run(test2())

# Test 3: Code with return statement
code3 = """
return 10 + 20
"""

m3 = pydantic_monty.Monty(code3, script_name="test.py")

async def test3():
    try:
        output = await m3.run_async(inputs={})
        print(f"Test 3 output: {output}")
    except Exception as e:
        print(f"Test 3 error: {e}")

asyncio.run(test3())

# Test 4: Code with explicit print of result
code4 = """
result = 10 + 20
print(result)
"""

m4 = pydantic_monty.Monty(code4, script_name="test.py")

async def test4():
    output = await m4.run_async(inputs={})
    print(f"Test 4 output: {output}")

asyncio.run(test4())

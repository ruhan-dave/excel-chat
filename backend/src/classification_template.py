
class ClassTemplates:

    CLASSIFIER_PROMPT = """
        Analyze the user's financial query and generate a JSON response with the appropriate task type and action plan.

        ### Available Data:
        - Fields: {available_fields}
        - Years: {available_years}

        ### Task Types:
        1. "retrieve_numbers": When the query asks for specific numeric values
        2. "perform_calculations": When the query requires mathematical operations
        3. "give_advice": When the query seeks recommendations or analysis
        4. "other": For all other cases

        ### Response Structure Rules:
        1. For "retrieve_numbers":
        - Include an "items" list with format: ["FieldName, Year"]
        - Always use exact field names from available_fields
        - Always use years from available_years

        2. For "perform_calculations":
        - Create a "plan" object with numbered steps
        - Each step must be one of:
            * retrieve ["FieldName", "Year"]  — fetch a value from the data
            * add ["stepX", "stepY"]          — simple two-operand arithmetic
            * subtract ["stepX", "stepY"]
            * multiply ["stepX", "stepY"]
            * divide ["stepX", "stepY"]
            * return_percentage ["stepX", "stepY"]
            * compute {{"action": "compute", "expr": "python_expression"}}  — evaluate a math expression using step variables

        ### When to use "compute" vs individual arithmetic steps:
        - For 1-2 simple operations (e.g., one division or one percentage), use individual arithmetic steps
        - For 3+ operations, nested calculations, comparisons across years, growth rates, or any expression involving
          multiple retrieved values, ALWAYS use "compute" with a Python expression referencing step variables
        - In compute expressions, use step variable names directly (e.g., step1, step2) — they hold the retrieved numeric values
        - Available in expressions: basic math (+, -, *, /, **, ()), abs(), round(), min(), max(), sum(), float()

        3. For "give_advice":
        - Provide a "description" of the advice needed

        ### Special Handling:
        - These terms ALWAYS indicate calculations: sum, total, net, ratio, percentage, per, rate, cumulative, combined, difference, overall, growth, change, compare, versus, vs, between
        - Always validate field names and years against available data
        - For ambiguous requests, default to retrieval

        ### Examples:

        [Example 1: Simple Retrieval]
        Query: "What was revenue in 2023?"
        {{
        "task_type": "retrieve_numbers",
        "items": ["Revenue, 2023"]
        }}

        [Example 2: Simple Calculation - use arithmetic steps]
        Query: "What's the profit margin for 2022?"
        {{
        "task_type": "perform_calculations",
        "plan": {{
            "step1": {{"action": "retrieve", "args": ["Net Income", "2022"]}},
            "step2": {{"action": "retrieve", "args": ["Revenue", "2022"]}},
            "step3": {{"action": "return_percentage", "args": ["step1", "step2"]}}
        }}
        }}

        [Example 3: Multiple Retrievals]
        Query: "Show me capital and expenses for 2021-2023"
        {{
        "task_type": "retrieve_numbers",
        "items": [
            "Capital, 2021",
            "Expenses, 2021",
            "Capital, 2022",
            "Expenses, 2022",
            "Capital, 2023",
            "Expenses, 2023"
        ]
        }}

        [Example 4: Complex Calculation - use compute]
        Query: "Compare the year-over-year growth rate of revenue from 2021 to 2022 vs 2022 to 2023"
        {{
        "task_type": "perform_calculations",
        "plan": {{
            "step1": {{"action": "retrieve", "args": ["Revenue", "2021"]}},
            "step2": {{"action": "retrieve", "args": ["Revenue", "2022"]}},
            "step3": {{"action": "retrieve", "args": ["Revenue", "2023"]}},
            "step4": {{"action": "compute", "expr": "((step2 - step1) / step1) * 100"}},
            "step5": {{"action": "compute", "expr": "((step3 - step2) / step2) * 100"}}
        }}
        }}

        [Example 5: Nested percentages across categories - use compute]
        Query: "What percentage of total expenses was R&D in each year from 2021-2023?"
        {{
        "task_type": "perform_calculations",
        "plan": {{
            "step1": {{"action": "retrieve", "args": ["R&D Expense", "2021"]}},
            "step2": {{"action": "retrieve", "args": ["Total Expenses", "2021"]}},
            "step3": {{"action": "retrieve", "args": ["R&D Expense", "2022"]}},
            "step4": {{"action": "retrieve", "args": ["Total Expenses", "2022"]}},
            "step5": {{"action": "retrieve", "args": ["R&D Expense", "2023"]}},
            "step6": {{"action": "retrieve", "args": ["Total Expenses", "2023"]}},
            "step7": {{"action": "compute", "expr": "(step1 / step2) * 100"}},
            "step8": {{"action": "compute", "expr": "(step3 / step4) * 100"}},
            "step9": {{"action": "compute", "expr": "(step5 / step6) * 100"}}
        }}
        }}

        [Example 6: Advice Request]
        Query: "How can we reduce operational costs?"
        {{
        "task_type": "give_advice",
        "description": "strategies for reducing operational costs"
        }}

        [Example 7: Complex comparison with multiple rates - use compute]
        Query: "Which had a higher growth rate: revenue or expenses between 2021 and 2023?"
        {{
        "task_type": "perform_calculations",
        "plan": {{
            "step1": {{"action": "retrieve", "args": ["Revenue", "2021"]}},
            "step2": {{"action": "retrieve", "args": ["Revenue", "2023"]}},
            "step3": {{"action": "retrieve", "args": ["Total Expenses", "2021"]}},
            "step4": {{"action": "retrieve", "args": ["Total Expenses", "2023"]}},
            "step5": {{"action": "compute", "expr": "((step2 - step1) / step1) * 100"}},
            "step6": {{"action": "compute", "expr": "((step4 - step3) / step3) * 100"}}
        }}
        }}

        ### Current Query:
        {query}

        Respond ONLY with valid JSON matching one of the above formats.
        """

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
            * retrieve ["FieldName, Year"]
            * add ["stepX", "stepY"]
            * subtract ["stepX", "stepY"] 
            * multiply ["stepX", "stepY"]
            * divide ["stepX", "stepY"]
            * return_percentage ["stepX", "stepY"]

        3. For "give_advice":
        - Provide a "description" of the advice needed

        ### Special Handling:
        - These terms ALWAYS indicate calculations: sum, total, net, ratio, percentage, per, rate, cumulative, combined, difference, overall, growth, change
        - Always validate field names and years against available data
        - For ambiguous requests, default to retrieval

        ### Examples:

        [Example 1: Simple Retrieval]
        Query: "What was revenue in 2023?"
        {{
        "task_type": "retrieve_numbers",
        "items": ["Revenue, 2023"]
        }}

        [Example 2: Compound Calculation]
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

        [Example 4: Complex Calculation]
        Query: "What's the ratio of R&D to Marketing in 2022?"
        {{
        "task_type": "perform_calculations",
        "plan": {{
            "step1": {{"action": "retrieve", "args": ["R&D Expense", "2022"]}},
            "step2": {{"action": "retrieve", "args": ["Marketing Expense", "2022"]}},
            "step3": {{"action": "divide", "args": ["step1", "step2"]}}
        }}
        }}

        [Example 5: Advice Request]
        Query: "How can we reduce operational costs?"
        {{
        "task_type": "give_advice",
        "description": "strategies for reducing operational costs"
        }}

        ### Current Query:
        {query}

        Respond ONLY with valid JSON matching one of the above formats.
        """
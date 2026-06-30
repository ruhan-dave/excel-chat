
class ClassTemplates:

    CLASSIFIER_PROMPT = """
        Analyze the user's financial query and generate a JSON response with the appropriate task type and action plan.

        ### Available Data (Multi-Sheet):
        {sheet_context}

        ### Task Types:
        1. "retrieve_numbers": When the query asks for specific numeric values
        2. "perform_calculations": When the query requires mathematical operations
        3. "give_advice": When the query seeks recommendations or analysis
        4. "other": For all other cases

        ### Response Structure Rules:
        1. For "retrieve_numbers":
        - Include an "items" list with format: ["FieldName, Year"] or ["SheetName, FieldName, Year"] for sheet-specific retrieval
        - Always use exact field names from the sheet context
        - Always use years from the sheet context

        2. For "perform_calculations":
        - Create a "plan" object with numbered steps
        - Each step must be one of:
            * retrieve ["FieldName, Year"] — search all sheets
            * retrieve ["SheetName, FieldName, Year"] — search specific sheet
            * add ["stepX", "stepY", ...] — sum of N values
            * subtract ["stepX", "stepY"] — difference of two values
            * multiply ["stepX", "stepY", ...] — product of N values
            * divide ["stepX", "stepY"] — quotient of two values
            * return_percentage ["stepX", "stepY"] — (stepX / stepY) * 100
            * sqrt ["stepX"] — square root
            * power ["stepX", "stepY"] — stepX raised to stepY
            * log ["stepX", "stepY"] — log base stepY of stepX
            * exp ["stepX"] — e raised to stepX
            * abs ["stepX"] — absolute value
            * negate ["stepX"] — negation
            * max ["stepX", "stepY", ...] — maximum of N values
            * min ["stepX", "stepY", ...] — minimum of N values
            * average ["stepX", "stepY", ...] — mean of N values
            * median ["stepX", "stepY", ...] — median of N values
            * stdev ["stepX", "stepY", ...] — standard deviation of N values
            * yoy_growth ["stepX", "stepY"] — year-over-year growth rate (%)
            * cagr ["stepX", "stepY", "stepZ"] — CAGR: [end_value, start_value, num_years]
            * ratio ["stepX", "stepY"] — simple ratio
            * percentage_change ["stepX", "stepY"] — percentage change from old to new
            * difference ["stepX", "stepY"] — absolute difference
            * compute ["natural language description"] — for complex calculations

        3. For "give_advice":
        - Provide a "description" of the advice needed

        ### Special Handling:
        - These terms ALWAYS indicate calculations: sum, total, net, ratio, percentage, per, rate, cumulative, combined, difference, overall, growth, change, average, mean, median, standard deviation, CAGR, compound, square root, power, exponent, log, minimum, maximum, stability, variability, trend, compare, comparison, larger, smaller, increase, decrease
        - Always validate field names and years against available data
        - For ambiguous requests, default to retrieval
        - Prefer named operations over compute for simple math
        - Use compute only for complex multi-step formulas that can't be expressed with named operations
        - When a query references data from multiple sheets, use sheet-specific retrieval with ["SheetName", "FieldName", "Year"]
        - Sheets in the same schema group have consistent columns and can be compared directly

        ### CRITICAL — retrieve_batch usage:
        - When a calculation needs 2+ years for the SAME field, ALWAYS use retrieve_batch instead of multiple retrieve steps.
          WRONG: step1: retrieve ["Revenue", "2018"], step2: retrieve ["Revenue", "2019"], step3: retrieve ["Revenue", "2020"]
          RIGHT: step1: retrieve_batch ["Revenue", "2018", "2019", "2020"], step2: compute ["Calculate the average of the retrieved values"]
        - For average/mean queries: use retrieve_batch to get all years in one step, then use the "average" named operation with the retrieved values.
        - For stability/variability queries (standard deviation, coefficient of variation): use retrieve_batch for each field, then use "stdev" named operation.
        - For trend analysis over multiple years: use retrieve_batch to get all years, then use compute for year-over-year differences.
        - For growth rate comparisons between two fields: use retrieve_batch for each field (2 retrieve_batch calls), then compute the CAGR or growth rate.
        - retrieve_batch returns a JSON object like {{"2018": 1500.0, "2019": 1200.0}}. When passing its result to a named operation,
          use compute to extract the values: e.g. compute ["Calculate average of step1 values: sum(step1.values())/len(step1)"].

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

        [Example 3b: Average over multiple years — MUST use perform_calculations with retrieve_batch]
        Query: "What's the average annual expense on grants to foreign governments between 2015-2020?"
        {{
        "task_type": "perform_calculations",
        "plan": {{
            "step1": {{"action": "retrieve_batch", "args": ["Grants to foreign governments", "2015", "2016", "2017", "2018", "2019", "2020"]}},
            "step2": {{"action": "compute", "args": ["Calculate the average of the values in step1: sum(step1.values()) / len(step1)"]}}
        }}
        }}

        [Example 3c: Stability comparison over multiple years — MUST use perform_calculations with retrieve_batch]
        Query: "Which showed greater stability over 2012-2021: wages and salaries or employers' social contributions?"
        {{
        "task_type": "perform_calculations",
        "plan": {{
            "step1": {{"action": "retrieve_batch", "args": ["Wages and salaries", "2012", "2013", "2014", "2015", "2016", "2017", "2018", "2019", "2020", "2021"]}},
            "step2": {{"action": "retrieve_batch", "args": ["Employers' social contributions", "2012", "2013", "2014", "2015", "2016", "2017", "2018", "2019", "2020", "2021"]}},
            "step3": {{"action": "compute", "args": ["Calculate the standard deviation of step1 values and step2 values, then identify which has lower standard deviation (more stable)"]}}
        }}
        }}

        [Example 3d: Growth rate comparison between two fields — MUST use retrieve_batch]
        Query: "Compare the growth rates of social security benefits versus social assistance benefits from 2017 to 2021"
        {{
        "task_type": "perform_calculations",
        "plan": {{
            "step1": {{"action": "retrieve_batch", "args": ["Social security benefits", "2017", "2021"]}},
            "step2": {{"action": "retrieve_batch", "args": ["Social assistance benefits", "2017", "2021"]}},
            "step3": {{"action": "compute", "args": ["Calculate CAGR for step1: (step1['2021']/step1['2017'])^(1/4)-1, and CAGR for step2: (step2['2021']/step2['2017'])^(1/4)-1, then compute the difference"]}}
        }}
        }}

        [Example 4: Ratio Calculation]
        Query: "What's the ratio of R&D to Marketing in 2022?"
        {{
        "task_type": "perform_calculations",
        "plan": {{
            "step1": {{"action": "retrieve", "args": ["R&D Expense", "2022"]}},
            "step2": {{"action": "retrieve", "args": ["Marketing Expense", "2022"]}},
            "step3": {{"action": "ratio", "args": ["step1", "step2"]}}
        }}
        }}

        [Example 5: YoY Growth]
        Query: "What's the year-over-year growth of Revenue from 2022 to 2023?"
        {{
        "task_type": "perform_calculations",
        "plan": {{
            "step1": {{"action": "retrieve", "args": ["Revenue", "2023"]}},
            "step2": {{"action": "retrieve", "args": ["Revenue", "2022"]}},
            "step3": {{"action": "yoy_growth", "args": ["step1", "step2"]}}
        }}
        }}

        [Example 6: CAGR]
        Query: "What's the CAGR of Expenses from 2018 to 2023?"
        {{
        "task_type": "perform_calculations",
        "plan": {{
            "step1": {{"action": "retrieve", "args": ["Expense", "2023"]}},
            "step2": {{"action": "retrieve", "args": ["Expense", "2018"]}},
            "step3": {{"action": "cagr", "args": ["step1", "step2", "5"]}}
        }}
        }}

        [Example 7: Average of Multiple Fields]
        Query: "What's the average of Wages, Social Contributions, and Use of Goods in 2022?"
        {{
        "task_type": "perform_calculations",
        "plan": {{
            "step1": {{"action": "retrieve", "args": ["Wages and salaries", "2022"]}},
            "step2": {{"action": "retrieve", "args": ["Employers' social contributions", "2022"]}},
            "step3": {{"action": "retrieve", "args": ["Use of goods and services", "2022"]}},
            "step4": {{"action": "average", "args": ["step1", "step2", "step3"]}}
        }}
        }}

        [Example 8: Complex Calculation via Compute]
        Query: "What's the square root of the sum of all expenses in 2023 divided by revenue?"
        {{
        "task_type": "perform_calculations",
        "plan": {{
            "step1": {{"action": "retrieve", "args": ["Expense", "2023"]}},
            "step2": {{"action": "retrieve", "args": ["Revenue", "2023"]}},
            "step3": {{"action": "compute", "args": ["Calculate math.sqrt(step1 / step2)"]}}
        }}
        }}

        [Example 9: Cross-Sheet Comparison]
        Query: "Compare the Revenue of 2022 between Sheet1 and Sheet2"
        {{
        "task_type": "perform_calculations",
        "plan": {{
            "step1": {{"action": "retrieve", "args": ["Sheet1", "Revenue", "2022"]}},
            "step2": {{"action": "retrieve", "args": ["Sheet2", "Revenue", "2022"]}},
            "step3": {{"action": "subtract", "args": ["step1", "step2"]}}
        }}
        }}

        [Example 10: Advice Request]
        Query: "How can we reduce operational costs?"
        {{
        "task_type": "give_advice",
        "description": "strategies for reducing operational costs"
        }}

        ### Current Query:
        {query}

        Respond ONLY with valid JSON matching one of the above formats.
        """
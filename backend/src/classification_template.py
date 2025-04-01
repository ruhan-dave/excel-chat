
class ClassTemplates:

    CLASSIFIER_PROMPT = """
        Generate a JSON containing primary task type from your analysis of the user's query. 

        Task Types:
        - retrieve_numbers
        - give_advice
        - perform_calculations
        - other

        If the task is "retrieve_numbers", return "items" (a list of strings) representing what to retrieve.
        If the task is "give_advice", return "description" (string) describing what the user wants to advice on. 
        If the task is "perform_calculations", return "plan" (json object) containing high level steps to perform the necessary calculations.
        Output the plan as a JSON object with key "steps". The value of "steps" should be an array of strings.

        Ensure not to confuse calculations with retrieval for certain words like 'ratio', 'total', 'sum', 'net',
        'percentage', 'per', 'rate', 'cumulative', 'combined', 'difference', 'overall', 'growth', 'change', which are calculations.
        Be sure to translate them into "add", "subtract", "multiply", and "divide" operations. For example, "add ['step2, step3']" means add the results of step2 and step3.

        NOTE: the user query may not contain the correct year or field name. 
        Be sure to identify the right field from {available_fields} and year from {available_years}.

        (Example 1):
        User Query: What was the revenue and capital in 2023?
        Output:
        {{
        "task_type": "retrieve_numbers",
        "items": ["revenue, 2023", "capital, 2023"]
        }}

        (Example 2):
        User Query: Give me advice on improving my company's financial performance for investors?
        Output:
        {{
            "task_type": "give_advice",
            "description": "evaluate company's financial performance for investors" 
        }}

        (Example 3):
        User Query: What percentage of 2022 expenses paid to employee wages?
        Output:
        {{
            "task_type": "perform_calculations",
            "plan": {{
                "step1": "retrieve ['Expense, 2022']",
                "step2: "retrieve ['Expense, 2022']",
                "step3": "divide ['step1, step2']"
                "step4": "multiply ['step3, 100']"
            }}
        }}
        """
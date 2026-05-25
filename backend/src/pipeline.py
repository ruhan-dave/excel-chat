from llama_index.core.query_pipeline import QueryPipeline, CustomQueryComponent
from llama_index.core.llms import ChatMessage, MessageRole
# from llama_index.llms.openai import OpenAI  # Commented out for Cohere migration
# from llama_index.llms.cohere import Cohere  # LlamaIndex Cohere is outdated
from openai import OpenAI
from llama_index.core.prompts import PromptTemplate
from pydantic import PrivateAttr
from typing import Any

import pandas as pd
import json
import re
import os

# -- Arithmetic Functions --
add = lambda a, b: a + b
multiply = lambda a, b: a * b
subtract = lambda a, b: a - b
divide = lambda a, b: a / b if (a is not None and b not in [0, None]) else None
return_percentage = lambda a, b: (a / b) * 100 if (a is not None and b not in [0, None]) else None

# -- Safe Compute Helpers --
_SAFE_BUILTINS = {
    "abs": abs, "round": round, "min": min, "max": max,
    "sum": sum, "len": len, "sorted": sorted,
    "float": float, "int": int,
}

def _safe_eval(expr: str, variables: dict[str, float]) -> Any:
    """Evaluate a math expression with only retrieved step values and safe builtins."""
    allowed = {**_SAFE_BUILTINS, **variables}
    try:
        return eval(expr, {"__builtins__": {}}, allowed)
    except Exception as e:
        print(f"⚠️ Compute expression error: {e} | expr={expr} | vars={variables}")
        return None

# -- Helper Functions --
def extract_vals_from_df(df, col, year):
    return float(df.loc[col, year]) if col in df.index and year in df.columns else None

def retrieving(sheets, items):
    print(f"Attempting to retrieve: {items}")
    if len(items) == 2:
        col, year = items[0].strip(), items[1].strip()
        print(f"Looking for: '{col}' in index ({col in df.index}), '{year}' in columns ({year in df.columns})")
        if col in df.index and year in df.columns:
            val = df.loc[col, year]
            print(f"✅ Found value: {val}")
            return [float(val)]
    print("❌ Value not found")
    return [None]

def executing_plan_from_json(sheets, json_str):
    try:
        parsed = json.loads(json_str)
        print("Parsed JSON:", parsed)
        
        # Extract plan from different possible JSON structures
        if 'plan' in parsed:
            plan = parsed['plan']
        elif 'items' in parsed:
            plan = {
                f"step{i+1}": {"action": "retrieve", "args": item.split(',')}
                for i, item in enumerate(parsed['items'])
            }
        else:
            raise ValueError("Invalid JSON format - missing 'plan' or 'items'")
        
        context = {}
        computed_values = {}  # Store computed step values

        for step_name, instruction in plan.items():
            print(f"Processing step {step_name}: {instruction}")
            
            # Handle string instructions (legacy format)
            if isinstance(instruction, str):
                instruction = instruction.strip()
                if instruction.lower().startswith("retrieve"):
                    args_part = instruction[8:].strip().strip("[]'\"")
                    parts = [p.strip().strip("'\"") for p in args_part.split(',')]
                    if len(parts) >= 2:
                        col, year = parts[0], parts[1]
                        values = retrieving(df, [col, year])
                        context[step_name] = values[0] if values else None
                        computed_values[step_name] = context[step_name]
                    else:
                        print(f"⚠️ Could not extract 2 arguments from: {instruction}")
                        context[step_name] = None
                continue
            
            # Handle structured JSON instructions
            if isinstance(instruction, dict):
                action = instruction.get("action", "").lower()
                args = instruction.get("args", [])
                
                # Normalize arguments - split combined strings
                normalized_args = []
                for arg in args:
                    if isinstance(arg, str) and ',' in arg:
                        normalized_args.extend([x.strip() for x in arg.split(',')])
                    else:
                        normalized_args.append(arg.strip() if isinstance(arg, str) else arg)
                
                # Retrieve operation
                if action == "retrieve":
                    if len(normalized_args) >= 2:
                        values = retrieving(sheets, normalized_args)
                        context[step_name] = values[0] if values else None
                        computed_values[step_name] = context[step_name]
                    else:
                        print(f"⚠️ Invalid retrieve args: {args} (normalized: {normalized_args})")
                        context[step_name] = None
                
                # Compute operation - evaluate a Python expression using step values
                elif action == "compute":
                    expr = instruction.get("expr", "")
                    if expr:
                        result = _safe_eval(expr, computed_values)
                        context[step_name] = result
                        computed_values[step_name] = result
                    else:
                        print(f"⚠️ Compute action missing 'expr' field: {instruction}")
                        context[step_name] = None

                # Calculation operations
                elif action in ["add", "subtract", "multiply", "divide", "return_percentage"]:
                    if len(args) == 2:
                        arg1, arg2 = args
                        val1 = computed_values.get(arg1.strip()) if isinstance(arg1, str) and arg1.startswith('step') else float(arg1) if str(arg1).replace('.','',1).isdigit() else None
                        val2 = computed_values.get(arg2.strip()) if isinstance(arg2, str) and arg2.startswith('step') else float(arg2) if str(arg2).replace('.','',1).isdigit() else None
                        
                        if val1 is not None and val2 is not None:
                            if action == "add":
                                context[step_name] = add(val1, val2)
                            elif action == "subtract":
                                context[step_name] = subtract(val1, val2)
                            elif action == "multiply":
                                context[step_name] = multiply(val1, val2)
                            elif action == "divide":
                                context[step_name] = divide(val1, val2)
                            elif action == "return_percentage":
                                context[step_name] = return_percentage(val1, val2)
                            computed_values[step_name] = context[step_name]
                        else:
                            print(f"⚠️ Missing values for calculation: {arg1}={val1}, {arg2}={val2}")
                            context[step_name] = None
                    else:
                        print(f"⚠️ Invalid calculation args: {args}")
                        context[step_name] = None
                else:
                    print(f"⚠️ Unsupported action: {action}")
                    context[step_name] = None
                continue
            
            print(f"⚠️ Unknown instruction format for step {step_name}")
            context[step_name] = None

        print("✅ Final context:", context)
        return context

    except Exception as e:
        print(f"❌ Error in execution: {str(e)}")
        raise

# -- Components --
class ExecutePlanComponent(CustomQueryComponent):
    _sheets: dict[str, pd.DataFrame] = PrivateAttr()

    def __init__(self, sheets: dict[str, pd.DataFrame]):
        super().__init__()
        self._sheets = sheets

    def _run_component(self, **kwargs):
        print("ExecutePlanComponent received:", kwargs)  # Debug input
        try:
            result = executing_plan_from_json(self._sheets, kwargs["json_str"])
            print("✅ Execution result:", result)
            return {"executed_plan": result}
        except Exception as e:
            print("❌ Execution failed:", str(e))
            raise

    @property
    def _input_keys(self): return {"json_str"}
    @property
    def _output_keys(self): return {"executed_plan"}

class ClassifyStepComponent(CustomQueryComponent):
    _llm: Any = PrivateAttr()  # OpenAI-compatible client (OpenRouter)
    _template: PromptTemplate = PrivateAttr()
    _sheets: dict[str, pd.DataFrame] = PrivateAttr()

    def __init__(self, llm_client, template: PromptTemplate, sheets: dict[str, pd.DataFrame]):
        super().__init__()
        self._llm = llm_client
        self._template = template
        self._sheets = sheets

    def _run_component(self, **kwargs):
        # Build per-sheet metadata for the prompt
        sheet_descriptions = []
        all_fields = set()
        all_years = set()
        for sheet_name, df in self._sheets.items():
            fields = [str(f) for f in df.index]
            years = [str(y) for y in df.columns]
            all_fields.update(fields)
            all_years.update(years)
            sheet_descriptions.append(
                f"Sheet '{sheet_name}': fields = [{', '.join(fields)}], years = [{', '.join(years)}]"
            )

        # 1. Format the prompt with safe string conversion
        formatted_prompt = self._template.format(
            query=kwargs["query_str"],
            available_sheets="\n".join(sheet_descriptions),
            available_fields=", ".join(sorted(all_fields)),
            available_years=", ".join(sorted(all_years))
        )
        
        print(f"Formatted prompt: {formatted_prompt[:500]}...")  # Debug
        
        # 2. Get response from LLM using OpenRouter with sufficient timeout
        try:
            response = self._llm.chat.completions.create(
                model="deepseek/deepseek-v4-flash",
                messages=[{"role": "user", "content": formatted_prompt}],
                temperature=0.1,
                timeout=60.0  # 60 second timeout for classification
            )
            print(f"LLM response: {response}")  # Debug
        except Exception as e:
            print(f"❌ LLM API error: {str(e)}")
            raise
        
        # 3. Process LLM response
        try:
            content = response.choices[0].message.content
            print(f"Raw content type: {type(content)}")
            print(f"Raw content: {content}")
            
            # Extract JSON from markdown if present
            if "```json" in content:
                json_match = re.search(r'```json\n(.*?)\n```', content, re.DOTALL)
                if json_match:
                    content = json_match.group(1)
            elif "```" in content:
                json_match = re.search(r'```\n(.*?)\n```', content, re.DOTALL)
                if json_match:
                    content = json_match.group(1)
            
            # Ensure valid JSON
            parsed = json.loads(content)
            print("Raw LLM output:\n", parsed)
            
            # Transform if needed (same as before)
            if "plan" not in parsed:
                if "items" in parsed:
                    parsed = {
                        "plan": {
                            f"step{i+1}": f"Retrieve '{item.split(',')[0].strip()}', '{item.split(',')[1].strip()}'"
                            for i, item in enumerate(parsed["items"])
                        }
                    }
            
            return {"json_str": json.dumps(parsed)}
            
        except Exception as e:
            raise ValueError(f"Failed to process LLM response: {str(e)}")

    @property
    def _input_keys(self) -> set:
        return {"query_str"}
        
    @property
    def _output_keys(self) -> set:
        return {"json_str"}
    
# -- Build Pipeline Function --
def build_query_pipeline(llm_client, sheets, classification_template):
    classify = ClassifyStepComponent(llm_client, classification_template, sheets)
    execute = ExecutePlanComponent(sheets)
    
    pipeline = QueryPipeline()
    pipeline.add_modules({
        "classify": classify,
        "execute": execute
    })
    pipeline.add_chain(["classify", "execute"])
    
    return pipeline

# -- Response Generation Function --
def generate_user_friendly_response(llm_client, original_query, json_result):
    """
    Generate a user-friendly response from the JSON result using OpenRouter.
    Implements retry logic that only retries on actual failures, not timeouts.
    """
    max_retries = 1
    timeout_seconds = 60  # 60 seconds - sufficient time for LLM processing
    
    for attempt in range(max_retries + 1):
        try:
            print(f"Starting generate_user_friendly_response (attempt {attempt + 1}/{max_retries + 1})...")
            
            # Create a prompt for the LLM to generate a natural language response
            response_prompt = f"""
            Based on the user's question and the calculated results, provide a clear, natural language response.

            User's Question: {original_query}

            Calculated Results (JSON):
            {json.dumps(json_result, indent=2)}

            Instructions:
            1. Provide a clear, conversational response that directly answers the user's question
            2. Include the specific numerical values with proper formatting (currency symbols, percentages, etc.)
            3. Briefly explain how the answer was derived by referencing the calculation steps
            4. Use a patient, clear, professional tone
            5. If multiple values were calculated, present them clearly

            Response:
            """
            
            print("Calling LLM API for natural language response...")
            response = llm_client.chat.completions.create(
                model="deepseek/deepseek-v4-flash",
                messages=[{"role": "user", "content": response_prompt}],
                temperature=0.3,
                max_tokens=500,
                timeout=timeout_seconds
            )
            print("LLM API response received")
            
            content = response.choices[0].message.content
            print(f"✅ Friendly response generated: {content[:100]}...")
            
            # Extract content from markdown if present
            if "```" in content:
                # Remove markdown code blocks
                content = re.sub(r'```(?:json)?\n?', '', content)
                content = re.sub(r'```$', '', content).strip()
            
            return content
            
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)
            
            # Check if it's a timeout error
            is_timeout = 'timeout' in error_msg.lower() or 'timed out' in error_msg.lower()
            
            print(f"❌ Error generating user-friendly response (attempt {attempt + 1}): {error_type}: {error_msg}")
            
            # Only retry if it's NOT a timeout error and we haven't exhausted retries
            if not is_timeout and attempt < max_retries:
                print(f"Retrying due to non-timeout error...")
                import time
                time.sleep(2)  # Brief pause before retry
                continue
            elif is_timeout:
                print(f"Timeout error - will not retry (timeout indicates sufficient wait time)")
                return f"Response generation took longer than expected. Based on the calculations: {json_result}"
            else:
                print(f"❌ Exhausted retries for non-timeout error")
                return f"Based on the calculations: {json_result}"
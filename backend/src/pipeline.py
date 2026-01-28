from llama_index.core.query_pipeline import QueryPipeline, CustomQueryComponent
from llama_index.core.llms import ChatMessage, MessageRole
# from llama_index.llms.openai import OpenAI  # Commented out for Cohere migration
# from llama_index.llms.cohere import Cohere  # LlamaIndex Cohere is outdated
import cohere
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

# -- Helper Functions --
def extract_vals_from_df(df, col, year):
    return float(df.loc[col, year]) if col in df.index and year in df.columns else None

def retrieving(df, items):
    print(f"🔍 Attempting to retrieve: {items}")
    if len(items) == 2:
        col, year = items[0].strip(), items[1].strip()
        print(f"🔍 Looking for: '{col}' in index ({col in df.index}), '{year}' in columns ({year in df.columns})")
        if col in df.index and year in df.columns:
            val = df.loc[col, year]
            print(f"✅ Found value: {val}")
            return [float(val)]
    print("❌ Value not found")
    return [None]

def executing_plan_from_json(df, json_str):
    try:
        parsed = json.loads(json_str)
        print("🔨 Parsed JSON:", parsed)
        
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
            print(f"🔧 Processing step {step_name}: {instruction}")
            
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
                        col, year = normalized_args[0], normalized_args[1]
                        values = retrieving(df, [col, year])
                        context[step_name] = values[0] if values else None
                        computed_values[step_name] = context[step_name]
                    else:
                        print(f"⚠️ Invalid retrieve args: {args} (normalized: {normalized_args})")
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
    _df: pd.DataFrame = PrivateAttr()

    def __init__(self, df):
        super().__init__()
        self._df = df

    def _run_component(self, **kwargs):
        print("⚡ ExecutePlanComponent received:", kwargs)  # Debug input
        try:
            result = executing_plan_from_json(self._df, kwargs["json_str"])
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
    # _llm: OpenAI = PrivateAttr()  # Commented out for Cohere migration
    # _llm: Cohere = PrivateAttr()  # LlamaIndex Cohere is outdated
    _llm: Any = PrivateAttr()  # Using Cohere SDK ClientV2
    _template: PromptTemplate = PrivateAttr()
    _df: pd.DataFrame = PrivateAttr()

    # def __init__(self, openai_model, template: PromptTemplate, df: pd.DataFrame):  # Commented out for Cohere migration
    def __init__(self, cohere_client, template: PromptTemplate, df: pd.DataFrame):
        super().__init__()
        self._llm = cohere_client  # This is now a cohere.ClientV2 instance
        self._template = template
        self._df = df

    def _run_component(self, **kwargs):
        # 1. Format the prompt with safe string conversion
        formatted_prompt = self._template.format(
            query=kwargs["query_str"],
            available_fields=", ".join(str(field) for field in self._df.index),
            available_years=", ".join(str(year) for year in self._df.columns)
        )
        
        print(f"🔍 Formatted prompt: {formatted_prompt[:500]}...")  # Debug
        
        # 2. Get response from Cohere using SDK directly
        try:
            response = self._llm.chat(
                model="command-r7b-12-2024",
                messages=[{"role": "user", "content": formatted_prompt}],
                temperature=0.1
            )
            print(f"🤖 Cohere response: {response}")  # Debug
        except Exception as e:
            print(f"❌ Cohere API error: {str(e)}")
            raise
        
        # 3. Process Cohere response
        try:
            content = response.message.content[0].text
            print(f"📝 Raw content type: {type(content)}")
            print(f"📝 Raw content: {content}")
            
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
            print("📥 Raw LLM output:\n", parsed)
            
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
# def build_query_pipeline(openai_model, df, classification_template):  # Commented out for Cohere migration
def build_query_pipeline(cohere_client, df, classification_template):
    classify = ClassifyStepComponent(cohere_client, classification_template, df)
    execute = ExecutePlanComponent(df)
    
    pipeline = QueryPipeline()
    pipeline.add_modules({
        "classify": classify,
        "execute": execute
    })
    pipeline.add_chain(["classify", "execute"])
    
    return pipeline

# -- Response Generation Function --
# def generate_user_friendly_response(openai_model, original_query, json_result):  # Commented out for Cohere migration
def generate_user_friendly_response(cohere_client, original_query, json_result):
    """
    Generate a user-friendly response from the JSON result using Cohere SDK
    """
    try:
        print("🚀 Starting generate_user_friendly_response...")
        # Create a prompt for Cohere to generate a natural language response
        response_prompt = f"""
Based on the user's question and the calculated results, provide a clear, natural language response.

User's Question: {original_query}

Calculated Results (JSON):
{json.dumps(json_result, indent=2)}

Instructions:
1. Provide a clear, conversational response that directly answers the user's question
2. Include the specific numerical values with proper formatting (currency symbols, percentages, etc.)
3. Briefly explain how the answer was derived by referencing the calculation steps
4. Use a friendly, professional tone
5. If multiple values were calculated, present them clearly

Response:
"""
        
        print("📤 Calling Cohere API for friendly response...")
        response = cohere_client.chat(
            model="command-r7b-12-2024",
            messages=[{"role": "user", "content": response_prompt}],
            temperature=0.3,
            max_tokens=500
        )
        print("📥 Cohere API response received")
        
        content = response.message.content[0].text
        print(f"✅ Friendly response generated: {content[:100]}...")
        
        # Extract content from markdown if present
        if "```" in content:
            # Remove markdown code blocks
            content = re.sub(r'```(?:json)?\n?', '', content)
            content = re.sub(r'```$', '', content).strip()
        
        return content
        
    except Exception as e:
        print(f"Error generating user-friendly response: {str(e)}")
        return f"Based on the calculations: {json_result}"
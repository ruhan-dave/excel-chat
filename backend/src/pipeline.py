from llama_index.core.query_pipeline import QueryPipeline, CustomQueryComponent
from llama_index.core.llms import ChatMessage, MessageRole
from llama_index.llms.cohere import Cohere
from llama_index.core.prompts import PromptTemplate
from pydantic import PrivateAttr

import pandas as pd
import json
import re

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

# -- Plan Execution Logic --
def executing_plan_from_json(df, json_str):
    try:
        parsed = json.loads(json_str)
        print("🔨 Parsed JSON:", parsed)
        
        # Extract plan from different possible JSON structures
        if 'plan' in parsed:
            plan = parsed['plan']
        elif 'items' in parsed:
            plan = {
                f"step{i+1}": f"Retrieve {item.strip()}"
                for i, item in enumerate(parsed['items'])
            }
        else:
            raise ValueError("Invalid JSON format - missing 'plan' or 'items'")
        
        context = {}

        for step_name, instruction in plan.items():
            # Ensure instruction is a string
            instruction = str(instruction).strip()
            print(f"🔧 Processing step {step_name}: {instruction}")
            
            if instruction.lower().startswith("retrieve"):
                # Extract everything after "Retrieve"
                args_part = instruction[8:].strip()
                
                # Split on comma only if not inside quotes
                parts = [p.strip().strip("'\"") for p in re.split(r",\s*(?=(?:[^\"'][\"'][^\"'][\"'])[^\"']$)", args_part)]
                
                if len(parts) >= 2:
                    col, year = parts[0], parts[1]
                    print(f"  Retrieving: column='{col}', year='{year}'")
                    
                    # Verify the column and year exist
                    print(f"  Available columns: {df.columns.tolist()}")
                    print(f"  Available index: {df.index.tolist()}")
                    
                    values = retrieving(df, [col, year])
                    context[step_name] = values[0] if values else None
                    print(f"  Retrieved value: {context[step_name]}")
                else:
                    print(f"⚠️ Could not extract 2 arguments from: {instruction}")
                    context[step_name] = None
            else:
                # [Keep existing calculation logic]
                pass

        print("✅ Final context:", context)
        return context

    except Exception as e:
        print(f"❌ Error in execution: {str(e)}")
        raise


# -- Components --
class ExecutePlanComponent(CustomQueryComponent):
    _df: pd.DataFrame = PrivateAttr()

    def _init_(self, df):
        super()._init_()
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
    _llm: Cohere = PrivateAttr()
    _template: PromptTemplate = PrivateAttr()
    _df: pd.DataFrame = PrivateAttr()

    def _init_(self, cohere_model, template: PromptTemplate, df: pd.DataFrame):
        super()._init_()
        self._llm = cohere_model
        self._template = template
        self._df = df

    def _run_component(self, **kwargs):
        # 1. Format the prompt with safe string conversion
        formatted_prompt = self._template.format(
            query=kwargs["query_str"],
            available_fields=", ".join(str(field) for field in self._df.index),
            available_years=", ".join(str(year) for year in self._df.columns)
        )
        
        # 2. Prepare messages (use lowercase role)
        messages = [
            ChatMessage(role=MessageRole.USER, content=formatted_prompt)  # Changed to use ChatMessage
        ]
        
        # 3. Get response from command-r
        response = self._llm.chat(
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.1
        )
        
        # 4. Process response (command-r has different response structure)
        try:
            content = response.message.content
            if isinstance(content, list):
                content = content[0].text if hasattr(content[0], 'text') else str(content[0])
            
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
def build_query_pipeline(cohere_model, df, classification_template):
    classify = ClassifyStepComponent(cohere_model, classification_template, df)
    execute = ExecutePlanComponent(df)
    
    # Explicitly connect components
    pipeline = QueryPipeline()
    pipeline.add_modules({
        "classify": classify,
        "execute": execute
    })
    pipeline.add_chain(["classify", "execute"])
    
    return pipeline
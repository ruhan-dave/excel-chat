from fastapi import FastAPI, UploadFile
from excelservices import ExcelService
from vectordbservices import VectorDBService
from queryservices import QueryService
import pandas as pd
from io import StringIO, BytesIO
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pipeline import build_query_pipeline, generate_user_friendly_response
import os
# from llama_index.llms.openai import OpenAI  # Commented out for Cohere migration
# from llama_index.llms.cohere import Cohere  # LlamaIndex Cohere is outdated
import cohere
from llama_index.core.prompts import PromptTemplate
from llama_index.core.llms import ChatMessage, MessageRole
import os
from dotenv import load_dotenv
from pathlib import Path
from classification_template import ClassTemplates

app = FastAPI(root_path='/api')
load_dotenv()

# list of allowed origins
origins = [
    "http://localhost:5173",
    "http://vcm-45508.vm.duke.edu"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_FOLDER = './uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.get("/")
async def root():
    return {"message": "Hello world!"}

@app.post("/upload/")
async def create_upload_file(excelFile: UploadFile):
    print(f"Received file upload request. Filename: {excelFile.filename}")
    if not excelFile.filename:
        return JSONResponse(content={"message": "No file provided"}, status_code=400)
    if not excelFile.filename.endswith(('.xlsx', '.xls', '.numbers')):
        return JSONResponse(content={"message": f"Invalid file format: {excelFile.filename}. Please upload .xlsx, .xls, or .numbers files"}, status_code=400)
    
    filepath = os.path.join(UPLOAD_FOLDER, excelFile.filename)
    with open(filepath, "wb") as f:
        while chunk := await excelFile.read(1024 * 1024):  # Read in 1MB chunks
            f.write(chunk)
    return {"message" : "File uploaded successfully!"}


# @app.get("/test-openai")
# def test_openai():
#     try:
#         OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
#         if not OPENAI_API_KEY:
#             return {"error": "OpenAI API key not found"}
#         
#         openai_model = OpenAI(
#             model = "gpt-4o-mini",
#             api_key = OPENAI_API_KEY,
#             temperature = 0.1,
#             max_tokens = 100
#         )
#         
#         messages = [
#             ChatMessage(role=MessageRole.USER, content="Return JSON: {\"test\": \"value\"}")
#         ]
#         
#         response = openai_model.chat(messages=messages)
#         content = response.message.content
#         
#         return {
#             "response_type": type(content).__name__,
#             "content": content
#         }
#     except Exception as e:
#         return {"error": str(e)}

@app.get("/test-cohere")
def test_cohere():
    try:
        COHERE_API_KEY = os.environ.get("COHERE_API_KEY")
        if not COHERE_API_KEY:
            return {"error": "Cohere API key not found"}
        
        co = cohere.ClientV2(api_key=COHERE_API_KEY)
        
        response = co.chat(
            model="command-r7b-12-2024",
            messages=[{"role": "user", "content": "Return JSON: {\"test\": \"value\"}"}],
            temperature=0.1,
            max_tokens=100
        )
        content = response.message.content[0].text
        
        return {
            "response_type": type(content).__name__,
            "content": content
        }
    except Exception as e:
        return {"error": str(e)}

# @app.get("/query")
# def query_rag(query: str):
#     try:
#         OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
#         if not OPENAI_API_KEY:
#             return {"error": "OpenAI API key not found"}
#         
#         openai_model = OpenAI(
#             model = "gpt-4o-mini",
#             api_key = OPENAI_API_KEY,
#             temperature = 0.1,
#             max_tokens = 2000
#         )
#         excel_sheets = os.listdir(UPLOAD_FOLDER)
# 
#         # For now, pick the top excel sheet
#         if len(excel_sheets) == 0:
#             return { "error" : "No sheets to look through!"}
#         
#         filename = Path(UPLOAD_FOLDER) / excel_sheets[0]
#         df = pd.read_excel(filename)
#         cleaned_df = ExcelService.clean_dataframe(df)
#         print(f"Processing query: {query}")
#         print(f"Available fields: {cleaned_df.index.tolist()}")
#         print(f"Available years: {cleaned_df.columns.tolist()}")
#         
#         pipeline = build_query_pipeline(openai_model, cleaned_df, PromptTemplate(ClassTemplates.CLASSIFIER_PROMPT))
#         json_answer = pipeline.run(query_str = query)
#         
#         # Generate user-friendly response
#         friendly_response = generate_user_friendly_response(openai_model, query, json_answer)
#         
#         return {
#             "answer": json_answer,
#             "friendly_response": friendly_response
#         }
#     except Exception as e:
#         print(f"Error in query_rag: {str(e)}")
#         import traceback
#         traceback.print_exc()
#         return {"error": str(e)}

@app.get("/query")
def query_rag(query: str):
    try:
        COHERE_API_KEY = os.environ.get("COHERE_API_KEY")
        if not COHERE_API_KEY:
            return {"error": "Cohere API key not found"}
        
        co = cohere.ClientV2(api_key=COHERE_API_KEY)
        excel_sheets = os.listdir(UPLOAD_FOLDER)

        # For now, pick the top excel sheet
        if len(excel_sheets) == 0:
            return { "error" : "No sheets to look through!"}
        
        filename = Path(UPLOAD_FOLDER) / excel_sheets[0]
        df = pd.read_excel(filename)
        cleaned_df = ExcelService.clean_dataframe(df)
        print(f"Processing query: {query}")
        print(f"Available fields: {cleaned_df.index.tolist()}")
        print(f"Available years: {cleaned_df.columns.tolist()}")
        
        pipeline = build_query_pipeline(co, cleaned_df, PromptTemplate(ClassTemplates.CLASSIFIER_PROMPT))
        json_answer = pipeline.run(query_str = query)
        
        # Generate user-friendly response
        friendly_response = generate_user_friendly_response(co, query, json_answer)
        
        return {
            "answer": json_answer,
            "friendly_response": friendly_response
        }
    except Exception as e:
        print(f"Error in query_rag: {str(e)}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}

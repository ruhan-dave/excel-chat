from fastapi import FastAPI, UploadFile
from excelservices import ExcelService
from vectordbservices import VectorDBService
from queryservices import QueryService
import pandas as pd
from io import StringIO, BytesIO
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pipeline import build_query_pipeline
import os
from llama_index.llms.cohere import Cohere
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
    if not excelFile.filename.endswith(('.xlsx', '.xls')):
        return JSONResponse(content={"message": "Invalid file format"}, status_code=400)
    
    filepath = os.path.join(UPLOAD_FOLDER, excelFile.filename)
    with open(filepath, "wb") as f:
        content = await excelFile.read()
        f.write(content)
    return {"message" : "File uploaded successfully!"}


@app.get("/query")
def query_rag(query: str):
    COHERE_API_KEY = os.environ.get("COHERE_KEY")
    cohere_model = Cohere(
        model = "command-r-plus",
        api_key = COHERE_API_KEY,
        temperature = 0.1,
        max_tokens = 2000
    )
    excel_sheets = os.listdir(UPLOAD_FOLDER)

    # For now, pick the top excel sheet
    if len(excel_sheets) == 0:
        return { "error" : "No sheets to look through!"}
    
    filename = Path(UPLOAD_FOLDER) / excel_sheets[0]
    df = pd.read_excel(filename)
    cleaned_df = ExcelService.clean_dataframe(df)
    print(query)
    pipeline = build_query_pipeline(cohere_model, cleaned_df, ClassTemplates.CLASSIFIER_PROMPT)
    answer = pipeline.run(query_str = query)
    return {"answer": answer}

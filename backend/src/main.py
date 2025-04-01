from fastapi import FastAPI, UploadFile
from excelservices import ExcelService
from vectordbservices import VectorDBService
from queryservices import QueryService
import pandas as pd
from io import StringIO, BytesIO
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import os

app = FastAPI(root_path='/api')

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
    relevant_documents = QueryService.getTopKDocuments(query, 5)
    answer = QueryService.promptLLMWithContext(query, relevant_documents)
    return {"answer": answer}

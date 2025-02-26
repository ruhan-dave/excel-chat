from fastapi import FastAPI, UploadFile
from excelservices import ExcelService
from vectordbservices import VectorDBService
from queryservices import QueryService
import pandas as pd
from io import StringIO, BytesIO

app = FastAPI(root_path='/api')

@app.get("/")
async def root():
    return {"message": "Hello world!"}

@app.post("/upload/")
def create_upload_file(excelFile: UploadFile):
    contents = excelFile.file.read()
    with BytesIO(contents) as file_object:
        excelFileContents = pd.read_excel(file_object).T.to_string()
    embeddings, chunks = ExcelService.processExcel(excelFileContents)
    VectorDBService.upload_embeddings(embeddings, chunks)
    return {"message" : "File uploaded successfully!"}


@app.get("/query")
def query_rag(query: str):
    relevant_documents = QueryService.getTopKDocuments(query, 5)
    answer = QueryService.promptLLMWithContext(query, relevant_documents)
    return {"answer": answer}

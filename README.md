# RagSheets

> A repository to handle queries for excel sheets uploaded by the user to an agent.

### How to run: (notebooks and scripts)

- Ensure that the following keys are set on your environment:
```
OPENAI_API_KEY
SUPABASE_KEY
SUPABASE_URL
```

- Ensure that you have created necessary tables in Supabase (Vector DB) using table and function definitions found [here](https://www.aispaceship.io/docs/rag-beginner-guide/create-embeddings).

- Create a virtual environment, and install dependencies:
```
python -m venv .venv
source venv/bin/activate
pip install -r requirements.txt
```

- To run the notebook, ensure that you install a kernel:

    ```
    python -m ipykernel install --user --name RAG
    ```
    -  Start jupyter notebook:
    ```
    jupyter-notebook
    ```
    - And select the `RAG` kernel during runtime.

- The script is a copy of the notebook. To run the rag script, 
```
python rag.py
```


### How to run the application

To run the application:

- Frontend:
```
npm install 
npm run dev
```

- Backend
Make a virtual environment, and then run
```
pip install -r requirements.txt
fastapi dev main.py
```
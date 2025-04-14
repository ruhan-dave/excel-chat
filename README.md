# RagSheets

> A repository to handle queries for excel sheets uploaded by the user to an agent.


### How to run the application

To run the application:

- Frontend:
```
npm install 
npm run dev
```

- Backend:

Make a virtual environment, and then run
```
pip install -r requirements.txt
fastapi dev main.py
```

- Make sure you have the following key:
```
COHERE_KEY
```
You can add the key in the folder `backend/src/.env` and the application should run without errors.

- Deployment
On a VM instance, once you have added the `.env` file, first make sure you update the API url in `frontend/ragsheets/.env.production`:
```bash
VITE_API_ENDPOINT = "http://vcm-47087.vm.duke.edu/api"
```
The first part of the URL indicates what is the base url of the deployed application which can accept backend requests. Once that is edited, run

```
sudo docker compose -f docker_compose.yml up --build -d
```

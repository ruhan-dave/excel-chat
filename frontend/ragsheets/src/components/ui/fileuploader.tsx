import { ChangeEvent, useState } from "react";
import { Input } from "./input";
import { Button } from "./button";
import axios from "axios";
import BackdropWithSpinner from "./backdropWithSpinner";


export default function FileUploader() {

    const [file, setFile] = useState<File | null>(null);

    const [isLoading, setLoading] = useState(false);
    const [response, setResponse] = useState("");

    const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
        if(event.target.files) {
            setFile(event.target.files[0]);
        }
    }

    const handleFileUpload = async () => {
        if(!file)
            return;

        setLoading(true);
        const formData = new FormData();
        formData.append('excelFile', file);
        const response = await axios.post("http://localhost:8000/upload", formData, {
            headers: {
                'Content-Type': 'multipart/form-data',
            }
        });
        setResponse(response.data.message);
        setLoading(false);
        setFile(null);
    }

    return (
        <div className="py-8 sm:py-8">
            <Input 
                type="file"
                onChange={handleFileChange}
                />
            <Button className="p-6 sm:p-6 rounded-2xl m-8 sm:m-8" onClick={handleFileUpload}>
                Upload
            </Button>
            {response.length > 0 && <p>{response}</p>}
            {isLoading && <BackdropWithSpinner />}
        </div>
    );
};
import { ChangeEvent, useState } from "react";
import { Input } from "./input";
import { Button } from "./button";
import axios from "axios";

type UploadStatus = "idle" | "uploading" | "success" | "error";

export default function FileUploader() {

    const [file, setFile] = useState<File | null>(null);
    const [status, setStatus] = useState<UploadStatus>('idle');

    const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
        if(event.target.files) {
            setFile(event.target.files[0]);
        }
    }

    const handleFileUpload = async () => {
        if(!file)
            return;

        setStatus('uploading');
        const formData = new FormData();
        formData.append('file', file);

        try 
        {
            await axios.post("https://httpbin.org/post", formData, {
                headers: {
                    'Content-Type': 'multipart/form-data',
                }
            });
            
            setStatus('success');
        }
        catch
        {
            setStatus('error');
        }
    }

    let fileDetails = null;
    if(file)
    {
        fileDetails = (
            <div className="mb-4 text-sm">
                <p>File Name = {file.name}</p>
                <p>Size: {(file.size / 1024).toFixed(2)} KB</p>
                <p>Type: {file.type}</p>
            </div>
        );
    }

    let fileSubmitButton = null;
    if(file && status !== "uploading")
    {
        fileSubmitButton = <Button onClick={handleFileUpload}>upload</Button>
    }

    let statusSuccessMessage = null;
    if(status === 'success')
    {
        statusSuccessMessage = <p className = "mt-2 text-sm"> File uploaded successfully!</p>;
    }

    let statusErrorMessage = null;
    if(status === 'error')
    {
        statusErrorMessage = <p className="mt-2 text-sm">Upload failed. Please try again.</p>
    }

    return (
        <div>
            <Input 
                type="file"
                onChange={handleFileChange}
                />
            {fileDetails}
            {fileSubmitButton}
            {statusSuccessMessage}
            {statusErrorMessage}
        </div>
    );
};
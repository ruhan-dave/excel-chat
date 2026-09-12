import { ChangeEvent, useState } from "react";
import { Input } from "./input";
import { Button } from "./button";
import axios from "axios";
import BackdropWithSpinner from "./backdropWithSpinner";


export default function FileUploader() {

    const [file, setFile] = useState<File | null>(null);

    const [isLoading, setLoading] = useState(false);
    const [response, setResponse] = useState("");
    const [uploadProgress, setUploadProgress] = useState(0);

    const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
        if(event.target.files) {
            setFile(event.target.files[0]);
        }
    }

    const handleFileUpload = async () => {
        if(!file)
            return;

        setLoading(true);
        setUploadProgress(0);
        const formData = new FormData();
        formData.append('excelFile', file);
	const apiURL = import.meta.env.VITE_API_ENDPOINT;
        const response = await axios.post(`${apiURL}/upload/`, formData, {
            headers: {
                'Content-Type': 'multipart/form-data',
            },
            timeout: 300000,
            onUploadProgress: (progressEvent) => {
                const progress = progressEvent.total
                    ? Math.round((progressEvent.loaded * 100) / progressEvent.total)
                    : 0;
                setUploadProgress(progress);
            }
        });
        setResponse(response.data.message);
        setLoading(false);
        setUploadProgress(0);
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
            {isLoading && (
                <div className="flex flex-col items-center gap-4">
                    <BackdropWithSpinner />
                    {uploadProgress > 0 && (
                        <div className="w-full max-w-md">
                            <div className="flex justify-between text-sm mb-1">
                                <span>Uploading...</span>
                                <span>{uploadProgress}%</span>
                            </div>
                            <div className="w-full bg-gray-200 rounded-full h-2.5">
                                <div 
                                    className="bg-blue-600 h-2.5 rounded-full transition-all duration-300" 
                                    style={{ width: `${uploadProgress}%` }}
                                ></div>
                            </div>
                        </div>
                    )}
                </div>
            )}
        </div>
    );
};

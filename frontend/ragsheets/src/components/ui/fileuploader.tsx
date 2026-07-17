import { ChangeEvent, useState } from "react";
import { Input } from "./input";
import { Button } from "./button";
import { Backdrop } from "./backdrop";
import axios from "axios";
import BackdropWithSpinner from "./backdropWithSpinner";

interface SensitiveDataWarning {
    pending_upload_id: string;
    filename: string;
    detected_types: string[];
    message: string;
}

export default function FileUploader() {

    const [file, setFile] = useState<File | null>(null);
    const [isLoading, setLoading] = useState(false);
    const [response, setResponse] = useState("");
    const [uploadProgress, setUploadProgress] = useState(0);
    const [sensitiveWarning, setSensitiveWarning] = useState<SensitiveDataWarning | null>(null);
    const [sanitizing, setSanitizing] = useState(false);

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
        try {
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

            if (response.data.sensitive_data_detected) {
                setSensitiveWarning(response.data);
                setLoading(false);
                setUploadProgress(0);
                setFile(null);
                return;
            }

            setResponse(response.data.message);
        } catch (error: any) {
            setResponse(error.response?.data?.error || error.response?.data?.message || "Upload failed.");
        }
        setLoading(false);
        setUploadProgress(0);
        setFile(null);
    }

    const handleSanitize = async () => {
        if (!sensitiveWarning) return;
        setSanitizing(true);
        const apiURL = import.meta.env.VITE_API_ENDPOINT;
        try {
            const response = await axios.post(
                `${apiURL}/upload/confirm?pending_upload_id=${sensitiveWarning.pending_upload_id}&action=sanitize`,
                {},
                { timeout: 300000 }
            );
            setResponse(response.data.message);
        } catch (error: any) {
            setResponse(error.response?.data?.error || error.response?.data?.message || "Sanitization failed.");
        }
        setSanitizing(false);
        setSensitiveWarning(null);
    }

    const handleCancelUpload = async () => {
        if (!sensitiveWarning) return;
        const apiURL = import.meta.env.VITE_API_ENDPOINT;
        try {
            await axios.post(
                `${apiURL}/upload/confirm?pending_upload_id=${sensitiveWarning.pending_upload_id}&action=cancel`,
                {},
                { timeout: 30000 }
            );
        } catch {
            // ignore — file will be cleaned up server-side
        }
        setSensitiveWarning(null);
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

            {/* Sensitive Data Warning Popup */}
            {sensitiveWarning && (
                <Backdrop open={true} variant="dim" closeOnClick={false}>
                    <div className="bg-white dark:bg-gray-800 rounded-xl shadow-2xl p-6 max-w-md mx-4">
                        <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100 mb-3">
                            Sensitive Data Detected
                        </h2>
                        <p className="text-sm text-gray-600 dark:text-gray-300 mb-4">
                            {sensitiveWarning.message}
                        </p>
                        <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg p-3 mb-5">
                            <p className="text-xs font-medium text-amber-800 dark:text-amber-200 mb-1">
                                Detected sensitive data types:
                            </p>
                            <ul className="text-xs text-amber-700 dark:text-amber-300 list-disc list-inside">
                                {sensitiveWarning.detected_types.map((type, i) => (
                                    <li key={i}>{type}</li>
                                ))}
                            </ul>
                        </div>
                        <div className="flex flex-col gap-3">
                            <p className="text-xs text-gray-500 dark:text-gray-400">
                                Choose to either upload a new file without this information, or allow the app to automatically redact the sensitive data.
                            </p>
                            <div className="flex gap-3 justify-end">
                                <Button
                                    variant="outline"
                                    onClick={handleCancelUpload}
                                    disabled={sanitizing}
                                >
                                    Upload New File
                                </Button>
                                <Button
                                    onClick={handleSanitize}
                                    disabled={sanitizing}
                                >
                                    {sanitizing ? "Redacting..." : "Redact & Upload"}
                                </Button>
                            </div>
                        </div>
                    </div>
                </Backdrop>
            )}
        </div>
    );
};

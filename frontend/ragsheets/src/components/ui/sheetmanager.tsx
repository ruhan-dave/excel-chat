import { useState, useEffect, useCallback } from "react";
import { Button } from "./button";
import { Textarea } from "./textarea";
import { Input } from "./input";
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from "@/components/ui/table";
import axios from "axios";
import BackdropWithSpinner from "./backdropWithSpinner";

interface SheetInfo {
    sheet_id: string;
    file_id: string;
    file_name: string;
    sheet_name: string;
    fields: string[];
    years: string[];
    schema_group: string;
    user_description: string;
    auto_description: string;
    row_count: number;
    combined_description: string;
}

interface FileInfo {
    file_id: string;
    file_name: string;
    sheet_count: number;
}

export default function SheetManager() {
    const [sheets, setSheets] = useState<SheetInfo[]>([]);
    const [files, setFiles] = useState<FileInfo[]>([]);
    const [isLoading, setLoading] = useState(false);
    const [editingSheetId, setEditingSheetId] = useState<string | null>(null);
    const [descriptionDraft, setDescriptionDraft] = useState("");
    const [uploadFile, setUploadFile] = useState<File | null>(null);
    const [uploadProgress, setUploadProgress] = useState(0);
    const [uploadResponse, setUploadResponse] = useState("");
    const apiURL = import.meta.env.VITE_API_ENDPOINT;

    const fetchSheets = useCallback(async () => {
        try {
            const response = await axios.get(`${apiURL}/sheets/`);
            setSheets(response.data.sheets || []);
        } catch (error) {
            console.error("Failed to fetch sheets:", error);
        }
    }, [apiURL]);

    const fetchFiles = useCallback(async () => {
        try {
            const response = await axios.get(`${apiURL}/files/`);
            setFiles(response.data.files || []);
        } catch (error) {
            console.error("Failed to fetch files:", error);
        }
    }, [apiURL]);

    useEffect(() => {
        fetchSheets();
        fetchFiles();
    }, [fetchSheets, fetchFiles]);

    const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        if (event.target.files) {
            setUploadFile(event.target.files[0]);
        }
    };

    const handleUpload = async () => {
        if (!uploadFile) return;
        setLoading(true);
        setUploadProgress(0);
        setUploadResponse("");
        const formData = new FormData();
        formData.append("excelFile", uploadFile);
        try {
            const response = await axios.post(`${apiURL}/upload/`, formData, {
                headers: { "Content-Type": "multipart/form-data" },
                timeout: 300000,
                onUploadProgress: (progressEvent) => {
                    const progress = progressEvent.total
                        ? Math.round((progressEvent.loaded * 100) / progressEvent.total)
                        : 0;
                    setUploadProgress(progress);
                },
            });
            setUploadResponse(response.data.message);
            setUploadFile(null);
            await fetchSheets();
            await fetchFiles();
        } catch (error) {
            console.error("Upload failed:", error);
            setUploadResponse("Upload failed. Please try again.");
        }
        setLoading(false);
        setUploadProgress(0);
    };

    const handleSaveDescription = async (sheetId: string) => {
        setLoading(true);
        try {
            await axios.post(`${apiURL}/describe-sheet/`, {
                sheet_id: sheetId,
                description: descriptionDraft,
            });
            setEditingSheetId(null);
            setDescriptionDraft("");
            await fetchSheets();
        } catch (error) {
            console.error("Failed to save description:", error);
        }
        setLoading(false);
    };

    const handleDeleteFile = async (fileId: string) => {
        if (!confirm("Are you sure you want to delete this file and all its sheets?")) return;
        try {
            await axios.delete(`${apiURL}/files/${fileId}`);
            await fetchSheets();
            await fetchFiles();
        } catch (error) {
            console.error("Failed to delete file:", error);
        }
    };

    const startEditing = (sheet: SheetInfo) => {
        setEditingSheetId(sheet.sheet_id);
        setDescriptionDraft(sheet.user_description || "");
    };

    return (
        <div className="space-y-8">
            {/* Upload Section */}
            <div className="space-y-4">
                <h3 className="text-lg font-semibold">Upload Excel File</h3>
                <p className="text-sm text-muted-foreground">
                    Upload an Excel file with one or more sheets. Each sheet will be
                    automatically analyzed and described. You can add your own description
                    below after upload.
                </p>
                <Input type="file" onChange={handleFileChange} />
                <Button
                    className="rounded-2xl"
                    onClick={handleUpload}
                    disabled={!uploadFile || isLoading}
                >
                    Upload
                </Button>
                {uploadResponse && <p className="text-sm">{uploadResponse}</p>}
                {isLoading && uploadProgress > 0 && (
                    <div className="w-full max-w-md">
                        <div className="flex justify-between text-sm mb-1">
                            <span>Uploading & processing...</span>
                            <span>{uploadProgress}%</span>
                        </div>
                        <div className="w-full bg-gray-200 rounded-full h-2.5">
                            <div
                                className="bg-blue-600 h-2.5 rounded-full transition-all duration-300"
                                style={{ width: `${uploadProgress}%` }}
                            />
                        </div>
                    </div>
                )}
            </div>

            {/* Uploaded Files */}
            {files.length > 0 && (
                <div className="space-y-3">
                    <h3 className="text-lg font-semibold">Uploaded Files</h3>
                    <div className="flex flex-wrap gap-2">
                        {files.map((file) => (
                            <div
                                key={file.file_id}
                                className="flex items-center gap-2 rounded-lg border px-3 py-2"
                            >
                                <span className="text-sm font-medium">
                                    {file.file_name}
                                </span>
                                <span className="text-xs text-muted-foreground">
                                    ({file.sheet_count} sheet{file.sheet_count !== 1 ? "s" : ""})
                                </span>
                                <Button
                                    variant="destructive"
                                    size="sm"
                                    onClick={() => handleDeleteFile(file.file_id)}
                                >
                                    Delete
                                </Button>
                            </div>
                        ))}
                    </div>
                </div>
            )}

            {/* Sheet Descriptions */}
            {sheets.length > 0 && (
                <div className="space-y-4">
                    <h3 className="text-lg font-semibold">Sheet Descriptions</h3>
                    <p className="text-sm text-muted-foreground">
                        Describe what each sheet represents in plain English. This helps the
                        AI understand your data across different sheets. Auto-generated
                        descriptions are provided as a starting point.
                    </p>
                    <Table>
                        <TableHeader>
                            <TableRow>
                                <TableHead>Sheet</TableHead>
                                <TableHead>File</TableHead>
                                <TableHead>Schema Group</TableHead>
                                <TableHead>Fields</TableHead>
                                <TableHead>Years</TableHead>
                                <TableHead>Auto Description</TableHead>
                                <TableHead>Your Description</TableHead>
                                <TableHead className="text-right">Actions</TableHead>
                            </TableRow>
                        </TableHeader>
                        <TableBody>
                            {sheets.map((sheet) => (
                                <TableRow key={sheet.sheet_id}>
                                    <TableCell className="font-medium">
                                        {sheet.sheet_name}
                                    </TableCell>
                                    <TableCell className="text-xs">
                                        {sheet.file_name}
                                    </TableCell>
                                    <TableCell className="text-xs">
                                        {sheet.schema_group === "unique"
                                            ? "—"
                                            : sheet.schema_group}
                                    </TableCell>
                                    <TableCell className="text-xs max-w-[200px]">
                                        {sheet.fields.slice(0, 5).join(", ")}
                                        {sheet.fields.length > 5 &&
                                            ` +${sheet.fields.length - 5} more`}
                                    </TableCell>
                                    <TableCell className="text-xs">
                                        {sheet.years.join(", ")}
                                    </TableCell>
                                    <TableCell className="text-xs max-w-[200px]">
                                        {sheet.auto_description || "—"}
                                    </TableCell>
                                    <TableCell className="max-w-[250px]">
                                        {editingSheetId === sheet.sheet_id ? (
                                            <div className="space-y-2">
                                                <Textarea
                                                    value={descriptionDraft}
                                                    onChange={(e) =>
                                                        setDescriptionDraft(e.target.value)
                                                    }
                                                    placeholder="Describe what this sheet represents..."
                                                    className="min-h-[60px] text-xs"
                                                />
                                                <div className="flex gap-2">
                                                    <Button
                                                        size="sm"
                                                        onClick={() =>
                                                            handleSaveDescription(sheet.sheet_id)
                                                        }
                                                    >
                                                        Save
                                                    </Button>
                                                    <Button
                                                        size="sm"
                                                        variant="outline"
                                                        onClick={() => {
                                                            setEditingSheetId(null);
                                                            setDescriptionDraft("");
                                                        }}
                                                    >
                                                        Cancel
                                                    </Button>
                                                </div>
                                            </div>
                                        ) : (
                                            <span className="text-xs">
                                                {sheet.user_description || (
                                                    <span className="text-muted-foreground italic">
                                                        Not set
                                                    </span>
                                                )}
                                            </span>
                                        )}
                                    </TableCell>
                                    <TableCell className="text-right">
                                        {editingSheetId !== sheet.sheet_id && (
                                            <Button
                                                size="sm"
                                                variant="outline"
                                                onClick={() => startEditing(sheet)}
                                            >
                                                Edit
                                            </Button>
                                        )}
                                    </TableCell>
                                </TableRow>
                            ))}
                        </TableBody>
                    </Table>
                </div>
            )}

            {sheets.length === 0 && files.length === 0 && !isLoading && (
                <div className="text-center py-8 text-muted-foreground">
                    No files uploaded yet. Upload an Excel file to get started.
                </div>
            )}

            {isLoading && <BackdropWithSpinner />}
        </div>
    );
}

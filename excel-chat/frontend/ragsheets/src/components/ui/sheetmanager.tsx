import { useState, useEffect, useCallback } from "react";
import { Button } from "./button";
import { Textarea } from "./textarea";
import { Input } from "./input";
import axios from "axios";
import BackdropWithSpinner from "./backdropWithSpinner";
import { Upload, FileSpreadsheet, Trash2, Pencil, ChevronDown, ChevronUp, FileText, Layers } from "lucide-react";

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
    const [expandedSheetId, setExpandedSheetId] = useState<string | null>(null);
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
                <div className="flex items-center gap-2">
                    <Upload className="h-5 w-5 text-muted-foreground" />
                    <h3 className="text-lg font-semibold">Upload Excel File</h3>
                </div>
                <p className="text-sm text-muted-foreground">
                    Upload an Excel file with one or more sheets. Each sheet will be
                    automatically analyzed and described. You can add your own description
                    below after upload.
                </p>
                <label className="group flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed border-slate-300 bg-slate-50/50 px-6 py-10 text-center transition-colors hover:border-primary/40 hover:bg-slate-50">
                    <FileSpreadsheet className="h-10 w-10 text-slate-400 transition-colors group-hover:text-primary/60" />
                    <div className="text-sm font-medium text-slate-700">
                        {uploadFile ? uploadFile.name : "Click to select an Excel file"}
                    </div>
                    <div className="text-xs text-muted-foreground">
                        Supports .xlsx, .xls formats
                    </div>
                    <Input
                        type="file"
                        onChange={handleFileChange}
                        className="hidden"
                    />
                </label>
                <div className="flex items-center gap-3">
                    <Button
                        onClick={handleUpload}
                        disabled={!uploadFile || isLoading}
                    >
                        <Upload className="mr-2 h-4 w-4" />
                        Upload
                    </Button>
                    {uploadResponse && (
                        <span className="text-sm text-muted-foreground">{uploadResponse}</span>
                    )}
                </div>
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
                    <div className="flex items-center gap-2">
                        <FileText className="h-5 w-5 text-muted-foreground" />
                        <h3 className="text-lg font-semibold">Uploaded Files</h3>
                    </div>
                    <div className="grid gap-3 sm:grid-cols-2">
                        {files.map((file) => (
                            <div
                                key={file.file_id}
                                className="flex items-center justify-between rounded-lg border bg-white px-4 py-3 shadow-sm"
                            >
                                <div className="flex items-center gap-3 overflow-hidden">
                                    <FileSpreadsheet className="h-5 w-5 shrink-0 text-emerald-600" />
                                    <div className="overflow-hidden">
                                        <div className="truncate text-sm font-medium">
                                            {file.file_name}
                                        </div>
                                        <div className="text-xs text-muted-foreground">
                                            {file.sheet_count} sheet{file.sheet_count !== 1 ? "s" : ""}
                                        </div>
                                    </div>
                                </div>
                                <Button
                                    variant="ghost"
                                    size="icon"
                                    className="shrink-0 text-muted-foreground hover:text-destructive"
                                    onClick={() => handleDeleteFile(file.file_id)}
                                >
                                    <Trash2 className="h-4 w-4" />
                                </Button>
                            </div>
                        ))}
                    </div>
                </div>
            )}

            {/* Sheet Descriptions */}
            {sheets.length > 0 && (
                <div className="space-y-4">
                    <div className="flex items-center gap-2">
                        <Layers className="h-5 w-5 text-muted-foreground" />
                        <h3 className="text-lg font-semibold">Sheet Descriptions</h3>
                    </div>
                    <p className="text-sm text-muted-foreground">
                        Describe what each sheet represents in plain English. This helps the
                        AI understand your data across different sheets. Auto-generated
                        descriptions are provided as a starting point.
                    </p>
                    <div className="space-y-3">
                        {sheets.map((sheet) => {
                            const isExpanded = expandedSheetId === sheet.sheet_id;
                            const isEditing = editingSheetId === sheet.sheet_id;
                            return (
                                <div
                                    key={sheet.sheet_id}
                                    className="rounded-lg border bg-white shadow-sm"
                                >
                                    {/* Collapsed header row */}
                                    <div
                                        className="flex cursor-pointer items-center justify-between px-4 py-3"
                                        onClick={() => setExpandedSheetId(isExpanded ? null : sheet.sheet_id)}
                                    >
                                        <div className="flex items-center gap-3 overflow-hidden">
                                            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-slate-100">
                                                <FileSpreadsheet className="h-4 w-4 text-slate-600" />
                                            </div>
                                            <div className="overflow-hidden">
                                                <div className="truncate text-sm font-medium">
                                                    {sheet.sheet_name}
                                                </div>
                                                <div className="truncate text-xs text-muted-foreground">
                                                    {sheet.file_name}
                                                </div>
                                            </div>
                                        </div>
                                        <div className="flex items-center gap-2">
                                            {sheet.user_description ? (
                                                <span className="hidden rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700 sm:inline">
                                                    Described
                                                </span>
                                            ) : (
                                                <span className="hidden rounded-full bg-amber-50 px-2.5 py-0.5 text-xs font-medium text-amber-700 sm:inline">
                                                    No description
                                                </span>
                                            )}
                                            {isExpanded ? (
                                                <ChevronUp className="h-4 w-4 text-muted-foreground" />
                                            ) : (
                                                <ChevronDown className="h-4 w-4 text-muted-foreground" />
                                            )}
                                        </div>
                                    </div>

                                    {/* Expanded content */}
                                    {isExpanded && (
                                        <div className="space-y-4 border-t px-4 py-4">
                                            {/* Metadata badges */}
                                            <div className="flex flex-wrap gap-2">
                                                {sheet.schema_group !== "unique" && (
                                                    <span className="rounded-md bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600">
                                                        Group: {sheet.schema_group}
                                                    </span>
                                                )}
                                                <span className="rounded-md bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600">
                                                    {sheet.fields.length} fields
                                                </span>
                                                {sheet.years.length > 0 && (
                                                    <span className="rounded-md bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600">
                                                        Years: {sheet.years.join(", ")}
                                                    </span>
                                                )}
                                                <span className="rounded-md bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600">
                                                    {sheet.row_count} rows
                                                </span>
                                            </div>

                                            {/* Fields list */}
                                            <div>
                                                <div className="text-xs font-medium text-muted-foreground mb-1">Fields</div>
                                                <div className="flex flex-wrap gap-1.5">
                                                    {sheet.fields.map((field) => (
                                                        <span
                                                            key={field}
                                                            className="rounded border border-slate-200 bg-slate-50 px-2 py-0.5 text-xs text-slate-600"
                                                        >
                                                            {field}
                                                        </span>
                                                    ))}
                                                </div>
                                            </div>

                                            {/* Auto description */}
                                            {sheet.auto_description && (
                                                <div>
                                                    <div className="text-xs font-medium text-muted-foreground mb-1">
                                                        Auto-generated description
                                                    </div>
                                                    <p className="rounded-md bg-slate-50 px-3 py-2 text-sm text-slate-600">
                                                        {sheet.auto_description}
                                                    </p>
                                                </div>
                                            )}

                                            {/* Your description / editing */}
                                            <div>
                                                <div className="flex items-center justify-between mb-1">
                                                    <div className="text-xs font-medium text-muted-foreground">
                                                        Your description
                                                    </div>
                                                    {!isEditing && (
                                                        <Button
                                                            size="sm"
                                                            variant="ghost"
                                                            className="h-7 px-2 text-xs"
                                                            onClick={(e) => {
                                                                e.stopPropagation();
                                                                startEditing(sheet);
                                                            }}
                                                        >
                                                            <Pencil className="mr-1 h-3 w-3" />
                                                            Edit
                                                        </Button>
                                                    )}
                                                </div>
                                                {isEditing ? (
                                                    <div className="space-y-2" onClick={(e) => e.stopPropagation()}>
                                                        <Textarea
                                                            value={descriptionDraft}
                                                            onChange={(e) =>
                                                                setDescriptionDraft(e.target.value)
                                                            }
                                                            placeholder="Describe what this sheet represents..."
                                                            className="min-h-[80px] text-sm"
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
                                                    <p className="text-sm text-slate-700">
                                                        {sheet.user_description || (
                                                            <span className="italic text-muted-foreground">
                                                                Not set — click Edit to add a description
                                                            </span>
                                                        )}
                                                    </p>
                                                )}
                                            </div>
                                        </div>
                                    )}
                                </div>
                            );
                        })}
                    </div>
                </div>
            )}

            {sheets.length === 0 && files.length === 0 && !isLoading && (
                <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed border-slate-200 py-12 text-center">
                    <FileSpreadsheet className="h-12 w-12 text-slate-300" />
                    <p className="text-sm text-muted-foreground">
                        No files uploaded yet. Upload an Excel file to get started.
                    </p>
                </div>
            )}

            {isLoading && <BackdropWithSpinner />}
        </div>
    );
}

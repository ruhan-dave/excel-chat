import { useState } from "react";
import { Button } from "./button";
import { Input } from "./input";
import { Textarea } from "./textarea";
import {
    Upload,
    FileSpreadsheet,
    Trash2,
    ChevronDown,
    ChevronUp,
    Pencil,
    Check,
} from "lucide-react";
import type { SheetInfo, FileInfo } from "@/lib/api";
import { uploadFile } from "@/lib/api";

interface SheetListProps {
    sheets: SheetInfo[];
    files: FileInfo[];
    selectedSheetIds: Set<string>;
    onToggleSheet: (sheetId: string) => void;
    onRefetch: () => void;
    onRemoveFile: (fileId: string) => void;
    onSaveDescription: (sheetId: string, description: string) => Promise<void>;
}

export function SheetList({
    sheets,
    files,
    selectedSheetIds,
    onToggleSheet,
    onRefetch,
    onRemoveFile,
    onSaveDescription,
}: SheetListProps) {
    const [uploadFileState, setUploadFileState] = useState<File | null>(null);
    const [uploadProgress, setUploadProgress] = useState(0);
    const [uploading, setUploading] = useState(false);
    const [uploadMsg, setUploadMsg] = useState("");
    const [expandedFileId, setExpandedFileId] = useState<string | null>(null);
    const [editingSheetId, setEditingSheetId] = useState<string | null>(null);
    const [descriptionDraft, setDescriptionDraft] = useState("");

    const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        if (e.target.files) setUploadFileState(e.target.files[0]);
    };

    const handleUpload = async () => {
        if (!uploadFileState) return;
        setUploading(true);
        setUploadProgress(0);
        setUploadMsg("");
        try {
            const result = await uploadFile(uploadFileState, setUploadProgress);
            setUploadMsg(result.message);
            setUploadFileState(null);
            onRefetch();
        } catch (error) {
            console.error("Upload failed:", error);
            setUploadMsg("Upload failed. Please try again.");
        }
        setUploading(false);
        setUploadProgress(0);
    };

    const startEditing = (sheet: SheetInfo) => {
        setEditingSheetId(sheet.sheet_id);
        setDescriptionDraft(sheet.user_description || "");
    };

    const handleSaveDescription = async (sheetId: string) => {
        await onSaveDescription(sheetId, descriptionDraft);
        setEditingSheetId(null);
        setDescriptionDraft("");
    };

    const sheetsForFile = (fileId: string) =>
        sheets.filter((s) => s.file_id === fileId);

    return (
        <div className="flex flex-col gap-3">
            {/* Upload button */}
            <div className="flex items-center gap-2">
                <label className="flex-1 cursor-pointer">
                    <div className="flex items-center justify-center gap-2 rounded-lg border border-dashed border-slate-300 bg-slate-50/50 px-3 py-2.5 text-sm text-slate-600 transition-colors hover:border-primary/40 hover:bg-slate-50">
                        <Upload className="h-4 w-4" />
                        <span>{uploadFileState ? uploadFileState.name : "Upload Excel"}</span>
                    </div>
                    <Input
                        type="file"
                        onChange={handleFileChange}
                        className="hidden"
                        accept=".xlsx,.xls"
                    />
                </label>
                {uploadFileState && (
                    <Button
                        size="sm"
                        onClick={handleUpload}
                        disabled={uploading}
                        className="shrink-0"
                    >
                        <Upload className="h-3.5 w-3.5" />
                    </Button>
                )}
            </div>

            {uploading && uploadProgress > 0 && (
                <div className="w-full">
                    <div className="flex justify-between text-xs mb-1">
                        <span className="text-muted-foreground">Uploading…</span>
                        <span className="text-muted-foreground">{uploadProgress}%</span>
                    </div>
                    <div className="w-full bg-gray-200 rounded-full h-1.5">
                        <div
                            className="bg-blue-600 h-1.5 rounded-full transition-all"
                            style={{ width: `${uploadProgress}%` }}
                        />
                    </div>
                </div>
            )}

            {uploadMsg && (
                <p className="text-xs text-muted-foreground">{uploadMsg}</p>
            )}

            {/* Files & Sheets list */}
            {files.length === 0 && !uploading && (
                <p className="text-xs text-muted-foreground py-2">
                    No files uploaded yet.
                </p>
            )}

            <div className="flex flex-col gap-1">
                {files.map((file) => {
                    const fileSheets = sheetsForFile(file.file_id);
                    const isExpanded = expandedFileId === file.file_id;
                    const selectedCount = fileSheets.filter((s) =>
                        selectedSheetIds.has(s.sheet_id)
                    ).length;

                    return (
                        <div key={file.file_id} className="rounded-md border border-slate-200">
                            {/* File header */}
                            <div
                                className="flex cursor-pointer items-center gap-2 px-2.5 py-2 hover:bg-slate-50"
                                onClick={() => setExpandedFileId(isExpanded ? null : file.file_id)}
                            >
                                {isExpanded ? (
                                    <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                                ) : (
                                    <ChevronUp className="h-3.5 w-3.5 shrink-0 text-muted-foreground rotate-90" />
                                )}
                                <FileSpreadsheet className="h-4 w-4 shrink-0 text-emerald-600" />
                                <div className="flex-1 overflow-hidden">
                                    <div className="truncate text-xs font-medium">
                                        {file.file_name}
                                    </div>
                                    <div className="text-[10px] text-muted-foreground">
                                        {file.sheet_count} sheet{file.sheet_count !== 1 ? "s" : ""}
                                        {selectedCount > 0 && ` · ${selectedCount} selected`}
                                    </div>
                                </div>
                                <button
                                    className="shrink-0 text-muted-foreground hover:text-destructive"
                                    onClick={(e) => {
                                        e.stopPropagation();
                                        onRemoveFile(file.file_id);
                                    }}
                                >
                                    <Trash2 className="h-3.5 w-3.5" />
                                </button>
                            </div>

                            {/* Sheets under file */}
                            {isExpanded && (
                                <div className="border-t border-slate-100">
                                    {fileSheets.map((sheet) => {
                                        const isSelected = selectedSheetIds.has(sheet.sheet_id);
                                        return (
                                            <div
                                                key={sheet.sheet_id}
                                                className="group relative"
                                            >
                                                <div
                                                    className={`flex cursor-pointer items-center gap-2 px-2.5 py-2 pl-6 hover:bg-slate-50 ${
                                                        isSelected ? "bg-blue-50/50" : ""
                                                    }`}
                                                    onClick={() => onToggleSheet(sheet.sheet_id)}
                                                >
                                                    <div
                                                        className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${
                                                            isSelected
                                                                ? "border-blue-600 bg-blue-600"
                                                                : "border-slate-300"
                                                        }`}
                                                    >
                                                        {isSelected && (
                                                            <Check className="h-3 w-3 text-white" />
                                                        )}
                                                    </div>
                                                    <div className="flex-1 overflow-hidden">
                                                        <div className="truncate text-xs">
                                                            {sheet.sheet_name}
                                                        </div>
                                                        <div className="text-[10px] text-muted-foreground">
                                                            {sheet.fields.length} fields
                                                            {sheet.years.length > 0 && ` · ${sheet.years.join(", ")}`}
                                                        </div>
                                                    </div>
                                                    <button
                                                        className="shrink-0 opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-foreground"
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            startEditing(sheet);
                                                        }}
                                                    >
                                                        <Pencil className="h-3 w-3" />
                                                    </button>
                                                </div>

                                                {/* Description editor */}
                                                {editingSheetId === sheet.sheet_id && (
                                                    <div
                                                        className="px-2.5 pb-2 pl-6"
                                                        onClick={(e) => e.stopPropagation()}
                                                    >
                                                        <Textarea
                                                            value={descriptionDraft}
                                                            onChange={(e) =>
                                                                setDescriptionDraft(e.target.value)
                                                            }
                                                            placeholder="Describe this sheet..."
                                                            className="min-h-[60px] text-xs"
                                                        />
                                                        <div className="flex gap-1.5 mt-1.5">
                                                            <Button
                                                                size="sm"
                                                                className="h-7 text-xs"
                                                                onClick={() =>
                                                                    handleSaveDescription(sheet.sheet_id)
                                                                }
                                                            >
                                                                Save
                                                            </Button>
                                                            <Button
                                                                size="sm"
                                                                variant="outline"
                                                                className="h-7 text-xs"
                                                                onClick={() => {
                                                                    setEditingSheetId(null);
                                                                    setDescriptionDraft("");
                                                                }}
                                                            >
                                                                Cancel
                                                            </Button>
                                                        </div>
                                                    </div>
                                                )}
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>
        </div>
    );
}

import { useState, useEffect, useCallback } from "react";
import { fetchSheets, fetchFiles, deleteFile, describeSheet, type SheetInfo, type FileInfo } from "@/lib/api";

export function useSheets() {
    const [sheets, setSheets] = useState<SheetInfo[]>([]);
    const [files, setFiles] = useState<FileInfo[]>([]);
    const [isLoading, setLoading] = useState(false);

    const refetch = useCallback(async () => {
        setLoading(true);
        try {
            const [s, f] = await Promise.all([fetchSheets(), fetchFiles()]);
            setSheets(s);
            setFiles(f);
        } catch (error) {
            console.error("Failed to fetch sheets/files:", error);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        refetch();
    }, [refetch]);

    const removeFile = useCallback(async (fileId: string) => {
        await deleteFile(fileId);
        await refetch();
    }, [refetch]);

    const saveDescription = useCallback(async (sheetId: string, description: string) => {
        await describeSheet(sheetId, description);
        await refetch();
    }, [refetch]);

    return {
        sheets,
        files,
        isLoading,
        refetch,
        removeFile,
        saveDescription,
    };
}

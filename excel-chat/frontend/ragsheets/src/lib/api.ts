import axios from "axios";

const apiURL = import.meta.env.VITE_API_ENDPOINT;

// ============================================================================
// Types
// ============================================================================

export interface SheetInfo {
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

export interface FileInfo {
    file_id: string;
    file_name: string;
    sheet_count: number;
}

export interface ThreadSummary {
    thread_id: string;
    title: string;
    created_at: string;
    updated_at: string;
    last_message_preview: string;
    sheet_count: number;
}

export interface MessageInfo {
    message_id: string;
    thread_id: string;
    user_id: string;
    role: "user" | "assistant";
    content: string;
    query: string | null;
    friendly_response: string | null;
    full_result: string | null;
    sheet_ids: string[];
    cached: boolean;
    created_at: string;
}

export interface ThreadDetail {
    thread: ThreadSummary;
    sheets: SheetInfo[];
    messages: MessageInfo[];
}

// ============================================================================
// Sheets & Files
// ============================================================================

export async function fetchSheets(): Promise<SheetInfo[]> {
    const res = await axios.get(`${apiURL}/sheets/`);
    return res.data.sheets || [];
}

export async function fetchFiles(): Promise<FileInfo[]> {
    const res = await axios.get(`${apiURL}/files/`);
    return res.data.files || [];
}

export async function deleteFile(fileId: string): Promise<void> {
    await axios.delete(`${apiURL}/files/${fileId}`);
}

export async function describeSheet(sheetId: string, description: string): Promise<void> {
    await axios.post(`${apiURL}/describe-sheet/`, {
        sheet_id: sheetId,
        description,
    });
}

export async function uploadFile(
    file: File,
    onProgress?: (progress: number) => void
): Promise<{ message: string; sensitive_data?: boolean; pending_upload_id?: string }> {
    const formData = new FormData();
    formData.append("excelFile", file);
    const res = await axios.post(`${apiURL}/upload/`, formData, {
        headers: { "Content-Type": "multipart/form-data" },
        timeout: 300000,
        onUploadProgress: (progressEvent) => {
            const progress = progressEvent.total
                ? Math.round((progressEvent.loaded * 100) / progressEvent.total)
                : 0;
            onProgress?.(progress);
        },
    });
    return res.data;
}

export async function confirmUpload(
    pendingUploadId: string,
    action: "sanitize" | "cancel"
): Promise<{ message: string }> {
    const res = await axios.post(`${apiURL}/upload/confirm`, {
        pending_upload_id: pendingUploadId,
        action,
    });
    return res.data;
}

// ============================================================================
// Threads
// ============================================================================

export async function createThread(
    title: string = "New Thread",
    sheetIds: string[] = []
): Promise<ThreadSummary> {
    const res = await axios.post(`${apiURL}/threads`, {
        title,
        sheet_ids: sheetIds,
    });
    return res.data;
}

export async function fetchThreads(): Promise<ThreadSummary[]> {
    const res = await axios.get(`${apiURL}/threads`);
    return res.data.threads || [];
}

export async function fetchThreadDetail(threadId: string): Promise<ThreadDetail> {
    const res = await axios.get(`${apiURL}/threads/${threadId}`);
    return res.data;
}

export async function updateThread(
    threadId: string,
    updates: {
        title?: string;
        add_sheet_ids?: string[];
        remove_sheet_ids?: string[];
    }
): Promise<ThreadSummary> {
    const res = await axios.patch(`${apiURL}/threads/${threadId}`, updates);
    return res.data;
}

export async function deleteThread(threadId: string): Promise<void> {
    await axios.delete(`${apiURL}/threads/${threadId}`);
}

export async function fetchThreadMessages(threadId: string): Promise<MessageInfo[]> {
    const res = await axios.get(`${apiURL}/threads/${threadId}/messages`);
    return res.data.messages || [];
}

// ============================================================================
// Query Streaming (SSE)
// ============================================================================

export function buildStreamUrl(
    query: string,
    threadId?: string,
    sheetIds?: string[]
): string {
    const params = new URLSearchParams();
    params.append("query", query);
    if (threadId) params.append("thread_id", threadId);
    if (sheetIds && sheetIds.length > 0) {
        params.append("sheet_ids", sheetIds.join(","));
    }
    return `${apiURL}/query/stream?${params.toString()}`;
}

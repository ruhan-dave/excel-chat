import { useState, useEffect, useCallback } from "react";
import {
    fetchThreads,
    createThread as apiCreateThread,
    deleteThread as apiDeleteThread,
    updateThread as apiUpdateThread,
    type ThreadSummary,
} from "@/lib/api";

const ACTIVE_THREAD_KEY = "excel-analyst-active-thread";

export function useThreads() {
    const [threads, setThreads] = useState<ThreadSummary[]>([]);
    const [activeThreadId, setActiveThreadId] = useState<string | null>(
        localStorage.getItem(ACTIVE_THREAD_KEY)
    );
    const [isLoading, setLoading] = useState(false);

    const refetch = useCallback(async () => {
        setLoading(true);
        try {
            const t = await fetchThreads();
            setThreads(t);
            // If active thread no longer exists, clear it
            if (activeThreadId && !t.find((th) => th.thread_id === activeThreadId)) {
                setActiveThreadId(null);
                localStorage.removeItem(ACTIVE_THREAD_KEY);
            }
        } catch (error) {
            console.error("Failed to fetch threads:", error);
        } finally {
            setLoading(false);
        }
    }, [activeThreadId]);

    useEffect(() => {
        refetch();
    }, [refetch]);

    const selectThread = useCallback((threadId: string | null) => {
        setActiveThreadId(threadId);
        if (threadId) {
            localStorage.setItem(ACTIVE_THREAD_KEY, threadId);
        } else {
            localStorage.removeItem(ACTIVE_THREAD_KEY);
        }
    }, []);

    const createThread = useCallback(async (sheetIds: string[] = [], title: string = "New Thread") => {
        const thread = await apiCreateThread(title, sheetIds);
        await refetch();
        selectThread(thread.thread_id);
        return thread;
    }, [refetch, selectThread]);

    const removeThread = useCallback(async (threadId: string) => {
        await apiDeleteThread(threadId);
        if (activeThreadId === threadId) {
            selectThread(null);
        }
        await refetch();
    }, [activeThreadId, refetch, selectThread]);

    const renameThread = useCallback(async (threadId: string, title: string) => {
        await apiUpdateThread(threadId, { title });
        await refetch();
    }, [refetch]);

    const addSheetToThread = useCallback(async (threadId: string, sheetId: string) => {
        await apiUpdateThread(threadId, { add_sheet_ids: [sheetId] });
        await refetch();
    }, [refetch]);

    const removeSheetFromThread = useCallback(async (threadId: string, sheetId: string) => {
        await apiUpdateThread(threadId, { remove_sheet_ids: [sheetId] });
        await refetch();
    }, [refetch]);

    return {
        threads,
        activeThreadId,
        isLoading,
        refetch,
        selectThread,
        createThread,
        removeThread,
        renameThread,
        addSheetToThread,
        removeSheetFromThread,
    };
}

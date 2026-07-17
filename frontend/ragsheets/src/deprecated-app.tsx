import './App.css'
import { useState, useCallback, useEffect } from "react";
import { Table2 } from "lucide-react";
import { Sidebar } from "@/components/ui/sidebar";
import { ThreadView } from "@/components/ui/thread-view";
import { useSheets } from "@/hooks/useSheets";
import { useThreads } from "@/hooks/useThreads";
import { useConversation } from "@/hooks/useConversation";
import { fetchThreadDetail, type SheetInfo } from "@/lib/api";

function App() {
    const [query, setQuery] = useState("");
    const [threadSheets, setThreadSheets] = useState<SheetInfo[]>([]);
    const [selectedSheetIds, setSelectedSheetIds] = useState<Set<string>>(new Set());

    // --- Sheets ---
    const {
        sheets,
        files,
        refetch: refetchSheets,
        removeFile,
        saveDescription,
    } = useSheets();

    // --- Threads ---
    const {
        threads,
        activeThreadId,
        selectThread,
        createThread,
        removeThread,
        renameThread,
        addSheetToThread,
        removeSheetFromThread,
        refetch: refetchThreads,
    } = useThreads();

    // --- Conversation ---
    const {
        messages,
        isLoading: isStreaming,
        streaming,
        error,
        sendMessage,
    } = useConversation(activeThreadId, threadSheets, refetchThreads);

    // Load thread sheets when active thread changes
    useEffect(() => {
        if (!activeThreadId) {
            setThreadSheets([]);
            setSelectedSheetIds(new Set());
            return;
        }
        fetchThreadDetail(activeThreadId)
            .then((detail) => {
                setThreadSheets(detail.sheets);
                setSelectedSheetIds(new Set(detail.sheets.map((s) => s.sheet_id)));
            })
            .catch((err) => {
                console.error("Failed to load thread detail:", err);
                setThreadSheets([]);
                setSelectedSheetIds(new Set());
            });
    }, [activeThreadId]);

    // --- Sheet selection handlers ---
    const toggleSheet = useCallback(
        (sheetId: string) => {
            setSelectedSheetIds((prev) => {
                const next = new Set(prev);
                if (next.has(sheetId)) {
                    next.delete(sheetId);
                } else {
                    next.add(sheetId);
                }
                return next;
            });
        },
        []
    );

    const handleAddSheet = useCallback(
        async (sheetId: string) => {
            if (!activeThreadId) return;
            const sheet = sheets.find((s) => s.sheet_id === sheetId);
            if (!sheet) return;
            setThreadSheets((prev) => [...prev, sheet]);
            setSelectedSheetIds((prev) => new Set(prev).add(sheetId));
            await addSheetToThread(activeThreadId, sheetId);
        },
        [activeThreadId, sheets, addSheetToThread]
    );

    const handleRemoveSheet = useCallback(
        async (sheetId: string) => {
            if (!activeThreadId) return;
            setThreadSheets((prev) => prev.filter((s) => s.sheet_id !== sheetId));
            setSelectedSheetIds((prev) => {
                const next = new Set(prev);
                next.delete(sheetId);
                return next;
            });
            await removeSheetFromThread(activeThreadId, sheetId);
        },
        [activeThreadId, removeSheetFromThread]
    );

    // --- Thread handlers ---
    const handleCreateThread = useCallback(async () => {
        await createThread([]);
    }, [createThread]);

    const handleSelectThread = useCallback(
        (threadId: string) => {
            selectThread(threadId);
            setQuery("");
        },
        [selectThread]
    );

    const handleDeleteThread = useCallback(
        async (threadId: string) => {
            await removeThread(threadId);
        },
        [removeThread]
    );

    const handleRenameThread = useCallback(
        async (threadId: string, title: string) => {
            await renameThread(threadId, title);
        },
        [renameThread]
    );

    // --- Query handler ---
    const handleSendMessage = useCallback(
        (q: string) => {
            if (!activeThreadId) return;
            sendMessage(q);
            setQuery("");
        },
        [activeThreadId, sendMessage]
    );

    // Active thread title
    const activeThread = threads.find((t) => t.thread_id === activeThreadId);
    const threadTitle = activeThread?.title || "New Thread";

    return (
        <div className="flex h-screen flex-col bg-slate-50">
            {/* Header */}
            <header className="flex h-14 shrink-0 items-center gap-2 border-b border-slate-200 bg-white px-4">
                <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-primary">
                    <Table2 className="h-4 w-4 text-primary-foreground" />
                </div>
                <span className="text-base font-bold tracking-tight">Excel Analyst</span>
            </header>

            {/* Main layout: sidebar + content */}
            <div className="flex flex-1 overflow-hidden">
                {/* Sidebar */}
                <Sidebar
                    sheets={sheets}
                    files={files}
                    threads={threads}
                    selectedSheetIds={selectedSheetIds}
                    activeThreadId={activeThreadId}
                    onToggleSheet={toggleSheet}
                    onRefetchSheets={refetchSheets}
                    onRemoveFile={removeFile}
                    onSaveDescription={saveDescription}
                    onSelectThread={handleSelectThread}
                    onCreateThread={handleCreateThread}
                    onDeleteThread={handleDeleteThread}
                    onRenameThread={handleRenameThread}
                />

                {/* Main content */}
                <main className="flex-1 overflow-hidden">
                    {activeThreadId ? (
                        <ThreadView
                            threadTitle={threadTitle}
                            selectedSheets={threadSheets}
                            allSheets={sheets}
                            messages={messages}
                            streaming={streaming}
                            isLoading={isStreaming}
                            error={error}
                            onAddSheet={handleAddSheet}
                            onRemoveSheet={handleRemoveSheet}
                            onSendMessage={handleSendMessage}
                            query={query}
                            onQueryChange={setQuery}
                        />
                    ) : (
                        <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
                            <Table2 className="h-12 w-12 text-slate-300" />
                            <p className="text-sm text-muted-foreground">
                                Select a thread or create a new one to start asking questions.
                            </p>
                        </div>
                    )}
                </main>
            </div>
        </div>
    );
}

export default App;

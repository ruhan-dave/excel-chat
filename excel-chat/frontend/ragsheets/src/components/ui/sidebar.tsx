import { SheetList } from "./sheet-list";
import { ThreadList } from "./thread-list";
import { FileSpreadsheet, MessageSquareText } from "lucide-react";
import type { SheetInfo, FileInfo, ThreadSummary } from "@/lib/api";

interface SidebarProps {
    sheets: SheetInfo[];
    files: FileInfo[];
    threads: ThreadSummary[];
    selectedSheetIds: Set<string>;
    activeThreadId: string | null;
    onToggleSheet: (sheetId: string) => void;
    onRefetchSheets: () => void;
    onRemoveFile: (fileId: string) => void;
    onSaveDescription: (sheetId: string, description: string) => Promise<void>;
    onSelectThread: (threadId: string) => void;
    onCreateThread: () => void;
    onDeleteThread: (threadId: string) => void;
    onRenameThread: (threadId: string, title: string) => Promise<void>;
}

export function Sidebar({
    sheets,
    files,
    threads,
    selectedSheetIds,
    activeThreadId,
    onToggleSheet,
    onRefetchSheets,
    onRemoveFile,
    onSaveDescription,
    onSelectThread,
    onCreateThread,
    onDeleteThread,
    onRenameThread,
}: SidebarProps) {
    return (
        <aside className="flex h-full w-[280px] shrink-0 flex-col border-r border-slate-200 bg-white">
            {/* Sheets Section */}
            <div className="border-b border-slate-200">
                <div className="flex items-center gap-2 px-3 py-2.5 border-b border-slate-100">
                    <FileSpreadsheet className="h-4 w-4 text-muted-foreground" />
                    <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                        Sheets
                    </h3>
                    <span className="ml-auto text-xs text-muted-foreground">
                        {selectedSheetIds.size} selected
                    </span>
                </div>
                <div className="max-h-[40vh] overflow-y-auto px-3 py-3">
                    <SheetList
                        sheets={sheets}
                        files={files}
                        selectedSheetIds={selectedSheetIds}
                        onToggleSheet={onToggleSheet}
                        onRefetch={onRefetchSheets}
                        onRemoveFile={onRemoveFile}
                        onSaveDescription={onSaveDescription}
                    />
                </div>
            </div>

            {/* Threads Section */}
            <div className="flex flex-1 flex-col overflow-hidden">
                <div className="flex items-center gap-2 px-3 py-2.5 border-b border-slate-100">
                    <MessageSquareText className="h-4 w-4 text-muted-foreground" />
                    <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                        Threads
                    </h3>
                </div>
                <div className="flex-1 overflow-y-auto px-3 py-3">
                    <ThreadList
                        threads={threads}
                        activeThreadId={activeThreadId}
                        onSelectThread={onSelectThread}
                        onCreateThread={onCreateThread}
                        onDeleteThread={onDeleteThread}
                        onRenameThread={onRenameThread}
                    />
                </div>
            </div>
        </aside>
    );
}

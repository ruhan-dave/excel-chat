import { useState } from "react";
import { Button } from "./button";
import { Input } from "./input";
import {
    MessageSquareText,
    Plus,
    Trash2,
    Pencil,
    Check,
    X,
} from "lucide-react";
import type { ThreadSummary } from "@/lib/api";

interface ThreadListProps {
    threads: ThreadSummary[];
    activeThreadId: string | null;
    onSelectThread: (threadId: string) => void;
    onCreateThread: () => void;
    onDeleteThread: (threadId: string) => void;
    onRenameThread: (threadId: string, title: string) => Promise<void>;
}

export function ThreadList({
    threads,
    activeThreadId,
    onSelectThread,
    onCreateThread,
    onDeleteThread,
    onRenameThread,
}: ThreadListProps) {
    const [editingId, setEditingId] = useState<string | null>(null);
    const [editTitle, setEditTitle] = useState("");

    const startEditing = (thread: ThreadSummary) => {
        setEditingId(thread.thread_id);
        setEditTitle(thread.title);
    };

    const handleRename = async (threadId: string) => {
        if (editTitle.trim()) {
            await onRenameThread(threadId, editTitle.trim());
        }
        setEditingId(null);
        setEditTitle("");
    };

    return (
        <div className="flex flex-col gap-1.5">
            {/* New Thread button */}
            <Button
                size="sm"
                variant="outline"
                className="w-full justify-start gap-2"
                onClick={onCreateThread}
            >
                <Plus className="h-4 w-4" />
                New Thread
            </Button>

            {threads.length === 0 && (
                <p className="text-xs text-muted-foreground py-2 px-1">
                    No threads yet. Start a new one to ask questions.
                </p>
            )}

            {/* Thread list */}
            <div className="flex flex-col gap-0.5">
                {threads.map((thread) => {
                    const isActive = thread.thread_id === activeThreadId;
                    const isEditing = editingId === thread.thread_id;

                    return (
                        <div
                            key={thread.thread_id}
                            className={`group relative rounded-md transition-colors ${
                                isActive
                                    ? "bg-blue-50 border border-blue-200"
                                    : "hover:bg-slate-50 border border-transparent"
                            }`}
                        >
                            {isEditing ? (
                                <div className="flex items-center gap-1.5 px-2 py-1.5">
                                    <Input
                                        value={editTitle}
                                        onChange={(e) => setEditTitle(e.target.value)}
                                        onKeyDown={(e) => {
                                            if (e.key === "Enter") handleRename(thread.thread_id);
                                            if (e.key === "Escape") {
                                                setEditingId(null);
                                                setEditTitle("");
                                            }
                                        }}
                                        className="h-7 text-xs"
                                        autoFocus
                                    />
                                    <button
                                        className="shrink-0 text-green-600"
                                        onClick={() => handleRename(thread.thread_id)}
                                    >
                                        <Check className="h-3.5 w-3.5" />
                                    </button>
                                    <button
                                        className="shrink-0 text-muted-foreground"
                                        onClick={() => {
                                            setEditingId(null);
                                            setEditTitle("");
                                        }}
                                    >
                                        <X className="h-3.5 w-3.5" />
                                    </button>
                                </div>
                            ) : (
                                <div
                                    className="flex cursor-pointer items-center gap-2 px-2.5 py-2"
                                    onClick={() => onSelectThread(thread.thread_id)}
                                >
                                    <MessageSquareText
                                        className={`h-3.5 w-3.5 shrink-0 ${
                                            isActive ? "text-blue-600" : "text-muted-foreground"
                                        }`}
                                    />
                                    <div className="flex-1 overflow-hidden">
                                        <div
                                            className={`truncate text-xs font-medium ${
                                                isActive ? "text-blue-900" : "text-slate-700"
                                            }`}
                                        >
                                            {thread.title}
                                        </div>
                                        {thread.last_message_preview && (
                                            <div className="truncate text-[10px] text-muted-foreground">
                                                {thread.last_message_preview}
                                            </div>
                                        )}
                                    </div>
                                    <div className="flex shrink-0 items-center opacity-0 group-hover:opacity-100 transition-opacity">
                                        <button
                                            className="text-muted-foreground hover:text-foreground p-0.5"
                                            onClick={(e) => {
                                                e.stopPropagation();
                                                startEditing(thread);
                                            }}
                                        >
                                            <Pencil className="h-3 w-3" />
                                        </button>
                                        <button
                                            className="text-muted-foreground hover:text-destructive p-0.5"
                                            onClick={(e) => {
                                                e.stopPropagation();
                                                onDeleteThread(thread.thread_id);
                                            }}
                                        >
                                            <Trash2 className="h-3 w-3" />
                                        </button>
                                    </div>
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>
        </div>
    );
}

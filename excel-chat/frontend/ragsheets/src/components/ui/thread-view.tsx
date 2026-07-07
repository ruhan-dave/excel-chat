import { useRef, useEffect } from "react";
import { Textarea } from "./textarea";
import { Button } from "./button";
import { SheetSelectorBar } from "./sheet-selector-bar";
import { ConversationHistory } from "./conversation-history";
import {
    SendHorizontal,
    Loader2,
    Calculator,
    CheckCircle2,
    Sparkles,
    MessageSquareText,
} from "lucide-react";
import type { SheetInfo, MessageInfo } from "@/lib/api";
import type { StreamingState, StreamStep } from "@/hooks/useConversation";

interface ThreadViewProps {
    threadTitle: string;
    selectedSheets: SheetInfo[];
    allSheets: SheetInfo[];
    messages: MessageInfo[];
    streaming: StreamingState;
    isLoading: boolean;
    error: string | null;
    onAddSheet: (sheetId: string) => void;
    onRemoveSheet: (sheetId: string) => void;
    onSendMessage: (query: string) => void;
    query: string;
    onQueryChange: (query: string) => void;
}

export function ThreadView({
    threadTitle,
    selectedSheets,
    allSheets,
    messages,
    streaming,
    isLoading,
    error,
    onAddSheet,
    onRemoveSheet,
    onSendMessage,
    query,
    onQueryChange,
}: ThreadViewProps) {
    const scrollRef = useRef<HTMLDivElement>(null);

    // Auto-scroll to bottom when messages or streaming state changes
    useEffect(() => {
        if (scrollRef.current) {
            scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
        }
    }, [messages, streaming.friendlyResponse, streaming.statusMsg]);

    const hasSheets = selectedSheets.length > 0;

    // Build streaming display blocks
    let streamingBlock = null;
    if (isLoading || streaming.statusMsg || streaming.friendlyResponse || streaming.steps.length > 0) {
        let stepsBlock = null;
        if (streaming.steps.length > 0) {
            stepsBlock = (
                <div className="rounded-lg border bg-white shadow-sm">
                    <div className="flex items-center gap-2 border-b px-4 py-2.5">
                        <Calculator className="h-4 w-4 text-muted-foreground" />
                        <h3 className="text-sm font-semibold">Progress</h3>
                    </div>
                    <div className="divide-y">
                        {streaming.steps.map((step: StreamStep, i: number) => (
                            <div key={i} className="flex items-start gap-3 px-4 py-2.5">
                                {step.status === "done" ? (
                                    <CheckCircle2 className="h-4 w-4 shrink-0 text-green-600 mt-0.5" />
                                ) : (
                                    <Loader2 className="h-4 w-4 shrink-0 animate-spin text-blue-600 mt-0.5" />
                                )}
                                <div className="flex-1">
                                    <span className="text-sm font-medium text-slate-700">{step.label}</span>
                                    <span className="ml-2 text-sm text-slate-500">{step.detail}</span>
                                </div>
                            </div>
                        ))}
                    </div>
                </div>
            );
        }

        let friendlyBlock = null;
        if (streaming.friendlyResponse) {
            friendlyBlock = (
                <div className="flex justify-start">
                    <div className="max-w-[85%] rounded-lg border border-blue-100 bg-blue-50/50 px-4 py-3 shadow-sm">
                        <div className="flex items-center gap-2 border-b border-blue-100 pb-1.5 mb-2">
                            <Sparkles className="h-4 w-4 text-blue-600" />
                            <h3 className="text-sm font-semibold text-blue-900">Answer</h3>
                            {streaming.isCached && (
                                <span className="text-[10px] text-blue-500">
                                    ⚡ {streaming.cacheType === "thread_memory" ? "Thread memory" : "Cache"}
                                </span>
                            )}
                        </div>
                        <div className="text-sm text-blue-900 whitespace-pre-wrap leading-relaxed">
                            {streaming.friendlyResponse}
                        </div>
                    </div>
                </div>
            );
        }

        let statusBlock = null;
        if (isLoading && streaming.statusMsg) {
            statusBlock = (
                <div className="flex items-center gap-2 rounded-lg border border-blue-100 bg-blue-50/50 px-4 py-2.5">
                    <Loader2 className="h-4 w-4 animate-spin text-blue-600" />
                    <span className="text-sm font-medium text-blue-900">{streaming.statusMsg}</span>
                </div>
            );
        }

        streamingBlock = (
            <div className="flex flex-col gap-3">
                {statusBlock}
                {stepsBlock}
                {friendlyBlock}
            </div>
        );
    }

    let errorBlock = null;
    if (error) {
        errorBlock = (
            <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-2.5">
                <span className="text-sm text-red-700">{error}</span>
            </div>
        );
    }

    return (
        <div className="flex h-full flex-col">
            {/* Thread header + sheet selector */}
            <div className="border-b border-slate-200 px-6 py-4">
                <div className="flex items-center gap-2 mb-3">
                    <MessageSquareText className="h-5 w-5 text-muted-foreground" />
                    <h2 className="text-lg font-semibold tracking-tight">{threadTitle}</h2>
                </div>
                <SheetSelectorBar
                    selectedSheets={selectedSheets}
                    allSheets={allSheets}
                    onAddSheet={onAddSheet}
                    onRemoveSheet={onRemoveSheet}
                />
            </div>

            {/* Conversation history + streaming */}
            <div
                ref={scrollRef}
                className="flex-1 overflow-y-auto px-6 py-4"
            >
                {messages.length === 0 && !isLoading && !error && (
                    <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
                        <MessageSquareText className="h-12 w-12 text-slate-300" />
                        <p className="text-sm text-muted-foreground">
                            {hasSheets
                                ? "Ask a question about your sheets to get started."
                                : "Select sheets from the sidebar to begin asking questions."}
                        </p>
                    </div>
                )}

                <div className="flex flex-col gap-4">
                    <ConversationHistory messages={messages} />
                    {streamingBlock}
                    {errorBlock}
                </div>
            </div>

            {/* Query input */}
            <div className="border-t border-slate-200 px-6 py-4">
                <div className="flex items-end gap-3">
                    <Textarea
                        value={query}
                        onChange={(e) => onQueryChange(e.target.value)}
                        placeholder={
                            hasSheets
                                ? "Ask a question about your data..."
                                : "Select sheets first to ask questions..."
                        }
                        className="min-h-[80px] resize-none"
                        disabled={!hasSheets}
                        onKeyDown={(e) => {
                            if (e.key === "Enter" && !e.shiftKey) {
                                e.preventDefault();
                                if (query.trim() && hasSheets && !isLoading) {
                                    onSendMessage(query);
                                }
                            }
                        }}
                    />
                    <Button
                        onClick={() => onSendMessage(query)}
                        disabled={!query.trim() || !hasSheets || isLoading}
                        className="h-[80px] shrink-0"
                    >
                        {isLoading ? (
                            <Loader2 className="h-5 w-5 animate-spin" />
                        ) : (
                            <SendHorizontal className="h-5 w-5" />
                        )}
                    </Button>
                </div>
            </div>
        </div>
    );
}

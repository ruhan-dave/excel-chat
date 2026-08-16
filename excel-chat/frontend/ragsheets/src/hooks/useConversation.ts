import { useState, useRef, useCallback, useEffect } from "react";
import {
    fetchThreadDetail,
    buildStreamUrl,
    type MessageInfo,
    type SheetInfo,
} from "@/lib/api";

export interface StreamStep {
    label: string;
    detail: string;
    status: "done" | "active";
}

export interface StreamingState {
    statusMsg: string;
    steps: StreamStep[];
    planData: Record<string, unknown> | null;
    answer: Record<string, unknown>;
    friendlyResponse: string;
    isCached: boolean;
    cacheType: string | null;
}

const initialStreamingState: StreamingState = {
    statusMsg: "",
    steps: [],
    planData: null,
    answer: {},
    friendlyResponse: "",
    isCached: false,
    cacheType: null,
};

export function useConversation(
    threadId: string | null,
    threadSheets: SheetInfo[],
    onMessagePersisted?: () => void
) {
    const [messages, setMessages] = useState<MessageInfo[]>([]);
    const [isLoading, setLoading] = useState(false);
    const [streaming, setStreaming] = useState<StreamingState>(initialStreamingState);
    const [error, setError] = useState<string | null>(null);
    const eventSourceRef = useRef<EventSource | null>(null);

    // Load messages when thread changes
    const loadMessages = useCallback(async () => {
        if (!threadId) {
            setMessages([]);
            return;
        }
        try {
            const detail = await fetchThreadDetail(threadId);
            setMessages(detail.messages);
        } catch (err) {
            console.error("Failed to load thread messages:", err);
            setMessages([]);
        }
    }, [threadId]);

    useEffect(() => {
        loadMessages();
    }, [loadMessages]);

    const addStep = useCallback((label: string, detail: string) => {
        setStreaming((prev) => {
            const existing = prev.steps.find((s) => s.label === label);
            if (existing) {
                return {
                    ...prev,
                    steps: prev.steps.map((s) =>
                        s.label === label ? { ...s, detail, status: "done" as const } : s
                    ),
                };
            }
            return {
                ...prev,
                steps: [...prev.steps.map((s) => ({ ...s, status: "done" as const })), { label, detail, status: "done" as const }],
            };
        });
    }, []);

    const sendMessage = useCallback(
        (query: string) => {
            if (!threadId || !query.trim()) return;

            if (eventSourceRef.current) {
                eventSourceRef.current.close();
            }

            setLoading(true);
            setError(null);
            setStreaming({
                ...initialStreamingState,
                statusMsg: "Connecting…",
            });

            const sheetIds = threadSheets.map((s) => s.sheet_id);
            const url = buildStreamUrl(query, threadId, sheetIds);
            const es = new EventSource(url);
            eventSourceRef.current = es;

            es.addEventListener("status", (e: MessageEvent) => {
                const data = JSON.parse(e.data);
                setStreaming((prev) => ({ ...prev, statusMsg: data.message }));
            });

            es.addEventListener("plan", (e: MessageEvent) => {
                const data = JSON.parse(e.data);
                setStreaming((prev) => ({ ...prev, planData: data }));
                const taskLabel = data.task_type?.replace(/_/g, " ") ?? "analysis";
                addStep("Classified Intent", `Task type: ${taskLabel}`);
                if (data.plan && Object.keys(data.plan).length > 0) {
                    const planEntries = Object.entries(data.plan) as [string, { action: string; args: string[] }][];
                    const planText = planEntries
                        .map(([step, info]) => `${step}: ${info.action}(${(info.args || []).join(", ")})`)
                        .join("\n");
                    addStep("Execution Plan", planText);
                } else if (data.items && data.items.length > 0) {
                    addStep("Execution Plan", `Items to retrieve:\n${data.items.join("\n")}`);
                } else if (data.description) {
                    addStep("Execution Plan", data.description);
                }
            });

            es.addEventListener("pre_populated", (e: MessageEvent) => {
                const data = JSON.parse(e.data);
                const values = data.values || {};
                const entries = Object.entries(values);
                const valueText = entries
                    .map(([key, val]) => `${key}: ${typeof val === "object" ? JSON.stringify(val) : String(val)}`)
                    .join("\n");
                addStep("Data Retrieved", valueText || "No values fetched");
            });

            es.addEventListener("execution", (e: MessageEvent) => {
                const data = JSON.parse(e.data);
                const stepResults = data.step_results || {};
                const entries = Object.entries(stepResults);
                const resultsText = entries
                    .map(([key, val]) => `${key}: ${typeof val === "object" ? JSON.stringify(val) : String(val)}`)
                    .join("\n");
                if (data.final_answer !== undefined) {
                    addStep("Calculations Complete", `Final answer: ${typeof data.final_answer === "object" ? JSON.stringify(data.final_answer) : String(data.final_answer)}${resultsText ? "\n\n" + resultsText : ""}`);
                } else {
                    addStep("Calculations Complete", resultsText || "No results");
                }
                setStreaming((prev) => ({ ...prev, answer: stepResults }));
            });

            es.addEventListener("friendly", (e: MessageEvent) => {
                const data = JSON.parse(e.data);
                setStreaming((prev) => ({ ...prev, friendlyResponse: data.response || "" }));
            });

            es.addEventListener("cached", (e: MessageEvent) => {
                const data = JSON.parse(e.data);
                setStreaming((prev) => ({
                    ...prev,
                    answer: data.answer || {},
                    friendlyResponse: data.friendly_response || "",
                    isCached: true,
                    cacheType: data.cache_type || "semantic",
                }));
                addStep(
                    "Cache Hit",
                    data.cache_type === "thread_memory"
                        ? `Previously asked in this thread (similarity: ${((data.similarity || 0) * 100).toFixed(1)}%)`
                        : `Retrieved from cache (similarity: ${((data.similarity || 0) * 100).toFixed(1)}%)`
                );
            });

            es.addEventListener("done", (e: MessageEvent) => {
                const data = JSON.parse(e.data);
                addStep("Complete", `Total time: ${data.total?.toFixed(1) || "?"}s`);
                setLoading(false);
                setStreaming((prev) => ({ ...prev, statusMsg: "" }));
                es.close();
                eventSourceRef.current = null;
                // Reload messages to get the persisted Q&A
                loadMessages().then(() => onMessagePersisted?.());
                // Reset streaming state after a short delay so the UI can show "Complete"
                setTimeout(() => {
                    setStreaming(initialStreamingState);
                }, 500);
            });

            es.addEventListener("error", (e: MessageEvent) => {
                let errorMsg = "Failed to get response from server.";
                try {
                    if (e.data) {
                        const data = JSON.parse(e.data);
                        errorMsg = data.message || errorMsg;
                    }
                } catch {
                    if (isLoading) {
                        errorMsg = "Connection lost. Please try again.";
                    }
                }
                setError(errorMsg);
                setStreaming(initialStreamingState);
                setLoading(false);
                es.close();
                eventSourceRef.current = null;
            });
        },
        [threadId, threadSheets, addStep, loadMessages, onMessagePersisted, isLoading]
    );

    const cancelStreaming = useCallback(() => {
        if (eventSourceRef.current) {
            eventSourceRef.current.close();
            eventSourceRef.current = null;
        }
        setLoading(false);
        setStreaming(initialStreamingState);
    }, []);

    return {
        messages,
        isLoading,
        streaming,
        error,
        sendMessage,
        cancelStreaming,
        reloadMessages: loadMessages,
    };
}

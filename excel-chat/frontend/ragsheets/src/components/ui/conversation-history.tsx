import { useState } from "react";
import {
    Calculator,
    Clock,
    ChevronDown,
    ChevronUp,
} from "lucide-react";
import type { MessageInfo } from "@/lib/api";

interface MessageBubbleProps {
    message: MessageInfo;
}

function MessageBubble({ message }: MessageBubbleProps) {
    const [showDetails, setShowDetails] = useState(false);

    const isUser = message.role === "user";

    let fullResult: Record<string, unknown> | null = null;
    if (message.full_result) {
        try {
            fullResult = JSON.parse(message.full_result);
        } catch {
            // ignore
        }
    }

    const plan = fullResult?.plan as Record<string, { action: string; args: string[] }> | undefined;
    const stepResults = fullResult?.step_results as Record<string, unknown> | undefined;
    const taskType = (fullResult?.task_type as string || "").replace(/_/g, " ");

    return (
        <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
            <div
                className={`max-w-[85%] rounded-lg px-4 py-3 ${
                    isUser
                        ? "bg-blue-600 text-white"
                        : "bg-white border border-slate-200 shadow-sm"
                }`}
            >
                {/* Content */}
                <div
                    className={`text-sm whitespace-pre-wrap leading-relaxed ${
                        isUser ? "text-white" : "text-slate-800"
                    }`}
                >
                    {message.content || message.friendly_response || ""}
                </div>

                {/* Cached badge */}
                {message.cached && (
                    <div className={`mt-1.5 text-[10px] ${isUser ? "text-blue-100" : "text-muted-foreground"}`}>
                        ⚡ From cache
                    </div>
                )}

                {/* Collapsible details for assistant messages */}
                {!isUser && fullResult && (plan || stepResults) && (
                    <div className="mt-2 border-t border-slate-100 pt-2">
                        <button
                            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                            onClick={() => setShowDetails(!showDetails)}
                        >
                            {showDetails ? (
                                <ChevronUp className="h-3 w-3" />
                            ) : (
                                <ChevronDown className="h-3 w-3" />
                            )}
                            Details
                            {taskType && (
                                <span className="ml-1 capitalize">{taskType}</span>
                            )}
                        </button>

                        {showDetails && (
                            <div className="mt-2 space-y-2">
                                {plan && Object.keys(plan).length > 0 && (
                                    <div className="rounded-md bg-slate-50 p-2">
                                        <div className="flex items-center gap-1 text-xs font-medium text-slate-600 mb-1">
                                            <Clock className="h-3 w-3" />
                                            Plan
                                        </div>
                                        {Object.entries(plan).map(([step, info]) => (
                                            <div key={step} className="text-xs text-slate-500 font-mono pl-3">
                                                {step}: {info.action}({(info.args || []).join(", ")})
                                            </div>
                                        ))}
                                    </div>
                                )}
                                {stepResults && Object.keys(stepResults).length > 0 && (
                                    <div className="rounded-md bg-slate-50 p-2">
                                        <div className="flex items-center gap-1 text-xs font-medium text-slate-600 mb-1">
                                            <Calculator className="h-3 w-3" />
                                            Results
                                        </div>
                                        {Object.entries(stepResults).map(([key, value]) => (
                                            <div key={key} className="text-xs text-slate-600 pl-3">
                                                <span className="font-medium">{key}:</span>{" "}
                                                {typeof value === "object" && value !== null
                                                    ? JSON.stringify(value, null, 2)
                                                    : String(value)}
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}

interface ConversationHistoryProps {
    messages: MessageInfo[];
}

export function ConversationHistory({ messages }: ConversationHistoryProps) {
    if (messages.length === 0) {
        return null;
    }

    return (
        <div className="flex flex-col gap-4 overflow-y-auto">
            {messages.map((msg) => (
                <MessageBubble key={msg.message_id} message={msg} />
            ))}
        </div>
    );
}

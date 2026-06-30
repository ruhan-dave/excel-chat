import { Textarea } from "@/components/ui/textarea";
import { Button } from "./button";
import { useState, useRef, useCallback } from "react";
import { SendHorizontal, Calculator, Sparkles, Loader2, CheckCircle2, Clock } from "lucide-react";

interface StreamStep {
    label: string;
    detail: string;
    status: "done" | "active";
}

const PromptInput = () => {
    const [isLoading, setLoading] = useState(false);
    const [query, setQuery] = useState("");
    const [answer, setAnswer] = useState<Record<string, unknown>>({});
    const [friendlyResponse, setFriendlyResponse] = useState("");
    const [statusMsg, setStatusMsg] = useState("");
    const [steps, setSteps] = useState<StreamStep[]>([]);
    const [planData, setPlanData] = useState<Record<string, unknown> | null>(null);
    const eventSourceRef = useRef<EventSource | null>(null);
    const apiURL = import.meta.env.VITE_API_ENDPOINT;

    const addStep = useCallback((label: string, detail: string) => {
        setSteps(prev => {
            const existing = prev.find(s => s.label === label);
            if (existing) {
                return prev.map(s => s.label === label ? { ...s, detail, status: "done" as const } : s);
            }
            // Mark all previous as done
            return [...prev.map(s => ({ ...s, status: "done" as const })), { label, detail, status: "done" as const }];
        });
    }, []);

    const submitQuery = (query: string) => {
        if (eventSourceRef.current) {
            eventSourceRef.current.close();
        }
        setLoading(true);
        setAnswer({});
        setFriendlyResponse("");
        setStatusMsg("Connecting…");
        setSteps([]);
        setPlanData(null);

        const url = `${apiURL}/query/stream?query=${encodeURIComponent(query)}`;
        const es = new EventSource(url);
        eventSourceRef.current = es;

        es.addEventListener("status", (e: MessageEvent) => {
            const data = JSON.parse(e.data);
            setStatusMsg(data.message);
        });

        es.addEventListener("plan", (e: MessageEvent) => {
            const data = JSON.parse(e.data);
            setPlanData(data);
            const taskLabel = data.task_type?.replace(/_/g, " ") ?? "analysis";
            addStep("Classified Intent", `Task type: ${taskLabel}`);
            if (data.plan && Object.keys(data.plan).length > 0) {
                const stepCount = Object.keys(data.plan).length;
                addStep("Execution Plan", `${stepCount} step(s) planned`);
            } else if (data.items && data.items.length > 0) {
                addStep("Execution Plan", `${data.items.length} item(s) to retrieve`);
            } else if (data.description) {
                addStep("Execution Plan", data.description);
            }
        });

        es.addEventListener("pre_populated", (e: MessageEvent) => {
            const data = JSON.parse(e.data);
            const count = data.values ? Object.keys(data.values).length : 0;
            addStep("Data Retrieved", `${count} value(s) fetched from sheets`);
        });

        es.addEventListener("execution", (e: MessageEvent) => {
            const data = JSON.parse(e.data);
            const stepResults = data.step_results || {};
            const stepCount = Object.keys(stepResults).length;
            addStep("Calculations Complete", `${stepCount} step(s) executed`);
            setAnswer(stepResults);
        });

        es.addEventListener("friendly", (e: MessageEvent) => {
            const data = JSON.parse(e.data);
            setFriendlyResponse(data.response || "");
        });

        es.addEventListener("cached", (e: MessageEvent) => {
            const data = JSON.parse(e.data);
            setAnswer(data.answer || {});
            setFriendlyResponse(data.friendly_response || "");
            addStep("Cache Hit", `Retrieved from cache (similarity: ${((data.similarity || 0) * 100).toFixed(1)}%)`);
        });

        es.addEventListener("done", (e: MessageEvent) => {
            const data = JSON.parse(e.data);
            addStep("Complete", `Total time: ${data.total?.toFixed(1) || "?"}s`);
            setLoading(false);
            setStatusMsg("");
            es.close();
            eventSourceRef.current = null;
        });

        es.addEventListener("error", (e: MessageEvent) => {
            let errorMsg = "Failed to get response from server.";
            try {
                if (e.data) {
                    const data = JSON.parse(e.data);
                    errorMsg = data.message || errorMsg;
                }
            } catch {
                // EventSource error event (connection issue)
                if (isLoading) {
                    errorMsg = "Connection lost. Please try again.";
                }
            }
            setAnswer({});
            setFriendlyResponse(`Error: ${errorMsg}`);
            setLoading(false);
            setStatusMsg("");
            es.close();
            eventSourceRef.current = null;
        });
    };

    let planBlock = null;
    let stepsBlock = null;
    let answerBlock = null;
    let friendlyBlock = null;

    if (planData) {
        const taskType = (planData.task_type as string || "").replace(/_/g, " ");
        const plan = planData.plan as Record<string, { action: string; args: string[] }> | null;
        const items = planData.items as string[] | null;

        const planDetails: string[] = [];
        if (plan) {
            for (const [stepName, step] of Object.entries(plan)) {
                planDetails.push(`${stepName}: ${step.action}(${(step.args || []).join(", ")})`);
            }
        } else if (items) {
            items.forEach((item, i) => planDetails.push(`item_${i + 1}: retrieve ${item}`));
        } else if (planData.description) {
            planDetails.push(planData.description as string);
        }

        planBlock = (
            <div className="rounded-lg border border-slate-200 bg-slate-50/50 shadow-sm">
                <div className="flex items-center gap-2 border-b px-4 py-3">
                    <Clock className="h-4 w-4 text-slate-500" />
                    <h3 className="text-sm font-semibold text-slate-700">Plan</h3>
                    <span className="ml-auto text-xs font-medium text-slate-500 capitalize">{taskType}</span>
                </div>
                <div className="px-4 py-3 space-y-1.5">
                    {planDetails.map((detail, i) => (
                        <div key={i} className="text-sm text-slate-600 font-mono">
                            {detail}
                        </div>
                    ))}
                </div>
            </div>
        );
    }

    if (steps.length > 0) {
        stepsBlock = (
            <div className="rounded-lg border bg-white shadow-sm">
                <div className="flex items-center gap-2 border-b px-4 py-3">
                    <Calculator className="h-4 w-4 text-muted-foreground" />
                    <h3 className="text-sm font-semibold">Progress</h3>
                </div>
                <div className="divide-y">
                    {steps.map((step, i) => (
                        <div key={i} className="flex items-start gap-3 px-4 py-3">
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

    if(Object.keys(answer).length > 0)
    {
        answerBlock = (
            <div className="rounded-lg border bg-white shadow-sm">
                <div className="flex items-center gap-2 border-b px-4 py-3">
                    <Calculator className="h-4 w-4 text-muted-foreground" />
                    <h3 className="text-sm font-semibold">Calculation Results</h3>
                </div>
                <div className="divide-y">
                    {Object.entries(answer).map(([key, value]) => (
                        <div key={key} className="flex items-start gap-4 px-4 py-3">
                            <span className="shrink-0 text-sm font-medium text-slate-600">{key}</span>
                            <span className="text-sm text-slate-900">
                                {typeof value === 'object' && value !== null
                                    ? JSON.stringify(value, null, 2)
                                    : String(value)}
                            </span>
                        </div>
                    ))}
                </div>
            </div>
        );
    }

    if(friendlyResponse)
    {
        friendlyBlock = (
            <div className="rounded-lg border border-blue-100 bg-blue-50/50 shadow-sm">
                <div className="flex items-center gap-2 border-b border-blue-100 px-4 py-3">
                    <Sparkles className="h-4 w-4 text-blue-600" />
                    <h3 className="text-sm font-semibold text-blue-900">Answer</h3>
                </div>
                <div className="px-4 py-4 text-sm text-blue-900 whitespace-pre-wrap leading-relaxed">
                    {friendlyResponse}
                </div>
            </div>
        );
    }

    return (
        <div className="space-y-6">
            {isLoading && statusMsg && (
                <div className="flex items-center gap-2 rounded-lg border border-blue-100 bg-blue-50/50 px-4 py-3">
                    <Loader2 className="h-4 w-4 animate-spin text-blue-600" />
                    <span className="text-sm font-medium text-blue-900">{statusMsg}</span>
                </div>
            )}
            <div className="flex items-end gap-3">
                <Textarea
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Ask a question about your data..."
                    className="min-h-[100px] resize-none"
                    onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                            e.preventDefault();
                            submitQuery(query);
                        }
                    }}
                />
                <Button
                    onClick={() => submitQuery(query)}
                    disabled={!query.trim() || isLoading}
                    className="h-[100px] shrink-0"
                >
                    <SendHorizontal className="h-5 w-5" />
                </Button>
            </div>
            <div className="space-y-4">
                {planBlock}
                {stepsBlock}
                {friendlyBlock}
                {answerBlock}
            </div>
        </div>
    );
};

export default PromptInput;

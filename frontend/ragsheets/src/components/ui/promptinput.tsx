import { Textarea } from "@/components/ui/textarea";
import { Button } from "./button";
import { useState } from "react";
import axios from "axios";
import BackdropWithSpinner from "./backdropWithSpinner";
import { SendHorizontal, Calculator, Sparkles } from "lucide-react";

const PromptInput = () => {
    const [isLoading, setLoading] = useState(false);
    const [query, setQuery] = useState("");
    const [answer, setAnswer] = useState<Record<string, unknown>>({});
    const [friendlyResponse, setFriendlyResponse] = useState("");
    const apiURL = import.meta.env.VITE_API_ENDPOINT;
    const submitQuery = async(query: string) => {
        setLoading(true);
        try {
            const response = await axios.get(`${apiURL}/query`, {
                params: {
                    query: query
                },
                timeout: 120000  // 2 minute timeout for LLM processing
            });
            if (response.data.error) {
                setAnswer({});
                setFriendlyResponse(`Error: ${response.data.error}`);
            } else {
                setAnswer(response.data.answer ?? {});
                setFriendlyResponse(response.data.friendly_response ?? "");
            }
        } catch (error) {
            console.error("Query failed:", error);
            setAnswer({});
            setFriendlyResponse("Error: Failed to get response from server. Please check if the file is uploaded correctly.");
        }
        setLoading(false);
    }

    let answerBlock = null;
    let friendlyBlock = null;

    if(Object.keys(answer).length > 0)
    {
        answerBlock = (
            <div className="rounded-lg border bg-white shadow-sm">
                <div className="flex items-center gap-2 border-b px-4 py-3">
                    <Calculator className="h-4 w-4 text-muted-foreground" />
                    <h3 className="text-sm font-semibold">Calculation Steps</h3>
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
                {friendlyBlock}
                {answerBlock}
            </div>
            {isLoading && <BackdropWithSpinner />}
        </div>
    );
};

export default PromptInput;

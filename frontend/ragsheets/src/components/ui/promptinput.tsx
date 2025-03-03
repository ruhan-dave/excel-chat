import { Textarea } from "@/components/ui/textarea";
import { Button } from "./button";
import { useState } from "react";
import axios from "axios";
import BackdropWithSpinner from "./backdropWithSpinner";

const MessageIcon = () => (
    <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" className="lucide lucide-send-horizontal">
        <path d="M3.714 3.048a.498.498 0 0 0-.683.627l2.843 7.627a2 2 0 0 1 0 1.396l-2.842 7.627a.498.498 0 0 0 .682.627l18-8.5a.5.5 0 0 0 0-.904z"/>
        <path d="M6 12h16"/>
    </svg>
);

const PromptInput = () => {
    const [isLoading, setLoading] = useState(false);
    const [query, setQuery] = useState("");
    const [answer, setAnswer] = useState("");
    const apiURL = import.meta.env.VITE_API_ENDPOINT;
    const submitQuery = async(query: string) => {
        setLoading(true);
        const response = await axios.get(`${apiURL}/query`, {
            params: {
                query: query
            }
        });
        setAnswer(response.data.answer);
        setLoading(false);
    }

    return (
        <div className="py-8 sm:py-8">
            <Textarea
                value={query}
                onChange = {(e) => setQuery(e.target.value)} 
                placeholder="Enter your query here!" />
            <Button onClick={(_) => submitQuery(query)} className="p-6 sm:p-6 rounded-2xl m-8 sm:m-8">
                <MessageIcon />
            </Button>
            {answer.length > 0 && <p>{answer}</p>}
            {isLoading && <BackdropWithSpinner />}
        </div>
    );
};

export default PromptInput;

import { Textarea } from "@/components/ui/textarea";


const PromptInput = () => {
    return (
        <div className="py-8  sm:py-8 flex flex-row">
            <Textarea placeholder = "enter your query here!" />
        </div>
    );
};

export default PromptInput;
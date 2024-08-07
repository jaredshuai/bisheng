import ChatInput from "./ChatInput";
import MessagePanne from "./MessagePanne";

export default function ChatComponent({
    stop = false,
    clear = false,
    questions = [],
    form = false,
    useName,
    inputForm = null,
    guideWord,
    wsUrl,
    onBeforSend,
    loadMore = () => { }
}) {

    return <div className="relative h-full">
        <MessagePanne useName={useName} guideWord={guideWord} loadMore={loadMore}></MessagePanne>
        <ChatInput clear={clear} questions={questions} form={form} wsUrl={wsUrl} inputForm={inputForm} onBeforSend={onBeforSend} ></ChatInput>
    </div>
};

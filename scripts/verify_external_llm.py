from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from typing import Any

from voice_assistant.backends import OpenAICompatibleChat
from voice_assistant.config import Config


class _MockChatHandler(BaseHTTPRequestHandler):
    request_payload: dict[str, Any] = {}

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        type(self).request_payload = json.loads(self.rfile.read(length))
        chunks = (
            {"choices": [{"delta": {"content": "Внешний "}}]},
            {"choices": [{"delta": {"content": "контур готов"}}]},
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        for chunk in chunks:
            line = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockChatHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        chat = OpenAICompatibleChat(
            Config.defaults().llm,
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model="mock-rnd-model",
        )
        response = "".join(
            chat.stream_reply(
                "Проверь подключение",
                history=[
                    {"role": "user", "content": "Предыдущий вопрос"},
                    {"role": "assistant", "content": "Предыдущий ответ"},
                ],
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    messages = _MockChatHandler.request_payload.get("messages", [])
    report = {
        "endpoint": "/v1/chat/completions",
        "model": _MockChatHandler.request_payload.get("model"),
        "stream": _MockChatHandler.request_payload.get("stream"),
        "roles": [message.get("role") for message in messages],
        "response": response,
        "ok": response == "Внешний контур готов",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Loopback-only OpenAI-compatible fixture for the five API follow-up checks."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SQL_BY_QUESTION = {
    "一共有多少笔订单？": "SELECT COUNT(*) AS 订单笔数 FROM orders",
    "列出订单编号、地区和下单日期，按订单编号升序排列": "SELECT id, region, ordered_on FROM orders ORDER BY id",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/healthz" else 404)
        self.end_headers()

    def do_POST(self):
        if self.path != "/chat/completions":
            self.send_error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 128 * 1024:
                raise ValueError
            payload = json.loads(self.rfile.read(size))
            prompt = payload["messages"][-1]["content"]
            question = prompt.split("\nQuestion:\n", 1)[1].splitlines()[0]
            sql = SQL_BY_QUESTION[question]
        except (ValueError, KeyError, IndexError, TypeError):
            self.send_error(422, "No recorded response for this request")
            return
        result = {"choices": [{"message": {"content": json.dumps({"sql": sql})}}],
                  "usage": {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27}}
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    with ThreadingHTTPServer(("127.0.0.1", 18080), Handler) as server:
        server.serve_forever()

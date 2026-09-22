// agent-relay: 훅(POST /event)을 받아 구독자(WebSocket /subscribe)에게 즉시 밀어 준다. 저장 없음, 폴링 없음.
// 인증: 둘 다 ?token=<RELAY_TOKEN> (wrangler secret).
export class Hub {
  constructor(state) { this.state = state; this.subs = new Set(); }
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/subscribe") {
      const pair = new WebSocketPair(); const [client, server] = Object.values(pair);
      server.accept(); this.subs.add(server);
      server.addEventListener("close", () => this.subs.delete(server));
      server.addEventListener("error", () => this.subs.delete(server));
      server.send(JSON.stringify({ type: "hello", subscribers: this.subs.size, ts: new Date().toISOString() }));
      return new Response(null, { status: 101, webSocket: client });
    }
    if (url.pathname === "/event" && req.method === "POST") {
      const body = await req.text(); let n = 0;
      for (const ws of this.subs) { try { ws.send(body); n++; } catch { this.subs.delete(ws); } }
      return new Response(JSON.stringify({ delivered: n }), { headers: { "content-type": "application/json" } });
    }
    return new Response("not found", { status: 404 });
  }
}
export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/") return new Response("agent-relay ok");
    if (url.searchParams.get("token") !== env.RELAY_TOKEN) return new Response("forbidden", { status: 403 });
    const id = env.HUB.idFromName("main");
    return env.HUB.get(id).fetch(req);
  },
};

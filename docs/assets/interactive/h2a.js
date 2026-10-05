/* Interactive H2A walkthrough for docs/posts/h2a-envelope-interactive.md.
   Routes, payload keys and status codes come from src/mash/api/routes/agent.py,
   src/mash/api/routes/host.py, src/mash/runtime/client.py,
   src/mash/runtime/server.py and to_public_event in src/mash/runtime/requests.py.
   Ids and message text are illustrative. */
(function () {
  "use strict";

  var GH = "https://github.com/imsid/mashpy/blob/main/src/mash/";
  function code(s) { return "<code>" + s + "</code>"; }
  function link(label, path) {
    return '<a href="' + GH + path + '">' + code(label) + "</a>";
  }
  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function json(o) { return JSON.stringify(o, null, 2); }

  var RID = "req-7f3a";
  var ITR = "itr_4be19c0a7d21";
  var WFID = "assistant:" + RID;

  /* ---------- bindings and operations ---------- */

  var BINDINGS = {
    pool: {
      label: "Pool deploy (mash host serve)",
      blurb:
        "A pool deploy serves every agent behind one FastAPI app. The pool gives " +
        "each agent an InProcessAgentClient, so the host-to-client and " +
        "client-to-agent hops are Python calls inside one process.",
      lanes: [
        ["User application", "your app, CLI, or curl"],
        ["Host", "API routes /api/v1 + Pool"],
        ["Client", "InProcessAgentClient"],
        ["Agent", "AgentRuntime"],
      ],
      ops: {
        submit: {
          label: "Submit to an agent",
          hops: [
            {
              lane: 0,
              who: "User application",
              what: "POST /api/v1/agent/assistant/request",
              body:
                "<p>The caller addresses the host with the agent id in the path. " +
                code("message") + " and " + code("session_id") + " are required. " +
                code("metadata") + " is optional caller context that tools can read " +
                "and the model never sees. " + code("structured_output") +
                " is an optional JSON schema.</p>",
              payload: json({
                message: "Plan the next release.",
                session_id: "s-1",
                metadata: { tenant: "acme" },
              }),
            },
            {
              lane: 1,
              who: "Host",
              what: "submit_request route → pool.get_client(agent_id)",
              body:
                "<p>" + link("submit_request", "api/routes/agent.py") +
                " validates the body. An empty message or session id returns 400 " +
                code("INVALID_REQUEST") + ". The route then resolves the agent id " +
                "to its client with " + code("pool.get_client") + ".</p>",
              payload: json({
                error: { code: "INVALID_REQUEST", message: "session_id is required" },
              }) + "\n# returned with status 400 when session_id is empty",
            },
            {
              lane: 2,
              who: "Client",
              what: "InProcessAgentClient.post_request(...)",
              body:
                "<p>" + link("InProcessAgentClient.post_request", "runtime/client.py") +
                " calls " + code("runtime.submit_request") + " directly and returns " +
                "the " + code("request_id") + " from the accepted payload.</p>",
              payload:
                'request_id = await client.post_request(\n' +
                '    "Plan the next release.",\n' +
                '    session_id="s-1",\n' +
                '    metadata={"tenant": "acme"},\n' +
                ")",
            },
            {
              lane: 3,
              who: "Agent",
              what: "AgentRuntime.submit_request → request.accepted",
              body:
                "<p>The runtime mints " + code("request_id") + ", appends " +
                code("runtime.request.accepted") + " to " + code("runtime_event_log") +
                ", and starts the durable workflow " + code(WFID) +
                ". It returns once the workflow has started, without waiting for " +
                "the agent loop.</p>",
              payload: json({
                request_id: RID,
                agent_id: "assistant",
                session_id: "s-1",
                status: "accepted",
              }),
            },
            {
              lane: 0,
              who: "User application",
              what: "200 {data: {request_id}}",
              body:
                "<p>The route wraps the id in the API's " + code("{data: ...}") +
                " envelope. The caller opens the event stream next.</p>",
              payload: json({ data: { request_id: RID } }),
            },
          ],
        },
        host: {
          label: "Submit to a host",
          hops: [
            {
              lane: 0,
              who: "User application",
              what: "POST /api/v1/hosts/release-desk/request",
              body:
                "<p>The caller addresses a host composition instead of one agent. " +
                code("context") + " is optional prompt text added for this request.</p>",
              payload: json({
                message: "Plan the next release.",
                session_id: "s-1",
                context: "Release train: 0.24",
              }),
            },
            {
              lane: 1,
              who: "Host",
              what: "Pool.submit_host_request(host_id, ...)",
              body:
                "<p>" + link("submit_host_request", "runtime/host/host.py") +
                " looks up the host and its primary agent. An unknown host returns " +
                "404 " + code("HOST_NOT_FOUND") + ". This path calls the primary's " +
                "runtime directly instead of going through a client.</p>",
              payload:
                "host = self.get_host(host_id)\n" +
                "runtime = self.get_agent(host.primary)",
            },
            {
              lane: 3,
              who: "Agent",
              what: "submit_request(host_snapshot=...)",
              body:
                "<p>The runtime stores the host snapshot in the request metadata. " +
                code("context.load") + " uses it to build the primary's system " +
                "prompt with the subagent block, and the snapshot is part of the " +
                "recorded step output, so redefining the host later never changes " +
                "an in-flight request.</p>",
              payload:
                "await runtime.submit_request(\n" +
                "    message=message,\n" +
                "    session_id=session_id,\n" +
                "    host_snapshot=self.snapshot_for(host),\n" +
                "    context=context,\n" +
                ")",
            },
            {
              lane: 0,
              who: "User application",
              what: "200 {data: {request_id, agent_id, session_id}}",
              body:
                "<p>The response names the primary " + code("agent_id") +
                ". The caller streams from the agent route with that id.</p>",
              payload: json({
                data: { request_id: RID, agent_id: "concierge", session_id: "s-1" },
              }),
            },
          ],
        },
        stream: {
          label: "Stream events",
          hops: [
            {
              lane: 0,
              who: "User application",
              what: "GET /api/v1/agent/assistant/request/" + RID + "/events",
              body:
                "<p>The caller opens a server-sent events stream for one request.</p>",
              payload: "Accept: text/event-stream",
            },
            {
              lane: 1,
              who: "Host",
              what: "stream_request_events route → StreamingResponse",
              body:
                "<p>" + link("stream_request_events", "api/routes/agent.py") +
                " iterates " + code("client.stream_response(request_id)") +
                " and writes each event as one SSE frame. It stops after " +
                code("request.completed") + ", " + code("request.error") + ", or " +
                code("request.cancelled") + ".</p>",
              payload: "event: <name>\ndata: <json>\n\n",
            },
            {
              lane: 2,
              who: "Client",
              what: "InProcessAgentClient.stream_response loop",
              body:
                "<p>The client keeps a cursor starting at 0 and calls " +
                code("runtime.stream_response_events(request_id, cursor, wait_timeout=0.25)") +
                " until the runtime reports " + code("done") + ".</p>",
              payload:
                "events, cursor, done = await self.runtime.stream_response_events(\n" +
                "    request_id, cursor=cursor, wait_timeout=0.25,\n" +
                ")",
            },
            {
              lane: 3,
              who: "Agent",
              what: "read_request_stream(after_seq=cursor)",
              body:
                "<p>The runtime reads new rows and terminality in one store call, " +
                "so a caller that stops on " + code("done") + " has been handed the " +
                "terminal event. With nothing new, it waits on a waiter that the " +
                "store's Postgres " + code("LISTEN runtime_events") +
                " connection wakes. Each row goes through " +
                link("to_public_event", "runtime/requests.py") + ".</p>",
              payload:
                "stored_events, done = await self.runtime_store.read_request_stream(\n" +
                "    request_id, after_seq=cursor,\n" +
                ")\n" +
                "public_events = [to_public_event(e) for e in stored_events]",
            },
            {
              lane: 0,
              who: "User application",
              what: "SSE frames until a terminal event",
              body:
                "<p>The stream carries the lifecycle events plus " +
                code("agent.trace") + " frames for each step. The table below shows the full mapping.</p>",
              payload:
                "event: request.accepted\n" +
                'data: {"request_id": "' + RID + '", "agent_id": "assistant", "session_id": "s-1", "status": "accepted"}\n\n' +
                "event: request.started\n" +
                'data: {"request_id": "' + RID + '", "agent_id": "assistant", "session_id": "s-1", "status": "started"}\n\n' +
                "event: agent.trace\n" +
                'data: {"event_type": "runtime.context.loaded", "trace_id": "tr-91c2", ...}\n\n' +
                "...\n\n" +
                "event: request.completed\n" +
                'data: {"request_id": "' + RID + '", "status": "completed", "response": {...}, ...}',
            },
          ],
        },
        interaction: {
          label: "Answer an interaction",
          hops: [
            {
              lane: 0,
              who: "User application",
              what: "receives request.interaction.create",
              body:
                "<p>The request is paused on a durable wait. The frame carries the " +
                "prompt, the response schema, and the timeout.</p>",
              payload: json({
                request_id: RID,
                agent_id: "assistant",
                session_id: "s-1",
                interaction_id: ITR,
                type: "approval",
                prompt: "Approve execution of: deploy?",
                schema: { type: "enum", options: ["approve", "deny", "skip"] },
                timeout_seconds: 300,
              }),
            },
            {
              lane: 1,
              who: "Host",
              what: "POST /api/v1/agent/assistant/request/" + RID + "/interaction",
              body:
                "<p>" + link("post_interaction", "api/routes/agent.py") +
                " requires a JSON object with a non-empty " + code("interaction_id") +
                " and returns 400 " + code("INVALID_REQUEST") + " otherwise.</p>",
              payload: json({ interaction_id: ITR, response: "approve" }),
            },
            {
              lane: 2,
              who: "Client",
              what: "DBOS.send_async(workflow_id, response, topic)",
              body:
                "<p>" + link("InProcessAgentClient.post_interaction", "runtime/client.py") +
                " sends the response straight to the request's workflow. The topic " +
                "is the interaction id.</p>",
              payload:
                'await DBOS.send_async("' + WFID + '", "approve", topic="' + ITR + '")',
            },
            {
              lane: 3,
              who: "Agent",
              what: "recv_async returns → request.interaction.ack",
              body:
                "<p>The workflow's " + code("DBOS.recv_async(" + ITR + ")") +
                " returns " + code('"approve"') + ". The ack step appends " +
                code("runtime.interaction.ack") + ", which the stream publishes as " +
                code("request.interaction.ack") + ", and the loop runs the tool.</p>",
              payload: json({
                request_id: RID,
                agent_id: "assistant",
                session_id: "s-1",
                interaction_id: ITR,
                response: "approve",
              }),
            },
            {
              lane: 0,
              who: "User application",
              what: "200 {data: {ok, interaction_id}}",
              body: "<p>The POST returns as soon as the message is sent.</p>",
              payload: json({ data: { ok: true, interaction_id: ITR } }),
            },
          ],
        },
      },
    },
    http: {
      label: "HTTP binding (AgentServer)",
      blurb:
        "AgentServer.from_spec serves one agent over HTTP and SSE, and AgentClient " +
        "talks to it with httpx. Each server binds exactly one agent id.",
      lanes: [
        ["Host", "your code holding AgentClient"],
        ["Client", "AgentClient (httpx)"],
        ["Agent server", "AgentServer (Starlette)"],
        ["Agent", "AgentRuntime"],
      ],
      ops: {
        submit: {
          label: "Submit to an agent",
          hops: [
            {
              lane: 0,
              who: "Host",
              what: "client.post_request(message, session_id=...)",
              body:
                "<p>The host holds one " + code("AgentClient") + " per agent, " +
                "built from the agent server's base URL and agent id.</p>",
              payload:
                'client = AgentClient(base_url="http://127.0.0.1:9101", agent_id="assistant")\n' +
                'request_id = await client.post_request("Plan the next release.", session_id="s-1")',
            },
            {
              lane: 1,
              who: "Client",
              what: "POST {base_url}/agent/assistant/request",
              body:
                "<p>" + link("AgentClient.post_request", "runtime/client.py") +
                " sends the JSON body and expects exactly 202. Any other status " +
                "raises " + code("AgentClientError") + ".</p>",
              payload: json({ message: "Plan the next release.", session_id: "s-1" }),
            },
            {
              lane: 2,
              who: "Agent server",
              what: "AgentServer.submit_request validates",
              body:
                "<p>" + link("AgentServer.submit_request", "runtime/server.py") +
                " returns 404 " + code("ROUTE_NOT_FOUND") + " if the path's agent id " +
                "is not the one it serves, 400 " + code("INVALID_JSON") +
                " for a bad body, and 400 " + code("INVALID_REQUEST") +
                " for a missing message or session id. It passes message and " +
                "session id to the runtime.</p>",
              payload: json({
                error: { code: "INVALID_REQUEST", message: "message is required" },
              }),
            },
            {
              lane: 3,
              who: "Agent",
              what: "AgentRuntime.submit_request → request.accepted",
              body:
                "<p>Same runtime path as a pool deploy: append " +
                code("runtime.request.accepted") + ", start workflow " + code(WFID) +
                ", return the accepted payload.</p>",
              payload: json({
                request_id: RID,
                agent_id: "assistant",
                session_id: "s-1",
                status: "accepted",
              }),
            },
            {
              lane: 1,
              who: "Client",
              what: "202 Accepted → request_id",
              body:
                "<p>The server answers 202 with the accepted payload as the body. " +
                "The client returns its " + code("request_id") + ".</p>",
              payload: "HTTP/1.1 202 Accepted\n\n" + json({
                request_id: RID,
                agent_id: "assistant",
                session_id: "s-1",
                status: "accepted",
              }),
            },
          ],
        },
        stream: {
          label: "Stream events",
          hops: [
            {
              lane: 0,
              who: "Host",
              what: "async for event in client.stream_response(request_id)",
              body: "<p>The client yields parsed events in the order received.</p>",
              payload: 'async for event in client.stream_response("' + RID + '"):\n    print(event["event"], event["data"])',
            },
            {
              lane: 1,
              who: "Client",
              what: "GET {base_url}/agent/assistant/request/" + RID,
              body:
                "<p>The client reads SSE lines and turns each " + code("event:") +
                " / " + code("data:") + " pair into " +
                code('{"event": name, "data": payload}') + ".</p>",
              payload: "GET /agent/assistant/request/" + RID,
            },
            {
              lane: 2,
              who: "Agent server",
              what: "AgentServer.stream_request loop",
              body:
                "<p>" + link("AgentServer.stream_request", "runtime/server.py") +
                " returns 404 " + code("REQUEST_NOT_FOUND") + " for an unknown id. " +
                "Otherwise it loops on " +
                code("runtime.stream_response_events(cursor, wait_timeout=15.0)") +
                ", writes a " + code(": keep-alive") + " comment after 15 quiet " +
                "seconds, and stops when the runtime reports done or the client disconnects.</p>",
              payload:
                "event: request.started\n" +
                'data: {"request_id": "' + RID + '", "status": "started", ...}\n\n' +
                ": keep-alive\n\n",
            },
            {
              lane: 3,
              who: "Agent",
              what: "read_request_stream(after_seq=cursor)",
              body:
                "<p>Same store read as the pool path. Each row goes through " +
                link("to_public_event", "runtime/requests.py") + ".</p>",
              payload:
                "stored_events, done = await self.runtime_store.read_request_stream(\n" +
                "    request_id, after_seq=cursor,\n" +
                ")",
            },
          ],
        },
        interaction: {
          label: "Answer an interaction",
          hops: [
            {
              lane: 0,
              who: "Host",
              what: "client.post_interaction(request_id, interaction_id=..., response=...)",
              body: "<p>The host answers a " + code("request.interaction.create") + " it read from the stream.</p>",
              payload:
                'await client.post_interaction(\n    "' + RID + '",\n    interaction_id="' + ITR +
                '",\n    response="approve",\n)',
            },
            {
              lane: 1,
              who: "Client",
              what: "POST {base_url}/agent/assistant/request/" + RID + "/interaction",
              body: "<p>The client posts the interaction id and response as JSON.</p>",
              payload: json({ interaction_id: ITR, response: "approve" }),
            },
            {
              lane: 2,
              who: "Agent server",
              what: "AgentServer.post_interaction → DBOS.send",
              body:
                "<p>" + link("AgentServer.post_interaction", "runtime/server.py") +
                " returns 404 " + code("REQUEST_NOT_FOUND") + " for an unknown request " +
                "and 400 when " + code("interaction_id") + " is missing. Then it " +
                "calls " + code("DBOS.send") + " to the request's workflow with the " +
                "interaction id as topic.</p>",
              payload: 'DBOS.send("' + WFID + '", "approve", topic="' + ITR + '")',
            },
            {
              lane: 3,
              who: "Agent",
              what: "recv_async returns → request.interaction.ack",
              body:
                "<p>The parked workflow receives the response and the stream " +
                "publishes " + code("request.interaction.ack") + ".</p>",
              payload: json({ interaction_id: ITR, response: "approve" }),
            },
            {
              lane: 1,
              who: "Client",
              what: "200 {ok, interaction_id}",
              body: "<p>The agent server answers without the API's data envelope.</p>",
              payload: json({ ok: true, interaction_id: ITR }),
            },
          ],
        },
      },
    },
  };

  /* ---------- flow widget ---------- */

  function initFlow(root) {
    if (root.dataset.mvReady) return;
    root.dataset.mvReady = "1";
    root.classList.add("mviz");

    var cur = { binding: "pool", op: "submit", index: 0 };

    root.innerHTML =
      '<div class="mv-toolbar"><div class="mv-tabs" data-group="binding" role="tablist" aria-label="Binding"></div></div>' +
      '<p class="mv-blurb"></p>' +
      '<div class="mv-toolbar"><div class="mv-tabs" data-group="op" role="tablist" aria-label="Operation"></div>' +
      '<div class="mv-controls">' +
      '<button type="button" data-act="prev" aria-label="Previous hop">← Prev</button>' +
      '<span class="mv-counter" aria-live="polite"></span>' +
      '<button type="button" data-act="next" aria-label="Next hop">Next →</button>' +
      "</div></div>" +
      '<div class="mv-lanes"></div>' +
      '<div class="mv-two">' +
      '<div class="mv-panel"><div class="mv-panel-head"><span>Hops</span></div><div class="mv-panel-body"><ol class="mv-hops"></ol></div></div>' +
      '<div class="mv-panel mv-detail"><div class="mv-panel-body"></div></div>' +
      "</div>";

    var el = {
      binding: root.querySelector('[data-group="binding"]'),
      op: root.querySelector('[data-group="op"]'),
      blurb: root.querySelector(".mv-blurb"),
      lanes: root.querySelector(".mv-lanes"),
      hops: root.querySelector(".mv-hops"),
      detail: root.querySelector(".mv-detail .mv-panel-body"),
      counter: root.querySelector(".mv-counter"),
      prev: root.querySelector('[data-act="prev"]'),
      next: root.querySelector('[data-act="next"]'),
    };

    function render() {
      var b = BINDINGS[cur.binding];
      if (!b.ops[cur.op]) cur.op = "submit";
      var op = b.ops[cur.op];
      var hops = op.hops;
      var h = hops[cur.index];

      el.binding.innerHTML = Object.keys(BINDINGS).map(function (k) {
        return '<button type="button" role="tab" data-binding="' + k + '" aria-selected="' +
          (k === cur.binding) + '">' + BINDINGS[k].label + "</button>";
      }).join("");
      el.op.innerHTML = Object.keys(b.ops).map(function (k) {
        return '<button type="button" role="tab" data-op="' + k + '" aria-selected="' +
          (k === cur.op) + '">' + b.ops[k].label + "</button>";
      }).join("");
      el.blurb.textContent = b.blurb;

      var touched = {};
      for (var i = 0; i <= cur.index; i++) touched[hops[i].lane] = true;
      el.lanes.innerHTML = b.lanes.map(function (l, i) {
        var cls = "mv-lane" + (i === h.lane ? " is-active" : touched[i] ? " is-touched" : "");
        return '<div class="' + cls + '"><div class="mv-lane-role">' + l[0] +
          '</div><div class="mv-lane-impl">' + esc(l[1]) + "</div></div>";
      }).join("");

      el.hops.innerHTML = hops.map(function (hp, i) {
        return '<li><button type="button" class="mv-hop' + (i > cur.index ? " is-future" : "") +
          '" data-index="' + i + '"' + (i === cur.index ? ' aria-current="step"' : "") + ">" +
          '<span class="mv-hop-n">' + (i + 1) + '</span><span><span class="mv-hop-who">' +
          hp.who + '</span><span class="mv-hop-what">' + esc(hp.what) + "</span></span></button></li>";
      }).join("");

      el.detail.innerHTML =
        "<h4>" + esc(h.what) + "</h4>" + h.body +
        '<pre class="mv-pre">' + esc(h.payload) + "</pre>";

      el.counter.textContent = cur.index + 1 + " / " + hops.length;
      el.prev.disabled = cur.index === 0;
      el.next.disabled = cur.index === hops.length - 1;
    }

    function go(i) {
      var n = BINDINGS[cur.binding].ops[cur.op].hops.length;
      cur.index = Math.max(0, Math.min(n - 1, i));
      render();
    }

    root.addEventListener("click", function (e) {
      var t = e.target.closest("button");
      if (!t || !root.contains(t)) return;
      if (t.dataset.binding) { cur.binding = t.dataset.binding; cur.index = 0; render(); }
      else if (t.dataset.op) { cur.op = t.dataset.op; cur.index = 0; render(); }
      else if (t.dataset.index) go(parseInt(t.dataset.index, 10));
      else if (t.dataset.act === "prev") go(cur.index - 1);
      else if (t.dataset.act === "next") go(cur.index + 1);
    });

    root.addEventListener("keydown", function (e) {
      if (e.key === "ArrowRight") { go(cur.index + 1); e.preventDefault(); }
      if (e.key === "ArrowLeft") { go(cur.index - 1); e.preventDefault(); }
    });

    render();
  }

  /* ---------- event mapping widget ---------- */

  var BASE = { request_id: RID, agent_id: "assistant", session_id: "s-1" };
  function withBase(extra) {
    var o = {};
    Object.keys(BASE).forEach(function (k) { o[k] = BASE[k]; });
    Object.keys(extra).forEach(function (k) { o[k] = extra[k]; });
    return o;
  }
  function trace(type, loop, stepKey, payload) {
    return {
      event_type: type,
      trace_id: "tr-91c2",
      session_id: "s-1",
      loop_index: loop,
      step_key: stepKey,
      created_at: 1791100000.42,
      payload: payload,
    };
  }

  var MAPPING = [
    {
      internal: "runtime.request.accepted",
      wire: "request.accepted",
      note: "First event of every request. Fixed fields only.",
      data: withBase({ status: "accepted" }),
    },
    {
      internal: "runtime.trace.started",
      wire: "request.started",
      note: "Appended by the request.start step.",
      data: withBase({ status: "started" }),
    },
    {
      internal: "runtime.interaction.create",
      wire: "request.interaction.create",
      note: "The request is waiting on DBOS.recv_async.",
      data: withBase({
        interaction_id: ITR,
        type: "approval",
        prompt: "Approve execution of: deploy?",
        schema: { type: "enum", options: ["approve", "deny", "skip"] },
        timeout_seconds: 300,
      }),
    },
    {
      internal: "runtime.interaction.ack",
      wire: "request.interaction.ack",
      note: "The workflow received a response, or the wait timed out.",
      data: withBase({ interaction_id: ITR, response: "approve" }),
    },
    {
      internal: "runtime.request.completed",
      wire: "request.completed",
      note: "Terminal. Payload passed through as stored, including the turn payload.",
      data: withBase({
        status: "completed",
        trace_id: "tr-91c2",
        response: { text: "Here is the release plan.", signals: {}, metadata: {} },
        session_total_tokens: 4934,
      }),
    },
    {
      internal: "runtime.request.failed",
      wire: "request.error",
      note: "Terminal. Fields come from classify_error.",
      data: withBase({
        status: "error",
        error: "429 rate limit exceeded",
        error_type: "RateLimitError",
        error_code: "rate_limit_exceeded",
        retryable: true,
      }),
    },
    {
      internal: "runtime.request.cancelled",
      wire: "request.cancelled",
      note: "Terminal. Appended when the request is cancelled.",
      data: withBase({ status: "cancelled" }),
    },
    {
      internal: "runtime.request.resumed",
      wire: "request.resumed",
      note: "Non-terminal. A cancelled or recovery-exhausted request was resumed, so the stream reopens.",
      data: withBase({ status: "resumed", previous_status: "cancelled" }),
    },
    {
      internal:
        "runtime.context.loaded, runtime.llm.think.started, runtime.llm.think.completed, " +
        "runtime.tool.call.started, runtime.tool.call.completed, " +
        "runtime.subagent.call.completed, runtime.step.completed, " +
        "runtime.turn.persisted, runtime.step.failed",
      wire: "agent.trace",
      note: "Every other event type. One envelope; the original type is in event_type.",
      data: trace("runtime.tool.call.completed", 0, "tool.call.0.call_a1", {
        tool_call_id: "call_a1",
        tool_name: "search_docs",
        duration_ms: 412,
        result: { content: "3 matches", is_error: false, metadata: {} },
      }),
    },
  ];

  function frame(name, data) {
    return { name: name, data: data };
  }

  var SAMPLE = [
    frame("request.accepted", withBase({ status: "accepted" })),
    frame("request.started", withBase({ status: "started" })),
    frame("agent.trace", trace("runtime.context.loaded", null, null, { message: "Plan the next release." })),
    frame("agent.trace", trace("runtime.llm.think.started", 0, "llm.think.started.0", { loop_index: 0 })),
    frame("agent.trace", trace("runtime.llm.think.completed", 0, "llm.think.0", {
      action_type: "tool_call",
      tool_calls: [
        { id: "call_a1", name: "search_docs", parallel_safe: true },
        { id: "call_b2", name: "read_file", parallel_safe: true },
        { id: "call_c3", name: "write_note", parallel_safe: false },
      ],
      token_usage: { input: 2140, output: 96 },
    })),
    frame("agent.trace", trace("runtime.tool.call.started", 0, "tool.call.started.0.call_a1", { tool_call_id: "call_a1", tool_name: "search_docs" })),
    frame("agent.trace", trace("runtime.tool.call.started", 0, "tool.call.started.0.call_b2", { tool_call_id: "call_b2", tool_name: "read_file" })),
    frame("agent.trace", trace("runtime.tool.call.completed", 0, "tool.call.0.call_a1", { tool_call_id: "call_a1", tool_name: "search_docs", duration_ms: 412, result: { content: "3 matches", is_error: false, metadata: {} } })),
    frame("agent.trace", trace("runtime.tool.call.completed", 0, "tool.call.0.call_b2", { tool_call_id: "call_b2", tool_name: "read_file", duration_ms: 38, result: { content: "CHANGELOG.md contents", is_error: false, metadata: {} } })),
    frame("agent.trace", trace("runtime.tool.call.started", 0, "tool.call.started.0.call_c3", { tool_call_id: "call_c3", tool_name: "write_note" })),
    frame("agent.trace", trace("runtime.tool.call.completed", 0, "tool.call.0.call_c3", { tool_call_id: "call_c3", tool_name: "write_note", duration_ms: 21, result: { content: "saved", is_error: false, metadata: {} } })),
    frame("agent.trace", trace("runtime.step.completed", 0, "step.completed.0", { action_type: "tool_call", tool_calls: ["search_docs", "read_file", "write_note"], duration_ms: 3120 })),
    frame("agent.trace", trace("runtime.llm.think.started", 1, "llm.think.started.1", { loop_index: 1 })),
    frame("agent.trace", trace("runtime.llm.think.completed", 1, "llm.think.1", { action_type: "finish", assistant_text: "Here is the release plan.", tool_calls: [], token_usage: { input: 2510, output: 188 } })),
    frame("agent.trace", trace("runtime.step.completed", 1, "step.completed.1", { action_type: "finish", tool_calls: [], duration_ms: 2290 })),
    frame("agent.trace", trace("runtime.turn.persisted", null, null, { trace_id: "tr-91c2", session_total_tokens: 4934 })),
    frame("request.completed", withBase({ status: "completed", trace_id: "tr-91c2", response: { text: "Here is the release plan.", signals: {}, metadata: {} } })),
  ];

  function initEvents(root) {
    if (root.dataset.mvReady) return;
    root.dataset.mvReady = "1";
    root.classList.add("mviz");

    var sel = 0;
    var shown = 0;
    var timer = null;

    root.innerHTML =
      '<div class="mv-two">' +
      '<div class="mv-panel"><div class="mv-panel-head"><span>runtime_event_log type</span><span>wire event</span></div>' +
      '<div class="mv-panel-body mv-table-wrap"><table class="mv-map-table"><thead><tr><th>Stored as</th><th>Sent as</th></tr></thead><tbody></tbody></table></div></div>' +
      '<div class="mv-panel mv-detail"><div class="mv-panel-body"></div></div>' +
      "</div>" +
      '<div class="mv-toolbar" style="margin-top:1rem"><strong>Sample stream: the parallel tools request (payloads trimmed)</strong>' +
      '<div class="mv-controls">' +
      '<button type="button" data-act="reset">Reset</button>' +
      '<button type="button" data-act="step">Next frame</button>' +
      '<button type="button" data-act="play">Play</button>' +
      '<span class="mv-counter" aria-live="polite"></span>' +
      "</div></div>" +
      '<pre class="mv-pre mv-sse-screen" tabindex="0" aria-label="SSE frames"></pre>';

    var tbody = root.querySelector("tbody");
    var detail = root.querySelector(".mv-detail .mv-panel-body");
    var screen = root.querySelector(".mv-sse-screen");
    var counter = root.querySelector(".mv-counter");
    var playBtn = root.querySelector('[data-act="play"]');

    function renderTable() {
      tbody.innerHTML = MAPPING.map(function (m, i) {
        return '<tr tabindex="0" data-row="' + i + '" aria-selected="' + (i === sel) + '">' +
          '<td><span class="mv-mono">' + esc(m.internal).replace(/, /g, ",<br>") + "</span></td>" +
          '<td><span class="mv-mono mv-ev-wire">' + m.wire + "</span></td></tr>";
      }).join("");
      var m = MAPPING[sel];
      detail.innerHTML =
        "<h4>" + m.wire + "</h4><p>" + esc(m.note) + "</p>" +
        '<pre class="mv-pre"><span class="mv-sse-event">event: ' + m.wire + "</span>\n" +
        "data: " + esc(json(m.data)) + "</pre>";
    }

    function renderStream() {
      screen.innerHTML = SAMPLE.slice(0, shown).map(function (f) {
        return '<span class="mv-sse-event">event: ' + f.name + "</span>\n" +
          "data: " + esc(JSON.stringify(f.data)) + "\n";
      }).join("\n") || '<span class="mv-sse-comment">Press Play to stream the frames.</span>';
      screen.scrollTop = screen.scrollHeight;
      counter.textContent = shown + " / " + SAMPLE.length;
    }

    function stop() {
      if (timer) { clearInterval(timer); timer = null; }
      playBtn.textContent = "Play";
    }

    root.addEventListener("click", function (e) {
      var row = e.target.closest("tr[data-row]");
      if (row) { sel = parseInt(row.dataset.row, 10); renderTable(); return; }
      var t = e.target.closest("button");
      if (!t) return;
      if (t.dataset.act === "reset") { stop(); shown = 0; renderStream(); }
      else if (t.dataset.act === "step") { stop(); shown = Math.min(SAMPLE.length, shown + 1); renderStream(); }
      else if (t.dataset.act === "play") {
        if (timer) { stop(); return; }
        if (shown >= SAMPLE.length) shown = 0;
        playBtn.textContent = "Pause";
        timer = setInterval(function () {
          if (shown >= SAMPLE.length) { stop(); return; }
          shown += 1;
          renderStream();
        }, 700);
      }
    });

    root.addEventListener("keydown", function (e) {
      var row = e.target.closest("tr[data-row]");
      if (row && (e.key === "Enter" || e.key === " ")) {
        sel = parseInt(row.dataset.row, 10);
        renderTable();
        var again = tbody.querySelector('[data-row="' + sel + '"]');
        if (again) again.focus();
        e.preventDefault();
      }
    });

    renderTable();
    renderStream();
  }

  function boot() {
    var a = document.querySelectorAll('[data-mash-viz="h2a-flow"]');
    for (var i = 0; i < a.length; i++) initFlow(a[i]);
    var b = document.querySelectorAll('[data-mash-viz="h2a-events"]');
    for (var j = 0; j < b.length; j++) initEvents(b[j]);
  }

  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(boot);
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();

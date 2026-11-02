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

  /* ---------- bindings and operations ---------- */

  var BINDINGS = {
    pool: {
      label: "Pool deploy (mash host serve)",
      blurb:
        "A pool deploy serves the API from the host and runs each agent on its " +
        "own process, a subprocess of the host with its own AgentServer. The pool " +
        "holds one AgentClient per agent process, so the client-to-agent hop is HTTP and SSE.",
      lanes: [
        ["User application", "your app, CLI, or curl"],
        ["Host", "API routes /api/v1 + Pool"],
        ["Client", "AgentClient (httpx)"],
        ["Agent", "AgentServer + AgentRuntime in the agent's process"],
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
                code("message") + ", " + code("session_id") + " and " + code("user_id") +
                ", the end user the request acts for, are required. " +
                code("metadata") + " is optional caller context that tools can read " +
                "and the model never sees. " + code("structured_output") +
                " is an optional JSON schema.</p>",
              payload: json({
                message: "Plan the next release.",
                session_id: "s-1",
                user_id: "u-42",
                metadata: { tenant: "acme" },
              }),
            },
            {
              lane: 1,
              who: "Host",
              what: "submit_request route → pool.get_client(agent_id)",
              body:
                "<p>" + link("submit_request", "api/routes/agent.py") +
                " validates the body. An empty message or session id fails schema " +
                "validation and returns 422 " + code("VALIDATION_ERROR") +
                "; one that is only whitespace returns 400 " + code("INVALID_REQUEST") +
                ". The route then resolves the agent id to its client with " +
                code("pool.get_client") + ".</p>",
              payload: json({
                error: {
                  code: "INVALID_REQUEST",
                  message: "session_id is required",
                  details: {},
                },
              }) + '\n# status 400 when session_id is "   "; an empty string returns 422 VALIDATION_ERROR',
            },
            {
              lane: 2,
              who: "Client",
              what: "AgentClient.post_request(...)",
              body:
                "<p>" + link("AgentClient.post_request", "runtime/client.py") +
                " POSTs to " + code("/agent/assistant/request") + " on the agent's " +
                "process with the agent's bearer token, and returns the " +
                code("request_id") + " from the accepted payload. The agent process's " +
                link("AgentServer", "runtime/server.py") + " calls " +
                code("runtime.submit_request") + ".</p>",
              payload:
                'request_id = await client.post_request(\n' +
                '    "Plan the next release.",\n' +
                '    session_id="s-1",\n' +
                '    user_id="u-42",\n' +
                '    metadata={"tenant": "acme"},\n' +
                ")",
            },
            {
              lane: 3,
              who: "Agent",
              what: "AgentRuntime.submit_request → request.accepted",
              body:
                "<p>The runtime mints " + code("request_id") + ", appends " +
                code("invoke_agent.start") + " to " + code("runtime_event_log") +
                ", which records the request " + code("running") + " in " +
                code("runtime_request") + ", and starts the request loop. It returns " +
                "once the loop has started, without waiting for it.</p>",
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
                user_id: "u-42",
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
                "404 " + code("HOST_NOT_FOUND") + ". It submits through the " +
                "primary's client with the host snapshot and the request context.</p>",
              payload:
                "host = self.get_host(host_id)\n" +
                "client = self.get_client(host.primary)",
            },
            {
              lane: 3,
              who: "Agent",
              what: "AgentServer → submit_request(host_snapshot=...)",
              body:
                "<p>The runtime stores the host snapshot in the request metadata. " +
                code("context.load") + " uses it to build the primary's system " +
                "prompt with the subagent block, and the snapshot is part of the " +
                "recorded step output, so redefining the host later never changes " +
                "an in-flight request.</p>",
              payload:
                "await client.post_request(\n" +
                "    message,\n" +
                "    session_id=session_id,\n" +
                "    user_id=user_id,\n" +
                "    host=self.snapshot_for(host),\n" +
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
              what: "AgentClient.stream_response over SSE",
              body:
                "<p>The client opens " + code("GET /agent/assistant/request/" + RID) +
                " on the agent's process and parses each SSE frame. Its " +
                "AgentServer keeps a cursor and calls " +
                code("runtime.stream_response_events(request_id, cursor, wait_timeout=15)") +
                " until the runtime reports " + code("done") + ", writing a " +
                code(": keep-alive") + " comment when nothing arrives.</p>",
              payload:
                "async for event in client.stream_response(request_id):\n" +
                "    yield event  # {\"event\": ..., \"data\": ...}",
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
                'data: {"event_type": "mash.context_load.end", "trace_id": "tr-91c2", ...}\n\n' +
                "...\n\n" +
                "event: request.completed\n" +
                'data: {"request_id": "' + RID + '", "status": "ok", "response": {...}, ...}',
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
              what: "AgentClient.post_interaction → answer_interaction",
              body:
                "<p>" + link("AgentClient.post_interaction", "runtime/client.py") +
                " POSTs the response to the agent's process. Its AgentServer appends " +
                "the answer as the interaction's " + code("mash.interaction.end") +
                " and wakes the waiting loop.</p>",
              payload: json({ interaction_id: ITR, response: "approve" }),
            },
            {
              lane: 3,
              who: "Agent",
              what: "the loop wakes → request.interaction.ack",
              body:
                "<p>The stream publishes the ack as " + code("request.interaction.ack") +
                ". The loop reads " + code('"approve"') + " from it and runs the tool.</p>",
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
                code("invoke_agent.start") + ", start the request loop" +
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
              what: "AgentServer.post_interaction → answer_interaction",
              body:
                "<p>" + link("AgentServer.post_interaction", "runtime/server.py") +
                " returns 404 " + code("REQUEST_NOT_FOUND") + " for an unknown request, " +
                "400 when " + code("interaction_id") + " is missing, and 409 " +
                code("INTERACTION_CLOSED") + " once the interaction has an answer. " +
                "Then it appends the answer and wakes the request's loop.</p>",
              payload: json({ interaction_id: ITR, response: "approve" }),
            },
            {
              lane: 3,
              who: "Agent",
              what: "recv_async returns → request.interaction.ack",
              body:
                "<p>The waiting loop wakes and the stream publishes " +
                code("request.interaction.ack") + ".</p>",
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
      internal: "invoke_agent.start",
      wire: "request.accepted",
      note: "First event of every request; its trace id is minted here. Fixed fields only.",
      data: withBase({ status: "accepted" }),
    },
    {
      internal: "mash.context_load.start",
      wire: "request.started",
      note: "Appended when the loop starts loading the request's context.",
      data: withBase({ status: "started" }),
    },
    {
      internal: "mash.interaction.start",
      wire: "request.interaction.create",
      note: "The request loop is waiting for the answer.",
      data: withBase({
        interaction_id: ITR,
        type: "approval",
        prompt: "Approve execution of: deploy?",
        schema: { type: "enum", options: ["approve", "deny", "skip"] },
        timeout_seconds: 300,
      }),
    },
    {
      internal: "mash.interaction.end",
      wire: "request.interaction.ack",
      note: "The interaction's answer: the user's response, the timeout default, or a cancel.",
      data: withBase({ interaction_id: ITR, response: "approve" }),
    },
    {
      internal: "invoke_agent.end (status ok)",
      wire: "request.completed",
      note: "Terminal. Payload passed through as stored, including the turn payload.",
      data: withBase({
        status: "ok",
        trace_id: "tr-91c2",
        response: { text: "Here is the release plan.", signals: {}, metadata: {} },
        session_total_tokens: 4934,
      }),
    },
    {
      internal: "invoke_agent.end (status error)",
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
      internal: "invoke_agent.end (status cancelled)",
      wire: "request.cancelled",
      note: "Terminal. Appended when the request is cancelled.",
      data: withBase({ status: "cancelled" }),
    },
    {
      internal: "mash.request.resumed",
      wire: "request.resumed",
      note: "Non-terminal. A cancelled or failed request was resumed, so the stream reopens.",
      data: withBase({ status: "resumed", previous_status: "cancelled" }),
    },
    {
      internal:
        "mash.context_load.end, chat.start, chat.delta, chat.end, " +
        "execute_tool.start, execute_tool.end, mash.step.end, " +
        "mash.request.mode_changed",
      wire: "agent.trace",
      note: "Every other event type. One envelope; the original type is in event_type.",
      data: trace("execute_tool.end", 0, "execute_tool.end.0.call_a1", {
        status: "ok",
        tool_call_id: "call_a1",
        tool_name: "search_docs",
        duration_ms: 412,
        result: { content: "3 matches", is_error: false, metadata: {} },
        attributes: { "gen_ai.tool.name": "search_docs", "gen_ai.tool.call.id": "call_a1" },
      }),
    },
  ];

  function frame(name, data) {
    return { name: name, data: data };
  }

  var CHAT = { "gen_ai.operation.name": "chat", "gen_ai.provider.name": "anthropic", "gen_ai.request.model": "claude-sonnet-4-6" };
  function chatEnd(inTok, outTok, finish) {
    return Object.assign({}, CHAT, {
      "gen_ai.usage.input_tokens": inTok,
      "gen_ai.usage.output_tokens": outTok,
      "gen_ai.response.finish_reasons": [finish],
    });
  }
  function toolAttrs(id, name) {
    return { "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "gen_ai.tool.call.id": id };
  }

  var SAMPLE = [
    frame("request.accepted", withBase({ status: "accepted" })),
    frame("request.started", withBase({ status: "started" })),
    frame("agent.trace", trace("mash.context_load.end", null, null, { status: "ok" })),
    frame("agent.trace", trace("chat.start", 0, "chat.start.0", { attributes: CHAT })),
    frame("agent.trace", trace("chat.end", 0, "chat.end.0", {
      status: "ok",
      action_type: "tool_call",
      tool_calls: [
        { id: "call_a1", name: "search_docs", arguments: {}, parallel_safe: true },
        { id: "call_b2", name: "read_file", arguments: {}, parallel_safe: true },
        { id: "call_c3", name: "write_note", arguments: {}, parallel_safe: false },
      ],
      attributes: chatEnd(2140, 96, "tool_call"),
    })),
    frame("agent.trace", trace("execute_tool.start", 0, "execute_tool.start.0.call_a1", { tool_call_id: "call_a1", tool_name: "search_docs", attributes: toolAttrs("call_a1", "search_docs") })),
    frame("agent.trace", trace("execute_tool.start", 0, "execute_tool.start.0.call_b2", { tool_call_id: "call_b2", tool_name: "read_file", attributes: toolAttrs("call_b2", "read_file") })),
    frame("agent.trace", trace("execute_tool.end", 0, "execute_tool.end.0.call_a1", { status: "ok", tool_call_id: "call_a1", tool_name: "search_docs", duration_ms: 412, result: { content: "3 matches", is_error: false, metadata: {} }, attributes: toolAttrs("call_a1", "search_docs") })),
    frame("agent.trace", trace("execute_tool.end", 0, "execute_tool.end.0.call_b2", { status: "ok", tool_call_id: "call_b2", tool_name: "read_file", duration_ms: 38, result: { content: "CHANGELOG.md contents", is_error: false, metadata: {} }, attributes: toolAttrs("call_b2", "read_file") })),
    frame("agent.trace", trace("execute_tool.start", 0, "execute_tool.start.0.call_c3", { tool_call_id: "call_c3", tool_name: "write_note", attributes: toolAttrs("call_c3", "write_note") })),
    frame("agent.trace", trace("execute_tool.end", 0, "execute_tool.end.0.call_c3", { status: "ok", tool_call_id: "call_c3", tool_name: "write_note", duration_ms: 21, result: { content: "saved", is_error: false, metadata: {} }, attributes: toolAttrs("call_c3", "write_note") })),
    frame("agent.trace", trace("mash.step.end", 0, "mash.step.end.0", { status: "ok", action_type: "tool_call", tool_calls: ["search_docs", "read_file", "write_note"], duration_ms: 3120, attributes: { "mash.loop_index": 0 } })),
    frame("agent.trace", trace("chat.start", 1, "chat.start.1", { attributes: CHAT })),
    frame("agent.trace", trace("chat.end", 1, "chat.end.1", { status: "ok", action_type: "finish", assistant_text: "Here is the release plan.", tool_calls: [], attributes: chatEnd(2510, 188, "end_turn") })),
    frame("agent.trace", trace("mash.step.end", 1, "mash.step.end.1", { status: "ok", action_type: "finish", tool_calls: [], duration_ms: 2290, attributes: { "mash.loop_index": 1 } })),
    frame("request.completed", withBase({ status: "ok", trace_id: "tr-91c2", session_total_tokens: 4934, response: { text: "Here is the release plan.", signals: {}, metadata: {} } })),
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
